"""Opt-in, RAM-only Codex sessions via the administrator's isolated bridge."""

import asyncio
import json
import re
import secrets
from collections import OrderedDict
from dataclasses import dataclass, field
from http.cookiejar import CookieJar, DefaultCookiePolicy

import httpx
from fastapi import Depends, HTTPException, Request
from pydantic import ValidationError

from .reply_stream import stream_reply
from .research import MODEL_ID, MODEL_TTL, PROVIDER_LIMIT, RESULT_TTL, TEXT_LIMIT, Input, Research, digest


DEVICE_URL = "https://auth.openai.com/codex/device"
LEASE_TTL = 120
LEASE_INTERVAL = 30
SESSION_TTL = 8 * 60 * 60
RETRY_WARNING = (
    " Retrying may consume additional plan allowance. Codex may retry during sign-in recovery."
    " No paid API fallback."
)
BRIDGE_ERROR_HEADER = "X-Evencomms-Codex-Error"
# The images are independently packaged. Accept only this private wire contract,
# never a bridge/provider error body. Tests keep this table aligned with the bridge.
BRIDGE_ERRORS = {
    "rate_limit": (429, "Codex allowance or rate limit reached. Check your ChatGPT usage limits before retrying."),
    "account_auth": (409, "ChatGPT authentication could not be recovered. Disconnect and sign in again."),
    "account_permission": (403, "OpenAI denied access to this request. Check your account and workspace permissions."),
    "model_unavailable": (422, "The selected Codex model is unavailable to this account. Check your plan's model access."),
    "request_rejected": (422, "OpenAI rejected the Codex request. Model or input compatibility may differ."),
    "context_limit": (422, "Codex rejected the conversation size or session budget. Start a new chat with less context."),
    "policy_rejected": (403, "OpenAI declined this request under its policy. No alternate model or provider was tried."),
    "provider_unavailable": (503, "The OpenAI Codex service could not complete the request."),
    "network_error": (503, "The bridge could not reach the OpenAI Codex service."),
    "timeout": (504, "The Codex request exceeded its response deadline."),
    "stream_incomplete": (502, "The Codex response stream ended before a complete reply was received."),
    "response_encoding": (502, "OpenAI returned an unsupported response encoding. The bridge did not forward it."),
    "protocol_mismatch": (502, "The Codex runtime and bridge request/response formats did not match."),
    "tool_rejected": (502, "Codex requested a tool or approval that Research does not permit. No tool was approved."),
    "model_changed": (422, "OpenAI selected a different model. Research discarded the response instead of switching models."),
    "unsupported_workspace": (422, "This account requires routing that the isolated Codex bridge does not support."),
    "generation_disabled": (503, "The Codex generation safety check did not pass. Sending remains disabled."),
    "runtime_error": (502, "The Codex runtime stopped before returning a complete reply."),
}


def disconnected(enabled=True):
    return {"enabled": enabled, "state": "disconnected", "verification_url": None,
            "user_code": None, "generation_enabled": False}


@dataclass(repr=False)
class Session:
    expires: float
    lease_until: float
    cleanup_until: float
    identity: str = field(default_factory=lambda: secrets.token_hex(32))
    started: bool = False
    creation_ack: object | None = None
    status: dict = field(default_factory=disconnected)
    models: tuple | None = None
    pending: tuple | None = None
    results: OrderedDict = field(default_factory=OrderedDict)


class CodexResearch:
    # Only the provider-independent, bounded JPEG worker is reused. Never create
    # another Research instance or inherit its API-key/provider fallback paths.
    # Admission is independent: two Codex workers plus two API Research workers.
    validate_chat = Research.validate_chat

    def __init__(self, config, credentials):
        self.config = config
        self.credentials = credentials
        self.enabled = bool(config.codex_bridge_url)
        self.closed = False
        self.sessions: dict[str, Session] = {}
        self.login_guards: dict[str, object] = {}
        self.retired: dict[str, Session] = {}
        self.cleanups = {}
        self.tasks = {}
        self.validations = {}
        self.chat_waiters: dict[str, int] = {}
        self.lifecycle_lock = asyncio.Lock()
        self.client = self.lifecycle_client = self.sweeper = None
        if self.enabled:
            self.client = httpx.AsyncClient(
                timeout=config.openai_timeout, trust_env=False, follow_redirects=False,
                cookies=CookieJar(policy=DefaultCookiePolicy(allowed_domains=())),
                limits=httpx.Limits(max_connections=2, max_keepalive_connections=2))
            # Leases and deletion must remain available with two busy generations.
            self.lifecycle_client = httpx.AsyncClient(
                timeout=min(5, config.openai_timeout), trust_env=False, follow_redirects=False,
                cookies=CookieJar(policy=DefaultCookiePolicy(allowed_domains=())),
                limits=httpx.Limits(max_connections=1, max_keepalive_connections=1))
            credentials.prune_hooks.append(self.prune)
            self.sweeper = asyncio.create_task(self.sweep())

    def check(self, parent, session=None):
        self.credentials.prune()
        if self.credentials.operators.get(parent, 0) <= self.credentials.now():
            raise HTTPException(401, "Invalid or expired operator token")
        if self.closed:
            raise HTTPException(503, "Codex Research is shutting down")
        if session is not None and self.sessions.get(parent) is not session:
            raise HTTPException(409, "Codex connection changed; request discarded")

    def session(self, parent, create=False):
        self.check(parent)
        if not self.enabled:
            raise HTTPException(503, "Codex account login is disabled")
        session = self.sessions.get(parent)
        if session is None:
            if not create:
                raise HTTPException(409, "Connect a Codex account before using Research")
            if parent in self.tasks:
                raise HTTPException(429, "Codex is still cancelling this operator's previous request")
            if len(self.sessions) + len(self.retired) >= 2:
                raise HTTPException(429, "Codex session limit reached; wait for cleanup or lease expiry")
            now = self.credentials.now()
            session = Session(expires=min(self.credentials.operators[parent], now + SESSION_TTL),
                              lease_until=now + LEASE_TTL, cleanup_until=now + LEASE_TTL)
            self.sessions[parent] = session
        if not create and not session.started:
            raise HTTPException(409, "Connect a Codex account before using Research")
        return session

    def clear(self, parent, *, bridge_closed=False):
        self.login_guards.pop(parent, None)
        session = self.sessions.pop(parent, None)
        if session is None:
            return None
        session.status = disconnected()
        session.results.clear()
        session.models = None
        if session.pending and session.pending[2] is not asyncio.current_task():
            session.pending[2].cancel()
        if session.started and not bridge_closed:
            self.retired[session.identity] = session
            return self.cleanup(session)
        return None

    def cleanup(self, session):
        if session.identity in self.cleanups:
            return self.cleanups[session.identity]

        async def remove():
            pending = session.pending[2] if session.pending else None
            for attempt in range(2 if pending and not pending.done() else 1):
                if attempt:
                    # A login transport may finish creating a process during
                    # cancellation. Delete again after its actual work finishes.
                    await asyncio.gather(asyncio.shield(pending), return_exceptions=True)
                try:
                    async with asyncio.timeout(min(5, self.config.openai_timeout)):
                        async with self.lifecycle_lock:
                            await self.fetch("DELETE", f"/sessions/{session.identity}", lifecycle=True)
                    if pending is None or pending.done():
                        self.retired.pop(session.identity, None)
                except (HTTPException, TimeoutError):
                    # Reserve capacity until the last possible lease expires.
                    pass

        task = asyncio.create_task(remove())
        self.cleanups[session.identity] = task

        def finished(done):
            self.cleanups.pop(session.identity, None)
            if not done.cancelled():
                done.exception()

        task.add_done_callback(finished)
        return task

    def prune(self):
        now = self.credentials.now()
        self.login_guards = {parent: guard for parent, guard in self.login_guards.items()
                             if self.credentials.operators.get(parent, 0) > now}
        for parent, session in list(self.sessions.items()):
            if (self.credentials.operators.get(parent, 0) <= now or session.expires <= now
                    or session.lease_until <= now):
                self.clear(parent)
        for identity, session in list(self.retired.items()):
            if session.cleanup_until <= now and identity not in self.cleanups:
                del self.retired[identity]
        for session in self.sessions.values():
            for identity, (_, expires, _) in list(session.results.items()):
                if expires <= now:
                    del session.results[identity]
            if session.models and session.models[0] <= now:
                session.models = None
        while sum(len(session.results) for session in self.sessions.values()) > 128:
            oldest = min((session for session in self.sessions.values() if session.results),
                         key=lambda session: next(iter(session.results.values()))[1])
            oldest.results.popitem(last=False)

    async def renew_leases(self):
        try:
            async with asyncio.timeout(min(5, self.config.openai_timeout)):
                async with self.lifecycle_lock:
                    self.credentials.prune()
                    if self.closed:
                        return
                    live = [(parent, session, session.creation_ack)
                            for parent, session in self.sessions.items() if session.started]
                    if not live:
                        return
                    sent = self.credentials.now()
                    for _, session, _ in live:
                        # Even a lost acknowledgement may have renewed remotely.
                        session.cleanup_until = min(session.expires, sent + LEASE_TTL)
                    identities = {session.identity for _, session, _ in live}
                    response = await self.fetch("POST", "/lease", {"sessions": sorted(identities)}, lifecycle=True)
                    renewed = response.get("sessions")
                    if (set(response) != {"sessions"} or not isinstance(renewed, list) or len(renewed) > len(live)
                            or any(not isinstance(identity, str) or not re.fullmatch(r"[a-f0-9]{64}", identity)
                                   or identity not in identities for identity in renewed)
                            or len(set(renewed)) != len(renewed)):
                        raise HTTPException(502, "Invalid Codex lease acknowledgement")
                    now = self.credentials.now()
                    for parent, session, creation_ack in live:
                        if self.sessions.get(parent) is not session:
                            continue
                        if session.identity not in renewed:
                            # Absence cannot prove reaping across an unacknowledged or newer login.
                            if creation_ack is not None and session.creation_ack is creation_ack:
                                self.clear(parent, bridge_closed=True)
                        elif (not self.closed and session.lease_until > now and session.expires > now
                              and self.credentials.operators.get(parent, 0) > now):
                            session.lease_until = min(session.expires, sent + LEASE_TTL)
                    self.credentials.prune()
        except (HTTPException, TimeoutError):
            pass  # Failed renewals never extend local lifetime; the bridge TTL is independent.

    async def sweep(self):
        next_lease = self.credentials.now() + LEASE_INTERVAL
        while True:
            await asyncio.sleep(5)
            self.credentials.prune()
            if self.credentials.now() >= next_lease:
                next_lease = self.credentials.now() + LEASE_INTERVAL
                await self.renew_leases()

    async def aclose(self):
        if self.closed:
            return
        self.closed = True
        self.login_guards.clear()
        if not self.enabled:
            return
        self.credentials.prune_hooks.remove(self.prune)
        self.sweeper.cancel()
        for parent in list(self.sessions):
            self.clear(parent)
        for session in list(self.retired.values()):
            self.cleanup(session)
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        workers = [asyncio.shield(task) for task in self.validations.values() if task is not None]
        await asyncio.gather(self.sweeper, *tasks, *workers, *list(self.cleanups.values()), return_exceptions=True)
        self.retired.clear()
        await self.client.aclose()
        await self.lifecycle_client.aclose()

    async def fetch(self, method, path, payload=None, *, lifecycle=False, on_text=None):
        client = self.lifecycle_client if lifecycle else self.client
        try:
            async with client.stream(
                method, self.config.codex_bridge_url + path, json=payload,
                headers={"Authorization": "Bearer " + self.config.codex_bridge_token,
                         "Accept-Encoding": "identity",
                         "Accept": "application/x-ndjson" if on_text else "application/json"}, follow_redirects=False,
            ) as response:
                status = response.status_code
                allowed = {204} if method == "DELETE" else {200}
                if status not in allowed:
                    code = response.headers.get(BRIDGE_ERROR_HEADER, "")
                    failure = BRIDGE_ERRORS.get(code)
                    if failure and status == failure[0]:
                        raise HTTPException(status, failure[1] + f" [codex:{code}]" + RETRY_WARNING)
                    if status in {401, 403}:
                        raise HTTPException(502, "Codex bridge service authentication failed. Recreate the updated app stack."
                                            " [codex:bridge_auth]" + RETRY_WARNING)
                    if status == 503:
                        raise HTTPException(503, "Codex bridge is unavailable or busy. [codex:bridge_unavailable]" + RETRY_WARNING)
                    if status == 504:
                        raise HTTPException(504, "Codex bridge request timed out. [codex:timeout]" + RETRY_WARNING)
                    if status == 429:
                        raise HTTPException(429, "Codex rate, allowance or capacity limit reached." + RETRY_WARNING)
                    if status in {400, 404, 422}:
                        raise HTTPException(422, "Codex rejected the request; check connection and model compatibility." + RETRY_WARNING)
                    if status == 409:
                        raise HTTPException(409, "Codex is not ready; check the account connection and generation setting." + RETRY_WARNING)
                    raise HTTPException(502, f"Codex bridge returned HTTP {status} without a recognized diagnosis."
                                        " [codex:bridge_http_error]" + RETRY_WARNING)
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    raise ValueError()
                size = response.headers.get("content-length")
                if size is not None and not 0 <= int(size) <= PROVIDER_LIMIT:
                    raise ValueError()
                if on_text and response.headers.get("content-type", "").split(";")[0] == "application/x-ndjson":
                    buffer = bytearray()
                    count = 0
                    async for chunk in response.aiter_bytes():
                        buffer.extend(chunk)
                        while b"\n" in buffer:
                            line, _, remainder = buffer.partition(b"\n")
                            buffer = bytearray(remainder)
                            if len(line) > 200000:
                                raise ValueError()
                            if not line:
                                continue
                            count += 1
                            if count > 4098:
                                raise ValueError()
                            event = json.loads(line)
                            if not isinstance(event, dict):
                                raise ValueError()
                            if event.get("type") == "text":
                                text = event.get("text")
                                if not isinstance(text, str) or len(text) > 16000:
                                    raise ValueError()
                                text.encode("utf-8")
                                on_text(text)
                            elif event.get("type") == "done":
                                result = event.get("response")
                                if not isinstance(result, dict):
                                    raise ValueError()
                                return result
                            elif event.get("type") == "error":
                                code = event.get("code")
                                failure = BRIDGE_ERRORS.get(code) if isinstance(code, str) else None
                                if failure:
                                    raise HTTPException(failure[0], failure[1] + f" [codex:{code}]" + RETRY_WARNING)
                                status = event.get("status")
                                labels = {
                                    409: "Codex is not ready; check the account connection.",
                                    422: "Codex rejected the request; check the selected model and input.",
                                    429: "Codex is busy or its usage limit was reached.",
                                    503: "Codex bridge is unavailable.",
                                    504: "Codex request timed out.",
                                }
                                if type(status) is int and status in labels:
                                    raise HTTPException(status, labels[status] + RETRY_WARNING)
                                raise HTTPException(502, "Codex response failed before completion." + RETRY_WARNING)
                            else:
                                raise ValueError()
                        if len(buffer) > 200000:
                            raise ValueError()
                    raise ValueError()
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(raw) + len(chunk) > PROVIDER_LIMIT:
                        raise ValueError()
                    raw.extend(chunk)
                if method == "DELETE":
                    return None
                data = json.loads(raw)
                if not isinstance(data, dict):
                    raise ValueError()
                return data
        except httpx.TimeoutException:
            raise HTTPException(504, "Codex request timed out." + RETRY_WARNING) from None
        except httpx.RequestError:
            raise HTTPException(503, "Codex bridge is unavailable." + RETRY_WARNING) from None
        except (ValueError, RecursionError):
            raise HTTPException(502, "Invalid Codex bridge response." + RETRY_WARNING) from None

    async def fetch_status(self, parent, session, login=False):
        try:
            data = await self.fetch("POST" if login else "GET",
                                    f"/sessions/{session.identity}/{'login' if login else 'status'}",
                                    {} if login else None)
            self.check(parent, session)
            state, url, code, generation = (data[name] for name in (
                "state", "verification_url", "user_code", "generation_enabled"))
            if (not isinstance(state, str) or state not in {"disconnected", "pending", "connected", "failed"}
                    or type(generation) is not bool):
                raise ValueError()
            if url is not None or code is not None:
                if (state != "pending" or url != DEVICE_URL or not isinstance(code, str)
                        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,31}", code)):
                    raise ValueError()
            result = {"enabled": True, "state": state, "verification_url": url,
                      "user_code": code, "generation_enabled": generation}
        except (ValueError, KeyError, TypeError):
            session.status = disconnected()
            session.results.clear()
            session.models = None
            raise HTTPException(502, "Invalid Codex login status") from None
        except HTTPException:
            session.status = disconnected()
            session.results.clear()
            session.models = None
            raise
        session.status = result
        if state != "connected" or not generation:
            session.results.clear()
            session.models = None
        return result

    @staticmethod
    def generation_ready(session):
        if session.status["state"] != "connected":
            raise HTTPException(409, "Connect a Codex account before using Research")
        if not session.status["generation_enabled"]:
            raise HTTPException(409, "Codex generation is not enabled for this prototype")

    async def run(self, parent, session, identity, fingerprint, work, *, cache=False):
        self.check(parent, session)
        if session.pending:
            previous_id, previous_fingerprint, task = session.pending
            if identity is None or previous_id != identity:
                raise HTTPException(429, "Codex already has an active request for this operator")
            if previous_fingerprint != fingerprint:
                raise HTTPException(409, "Request ID was already used with different content")
        else:
            if parent in self.tasks:
                raise HTTPException(429, "Codex is still cancelling this operator's previous request")
            if len(self.tasks) >= 2:
                raise HTTPException(429, "Codex is busy; at most two provider operations can run")

            async def execute():
                try:
                    self.check(parent, session)
                    async with asyncio.timeout(self.config.openai_timeout) as deadline:
                        result = await work()
                    if deadline.expired():
                        raise TimeoutError()
                    self.check(parent, session)
                    if cache:
                        session.results[identity] = (fingerprint, self.credentials.now() + RESULT_TTL, result)
                        while len(session.results) > 8:
                            session.results.popitem(last=False)
                        self.prune()
                    return result
                except (TimeoutError, HTTPException) as error:
                    self.check(parent, session)
                    if session.status["state"] in {"disconnected", "failed"}:
                        self.clear(parent)
                    if isinstance(error, TimeoutError):
                        raise HTTPException(504, "Codex request timed out." + RETRY_WARNING) from None
                    raise

            task = asyncio.create_task(execute())
            session.pending = (identity, fingerprint, task)
            self.tasks[parent] = task

            def finished(done):
                self.tasks.pop(parent, None)
                if session.pending and session.pending[2] is done:
                    session.pending = None
                if not done.cancelled():
                    done.exception()

            task.add_done_callback(finished)
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():
                raise
            self.check(parent, session)
            raise HTTPException(409, "Codex request was cancelled") from None
        self.check(parent, session)
        return result

    async def status(self, parent):
        self.check(parent)
        session = self.sessions.get(parent)
        if session is None or not session.started:
            return disconnected(self.enabled)
        result = await self.run(parent, session, None, None, lambda: self.fetch_status(parent, session))
        if result["state"] in {"disconnected", "failed"}:
            self.clear(parent)
        return result

    async def login(self, parent, session):
        async def work():
            session.started = True
            session.creation_ack = None
            result = await self.fetch_status(parent, session, login=True)
            session.creation_ack = object()
            return result

        result = await self.run(parent, session, "login", session.identity, work)
        if result["state"] in {"disconnected", "failed"}:
            self.clear(parent)
        return result

    async def fetch_models(self, parent, session):
        if session.models and session.models[0] > self.credentials.now():
            return session.models[1]
        data = await self.fetch("GET", f"/sessions/{session.identity}/models")
        self.check(parent, session)
        rows = data.get("models")
        if (not isinstance(rows, list) or len(rows) > 2000 or any(
                not isinstance(row, dict) or not isinstance(row.get("id"), str)
                or not MODEL_ID.fullmatch(row["id"]) or type(row.get("image")) is not bool for row in rows)):
            raise HTTPException(502, "Invalid Codex model list")
        models = {}
        for row in rows:
            if row["id"] in models and models[row["id"]] != row["image"]:
                raise HTTPException(502, "Invalid Codex model list")
            models[row["id"]] = row["image"]
        result = [{"id": identity, "image": image} for identity, image in sorted(models.items())]
        session.models = (self.credentials.now() + MODEL_TTL, result)
        return result

    async def models(self, parent, session):
        async def work():
            await self.fetch_status(parent, session)
            self.generation_ready(session)
            return {"models": await self.fetch_models(parent, session)}

        return await self.run(parent, session, None, None, work)

    async def chat(self, parent, session, data, on_text=None):
        self.check(parent, session)
        identity = str(data.request_id)
        fingerprint = digest(session.identity + data.model_dump_json())
        cached = session.results.get(identity)
        if cached:
            self.generation_ready(session)
            if cached[0] != fingerprint:
                raise HTTPException(409, "Request ID was already used with different content")
            return cached[2]

        async def work():
            await self.fetch_status(parent, session)
            self.generation_ready(session)
            models = await self.fetch_models(parent, session)
            selected = next((model for model in models if model["id"] == data.model), None)
            if selected is None:
                raise HTTPException(422, "Select a model from the available Codex model list")
            if not selected["image"] and any(message.images for message in data.messages):
                raise HTTPException(422, "The selected Codex model does not support images")
            response = await self.fetch("POST", f"/sessions/{session.identity}/chat", data.model_dump(mode="json"),
                                      **({"on_text": update} if on_text else {}))
            self.check(parent, session)
            try:
                text, incomplete, usage = (response[name] for name in ("text", "incomplete", "usage"))
                if (response["request_id"] != identity or response["model"] != data.model
                        or not isinstance(text, str) or not text.strip() or len(text.encode()) > TEXT_LIMIT
                        or type(incomplete) is not bool):
                    raise ValueError()
                if usage is not None:
                    fields = ("input_tokens", "output_tokens", "total_tokens")
                    if not isinstance(usage, dict) or any(
                            type(usage.get(name)) is not int or not 0 <= usage[name] <= 2**53 for name in fields):
                        raise ValueError()
                    usage = {name: usage[name] for name in fields}
                text = text.strip()
                return {"request_id": identity, "model": data.model, "text": text[:16000],
                        "incomplete": incomplete or len(text) > 16000, "usage": usage}
            except (ValueError, KeyError, TypeError):
                raise HTTPException(502, "Invalid or empty Codex response." + RETRY_WARNING) from None

        def update(text):
            self.check(parent, session)
            on_text(text)

        return await self.run(parent, session, identity, fingerprint, work, cache=True)


def register_codex_research_routes(app, operator, bearer, digest, body):
    @app.get("/api/research/codex/status", dependencies=[Depends(operator)])
    async def status(request: Request):
        return await app.state.codex_research.status(digest(bearer(request)))

    @app.post("/api/research/codex/login", dependencies=[Depends(operator)])
    async def login(request: Request):
        research = app.state.codex_research
        parent = digest(bearer(request))
        research.check(parent)
        if not research.enabled:
            raise HTTPException(503, "Codex account login is disabled")
        # Bound to live operator tokens, without reserving a bridge session for
        # unvalidated input. Even a DELETE with no session invalidates this guard.
        guard = research.login_guards.setdefault(parent, object())
        raw = await body(request, 65536, "application/json")
        try:
            Input.model_validate_json(raw)
        except ValidationError:
            raise HTTPException(422, "Codex login requires an empty JSON object") from None
        research.check(parent)
        if research.login_guards.get(parent) is not guard:
            raise HTTPException(409, "Codex connection changed; request discarded")
        session = research.session(parent, create=True)
        return await research.login(parent, session)

    @app.delete("/api/research/codex/connection", dependencies=[Depends(operator)])
    async def disconnect(request: Request):
        research = app.state.codex_research
        research.clear(digest(bearer(request)))
        return disconnected(research.enabled)

    @app.get("/api/research/codex/models", dependencies=[Depends(operator)])
    async def models(request: Request):
        research = app.state.codex_research
        parent = digest(bearer(request))
        return await research.models(parent, research.session(parent))

    @app.post("/api/research/codex/chat", dependencies=[Depends(operator)])
    async def chat(request: Request):
        research = app.state.codex_research
        parent = digest(bearer(request))
        session = research.session(parent)
        if research.chat_waiters.get(parent, 0) >= 2 or sum(research.chat_waiters.values()) >= 4:
            raise HTTPException(429, "Codex chat is busy; at most two requests per operator and four globally can wait")
        # Hold admission across validation and dedup waiting, even if the session
        # is replaced. Worker/provider slots separately survive waiter cancellation.
        research.chat_waiters[parent] = research.chat_waiters.get(parent, 0) + 1
        def release():
            research.chat_waiters[parent] -= 1
            if not research.chat_waiters[parent]:
                del research.chat_waiters[parent]

        streaming = False
        try:
            data = await research.validate_chat(parent, session, request, body)
            if "application/x-ndjson" in request.headers.get("accept", ""):
                response = stream_reply(lambda update: research.chat(parent, session, data, update), release)
                streaming = True
                return response
            return await research.chat(parent, session, data)
        finally:
            if not streaming:
                release()
