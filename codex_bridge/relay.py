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

from .policy import MODELS


UPSTREAM = "https://chatgpt.com/backend-api/codex/responses"
HEADER_LIMIT = 32768
BODY_LIMIT = 10 * 1024 * 1024
RESPONSE_LIMIT = 16 * 1024 * 1024
GENERATION_SECONDS = 85
MAX_CONNECTIONS = 4
FORWARD_HEADERS = frozenset({
    "authorization", "chatgpt-account-id", "user-agent", "originator", "accept", "content-type",
    "session-id", "thread-id", "x-client-request-id", "x-codex-beta-features", "x-codex-window-id",
    "x-codex-turn-metadata", "x-codex-routing-hint", "x-codex-turn-state", "x-codex-installation-id",
    "x-openai-internal-codex-residency", "x-openai-account-routing-override",
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
    def __init__(self, status=400):
        super().__init__("Inference relay rejected request")
        self.status = status


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
    if (not isinstance(value, dict) or value.get("tools") != [] or value.get("stream") is not True
            or value.get("store") is not False or value.get("background", False) is not False
            or value.get("previous_response_id") is not None or value.get("tool_choice") not in {"auto", "none"}):
        raise ValueError()
    items = value.get("input")
    if (not isinstance(items, list) or not 1 <= len(items) <= 64
            or any(not isinstance(item, dict) or item.get("type") != "message" for item in items)):
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
    fingerprint: bytes | None = None
    account: bytes | None = None
    tasks: set = field(default_factory=set)


class Relay:
    def __init__(self, *, probe_origin=None, transport=None):
        # Internal fixture injection only. Never read an upstream/proxy URL from
        # environment, headers, request body, or the public bridge API.
        if probe_origin is not None and not re.fullmatch(r"http://127\.0\.0\.1:[0-9]{1,5}", probe_origin):
            raise ValueError("Probe origin must be loopback")
        self.upstream = UPSTREAM if probe_origin is None else probe_origin + "/responses"
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
            timeout=httpx.Timeout(15, connect=5, pool=1, write=10), trust_env=False,
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
                            or name not in FORWARD_HEADERS | {"host", "content-length", "accept-encoding"}):
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
                length = headers.get("content-length", "")
                if not length.isdecimal() or len(length) > 8 or not 0 < int(length) <= BODY_LIMIT:
                    raise Rejected(413)
                if (headers.get("content-type") != "application/json" or headers.get("accept") != "text/event-stream"
                        or not re.fullmatch(r"Bearer [!-~]{1,8000}", headers.get("authorization", ""))
                        or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", headers.get("chatgpt-account-id", ""))
                        or not budget.thread_id or headers.get("thread-id") != budget.thread_id
                        or headers.get("x-client-request-id") != budget.thread_id
                        or not headers.get("user-agent") or not headers.get("originator")):
                    raise Rejected()
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
                forwarded = {key: value for key, value in headers.items() if key in FORWARD_HEADERS}
                forwarded["accept-encoding"] = "identity"
                # The pessimistic consumed reservation survives every exception,
                # cancellation and network uncertainty. HTTPX never retries.
                budget.attempts += 1
                self.forwarded_requests += 1
                async with self.client.stream("POST", self.upstream, content=raw, headers=forwarded,
                                              follow_redirects=False) as response:
                    status = response.status_code
                    if status != 401:
                        budget.non_401 += 1
                        self.forwarded_non_401 += 1
                    if (sum(len(k) + len(v) for k, v in response.headers.raw) > HEADER_LIMIT
                            or response.headers.get("content-encoding", "identity") != "identity"):
                        raise Rejected(502)
                    if status != 200:
                        await response.aclose()
                        await reply(status if 400 <= status <= 599 else 502)
                        if status == 401 and budget.attempts < 3 and budget.armed:
                            budget.consumed = False
                        return
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
            if not started:
                with contextlib.suppress(Exception):
                    await reply(error.status if isinstance(error, Rejected) else 502)
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
