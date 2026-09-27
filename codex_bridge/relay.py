"""Loopback Responses-only request barrier. OAuth headers exist only in RAM."""

import asyncio
import contextlib
from dataclasses import dataclass, field
import hashlib
from http.cookiejar import CookieJar, DefaultCookiePolicy
import hmac
import json
import logging
import re
import secrets

import httpx

from .errors import Failure, provider_failure
from .policy import INSTRUCTIONS, MODELS, VERSION


UPSTREAM = "https://chatgpt.com/backend-api/codex/responses"
HEADER_LIMIT = 32768
BODY_LIMIT = 10 * 1024 * 1024
RESPONSE_LIMIT = 16 * 1024 * 1024
ERROR_BODY_LIMIT = 16384
GENERATION_SECONDS = 85
MAX_CONNECTIONS = 4
FORWARD_HEADERS = frozenset({
    "authorization", "chatgpt-account-id", "user-agent", "originator", "accept", "content-type",
    "session-id", "thread-id", "x-client-request-id", "x-codex-beta-features", "x-codex-window-id",
    "x-codex-turn-metadata", "x-codex-routing-hint", "x-codex-turn-state", "x-codex-installation-id",
    "version", "x-openai-internal-codex-responses-lite",
})
ROUTING_HEADERS = frozenset({
    "x-openai-fedramp", "x-openai-internal-codex-residency", "x-openai-account-routing-override",
})
RESPONSE_HEADERS = frozenset({"x-codex-turn-state", "x-request-id", "openai-model", "x-reasoning-included"})

# The pinned HTTP/1.1 transport's DEBUG trace includes response headers. Do not
# allow an administrator's global debug level to turn this into credential logs.
for _logger in ("httpx", "httpcore", "httpcore.connection", "httpcore.http11", "httpcore.proxy"):
    logging.getLogger(_logger).disabled = True


class NoCookies(DefaultCookiePolicy):
    def set_ok(self, cookie, request):
        return False

    def return_ok(self, cookie, request):
        return False


class Rejected(Exception):
    def __init__(self, status=400, failure="protocol_mismatch"):
        super().__init__("Inference relay rejected request")
        self.status = status
        self.failure = failure


def decode_request(raw):
    def object_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError()
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValueError()

    value = json.loads(raw, object_pairs_hook=object_pairs, parse_constant=invalid_constant)
    # Only the pinned no-tool Lite request, not a general Responses proxy. Lite
    # embeds instructions/tools in input; the empty top-level fields are omitted.
    if (not isinstance(value, dict) or set(value) != {
            "model", "input", "tool_choice", "parallel_tool_calls", "reasoning", "store", "stream",
            "include", "prompt_cache_key", "text", "client_metadata",
        } or value.get("stream") is not True or value.get("store") is not False
            or value.get("tool_choice") != "auto" or value.get("parallel_tool_calls") is not False
            or value.get("include") != ["reasoning.encrypted_content"]
            or value.get("text") != {"verbosity": "low"}):
        raise ValueError()
    reasoning = value.get("reasoning")
    if (not isinstance(reasoning, dict) or set(reasoning) != {"effort", "context"}
            or reasoning.get("context") != "all_turns"
            or reasoning.get("effort") not in ("low", "medium", "high", "xhigh", "max", "ultra")):
        raise ValueError()
    metadata = value.get("client_metadata")
    if (not isinstance(value.get("prompt_cache_key"), str) or not 1 <= len(value["prompt_cache_key"]) <= 128
            or not isinstance(metadata, dict)
            or any(not isinstance(entry, str) for entry in metadata.values())):
        raise ValueError()
    items = value.get("input")
    if (not isinstance(items, list) or not 3 <= len(items) <= 64 or not isinstance(items[0], dict)
            or set(items[0]) != {"type", "id", "role", "tools"}
            or items[0].get("type") != "additional_tools" or items[0].get("role") != "developer"
            or items[0].get("tools") != [] or not isinstance(items[0].get("id"), str)
            or not re.fullmatch(r"at_[A-Za-z0-9_-]{1,124}", items[0]["id"])):
        raise ValueError()
    for index, item in enumerate(items[1:], 1):
        if (not isinstance(item, dict) or item.get("type") != "message"
                or set(item) - {"type", "id", "role", "content", "phase", "internal_chat_message_metadata_passthrough"}
                or item.get("role") not in ("developer", "user", "assistant")
                or "id" in item and (not isinstance(item["id"], str) or not 1 <= len(item["id"]) <= 128)
                or item.get("phase") not in (None, "commentary", "final_answer")
                or not isinstance(item.get("content"), list) or not 1 <= len(item["content"]) <= 4):
            raise ValueError()
        metadata = item.get("internal_chat_message_metadata_passthrough", {})
        if (not isinstance(metadata, dict) or set(metadata) - {"turn_id", "create_time", "content_item_kinds"}
                or "turn_id" in metadata and not isinstance(metadata["turn_id"], str)
                or "create_time" in metadata and type(metadata["create_time"]) not in (int, float)
                or "content_item_kinds" in metadata and (
                    not isinstance(metadata["content_item_kinds"], list)
                    or any(not isinstance(kind, str) for kind in metadata["content_item_kinds"]))):
            raise ValueError()
        if index == 1:
            if item.get("role") != "developer" or item["content"] != [{"type": "input_text", "text": INSTRUCTIONS}]:
                raise ValueError()
        elif item.get("role") == "developer":
            if index != 2 or metadata.get("content_item_kinds") != ["host_skills.instructions"]:
                raise ValueError()
        for part in item["content"]:
            if not isinstance(part, dict):
                raise ValueError()
            if part.get("type") == "input_image":
                if (item["role"] != "user" or set(part) - {"type", "image_url", "detail"}
                        or not isinstance(part.get("image_url"), str)
                        or not part["image_url"].startswith("data:image/jpeg;base64,")
                        or part.get("detail") not in (None, "auto", "low", "high", "original")):
                    raise ValueError()
            elif (set(part) != {"type", "text"} or not isinstance(part.get("text"), str)
                    or part.get("type") != ("output_text" if item["role"] == "assistant" else "input_text")):
                raise ValueError()
    if items[-1].get("role") != "user":
        raise ValueError()
    # Recovered authentication must not change the authorized inference input.
    fingerprint = hashlib.sha256(json.dumps(
        {key: value.get(key) for key in ("model", "input", "instructions", "tools", "tool_choice", "text", "reasoning")},
        ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode()).digest()
    return value.get("model"), fingerprint


@dataclass(repr=False)
class Budget:
    base_url: str
    path: str
    model: str
    deadline: float
    thread_id: str | None = None
    armed: bool = True
    busy: bool = False
    consumed: bool = False
    attempts: int = 0
    non_401: int = 0
    blocked: int = 0
    completed: bool = False
    failure: str | None = None
    upstream_status: int | None = None
    fingerprint: bytes | None = None
    account: bytes | None = None
    tasks: set = field(default_factory=set)


class Relay:
    def __init__(self, *, check_workspace=None, probe_origin=None, transport=None):
        # Internal fixture injection only. Never read an upstream/proxy URL from
        # environment, headers, request body, or the public bridge API.
        if probe_origin is not None and not re.fullmatch(r"http://127\.0\.0\.1:[0-9]{1,5}", probe_origin):
            raise ValueError("Probe origin must be loopback")
        self.upstream = UPSTREAM if probe_origin is None else probe_origin + "/responses"
        self.check_workspace = check_workspace
        self.transport = transport
        self.server = None
        self.client = None
        self.authority = None
        self.base_url = None
        self.current = None
        self.tasks = set()
        self.closed = False
        self.downstream_requests = 0
        self.blocked_requests = 0
        self.forwarded_requests = 0
        self.forwarded_non_401 = 0

    async def start(self):
        self.client = httpx.AsyncClient(
            transport=self.transport or httpx.AsyncHTTPTransport(
                retries=0, http1=True, http2=False, trust_env=False,
                limits=httpx.Limits(max_connections=1, max_keepalive_connections=1)),
            timeout=httpx.Timeout(GENERATION_SECONDS, connect=5, pool=1, write=10), trust_env=False,
            follow_redirects=False, cookies=CookieJar(policy=NoCookies()),
            headers={"Accept-Encoding": "identity"},
        )
        self.server = await asyncio.start_server(self.accept, "127.0.0.1", 0, limit=HEADER_LIMIT + 1, backlog=4)
        self.authority = "127.0.0.1:" + str(self.server.sockets[0].getsockname()[1])
        self.base_url = "http://" + self.authority + "/" + secrets.token_hex(32)

    def arm(self, model):
        if (self.closed or self.server is None or self.current is not None
                or model not in {row["id"] for row in MODELS}):
            raise Rejected(409)
        url = self.base_url + "/" + secrets.token_hex(32)
        budget = Budget(url, url.split(self.authority, 1)[1] + "/responses", model,
                        asyncio.get_running_loop().time() + GENERATION_SECONDS)
        self.current = budget
        return budget

    def bind(self, budget, thread_id):
        if (self.current is not budget or budget.thread_id is not None or not budget.armed
                or not isinstance(thread_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", thread_id)):
            raise Rejected(409)
        budget.thread_id = thread_id

    def disarm(self):
        if self.current:
            self.current.armed = False
            self.current.consumed = True
            for task in tuple(self.current.tasks):
                if not task.done() and not task.cancelling():
                    task.cancel()

    async def finish(self, budget):
        budget.armed = False
        budget.consumed = True
        for task in tuple(budget.tasks):
            if not task.done() and not task.cancelling():
                task.cancel()
        await asyncio.gather(*budget.tasks, return_exceptions=True)
        if self.current is budget:
            self.current = None

    def accept(self, reader, writer):
        # Admission happens before scheduling a handler, so connections cannot
        # create an unbounded queue of tasks holding request bodies or headers.
        peer = writer.get_extra_info("peername")
        if self.closed or not peer or peer[0] != "127.0.0.1" or len(self.tasks) >= MAX_CONNECTIONS:
            writer.close()
            return
        task = asyncio.create_task(self.handle(reader, writer))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def handle(self, reader, writer):
        budget = None
        claimed = False
        started = False

        async def reply(status):
            nonlocal started
            # Provider errors and cookies never cross this boundary. Status 401
            # alone is sufficient for the SDK's managed credential recovery.
            payload = b'{"error":{"message":"Inference request rejected or failed"}}'
            started = True
            writer.write(b"HTTP/1.1 " + str(status).encode() + b" Response\r\nContent-Type: application/json\r\n"
                         b"Cache-Control: no-store\r\nConnection: close\r\nContent-Length: "
                         + str(len(payload)).encode() + b"\r\n\r\n" + payload)
            async with asyncio.timeout(3):
                await writer.drain()

        try:
            async with asyncio.timeout(10):
                header = await reader.readuntil(b"\r\n\r\n")
                if len(header) > HEADER_LIMIT:
                    raise Rejected(431)
                lines = header[:-4].split(b"\r\n")
                method, target, version = lines[0].decode("ascii").split(" ")
                if method != "POST" or version != "HTTP/1.1":
                    raise Rejected(405)
                headers = {}
                for line in lines[1:]:
                    if len(line) > 8192 or b":" not in line:
                        raise Rejected(431)
                    name, value = line.split(b":", 1)
                    name = name.decode("ascii").lower()
                    value = value.strip(b" ").decode("ascii")
                    if (not re.fullmatch(r"[a-z0-9-]+", name) or name in headers
                            or any(not 32 <= ord(char) < 127 for char in value)
                            or name not in FORWARD_HEADERS | ROUTING_HEADERS | {"host", "content-length", "accept-encoding"}):
                        raise Rejected()
                    headers[name] = value
                if headers.get("host") != self.authority or headers.get("accept-encoding", "identity") != "identity":
                    raise Rejected()
                self.downstream_requests += 1
                candidate = self.current
                if candidate is None or not hmac.compare_digest(target, candidate.path):
                    raise Rejected(403)
                budget = candidate
                if (not budget.armed or budget.consumed or budget.busy or budget.attempts >= 3
                        or asyncio.get_running_loop().time() >= budget.deadline):
                    raise Rejected(409)
                # No await separates checking and reserving this generation.
                budget.busy = True
                budget.consumed = True
                budget.tasks.add(asyncio.current_task())
                claimed = True
                if budget.failure == "account_auth":
                    budget.failure = None  # A confirmed 401 permits managed recovery, not a permanent failure.
                if ("x-openai-fedramp" in headers or "x-openai-internal-codex-residency" in headers
                        or headers.get("x-openai-account-routing-override") not in (None, "NO_CONSTRAINT")):
                    raise Rejected(422, "unsupported_workspace")
                length = headers.get("content-length", "")
                if not length.isdecimal() or len(length) > 8 or not 0 < int(length) <= BODY_LIMIT:
                    raise Rejected(413)
                if (headers.get("content-type") != "application/json" or headers.get("accept") != "text/event-stream"
                        or not re.fullmatch(r"Bearer [!-~]{1,8000}", headers.get("authorization", ""))
                        or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", headers.get("chatgpt-account-id", ""))
                        or not budget.thread_id or headers.get("thread-id") != budget.thread_id
                        or headers.get("x-client-request-id") != budget.thread_id
                        or headers.get("version") != VERSION
                        or headers.get("x-openai-internal-codex-responses-lite") != "true"
                        or not headers.get("user-agent") or not headers.get("originator")):
                    raise Rejected()
                async with asyncio.timeout(max(0, budget.deadline - asyncio.get_running_loop().time())):
                    raw = await reader.readexactly(int(length))
                try:
                    model, fingerprint = decode_request(raw)
                except (ValueError, TypeError, RecursionError):
                    raise Rejected() from None
                account = hashlib.sha256(headers["chatgpt-account-id"].encode()).digest()
                if (model != budget.model or budget.fingerprint not in (None, fingerprint)
                        or budget.account not in (None, account)):
                    raise Rejected()
                budget.fingerprint, budget.account = fingerprint, account
            async with asyncio.timeout(max(0, budget.deadline - asyncio.get_running_loop().time())):
                # Opaque relay URLs bypass native Responses routing. Resolve the
                # account-owned policy on every attempt, including OAuth recovery.
                if self.check_workspace is None:
                    raise Failure("unsupported_workspace")
                try:
                    verified = await self.check_workspace(headers["chatgpt-account-id"])
                except Failure:
                    raise
                except Exception:
                    raise Failure("unsupported_workspace") from None
                if verified is not True:
                    raise Failure("unsupported_workspace")
                forwarded = {key: value for key, value in headers.items() if key in FORWARD_HEADERS}
                forwarded["accept-encoding"] = "identity"
                # The pessimistic consumed reservation survives every exception,
                # cancellation and network uncertainty. HTTPX never retries.
                budget.attempts += 1
                self.forwarded_requests += 1
                async with self.client.stream("POST", self.upstream, content=raw, headers=forwarded,
                                              follow_redirects=False, timeout=httpx.Timeout(
                                                  max(0.001, budget.deadline - asyncio.get_running_loop().time()),
                                                  connect=5, pool=1, write=10)) as response:
                    status = response.status_code
                    budget.upstream_status = status
                    if status != 401:
                        budget.non_401 += 1
                        self.forwarded_non_401 += 1
                    if status != 200:
                        budget.failure = provider_failure(status)
                        # Status wins even for encoded/unreadable errors. Inspect
                        # only small identity JSON and allowlisted code/type labels.
                        if (status in {400, 403, 404, 422}
                                and sum(len(k) + len(v) for k, v in response.headers.raw) <= HEADER_LIMIT
                                and response.headers.get("content-encoding", "identity") == "identity"
                                and response.headers.get("content-type", "").split(";", 1)[0].strip().lower() == "application/json"):
                            with contextlib.suppress(ValueError, TypeError, RecursionError, httpx.HTTPError, TimeoutError):
                                async with asyncio.timeout(1):
                                    body = bytearray()
                                    async for chunk in response.aiter_bytes(chunk_size=ERROR_BODY_LIMIT + 1):
                                        body.extend(chunk)
                                        if len(body) > ERROR_BODY_LIMIT:
                                            break
                                    if len(body) <= ERROR_BODY_LIMIT:
                                        document = json.loads(body)
                                        error = document.get("error") if isinstance(document, dict) else None
                                        if isinstance(error, dict):
                                            code = next((error.get(key) for key in ("code", "type")
                                                         if isinstance(error.get(key), str) and error[key] in {
                                                             "model_not_found", "model_not_available", "unsupported_model",
                                                             "context_length_exceeded", "context_window_exceeded",
                                                              "content_policy_violation", "policy_violation",
                                                              "cyber_policy", "bio_policy", "misalignment_policy_violation",
                                                         }), None)
                                            budget.failure = provider_failure(status, code)
                        await response.aclose()
                        await reply(status if 400 <= status <= 599 else 502)
                        if status == 401 and budget.attempts < 3 and budget.armed:
                            budget.consumed = False
                        return
                    if sum(len(k) + len(v) for k, v in response.headers.raw) > HEADER_LIMIT:
                        raise Rejected(502)
                    if response.headers.get("content-encoding", "identity") != "identity":
                        raise Rejected(502, "response_encoding")
                    if response.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "text/event-stream":
                        raise Rejected(502)
                    outgoing = b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nCache-Control: no-store\r\nConnection: close\r\n"
                    for name in RESPONSE_HEADERS:
                        if name in response.headers:
                            value = response.headers[name]
                            if len(value) > 8192 or any(not 32 <= ord(char) < 127 for char in value):
                                raise Rejected(502)
                            outgoing += name.encode() + b": " + value.encode("ascii") + b"\r\n"
                    started = True
                    writer.write(outgoing + b"\r\n")
                    total = 0
                    async for chunk in response.aiter_raw():
                        total += len(chunk)
                        if total > RESPONSE_LIMIT:
                            raise Rejected(502)
                        for offset in range(0, len(chunk), 65536):
                            writer.write(chunk[offset:offset + 65536])
                            async with asyncio.timeout(3):
                                await writer.drain()
                    budget.completed = True
        except asyncio.CancelledError:
            if claimed:
                budget.consumed = True
        except Exception as error:
            self.blocked_requests += 1
            if budget:
                budget.blocked += 1
            if claimed:
                budget.consumed = True
                if budget.failure is None:
                    budget.failure = (error.failure if isinstance(error, Rejected) else
                                      error.code if isinstance(error, Failure) else
                                      "timeout" if isinstance(error, (TimeoutError, httpx.TimeoutException)) else
                                      "network_error" if isinstance(error, (httpx.HTTPError, ConnectionError)) else
                                      "protocol_mismatch")
            if not started:
                with contextlib.suppress(Exception):
                    await reply(error.status if isinstance(error, (Rejected, Failure)) else 502)
        finally:
            if claimed:
                budget.busy = False
                budget.tasks.discard(asyncio.current_task())
            writer.close()
            with contextlib.suppress(Exception):
                async with asyncio.timeout(1):
                    await writer.wait_closed()

    async def close(self):
        self.closed = True
        self.disarm()
        if self.server:
            self.server.close()
        tasks = tuple(self.tasks)
        for task in tasks:
            if not task.done() and not task.cancelling():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()
        if self.server:
            await self.server.wait_closed()
        if self.client:
            await self.client.aclose()
        self.current = None
