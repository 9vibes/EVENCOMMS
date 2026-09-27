"""One explicit turn on a new ephemeral thread, with fail-closed events."""

import asyncio
from collections import deque

from .policy import PROVIDER, history_items, thread_params, turn_params
from .rpc import ProtocolError


class Generation:
    def __init__(self):
        self.thread_id = None
        self.turn_id = None
        self.done = asyncio.get_running_loop().create_future()
        self.texts = {}
        self.delta_length = 0
        self.events = 0
        self.raw_completed = 0
        self.replay = None
        self.replay_started = False
        self.prelude_seen = False
        self.budget = None

    def fail(self):
        if not self.done.done():
            self.done.set_exception(ProtocolError())

    def event(self, method, params):
        self.events += 1
        if self.events > 4096 or not isinstance(params, dict):
            raise ProtocolError()
        if method == "thread/started":
            thread = params.get("thread", {})
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
        if method in {"item/started", "item/completed"}:
            item = params.get("item", {})
            kind = item.get("type")
            if kind not in {"userMessage", "agentMessage", "reasoning"}:
                raise ProtocolError()
            if kind == "agentMessage":
                text = item.get("text")
                if not isinstance(text, str) or len(text) > 16000:
                    raise ProtocolError()
                text.encode("utf-8")
                if method == "item/completed" and item.get("phase") in {None, "final_answer"}:
                    identity = item.get("id")
                    if not isinstance(identity, str) or len(identity) > 128 or len(self.texts) >= 20:
                        raise ProtocolError()
                    self.texts[identity] = text
                    if len("\n".join(self.texts.values())) > 16000:
                        raise ProtocolError()
        elif method == "rawResponseItem/completed":
            item = params.get("item", {})
            if item.get("type") == "message":
                if item.get("role") not in {"assistant", "user", "developer", "system"} or not isinstance(item.get("content"), list):
                    raise ProtocolError()
                size = 0
                for part in item["content"]:
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
                raise ProtocolError()
        elif method == "item/agentMessage/delta":
            delta = params.get("delta")
            if not isinstance(delta, str):
                raise ProtocolError()
            self.delta_length += len(delta)
            delta.encode("utf-8")
            if self.delta_length > 16000:
                raise ProtocolError()
        elif method == "turn/started":
            identity = params.get("turn", {}).get("id")
            if (not isinstance(identity, str) or not 1 <= len(identity) <= 128
                    or self.turn_id is not None and self.turn_id != identity):
                raise ProtocolError()
            self.turn_id = identity
        elif method == "turn/completed":
            turn = params.get("turn", {})
            if turn.get("id") != self.turn_id or turn.get("status") != "completed" or turn.get("error") is not None:
                raise ProtocolError()
            if any(item.get("type") not in {"userMessage", "agentMessage", "reasoning"} for item in turn.get("items", [])):
                raise ProtocolError()
            text = "\n".join(self.texts.values())
            if not text or len(text) > 16000 or self.raw_completed != 1 or self.done.done() or self.replay:
                raise ProtocolError()
            self.done.set_result({"text": text, "incomplete": False, "usage": None})
        elif method == "rawResponse/completed":
            self.raw_completed += 1
            if self.raw_completed != 1:
                raise ProtocolError()
        elif method == "remoteControl/status/changed":
            if params.get("status") != "disabled":
                raise ProtocolError()
        elif method not in {
            "thread/started", "thread/status/changed", "thread/tokenUsage/updated",
            "deprecationNotice", "configWarning",
            "account/rateLimits/updated",
            "item/reasoning/summaryTextDelta", "item/reasoning/textDelta", "item/reasoning/summaryPartAdded",
        }:
            raise ProtocolError()

    async def run(self, runtime, data):
        budget = runtime.relay.arm(data.model)
        self.budget = budget
        try:
            params = thread_params(data.model)
            params["config"] = {"model_providers.evencomms.base_url": budget.base_url}
            result = await runtime.call("thread/start", params)
            thread = result.get("thread", {})
            self.thread_id = thread.get("id")
            if (not isinstance(self.thread_id, str) or not 1 <= len(self.thread_id) <= 128
                    or result.get("model") != data.model or thread.get("ephemeral") is not True
                    or result.get("modelProvider") != PROVIDER or thread.get("path") is not None):
                raise ProtocolError()
            runtime.relay.bind(budget, self.thread_id)
            if len(data.messages) > 1:
                items = history_items(data.messages[:-1])
                self.replay = deque(items)
                await runtime.call("thread/inject_items", {"threadId": self.thread_id, "items": items})
            result = await runtime.call("turn/start", turn_params(self.thread_id, data.messages[-1]))
            identity = result.get("turn", {}).get("id")
            if not isinstance(identity, str) or (self.turn_id is not None and identity != self.turn_id):
                raise ProtocolError()
            self.turn_id = identity
            result = await self.done
            if budget.non_401 != 1 or not 1 <= budget.attempts <= 3 or not budget.consumed:
                raise ProtocolError()
            return {"request_id": str(data.request_id), "model": data.model, **result}
        finally:
            try:
                await runtime.relay.finish(budget)
            finally:
                if not self.done.done():
                    self.done.cancel()
                elif not self.done.cancelled():
                    self.done.exception()
