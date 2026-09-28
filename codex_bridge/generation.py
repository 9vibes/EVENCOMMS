"""One explicit turn on a new ephemeral thread, with fail-closed events."""

import asyncio
from collections import deque
import json

from .errors import ERRORS, Failure, provider_failure
from .policy import PROVIDER, history_items, thread_params, turn_params
from .rpc import ProtocolError


TOOL_ITEMS = {
    "functionCallOutput", "commandExecution", "fileChange", "mcpToolCall", "dynamicToolCall",
    "collabAgentToolCall", "subAgentActivity", "imageView", "local_shell_call", "function_call",
    "function_call_output", "custom_tool_call", "custom_tool_call_output", "tool_search_call",
    "tool_search_output", "web_search_call", "image_generation_call", "additional_tools",
}


def bounded_json(value, limit=16384):
    # Moderation metadata is JsonValue in v2/model.rs, not an authorization or
    # an instruction. Bound its entire tree before discarding it.
    pending = [(value, 0)]
    count = 0
    while pending:
        item, depth = pending.pop()
        count += 1
        if depth > 8 or count > 1024:
            raise ProtocolError()
        if isinstance(item, dict):
            if len(item) > 128 or any(not isinstance(key, str) for key in item):
                raise ProtocolError()
            pending.extend((part, depth + 1) for pair in item.items() for part in pair)
        elif isinstance(item, list):
            if len(item) > 128:
                raise ProtocolError()
            pending.extend((part, depth + 1) for part in item)
        elif item is not None and type(item) not in {str, int, float, bool}:
            raise ProtocolError()
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(encoded) > limit:
            raise ProtocolError()
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ProtocolError() from None


def native_failure(error):
    # v2/shared.rs CodexErrorInfo and thread_data.rs TurnError. Never classify
    # from message/additionalDetails/misalignment text or retain those fields.
    if (not isinstance(error, dict) or set(error) - {"message", "codexErrorInfo", "additionalDetails", "misalignment"}
            or not isinstance(error.get("message"), str)
            or error.get("additionalDetails") is not None and not isinstance(error["additionalDetails"], str)):
        raise ProtocolError()
    bounded_json(error, 65536)
    misalignment = error.get("misalignment")
    if misalignment is not None:
        if (not isinstance(misalignment, dict)
                or set(misalignment) - {"errorType", "detailedExplanation", "steer"}
                or any(misalignment.get(key) is not None and not isinstance(misalignment[key], str)
                       for key in ("errorType", "detailedExplanation"))):
            raise ProtocolError()
        steer = misalignment.get("steer")
        if steer is not None and (not isinstance(steer, dict) or set(steer) != {"message"}
                                  or not isinstance(steer["message"], str)):
            raise ProtocolError()
    info = error.get("codexErrorInfo")
    codes = {
        "contextWindowExceeded": "context_limit", "sessionBudgetExceeded": "context_limit",
        "usageLimitExceeded": "rate_limit", "rateLimitExceeded": "rate_limit",
        "serverOverloaded": "provider_unavailable", "internalServerError": "provider_unavailable",
        "unauthorized": "account_auth", "badRequest": "request_rejected",
        "cyberPolicy": "policy_rejected", "misalignmentPolicyViolation": "policy_rejected",
        "threadRollbackFailed": "runtime_error", "sandboxError": "tool_rejected", "other": "runtime_error",
    }
    if info is None:
        return Failure("runtime_error")
    if isinstance(info, str) and info in codes:
        return Failure(codes[info])
    if not isinstance(info, dict) or len(info) != 1:
        raise ProtocolError()
    kind, details = next(iter(info.items()))
    if not isinstance(details, dict):
        raise ProtocolError()
    if kind == "activeTurnNotSteerable":
        if set(details) != {"turnKind"} or details["turnKind"] not in ("review", "compact"):
            raise ProtocolError()
        return Failure("request_rejected")
    if (kind not in {"httpConnectionFailed", "responseStreamConnectionFailed", "responseStreamDisconnected",
                     "responseTooManyFailedAttempts"} or set(details) - {"httpStatusCode"}):
        raise ProtocolError()
    status = details.get("httpStatusCode")
    if status is not None and (type(status) is not int or not 0 <= status <= 65535):
        raise ProtocolError()
    if status in {408, 504}:
        return Failure("timeout")
    if status is not None and 300 <= status <= 599:
        return Failure(provider_failure(status))
    return Failure("network_error" if kind in {"httpConnectionFailed", "responseStreamConnectionFailed"}
                   else "stream_incomplete")


class Generation:
    def __init__(self):
        self.thread_id = None
        self.turn_id = None
        self.done = asyncio.get_running_loop().create_future()
        self.texts = {}
        self.delta_length = 0
        self.on_text = None
        self.live_texts = {}
        self.events = 0
        self.raw_completed = 0
        self.replay = None
        self.replay_started = False
        self.prelude_seen = False
        self.budget = None
        self.failure = None
        self.retry_failure = None

    def fail(self, error=None):
        if self.failure is None:
            upstream = getattr(self.budget, "failure", None)
            code = error.code if isinstance(error, Failure) else "runtime_error"
            if isinstance(upstream, str) and upstream in ERRORS:
                code = upstream
            elif code in {"runtime_error", "protocol_mismatch", "timeout"} and self.retry_failure:
                code = self.retry_failure
            self.failure = code
        if not self.done.done():
            self.done.set_exception(Failure(self.failure))
        return Failure(self.failure)

    def event(self, method, params):
        self.events += 1
        if self.events > 4096 or not isinstance(params, dict):
            raise ProtocolError()
        if method == "thread/started":
            thread = params.get("thread", {})
            if not isinstance(thread, dict):
                raise ProtocolError()
            identity = thread.get("id")
            if (not isinstance(identity, str) or not 1 <= len(identity) <= 128 or thread.get("ephemeral") is not True
                    or self.thread_id is not None and self.thread_id != identity):
                raise ProtocolError()
            self.thread_id = identity
        if params.get("threadId", self.thread_id) != self.thread_id:
            raise ProtocolError()
        turn = params.get("turnId")
        if method == "rawResponseItem/completed" and turn == "auto-compact-0":
            # 0.157.1 inject_items uses this internal recording context, not the
            # generated turn UUID. Its echoes can arrive after turn/start's RPC
            # reply. Accept only our exact replay plus the bounded skills prelude.
            item = params.get("item", {})
            if self.replay is None or not isinstance(item, dict) or item.get("type") != "message" or self.done.done():
                raise ProtocolError()
            if item.get("role") == "developer":
                content = item.get("content")
                metadata = item.get("internal_chat_message_metadata_passthrough")
                if (self.prelude_seen or self.replay_started or not isinstance(content, list) or len(content) != 1
                        or not isinstance(content[0], dict) or set(content[0]) != {"type", "text"}
                        or content[0]["type"] != "input_text" or not isinstance(content[0]["text"], str)
                        or len(content[0]["text"]) > 64000
                        or not isinstance(metadata, dict) or metadata.get("content_item_kinds") != ["host_skills.instructions"]):
                    raise ProtocolError()
                content[0]["text"].encode("utf-8")
                self.prelude_seen = True
            else:
                if (not self.replay or item.get("role") != self.replay[0]["role"]
                        or item.get("content") != self.replay[0]["content"]):
                    raise ProtocolError()
                self.replay.popleft()
                self.replay_started = True
            return
        if turn is not None and turn != self.turn_id:
            raise ProtocolError()
        if method in {"model/verification", "turn/moderationMetadata", "model/safetyBuffering/updated",
                      "model/rerouted", "error"}:
            if (not self.thread_id or not self.turn_id or self.done.done()
                    or params.get("threadId") != self.thread_id or turn != self.turn_id):
                raise ProtocolError()
            if method == "error":
                if set(params) != {"threadId", "turnId", "error", "willRetry"} or type(params["willRetry"]) is not bool:
                    raise ProtocolError()
                error = native_failure(params["error"])
                if params["willRetry"] and error.code in {"network_error", "stream_incomplete", "timeout",
                                                        "provider_unavailable", "account_auth", "rate_limit"}:
                    # A native transport attempt is not a new application turn.
                    # The relay's existing deadline and request budget still apply.
                    self.retry_failure = self.retry_failure or error.code
                    return
                raise self.fail(error)
            bounded_json(params)
            scope = {"threadId", "turnId"}
            if method == "model/verification":
                values = params.get("verifications")
                if (set(params) != scope | {"verifications"} or not isinstance(values, list) or len(values) > 16
                        or any(value != "trustedAccessForCyber" for value in values)):
                    raise ProtocolError()
                # A recommendation, not proof of verification or an access grant.
            elif method == "turn/moderationMetadata":
                if set(params) != scope | {"metadata"}:
                    raise ProtocolError()
            elif method == "model/safetyBuffering/updated":
                required = scope | {"model", "useCases", "reasons", "showBufferingUi"}
                if (not required <= set(params) or set(params) - required - {"fasterModel"}
                        or type(params["showBufferingUi"]) is not bool
                        or not isinstance(params["model"], str) or not 1 <= len(params["model"]) <= 128
                        or params.get("fasterModel") is not None and (
                            not isinstance(params["fasterModel"], str) or not 1 <= len(params["fasterModel"]) <= 128)):
                    raise ProtocolError()
                for key in ("useCases", "reasons"):
                    values = params[key]
                    if (not isinstance(values, list) or len(values) > 32
                            or any(not isinstance(value, str) or len(value) > 512 for value in values)):
                        raise ProtocolError()
            else:
                if (set(params) != scope | {"fromModel", "toModel", "reason"}
                        or params["reason"] != "highRiskCyberActivity"
                        or any(not isinstance(params[key], str) or not 1 <= len(params[key]) <= 128
                               for key in ("fromModel", "toModel")) or params["fromModel"] == params["toModel"]):
                    raise ProtocolError()
                raise self.fail(Failure("model_changed"))
            return
        if method in {"item/started", "item/completed"}:
            item = params.get("item", {})
            if not isinstance(item, dict):
                raise ProtocolError()
            kind = item.get("type")
            if not isinstance(kind, str) or kind not in {"userMessage", "agentMessage", "reasoning"}:
                raise ProtocolError("tool_rejected" if isinstance(kind, str) and kind in TOOL_ITEMS else "protocol_mismatch")
            if kind == "agentMessage":
                text = item.get("text")
                if not isinstance(text, str) or len(text) > 16000:
                    raise ProtocolError()
                text.encode("utf-8")
                if self.on_text and item.get("phase") in {None, "final_answer"}:
                    identity = item.get("id")
                    if not isinstance(identity, str) or len(identity) > 128 or (identity not in self.live_texts and len(self.live_texts) >= 20):
                        raise ProtocolError()
                    self.live_texts[identity] = text
                    self.publish_text()
                if method == "item/completed" and item.get("phase") in {None, "final_answer"}:
                    identity = item.get("id")
                    if not isinstance(identity, str) or len(identity) > 128 or len(self.texts) >= 20:
                        raise ProtocolError()
                    self.texts[identity] = text
                    if len("\n".join(self.texts.values())) > 16000:
                        raise ProtocolError()
        elif method == "rawResponseItem/completed":
            item = params.get("item", {})
            if not isinstance(item, dict):
                raise ProtocolError()
            if item.get("type") == "message":
                if item.get("role") not in {"assistant", "user", "developer", "system"} or not isinstance(item.get("content"), list):
                    raise ProtocolError()
                size = 0
                for part in item["content"]:
                    if not isinstance(part, dict):
                        raise ProtocolError()
                    kind = part.get("type")
                    if kind == "input_image" and item["role"] == "user":
                        continue
                    if kind not in {"input_text", "output_text"} or not isinstance(part.get("text"), str):
                        raise ProtocolError()
                    size += len(part["text"])
                    part["text"].encode("utf-8")
                if item["role"] == "assistant" and size > 16000:
                    raise ProtocolError()
            elif item.get("type") != "reasoning":
                # Includes function/custom/MCP calls even if absent from tools[].
                kind = item.get("type")
                raise ProtocolError("tool_rejected" if isinstance(kind, str) and kind in TOOL_ITEMS else "protocol_mismatch")
        elif method == "item/agentMessage/delta":
            delta = params.get("delta")
            if not isinstance(delta, str):
                raise ProtocolError()
            self.delta_length += len(delta)
            delta.encode("utf-8")
            if self.delta_length > 16000:
                raise ProtocolError()
            identity = params.get("itemId")
            if self.on_text and identity in self.live_texts:
                self.live_texts[identity] += delta
                self.publish_text()
        elif method == "turn/started":
            started = params.get("turn")
            if not isinstance(started, dict) or not self.thread_id or params.get("threadId") != self.thread_id:
                raise ProtocolError()
            identity = started.get("id")
            if (not isinstance(identity, str) or not 1 <= len(identity) <= 128
                    or self.turn_id is not None and self.turn_id != identity):
                raise ProtocolError()
            self.turn_id = identity
        elif method == "turn/completed":
            turn = params.get("turn", {})
            if (not isinstance(turn, dict) or not self.turn_id or self.done.done()
                    or params.get("threadId") != self.thread_id or turn.get("id") != self.turn_id):
                raise ProtocolError()
            if turn.get("status") == "failed":
                error = turn.get("error")
                raise self.fail(native_failure(error) if error is not None else Failure("runtime_error"))
            if turn.get("status") == "interrupted" and turn.get("error") is None:
                raise self.fail(Failure("stream_incomplete"))
            if turn.get("status") != "completed" or turn.get("error") is not None:
                raise ProtocolError()
            items = turn.get("items", [])
            if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
                raise ProtocolError()
            for item in items:
                kind = item.get("type")
                if not isinstance(kind, str) or kind not in {"userMessage", "agentMessage", "reasoning"}:
                    raise ProtocolError("tool_rejected" if isinstance(kind, str) and kind in TOOL_ITEMS else "protocol_mismatch")
            text = "\n".join(self.texts.values())
            if self.replay or len(text) > 16000:
                raise ProtocolError()
            if not text or self.raw_completed != 1:
                raise self.fail(Failure("stream_incomplete"))
            self.done.set_result({"text": text, "incomplete": False, "usage": None})
        elif method == "rawResponse/completed":
            self.raw_completed += 1
            if self.raw_completed != 1:
                raise ProtocolError()
        elif method == "remoteControl/status/changed":
            if params.get("status") != "disabled":
                raise ProtocolError()
        elif method in {"item/commandExecution/requestApproval", "item/fileChange/requestApproval",
                        "item/permissions/requestApproval", "item/tool/requestUserInput", "item/tool/call",
                        "mcpServer/elicitation/request"}:
            raise ProtocolError("tool_rejected")
        elif method not in {
            "thread/started", "thread/status/changed", "thread/tokenUsage/updated",
            "deprecationNotice", "configWarning",
            "account/rateLimits/updated",
            "item/reasoning/summaryTextDelta", "item/reasoning/textDelta", "item/reasoning/summaryPartAdded",
        }:
            raise ProtocolError()

    def publish_text(self):
        text = "\n".join(self.live_texts.values())
        if len(text) > 16000:
            raise ProtocolError()
        self.on_text(text)

    async def run(self, runtime, data):
        budget = runtime.relay.arm(data.model)
        self.budget = budget
        try:
            params = thread_params(data.model)
            params["config"] = {"model_providers.evencomms.base_url": budget.base_url}
            result = await runtime.call("thread/start", params)
            if not isinstance(result, dict) or not isinstance(result.get("thread"), dict):
                raise ProtocolError()
            thread = result.get("thread", {})
            identity = thread.get("id")
            if (not isinstance(identity, str) or not 1 <= len(identity) <= 128
                    or self.thread_id is not None and identity != self.thread_id or thread.get("ephemeral") is not True
                    or result.get("modelProvider") != PROVIDER or thread.get("path") is not None):
                raise ProtocolError()
            if not isinstance(result.get("model"), str) or not 1 <= len(result["model"]) <= 128:
                raise ProtocolError()
            if result.get("model") != data.model:
                raise Failure("model_changed")
            self.thread_id = identity
            runtime.relay.bind(budget, self.thread_id)
            if len(data.messages) > 1:
                items = history_items(data.messages[:-1])
                self.replay = deque(items)
                await runtime.call("thread/inject_items", {"threadId": self.thread_id, "items": items})
            result = await runtime.call("turn/start", turn_params(self.thread_id, data.messages[-1]))
            if not isinstance(result, dict) or not isinstance(result.get("turn"), dict):
                raise ProtocolError()
            identity = result.get("turn", {}).get("id")
            if not isinstance(identity, str) or not 1 <= len(identity) <= 128 or (self.turn_id is not None and identity != self.turn_id):
                raise ProtocolError()
            self.turn_id = identity
            result = await self.done
            if budget.non_401 != 1 or not 1 <= budget.attempts <= 3 or not budget.consumed:
                raise ProtocolError()
            if getattr(budget, "failure", None) is not None:
                raise self.fail()
            return {"request_id": str(data.request_id), "model": data.model, **result}
        except asyncio.CancelledError:
            if self.failure or self.retry_failure or getattr(budget, "failure", None):
                raise self.fail() from None
            raise
        except Exception as error:
            raise self.fail(error) from None
        finally:
            try:
                async with asyncio.timeout(3):
                    await runtime.relay.finish(budget)
            except Exception:
                if self.failure is None and not asyncio.current_task().cancelling():
                    raise self.fail() from None
            finally:
                if not self.done.done():
                    self.done.cancel()
                elif not self.done.cancelled():
                    self.done.exception()
