"""Private HTTP adapter. Run one worker in the dedicated, volume-free container."""

import asyncio
import contextlib
from contextlib import asynccontextmanager
import hashlib
import hmac
import json
import os
import re
import resource
import time
from uuid import UUID

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from .generation import Generation
from .policy import BRIDGE_TOKEN_PATTERN, DEVICE_URL, MODELS, USER_CODE_PATTERN, generation_allowed
from .private_token import load_token_file
from .probe import run_probe
from .rpc import ProtocolError, Runtime
from .validation import BODY_LIMIT, Chat, Lease, SESSION_PATTERN


LEASE_SECONDS = 120
LOGIN_SECONDS = 15 * 60
SESSION_SECONDS = 8 * 60 * 60
CHAT_SECONDS = 85
THREAD_CAP = 8
RETRY_WARNING = (
    " The bridge does not automatically resubmit failed generation. "
    "Codex may retry during bounded OAuth credential recovery. "
    "No paid API fallback; sign in again before retrying manually."
)


async def body(request, limit):
    if request.headers.get("content-encoding", "identity") != "identity":
        raise HTTPException(415, "Unsupported content encoding")
    length = request.headers.get("content-length")
    if length is not None and (len(length) > 12 or not length.isdecimal() or int(length) > limit):
        raise HTTPException(413, "Request body exceeds limits")
    raw = bytearray()
    try:
        async with asyncio.timeout(10):
            async for chunk in request.stream():
                if len(raw) + len(chunk) > limit:
                    raise HTTPException(413, "Request body exceeds limits")
                raw.extend(chunk)
    except TimeoutError:
        raise HTTPException(408, "Request body timed out") from None
    return raw


class Session:
    def __init__(self, manager):
        self.manager = manager
        self.created = manager.clock()
        self.lease = self.created + LEASE_SECONDS
        self.state = "pending"
        self.reason = None
        self.verification_url = None
        self.user_code = None
        self.login_id = None
        self.completion = None
        self.ready = asyncio.Event()
        self.login_done = asyncio.Event()
        self.runtime = manager.runtime_factory(manager.binary, self.event, temp_parent=manager.temp_parent)
        self.login_task = None
        self.active = None
        self.generation = None
        self.cleanup = None
        self.threads = 0
        self.results = {}

    def status(self):
        return {"state": self.state, "verification_url": self.verification_url, "user_code": self.user_code,
                "generation_enabled": self.state == "connected" and self.manager.generation_enabled}

    def fail(self, reason="runtime"):
        self.state = "failed"
        self.reason = reason
        self.verification_url = self.user_code = None
        self.ready.set()
        self.login_done.set()
        if self.generation:
            self.generation.fail()
        self.runtime.abort()

    def event(self, method, params):
        try:
            if not isinstance(params, dict):
                raise ProtocolError()
            if method == "bridge/failed":
                self.fail()
            elif method == "account/login/completed":
                identity = params.get("loginId")
                if (self.state != "pending" or not isinstance(identity, str) or str(UUID(identity)) != identity
                        or type(params.get("success")) is not bool):
                    raise ProtocolError()
                if self.completion is not None or (self.login_id is not None and identity != self.login_id):
                    raise ProtocolError()
                self.verification_url = self.user_code = None
                self.completion = (identity, params["success"])
                self.login_done.set()
                if not params["success"]:
                    self.fail("login")
            elif method == "account/updated":
                # A notification alone never establishes readiness.
                if self.state == "connected" and params.get("authMode") != "chatgpt":
                    raise ProtocolError()
            elif method == "remoteControl/status/changed":
                if params.get("status") != "disabled":
                    raise ProtocolError()
            elif method in {"account/rateLimits/updated", "configWarning", "deprecationNotice"}:
                pass
            elif self.generation:
                self.generation.event(method, params)
            elif method != "thread/status/changed":
                raise ProtocolError()
        except Exception:
            self.fail()
            raise ProtocolError() from None

    async def login(self):
        try:
            await self.runtime.start()
            account = await self.runtime.call("account/read", {"refreshToken": False})
            if account.get("account") is not None:
                raise ProtocolError()
            result = await self.runtime.call("account/login/start", {"type": "chatgptDeviceCode"})
            identity, url, code = result.get("loginId"), result.get("verificationUrl"), result.get("userCode")
            if (result.get("type") != "chatgptDeviceCode" or not isinstance(identity, str)
                    or str(UUID(identity)) != identity or url != DEVICE_URL or not isinstance(code, str)
                    or not re.fullmatch(USER_CODE_PATTERN, code)):
                raise ProtocolError()
            self.login_id = identity
            if self.completion is None and self.state == "pending":
                self.verification_url, self.user_code = url, code
            self.ready.set()
            async with asyncio.timeout(max(0, self.created + LOGIN_SECONDS - self.manager.clock())):
                await self.login_done.wait()
            if self.state != "pending" or self.completion != (identity, True):
                raise ProtocolError()
            account = await self.runtime.call("account/read", {"refreshToken": False})
            if not isinstance(account.get("account"), dict) or account["account"].get("type") != "chatgpt":
                raise ProtocolError()
            if self.state != "pending":
                raise ProtocolError()
            self.state = "connected"
            self.login_id = None
            self.completion = None
        except asyncio.CancelledError:
            raise
        except Exception:
            self.fail("login")
            # Keep the failed reservation if cleanup needs a later retry, but
            # never leave a credential-bearing task exception unobserved.
            with contextlib.suppress(Exception):
                await self.runtime.close(logout=False)
        finally:
            if self.state != "pending":
                self.verification_url = self.user_code = None
            self.ready.set()

    async def stop(self):
        if self.cleanup is not None and self.cleanup.done():
            if self.cleanup.cancelled() or self.cleanup.exception() is not None:
                self.cleanup = None
        if self.cleanup is None:
            async def cleanup():
                self.verification_url = self.user_code = self.login_id = self.completion = None
                self.ready.set()
                self.login_done.set()
                tasks = [task for task in (self.login_task, self.active) if task]
                for task in tasks:
                    if not task.done():
                        task.cancel()
                if self.generation:
                    self.generation.fail()
                # Logout is best effort and bounded; process reaping precedes rmdir.
                try:
                    await self.runtime.close()
                finally:
                    await asyncio.gather(*tasks, return_exceptions=True)
                    self.login_task = self.active = self.generation = None
                    self.results.clear()
            self.cleanup = asyncio.create_task(cleanup())
        await asyncio.shield(self.cleanup)


class Bridge:
    def __init__(self, binary, *, runtime_factory=Runtime, clock=time.monotonic, temp_parent="/tmp",
                 generation_enabled=False, binary_verified=False):
        self.binary = binary
        self.runtime_factory = runtime_factory
        self.clock = clock
        self.temp_parent = temp_parent
        self.generation_enabled = generation_enabled
        self.binary_verified = binary_verified
        self.sessions = {}
        self.validations = set()
        self.lock = asyncio.Lock()
        self.closed = False
        self.sweeper = asyncio.create_task(self.sweep())

    def expired(self, session):
        now = self.clock()
        return (now >= session.lease or now >= session.created + SESSION_SECONDS
                or (session.state == "pending" and now >= session.created + LOGIN_SECONDS))

    async def prune(self):
        async with self.lock:
            for identity, session in list(self.sessions.items()):
                try:
                    if self.expired(session):
                        session.state = "disconnected"
                        await session.stop()
                        self.sessions.pop(identity, None)
                    elif session.state == "failed" or session.runtime.failed:
                        await session.stop()
                        self.sessions.pop(identity, None)
                except Exception:
                    # One failed directory removal must not stop expiry of the
                    # other process. Keep its admission slot reserved.
                    session.fail("cleanup")

    async def sweep(self):
        while True:
            await asyncio.sleep(1)
            await self.prune()

    async def login(self, identity):
        await self.prune()
        async with self.lock:
            if self.closed or not self.binary_verified:
                raise HTTPException(503, "Pinned Codex runtime is unavailable")
            session = self.sessions.get(identity)
            if session and session.state == "failed":
                await session.stop()
                del self.sessions[identity]
                session = None
            if session is None:
                if len(self.sessions) >= 2:
                    raise HTTPException(429, "At most two Codex sessions are permitted")
                session = Session(self)
                self.sessions[identity] = session
                session.login_task = asyncio.create_task(session.login())
        try:
            async with asyncio.timeout(20):
                await session.ready.wait()
        except TimeoutError:
            session.fail("login")
            await session.stop()
        return session.status()

    async def delete(self, identity):
        async with self.lock:
            session = self.sessions.get(identity)
            if session:
                session.state = "disconnected"
                await session.stop()
                self.sessions.pop(identity, None)

    async def close(self):
        self.closed = True
        self.sweeper.cancel()
        await asyncio.gather(self.sweeper, return_exceptions=True)
        for identity in list(self.sessions):
            await self.delete(identity)
        await asyncio.gather(*self.validations, return_exceptions=True)

    async def chat(self, identity, request):
        await self.prune()
        session = self.sessions.get(identity)
        if session and session.reason == "thread_cap":
            raise HTTPException(409, "Codex session reached eight requests; sign in again")
        if not session or session.state != "connected":
            raise HTTPException(409, "Connect a ChatGPT account before chatting")
        if not self.generation_enabled:
            raise HTTPException(503, "Codex generation is disabled: runtime safety probe did not pass")
        if session.active is not None:
            raise HTTPException(429, "A Codex request is already active for this session")
        if len(self.validations) >= 2:
            raise HTTPException(429, "Codex request validation is busy")

        async def work():
            async with asyncio.timeout(CHAT_SECONDS):
                raw = await body(request, BODY_LIMIT)
                if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
                    raise HTTPException(415, "Expected application/json")
                if len(self.validations) >= 2:
                    raise HTTPException(429, "Codex request validation is busy")
                validation = asyncio.create_task(asyncio.to_thread(Chat.model_validate_json, raw))
                self.validations.add(validation)

                def finished(done):
                    self.validations.discard(done)
                    if not done.cancelled():
                        done.exception()

                validation.add_done_callback(finished)
                try:
                    data = await asyncio.shield(validation)
                except (ValidationError, ValueError):
                    raise HTTPException(422, "Invalid chat request; check fields, images and text limits") from None
                if session.state != "connected" or self.expired(session):
                    raise ProtocolError()
                if data.model not in {row["id"] for row in MODELS}:
                    raise HTTPException(422, "Select a pinned model selector; availability depends on your account")
                key = str(data.request_id)
                fingerprint = hashlib.sha256(data.model_dump_json().encode()).digest()
                if key in session.results:
                    old_fingerprint, result = session.results[key]
                    if old_fingerprint != fingerprint:
                        raise HTTPException(409, "Request ID was already used with different content")
                    return result
                session.threads += 1
                session.generation = Generation()
                try:
                    result = await session.generation.run(session.runtime, data)
                finally:
                    session.generation = None
                if session.state != "connected":
                    raise ProtocolError()
                session.results[key] = (fingerprint, result)
                return result

        operation = session.active = asyncio.create_task(work())
        try:
            result = await asyncio.shield(operation)
            if session.threads >= THREAD_CAP:
                session.state, session.reason = "failed", "thread_cap"
                await session.stop()
            return result
        except HTTPException:
            raise
        except asyncio.CancelledError:
            session.fail()
            await session.stop()
            if asyncio.current_task().cancelling():
                raise
            raise HTTPException(409, "Codex request was cancelled") from None
        except TimeoutError:
            session.fail()
            await session.stop()
            raise HTTPException(504, "Codex request timed out." + RETRY_WARNING) from None
        except Exception:
            session.fail()
            await session.stop()
            raise HTTPException(502, "Codex request failed or an unsafe runtime event was rejected." + RETRY_WARNING) from None
        finally:
            # Retain admission through result handling and eighth-thread logout,
            # even if the worker finished before its HTTP waiter resumed.
            if session.active is operation:
                session.active = None


class PrivateBoundary:
    def __init__(self, app, token):
        self.app = app
        self.token = token
        self.active = 0

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
                return
            return await self.app(scope, receive, send)
        headers = scope.get("headers", [])
        auth = [value for key, value in headers if key.lower() == b"authorization"]
        token = self.token().encode("ascii")
        if any(key.lower() == b"origin" for key, _ in headers):
            response = JSONResponse({"detail": "Browser origins are not permitted"}, 403)
        elif not (scope["path"] == "/health" and scope["method"] == "GET") and (
                len(auth) != 1 or not hmac.compare_digest(auth[0], b"Bearer " + token)):
            response = JSONResponse({"detail": "Unauthorized"}, 401)
        elif self.active >= 16:
            response = JSONResponse({"detail": "Bridge is busy"}, 503)
        else:
            response = None
        started = False

        async def private_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                message["headers"].append((b"cache-control", b"no-store"))
            await send(message)

        if response is not None:
            await response(scope, receive, private_send)
            return
        self.active += 1
        try:
            await self.app(scope, receive, private_send)
        except Exception:
            # Never let provider messages, request bodies or credential-bearing
            # exception representations reach HTTP or uvicorn's error logger.
            if not started:
                await JSONResponse({"detail": "Bridge request failed"}, 502)(scope, receive, private_send)
        finally:
            self.active -= 1


def create_app(*, token=None, token_file=None, binary=None, runtime_factory=Runtime, probe=run_probe,
               clock=time.monotonic, temp_parent="/tmp"):
    token = token if token is not None else os.environ.get("CODEX_BRIDGE_TOKEN", "")
    token_file = token_file if token_file is not None else os.environ.get("CODEX_BRIDGE_TOKEN_FILE", "")
    secret = ""
    binary = binary or os.environ.get("CODEX_BINARY", "/usr/local/bin/codex")

    @asynccontextmanager
    async def lifespan(app):
        nonlocal secret
        if token and token_file:
            raise RuntimeError("Configure only one of CODEX_BRIDGE_TOKEN or CODEX_BRIDGE_TOKEN_FILE")
        try:
            secret = load_token_file(token_file) if token_file else token
        except ValueError:
            raise RuntimeError("Invalid CODEX_BRIDGE_TOKEN_FILE") from None
        if not re.fullmatch(BRIDGE_TOKEN_PATTERN, secret):
            raise RuntimeError("CODEX_BRIDGE_TOKEN must contain 32 to 256 hexadecimal characters")
        # Includes all subsequently spawned Codex children and their credentials.
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        report = {}
        with contextlib.suppress(Exception):
            async with asyncio.timeout(75):
                report = await probe(binary, temp_parent=temp_parent)
        app.state.bridge = Bridge(binary, runtime_factory=runtime_factory, clock=clock, temp_parent=temp_parent,
                                  generation_enabled=report.get("generation_enabled") is True and generation_allowed(report),
                                  binary_verified=report.get("binary_verified") is True)
        try:
            yield
        finally:
            await app.state.bridge.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(PrivateBoundary, token=lambda: secret)

    def identity(value):
        if not re.fullmatch(SESSION_PATTERN, value):
            raise HTTPException(422, "Expected a backend-generated 64-character session identifier")
        return value

    @app.get("/health")
    async def health():
        return {"ok": True}

    @app.get("/ready")
    async def ready():
        bridge = app.state.bridge
        return {"binary_verified": bridge.binary_verified, "generation_enabled": bridge.generation_enabled,
                "active_sessions": len(bridge.sessions)}

    @app.post("/sessions/{session_id}/login")
    async def login(session_id: str, request: Request):
        identity(session_id)
        raw = await body(request, 1024)
        if raw:
            try:
                if json.loads(raw) != {}:
                    raise ValueError()
            except (ValueError, RecursionError):
                raise HTTPException(422, "Login accepts only an empty object") from None
        return await app.state.bridge.login(session_id)

    @app.get("/sessions/{session_id}/status")
    async def status(session_id: str):
        identity(session_id)
        bridge = app.state.bridge
        await bridge.prune()
        session = bridge.sessions.get(session_id)
        return session.status() if session else {"state": "disconnected", "verification_url": None,
                                               "user_code": None, "generation_enabled": False}

    @app.get("/sessions/{session_id}/models")
    async def models(session_id: str):
        identity(session_id)
        bridge = app.state.bridge
        await bridge.prune()
        session = bridge.sessions.get(session_id)
        if not session or session.state != "connected":
            raise HTTPException(409, "Connect a ChatGPT account before listing model selectors")
        return {"models": MODELS}

    @app.post("/sessions/{session_id}/chat")
    async def chat(session_id: str, request: Request):
        return await app.state.bridge.chat(identity(session_id), request)

    @app.delete("/sessions/{session_id}", status_code=204)
    async def delete(session_id: str):
        await app.state.bridge.delete(identity(session_id))
        return Response(status_code=204)

    @app.post("/lease")
    async def lease(request: Request):
        raw = await body(request, 65536)
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise HTTPException(415, "Expected application/json")
        try:
            data = Lease.model_validate_json(raw)
        except (ValidationError, ValueError):
            raise HTTPException(422, "Invalid lease request") from None
        bridge = app.state.bridge
        await bridge.prune()
        renewed = []
        for session_id in data.sessions:
            session = bridge.sessions.get(session_id)
            if (session and session.state in {"pending", "connected"}
                    and not session.runtime.failed and not bridge.expired(session)):
                session.lease = bridge.clock() + LEASE_SECONDS
                renewed.append(session_id)
        return {"sessions": renewed}

    return app


app = create_app()
