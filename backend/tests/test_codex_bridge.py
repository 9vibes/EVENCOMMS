"""No credentials, live provider traffic or Docker are used by these tests."""

import asyncio
import base64
from collections import deque
from concurrent.futures import ThreadPoolExecutor
import contextlib
import copy
import io
import json
import logging
from pathlib import Path
import shutil
import sys
import threading
from urllib.parse import parse_qs
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from PIL import Image
import httpx
import pytest

from codex_bridge.errors import ERROR_HEADER
from codex_bridge.generation import Generation
from codex_bridge.policy import (
    AUTH_RECOVERY_PHASES, INSTRUCTIONS, MODEL_PROOF_FIELDS, MODELS, PROVIDER, REQUIRED_PROOFS, VERSION,
    configuration, generation_allowed, history_items, thread_params, turn_params,
)
from codex_bridge.probe import verify_binary
from codex_bridge.rpc import ProtocolError, Runtime, WIRE_LIMIT, child_environment
from codex_bridge.relay import Budget, Relay, Rejected, UPSTREAM
from codex_bridge.service import Bridge, DEVICE_URL, create_app
from codex_bridge.validation import BODY_LIMIT, Chat


TOKEN = "ab" * 32
AUTH = {"Authorization": "Bearer " + TOKEN}
SID = "1" * 64
BASE = "/sessions/" + SID
DISCONNECTED = {"state": "disconnected", "verification_url": None, "user_code": None, "generation_enabled": False}


@pytest.fixture(autouse=True)
def isolated_bridge_token_environment(monkeypatch):
    monkeypatch.setenv("CODEX_BRIDGE_TOKEN", "")
    monkeypatch.setenv("CODEX_BRIDGE_TOKEN_FILE", "")


def skills_prelude():
    return {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "No host skills."}],
            "internal_chat_message_metadata_passthrough": {"content_item_kinds": ["host_skills.instructions"]}}


def jpeg(size=(2, 2)):
    output = io.BytesIO()
    Image.new("RGB", size).save(output, format="JPEG")
    return "data:image/jpeg;base64," + base64.b64encode(output.getvalue()).decode()


def chat(**changes):
    return {"request_id": str(uuid4()), "model": MODELS[0]["id"],
            "messages": [{"role": "user", "text": "Question"}], **changes}


def passing_report():
    report = {"version": VERSION, **dict.fromkeys(REQUIRED_PROOFS, True),
              "generation_enabled": True, "no_retries": False,
              "tool_upstream_requests": 1, "tool_upstream_non_401": 1, "tool_blocked_requests": 1,
              "model_proofs": {row["id"]: {**dict.fromkeys(MODEL_PROOF_FIELDS, True), "requests": 1} for row in MODELS},
              "auth_recovery_phases": {case: list(phases) for case, phases in AUTH_RECOVERY_PHASES.items()}}
    for case, phases in AUTH_RECOVERY_PHASES.items():
        report[case + "_requests"] = sum(p.startswith("responses:") for p in phases)
        report[case + "_refreshes"] = phases.count("oauth:refresh")
    return report


class MockRelay:
    def __init__(self):
        self.current = self.last = None

    def arm(self, model):
        self.current = self.last = Budget("http://127.0.0.1:1/fixture/" + str(uuid4()), "/fixture/responses", model, float("inf"))
        return self.current

    def bind(self, budget, thread_id):
        budget.thread_id = thread_id

    def disarm(self):
        if self.current:
            self.current.armed = False
            self.current.consumed = True

    async def finish(self, budget):
        self.disarm()
        self.current = None


class MockRuntime:
    def __init__(self, binary, on_event, **kwargs):
        self.event = on_event
        self.calls = []
        self.account = None
        self.identity = str(uuid4())
        self.closed = self.failed = False
        self.mode = "text"
        self.url = DEVICE_URL
        self.code = "ABCD-EFGH"
        self.release = asyncio.Event()
        self.started_turn = asyncio.Event()
        self.injected = []
        self.relay = MockRelay()

    def replay(self):
        if self.injected:
            for item in [skills_prelude(), *self.injected]:
                self.event("rawResponseItem/completed", {"threadId": self.thread, "turnId": "auto-compact-0", "item": item})
            self.injected = []

    def finish_turn(self):
        shared = {"threadId": self.thread, "turnId": self.turn}
        self.replay()
        self.event("turn/started", {"threadId": self.thread, "turn": {"id": self.turn}})
        self.event("item/completed", {**shared, "item": {"type": "agentMessage", "id": "answer", "text": "Answer", "phase": "final_answer"}})
        self.event("rawResponse/completed", shared)
        self.event("turn/completed", {"threadId": self.thread, "turn": {"id": self.turn, "status": "completed", "error": None}})

    async def start(self):
        self.calls.append(("initialize", {}))

    async def call(self, method, params=None, **kwargs):
        self.calls.append((method, params or {}))
        if method == "account/read":
            return {"account": self.account, "requiresOpenaiAuth": True}
        if method == "account/login/start":
            if self.mode == "login_error":
                raise RuntimeError("secret-oauth-provider-error")
            return {"type": "chatgptDeviceCode", "loginId": self.identity,
                    "verificationUrl": self.url, "userCode": self.code}
        if method == "thread/start":
            self.thread = str(uuid4())
            result = {"model": params["model"], "modelProvider": PROVIDER, "thread": {"id": self.thread, "ephemeral": True}}
            if self.mode == "provider_fallback":
                result["modelProvider"] = "openai"
            if self.mode == "model_fallback":
                result["model"] = "another-model"
            self.event("thread/started", {"thread": result["thread"]})
            return result
        if method == "thread/inject_items":
            self.injected = params["items"]
            if self.mode != "late_replay":
                self.replay()
            return {}
        if method == "turn/start":
            self.relay.current.attempts = self.relay.current.non_401 = 1
            self.relay.current.consumed = True
            self.turn = str(uuid4())
            shared = {"threadId": self.thread, "turnId": self.turn}
            if self.mode == "late_replay":
                # The RPC result reaches Generation.run before recording-context
                # notifications, reproducing the pinned binary's load race.
                def finish():
                    try:
                        self.finish_turn()
                    except Exception:
                        self.abort()
                        self.event("bridge/failed", {})

                asyncio.get_running_loop().call_soon(finish)
                return {"turn": {"id": self.turn}}
            self.event("turn/started", {"threadId": self.thread, "turn": {"id": self.turn}})
            self.started_turn.set()
            if self.mode == "hold":
                await self.release.wait()
                if self.failed:
                    raise ProtocolError()
            if self.mode == "auth_refreshed":
                self.event("account/updated", {"authMode": "chatgpt"})
            if self.mode == "tool":
                self.event("rawResponseItem/completed", {**shared, "item": {"type": "function_call", "name": "exec_command"}})
            if self.mode == "overflow":
                self.event("item/agentMessage/delta", {**shared, "delta": "x" * 16001})
            if self.mode == "error":
                self.event("error", {**shared, "willRetry": False,
                                     "error": {"message": "secret-provider-error", "codexErrorInfo": "other"}})
            self.event("item/completed", {**shared, "item": {"type": "agentMessage", "id": "answer", "text": "Answer", "phase": "final_answer"}})
            self.event("rawResponse/completed", shared)
            self.event("turn/completed", {"threadId": self.thread, "turn": {"id": self.turn, "status": "completed", "error": None}})
            return {"turn": {"id": self.turn}}
        return {}

    def abort(self):
        self.failed = True
        self.relay.disarm()
        self.release.set()

    async def close(self, *, logout=True):
        if not self.closed:
            if logout and not self.failed:
                await self.call("account/logout")
            self.failed = self.closed = True

    async def complete(self, account_type="chatgpt", success=True):
        self.account = {"type": account_type, "email": "private@example.invalid", "id": "private-account-id"}
        self.event("account/updated", {"authMode": "chatgpt"})
        self.event("account/login/completed", {"loginId": self.identity, "success": success, "error": "secret-provider-error"})
        await asyncio.sleep(0)


@pytest.fixture
def bridge_client(tmp_path):
    runtimes = []
    now = [100.0]
    probe_result = passing_report()

    def factory(*args, **kwargs):
        runtime = MockRuntime(*args, **kwargs)
        runtimes.append(runtime)
        return runtime

    async def probe(*args, **kwargs):
        return probe_result

    app = create_app(token=TOKEN, runtime_factory=factory, probe=probe, _verify_binary=lambda binary: None,
                     temp_parent=str(tmp_path), clock=lambda: now[0])
    with TestClient(app) as client:
        client.headers.update(AUTH)
        client.runtimes = runtimes
        client.now = now
        yield client
    assert all(runtime.closed for runtime in runtimes)
    assert not app.state.bridge.sessions
    assert not app.state.bridge.validations


def connect(client, sid=SID):
    base = "/sessions/" + sid
    response = client.post(base + "/login", json={})
    assert response.status_code == 200
    runtime = client.runtimes[-1]
    client.portal.call(runtime.complete)
    assert client.get(base + "/status").json()["state"] == "connected"
    return runtime


@pytest.mark.parametrize("token", ["", "x" * 64, "a" * 31, "a" * 257, "token secret"])
def test_token_required_at_startup(token):
    with pytest.raises(RuntimeError, match="hexadecimal"):
        with TestClient(create_app(token=token)):
            pass


@pytest.mark.parametrize("token", ["AB" * 16, "ab" * 128])
def test_token_accepts_inclusive_backend_bounds(tmp_path, token):
    async def probe(*args, **kwargs):
        return passing_report()

    app = create_app(token=token, runtime_factory=MockRuntime, probe=probe, temp_parent=str(tmp_path),
                     _verify_binary=lambda binary: None)
    with TestClient(app, headers={"Authorization": "Bearer " + token}) as client:
        assert client.get(BASE + "/status").status_code == 200


def test_bridge_reads_file_at_startup_only_and_keeps_cached_credential(tmp_path, monkeypatch, caplog):
    path = tmp_path / "token"
    monkeypatch.setenv("CODEX_BRIDGE_TOKEN_FILE", str(path))
    probes = []

    async def probe(*args, **kwargs):
        probes.append(True)
        return passing_report()

    # Construction must not read the file; it may be provisioned before startup.
    app = create_app(probe=probe, runtime_factory=MockRuntime, temp_parent=str(tmp_path),
                     _verify_binary=lambda binary: None)
    path.write_text(TOKEN + "\n")
    path.chmod(0o440)
    with TestClient(app, headers=AUTH) as client:
        path.unlink()
        monkeypatch.setattr("codex_bridge.service.load_token_file", lambda *args: pytest.fail("No request-time reread"))
        for _ in range(2):
            assert client.get("/ready").json() == {
                "binary_verified": True, "generation_enabled": True, "active_sessions": 0,
            }
            assert client.get(BASE + "/status").json() == DISCONNECTED
        assert client.get("/ready", headers={"Authorization": "Bearer " + "cd" * 32}).status_code == 401
        assert client.post(BASE + "/login").json()["state"] == "pending"
        assert client.get("/ready").json()["active_sessions"] == 1
    assert probes == [True] and TOKEN not in caplog.text


def test_bridge_standalone_direct_token_does_not_read_files(monkeypatch):
    monkeypatch.setenv("CODEX_BRIDGE_TOKEN", TOKEN)
    monkeypatch.setattr("codex_bridge.service.load_token_file", lambda *args: pytest.fail("No file configured"))

    async def probe(*args, **kwargs):
        return passing_report()

    with TestClient(create_app(probe=probe, _verify_binary=lambda binary: None), headers=AUTH) as client:
        assert client.get("/ready").json()["generation_enabled"] is True


@pytest.mark.parametrize("explicit", [False, True])
def test_bridge_rejects_both_sources_before_read_or_probe(monkeypatch, explicit):
    monkeypatch.setenv("CODEX_BRIDGE_TOKEN", TOKEN)
    monkeypatch.setenv("CODEX_BRIDGE_TOKEN_FILE", "/private/not-read")
    monkeypatch.setattr("codex_bridge.service.load_token_file", lambda *args: pytest.fail("No ambiguous reads"))

    async def forbidden(*args, **kwargs):
        pytest.fail("Invalid credentials must fail before probe")

    options = {"token": TOKEN, "token_file": "/private/not-read"} if explicit else {}
    with pytest.raises(RuntimeError, match="Configure only one") as error:
        with TestClient(create_app(probe=forbidden, **options)):
            pass
    assert TOKEN not in str(error.value) and "/private/not-read" not in str(error.value)


@pytest.mark.parametrize("kind", ["missing", "invalid", "insecure"])
def test_bridge_token_file_failures_are_generic_at_startup(tmp_path, monkeypatch, kind):
    path = tmp_path / "private-token-path"
    if kind != "missing":
        path.write_text(TOKEN if kind == "insecure" else "private invalid content")
        path.chmod(0o444 if kind == "insecure" else 0o440)
    monkeypatch.setenv("CODEX_BRIDGE_TOKEN_FILE", str(path))

    async def forbidden(*args, **kwargs):
        pytest.fail("Invalid credentials must fail before probe")

    with pytest.raises(RuntimeError, match="^Invalid CODEX_BRIDGE_TOKEN_FILE$"):
        with TestClient(create_app(probe=forbidden)):
            pass


def test_auth_health_origins_and_private_surface(bridge_client):
    client = bridge_client
    client.headers.pop("Authorization")
    assert client.get("/health").json() == {"ok": True}
    for path in ("/ready", BASE + "/status", "/docs", "/openapi.json"):
        assert client.get(path).status_code == 401
        assert client.get(path, headers={"Authorization": "Bearer " + "00" * 32}).status_code == 401
    assert client.get(BASE + "/status", headers=AUTH).json()["state"] == "disconnected"
    for path in ("/health", "/ready", BASE + "/status"):
        response = client.get(path, headers={**AUTH, "Origin": "null"})
        assert response.status_code == 403
        assert response.headers["cache-control"] == "no-store"
    assert client.get("/docs", headers=AUTH).status_code == 404
    assert client.post("/rpc", headers=AUTH, json={"method": "command/exec"}).status_code == 404
    assert client.get("/sessions/operator-token/status", headers=AUTH).status_code == 422
    assert not client.runtimes


@pytest.mark.parametrize("fault", [None, "generation_enabled", "proof", "binary_verified", "exception", "timeout", "malformed"])
def test_ready_reports_only_consumed_startup_gate(tmp_path, monkeypatch, fault):
    monkeypatch.setattr("codex_bridge.service.PROBE_SECONDS", 0.03)
    report = passing_report()
    report.update(email="private@example.invalid", credential=TOKEN, blocker="private probe diagnostics")
    if fault in {"generation_enabled", "binary_verified"}:
        report[fault] = False
    elif fault == "proof":
        report["tool_continuation_guarded"] = False
    probes = []

    def verifier(binary):
        probes.append("binary")

    async def probe(*args, **kwargs):
        probes.append("generation")
        if fault == "exception":
            raise RuntimeError("private probe diagnostics " + TOKEN)
        if fault == "timeout":
            await asyncio.Event().wait()
        if fault == "malformed":
            return None
        return report

    app = create_app(token=TOKEN, runtime_factory=MockRuntime, probe=probe, temp_parent=str(tmp_path),
                     _verify_binary=verifier)
    with TestClient(app) as client:
        health = client.get("/health")
        assert health.status_code == 200 and health.json() == {"ok": True}
        assert client.get("/ready").status_code == 401
        response = client.get("/ready", headers=AUTH)
        expected = {"binary_verified": True, "generation_enabled": fault is None, "active_sessions": 0}
        assert response.status_code == 200 and response.json() == expected
        assert response.headers["cache-control"] == "no-store"
        assert TOKEN not in response.text and "private" not in response.text
        assert not app.state.bridge.sessions
        # The endpoint must use the gate consumed at startup, not a fresh probe/report.
        report.clear()
        assert client.get("/ready", headers=AUTH).json() == expected
        result = client.post(BASE + "/login", headers=AUTH)
        assert result.status_code == 200 and result.json()["state"] == "pending"
        session = app.state.bridge.sessions[SID]
        client.portal.call(session.runtime.complete)
        assert client.get(BASE + "/status", headers=AUTH).json()["state"] == "connected"
        assert client.get("/ready", headers=AUTH).json() == {**expected, "active_sessions": 1}
        assert client.post(BASE + "/chat", headers=AUTH, json=chat()).status_code == (200 if fault is None else 503)
        if fault is not None:
            assert not any(method == "thread/start" for method, _ in session.runtime.calls)
        client.delete(BASE, headers=AUTH)
        assert client.get("/ready", headers=AUTH).json() == expected
        assert probes == ["binary", "generation"]


@pytest.mark.parametrize("missing", [False, True])
def test_startup_rejects_unpinned_or_missing_binary_before_probe_or_runtime(tmp_path, missing):
    async def probe(*args, **kwargs):
        pytest.fail("An unverified binary must not run even the generation probe")

    def factory(*args, **kwargs):
        pytest.fail("An unverified binary must not create a runtime")

    app = create_app(token=TOKEN, binary=str(tmp_path / "missing") if missing else __file__,
                     probe=probe, runtime_factory=factory, temp_parent=str(tmp_path))
    with TestClient(app, headers=AUTH) as client:
        assert client.get("/ready").json() == {
            "binary_verified": False, "generation_enabled": False, "active_sessions": 0,
        }
        assert client.post(BASE + "/login").status_code == 503
        assert not app.state.bridge.sessions


@pytest.mark.parametrize("times_out", [False, True])
def test_startup_binary_verification_is_off_loop_bounded_and_cannot_enable_late(tmp_path, monkeypatch, times_out):
    monkeypatch.setattr("codex_bridge.service.BINARY_VERIFY_SECONDS", 0.1)

    async def run():
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()
        release, finished = threading.Event(), threading.Event()
        threads, probes = [], []

        def verifier(binary):
            threads.append(threading.get_ident())
            loop.call_soon_threadsafe(entered.set)
            try:
                assert release.wait(2)
            finally:
                finished.set()

        async def probe(*args, **kwargs):
            assert finished.is_set()
            probes.append(True)
            return passing_report()

        def factory(*args, **kwargs):
            pytest.fail("No runtime should be created")

        app = create_app(token=TOKEN, runtime_factory=factory, probe=probe, _verify_binary=verifier,
                         temp_parent=str(tmp_path))
        expected = {"binary_verified": not times_out, "generation_enabled": not times_out, "active_sessions": 0}

        async def startup():
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://bridge", headers=AUTH) as client:
                    assert (await client.get("/ready")).json() == expected
                    if times_out:
                        assert not release.is_set()
                        assert (await client.post(BASE + "/login")).status_code == 503
                    release.set()
                    assert await asyncio.to_thread(finished.wait, 1)
                    assert (await client.get("/ready")).json() == expected

        task = asyncio.create_task(startup())
        try:
            async with asyncio.timeout(1):
                await entered.wait()
                assert len(threads) == 1 and threads[0] != threading.get_ident()
                if not times_out:
                    release.set()
                await task
            assert probes == ([] if times_out else [True])
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


def test_login_idempotent_readiness_and_account_redaction(bridge_client):
    client = bridge_client
    pending = client.post(BASE + "/login", json={}).json()
    assert pending == {"state": "pending", "verification_url": DEVICE_URL, "user_code": "ABCD-EFGH", "generation_enabled": False}
    runtime = client.runtimes[0]
    assert client.post(BASE + "/login").json() == pending
    assert len(client.runtimes) == 1
    assert [method for method, _ in runtime.calls].count("account/login/start") == 1

    async def premature():
        runtime.account = {"type": "chatgpt"}
        runtime.event("account/updated", {"authMode": "chatgpt"})

    client.portal.call(premature)
    assert client.get(BASE + "/status").json() == pending
    assert client.get(BASE + "/models").status_code == 409
    client.portal.call(runtime.complete)
    status = client.get(BASE + "/status")
    assert status.json() == {"state": "connected", "verification_url": None, "user_code": None, "generation_enabled": True}
    assert "private" not in status.text
    assert client.post(BASE + "/login").json() == status.json()
    assert client.get(BASE + "/models").json() == {"models": MODELS}
    assert not any(method.startswith(("thread/", "turn/")) for method, _ in runtime.calls)


@pytest.mark.parametrize("account_type,success", [("apiKey", True), ("chatgptAuthTokens", True), ("chatgpt", False)])
def test_failed_or_api_login_is_never_connected(bridge_client, account_type, success):
    client = bridge_client
    client.post(BASE + "/login")
    runtime = client.runtimes[0]
    client.portal.call(runtime.complete, account_type, success)
    response = client.get(BASE + "/status")
    assert response.json() == DISCONNECTED
    assert "secret" not in response.text
    assert runtime.closed
    assert client.post(BASE + "/chat", json=chat()).status_code == 409


@pytest.mark.parametrize("field,value", [("url", "https://evil.invalid/codex/device"), ("url", DEVICE_URL + "?next=evil"),
                                        ("url", DEVICE_URL + "/"), ("url", "http://auth.openai.com/codex/device"),
                                        ("code", "<script>evil</script>"), ("code", "A" * 33), ("code", ""),
                                        ("code", "-ABC"), ("code", "CODE\n"), ("code", "cod\u00e9"), ("mode", "login_error")])
def test_bad_device_results_fail_closed(bridge_client, field, value):
    client = bridge_client
    original = client.app.state.bridge.runtime_factory

    def factory(*args, **kwargs):
        runtime = original(*args, **kwargs)
        setattr(runtime, field, value)
        return runtime

    client.app.state.bridge.runtime_factory = factory
    response = client.post(BASE + "/login")
    assert response.json()["state"] == "failed"
    assert response.json()["user_code"] is None
    assert "secret" not in response.text and "evil" not in response.text
    assert client.runtimes[0].closed


@pytest.mark.parametrize("code", ["123456789", "cOdE-12345", "Z", "a" * 32, "AB-cd-"])
def test_device_codes_are_bounded_case_preserving_and_match_backend(bridge_client, code):
    client = bridge_client
    original = client.app.state.bridge.runtime_factory

    def factory(*args, **kwargs):
        runtime = original(*args, **kwargs)
        runtime.code = code
        return runtime

    client.app.state.bridge.runtime_factory = factory
    result = client.post(BASE + "/login").json()
    assert result["state"] == "pending" and result["user_code"] == code
    assert result["verification_url"] == "https://auth.openai.com/codex/device"


@pytest.mark.parametrize("fault", ["mismatch", "invalid_uuid", "duplicate", "expired", "incompatible_success", "unknown_event"])
def test_invalid_or_stale_login_completions_fail_closed(bridge_client, fault):
    client = bridge_client
    client.post(BASE + "/login")
    session = client.app.state.bridge.sessions[SID]
    runtime = client.runtimes[0]
    params = {"loginId": runtime.identity, "success": True}
    if fault == "expired":
        client.now[0] += 901
        assert client.get(BASE + "/status").json() == DISCONNECTED
    elif fault == "mismatch":
        params["loginId"] = str(uuid4())
    elif fault == "invalid_uuid":
        params["loginId"] = "not-a-uuid"
    elif fault == "incompatible_success":
        params["success"] = 1

    async def notify():
        if fault == "duplicate":
            runtime.event("account/login/completed", params)
        with pytest.raises(ProtocolError):
            runtime.event("unknown/login/event" if fault == "unknown_event" else "account/login/completed", params)
        assert session.status() == {**DISCONNECTED, "state": "failed"}

    client.portal.call(notify)
    assert client.get(BASE + "/status").json() == DISCONNECTED
    assert runtime.failed and runtime.closed


def test_offline_pinned_binary_device_login_uses_production_session(tmp_path, monkeypatch):
    binary = shutil.which("codex")
    if binary is None:
        pytest.skip("Put the pinned native Codex binary on PATH to run the offline integration")

    async def run():
        async with asyncio.timeout(10):
            await asyncio.to_thread(verify_binary, binary)
        approved, polled = asyncio.Event(), asyncio.Event()
        polls, exchanges, errors = [], [], []
        handlers = set()

        async def respond(reader, writer):
            handlers.add(asyncio.current_task())
            try:
                async with asyncio.timeout(5):
                    lines = (await reader.readuntil(b"\r\n\r\n")).decode("ascii").split("\r\n")
                    headers = {key.lower(): value.strip() for key, value in
                               (line.split(":", 1) for line in lines[1:] if ":" in line)}
                    size = int(headers.get("content-length", "0"))
                    assert 0 <= size <= 65536
                    raw = await reader.readexactly(size)
                    method, path, _ = lines[0].split(" ")
                    path = path.split("?", 1)[0]
                    status, value = b"200 OK", {}
                    if method == "POST" and path == "/api/accounts/deviceauth/usercode":
                        value = {"device_auth_id": "offline-device", "user_code": "123456789", "interval": "1"}
                    elif method == "POST" and path == "/api/accounts/deviceauth/token":
                        assert json.loads(raw) == {"device_auth_id": "offline-device", "user_code": "123456789"}
                        if approved.is_set():
                            value = {"authorization_code": "offline-code", "code_challenge": "offline", "code_verifier": "offline"}
                        else:
                            status = b"403 Forbidden" if len(polls) % 2 == 0 else b"404 Not Found"
                            polls.append(int(status[:3]))
                            if len(polls) >= 3:
                                polled.set()
                    elif method == "POST" and path == "/oauth/token":
                        grant = (json.loads(raw) if headers.get("content-type", "").startswith("application/json")
                                 else {key: values[0] for key, values in parse_qs(raw.decode()).items()})
                        assert approved.is_set() and grant["grant_type"] == "authorization_code" and not exchanges
                        exchanges.append(True)
                        claims = {"email": "offline@example.invalid", "https://api.openai.com/auth": {
                            "chatgpt_user_id": "offline-user", "chatgpt_account_id": "offline-account", "chatgpt_plan_type": "pro"}}
                        token = "e30." + base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=") + ".offline"
                        value = {"id_token": token, "access_token": "offline-access", "refresh_token": "offline-refresh"}
                    elif method == "GET" and path.endswith("/accounts/check"):
                        value = {"accounts": [{"id": "offline-account", "workspace_backend_origin": origin.replace("http:", "https:"),
                                              "account_routing_override": "NO_CONSTRAINT"}]}
                    elif method == "POST" and path == "/oauth/revoke":
                        pass
                    elif method == "GET" and path.endswith(("/config/bundle", "/settings/user")):
                        pass
                    else:
                        raise AssertionError("Unexpected offline fixture request")
                    body = json.dumps(value).encode()
                    writer.write(b"HTTP/1.1 " + status + b"\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: "
                                 + str(len(body)).encode() + b"\r\n\r\n" + body)
                    await writer.drain()
            except (ConnectionError, asyncio.IncompleteReadError):
                pass
            except Exception:
                errors.append("fixture rejected request")
            finally:
                writer.close()
                with contextlib.suppress(Exception):
                    await writer.wait_closed()
                handlers.discard(asyncio.current_task())

        server = await asyncio.start_server(respond, "127.0.0.1", 0)
        origin = "http://127.0.0.1:" + str(server.sockets[0].getsockname()[1])
        # The native fixture issuer changes only the expected verification URL;
        # Session.login and its strict event callback are otherwise unmodified.
        monkeypatch.setattr("codex_bridge.service.DEVICE_URL", origin + "/codex/device")

        def factory(binary, on_event, **kwargs):
            return Runtime(binary, on_event, probe_origin=origin, probe_upstream=origin,
                           probe_config={"chatgpt_base_url": origin, "openai_base_url": origin}, **kwargs)

        bridge = Bridge(binary, runtime_factory=factory, temp_parent=str(tmp_path), binary_verified=True)
        try:
            async with asyncio.timeout(30):
                pending = await bridge.login(SID)
                assert pending == {"state": "pending", "verification_url": origin + "/codex/device",
                                   "user_code": "123456789", "generation_enabled": False}
                session = bridge.sessions[SID]
                runtime = session.runtime
                process = runtime.process
                directory = Path(runtime.directory.name)
                assert runtime.on_event == session.event
                assert str(UUID(session.login_id)) == session.login_id
                await polled.wait()
                assert polls[:3] == [403, 404, 403] and not exchanges
                assert not session.login_task.done() and not session.login_done.is_set()
                assert session.status() == pending and await bridge.login(SID) == pending
                assert not list(directory.rglob("auth.json"))
                approved.set()  # Synthetic provider approval, never a real account.
                await session.login_task
                assert session.status() == {**DISCONNECTED, "state": "connected"}
                assert session.login_id is None and session.completion is None
                assert runtime.process is process and process.returncode is None and not runtime.failed
                assert exchanges == [True] and not errors
                assert not list(directory.rglob("auth.json"))
        finally:
            await bridge.close()
            server.close()
            await server.wait_closed()
            for task in tuple(handlers):
                task.cancel()
            await asyncio.gather(*handlers, return_exceptions=True)
        assert process.returncode is not None and not directory.exists() and not runtime.pending
        assert not errors and not bridge.sessions

    asyncio.run(run())


def test_max_two_sessions_and_idempotent_privileged_delete(bridge_client):
    client = bridge_client
    for sid in (SID, "2" * 64):
        assert client.post(f"/sessions/{sid}/login").status_code == 200
    assert client.post(f"/sessions/{'3' * 64}/login").status_code == 429
    runtime = client.runtimes[0]
    assert client.delete(BASE).status_code == 204
    assert runtime.closed
    assert [method for method, _ in runtime.calls].count("account/logout") == 1
    assert client.delete(BASE).status_code == 204
    assert client.get(BASE + "/status").json()["state"] == "disconnected"
    assert len(client.runtimes) == 2
    assert client.post(f"/sessions/{'3' * 64}/login").status_code == 200


def test_lease_extends_only_existing_and_cannot_resurrect(bridge_client):
    client = bridge_client
    client.post(BASE + "/login")
    client.now[0] += 100
    response = client.post("/lease", json={"sessions": [SID, "2" * 64]})
    assert response.status_code == 200 and response.json() == {"sessions": [SID]}
    client.now[0] += 119
    assert client.get(BASE + "/status").json()["state"] == "pending"
    client.now[0] += 1
    response = client.post("/lease", json={"sessions": [SID]})
    assert response.status_code == 200 and response.json() == {"sessions": []}
    assert client.get(BASE + "/status").json()["state"] == "disconnected"
    assert len(client.runtimes) == 1 and client.runtimes[0].closed
    assert client.post("/lease", json={"sessions": [SID] * 3}).status_code == 422
    assert client.post("/lease", json={"sessions": [SID], "extra": True}).status_code == 422
    assert client.post("/lease", content=b"x" * 65537).status_code == 413


def test_two_failed_logins_do_not_reserve_slots_through_hidden_ui_leases(bridge_client):
    client = bridge_client
    identities = [SID, "2" * 64]
    for identity in identities:
        assert client.post(f"/sessions/{identity}/login").json()["state"] == "pending"
    for runtime in client.runtimes:
        client.portal.call(runtime.complete, "chatgpt", False)
    # Twenty minutes of backend heartbeats, deliberately without browser status
    # polls and even retaining stale IDs after their first missing acknowledgement.
    for _ in range(40):
        client.now[0] += 30
        response = client.post("/lease", json={"sessions": identities})
        assert response.status_code == 200 and response.json() == {"sessions": []}
        assert not client.app.state.bridge.sessions
    assert len(client.runtimes) == 2 and all(runtime.closed for runtime in client.runtimes)
    assert client.post(f"/sessions/{'3' * 64}/login").json()["state"] == "pending"


def test_cleanup_failure_alone_reserves_a_slot_but_is_not_renewed(bridge_client):
    client = bridge_client
    client.post(BASE + "/login")
    runtime = client.runtimes[0]
    session = client.app.state.bridge.sessions[SID]
    original_close = runtime.close
    broken = [True]
    attempts = []

    async def close(**kwargs):
        attempts.append(1)
        if broken[0]:
            raise OSError("private cleanup failure")
        await original_close(**kwargs)

    runtime.close = close
    healthy = "2" * 64
    connect(client, healthy)
    client.portal.call(runtime.complete, "chatgpt", False)
    original_lease = session.lease
    client.now[0] += 30
    response = client.post("/lease", json={"sessions": [SID, healthy]})
    assert response.status_code == 200 and response.json() == {"sessions": [healthy]}
    assert client.app.state.bridge.sessions[SID] is session and session.lease == original_lease
    assert client.post(f"/sessions/{'3' * 64}/login").status_code == 429
    broken[0] = False
    assert client.post("/lease", json={"sessions": [SID, healthy]}).json() == {"sessions": [healthy]}
    assert SID not in client.app.state.bridge.sessions and runtime.closed and len(attempts) >= 3
    assert client.post(f"/sessions/{'3' * 64}/login").status_code == 200


def test_sweeper_reclaims_failed_sessions_without_any_http_poll(bridge_client):
    client = bridge_client
    client.post(BASE + "/login")
    runtime = client.runtimes[0]
    client.portal.call(runtime.complete, "chatgpt", False)
    client.portal.call(asyncio.sleep, 1.1)
    assert runtime.closed and not client.app.state.bridge.sessions


def test_sweeper_expires_without_backend_requests(bridge_client):
    client = bridge_client
    client.post(BASE + "/login")
    client.now[0] += 121
    client.portal.call(asyncio.sleep, 1.1)
    assert not client.app.state.bridge.sessions
    assert client.runtimes[0].closed


@pytest.mark.parametrize("connected,deadline", [(False, 900), (True, 8 * 3600)])
def test_hard_deadlines_override_lease(bridge_client, connected, deadline):
    client = bridge_client
    if connected:
        connect(client)
    else:
        client.post(BASE + "/login")
    session = client.app.state.bridge.sessions[SID]
    session.lease = client.now[0] + deadline + 100
    client.now[0] += deadline
    assert client.get(BASE + "/status").json()["state"] == "disconnected"
    assert client.runtimes[0].closed


def test_probe_failure_keeps_login_but_disables_generation(bridge_client):
    client = bridge_client
    client.app.state.bridge.generation_enabled = False
    runtime = connect(client)
    assert client.get(BASE + "/status").json()["generation_enabled"] is False
    assert client.get(BASE + "/models").json() == {"models": MODELS}
    assert client.post(BASE + "/chat", json=chat()).status_code == 503
    assert not any(method == "thread/start" for method, _ in runtime.calls)


def test_favorable_tool_notification_timing_cannot_enable_unprotected_continuation(tmp_path):
    async def probe(*args, **kwargs):
        report = passing_report()
        report["tool_call_rejected"] = True
        report["tool_continuation_guarded"] = False
        return report

    app = create_app(token=TOKEN, runtime_factory=MockRuntime, probe=probe, temp_parent=str(tmp_path),
                     _verify_binary=lambda binary: None)
    with TestClient(app, headers=AUTH) as client:
        assert client.post(BASE + "/login").json()["state"] == "pending"
        session = app.state.bridge.sessions[SID]
        client.portal.call(session.runtime.complete)
        assert client.get(BASE + "/status").json()["state"] == "connected"
        assert client.get(BASE + "/status").json()["generation_enabled"] is False
        assert client.post(BASE + "/chat", json=chat()).status_code == 503


def test_actual_startup_gate_does_not_treat_tools_empty_as_sufficient(tmp_path):
    async def probe(*args, **kwargs):
        return {"binary_verified": True, "tools_empty": True, "authenticated_401_requests": 2,
                "generation_enabled": True, "blocker": "private probe diagnostics"}

    app = create_app(token=TOKEN, runtime_factory=MockRuntime, probe=probe, temp_parent=str(tmp_path),
                     _verify_binary=lambda binary: None)
    with TestClient(app, headers=AUTH) as client:
        assert client.post(BASE + "/login").json()["state"] == "pending"
        session = app.state.bridge.sessions[SID]
        client.portal.call(session.runtime.complete)
        response = client.get(BASE + "/status")
        assert response.json()["state"] == "connected"
        assert response.json()["generation_enabled"] is False
        assert "diagnostics" not in response.text
        assert client.post(BASE + "/chat", json=chat()).status_code == 503
        assert all(method != "thread/start" for method, _ in session.runtime.calls)


def test_startup_accepts_bounded_auth_recovery_without_claiming_no_retries(tmp_path):
    async def probe(*args, **kwargs):
        return passing_report()

    app = create_app(token=TOKEN, runtime_factory=MockRuntime, probe=probe, temp_parent=str(tmp_path),
                     _verify_binary=lambda binary: None)
    with TestClient(app, headers=AUTH) as client:
        client.post(BASE + "/login")
        session = app.state.bridge.sessions[SID]
        client.portal.call(session.runtime.complete)
        assert client.get(BASE + "/status").json()["generation_enabled"] is True
        assert client.post(BASE + "/chat", json=chat()).status_code == 200
    assert passing_report()["authenticated_401_requests"] == 2
    assert passing_report()["no_retries"] is False


@pytest.mark.parametrize("name", REQUIRED_PROOFS)
def test_every_safety_proof_is_required(name):
    report = passing_report()
    assert generation_allowed(report)
    for value in (False, None, 1, "true"):
        report[name] = value
        assert not generation_allowed(report)
    report.pop(name)
    assert not generation_allowed(report)


@pytest.mark.parametrize("model", [row["id"] for row in MODELS])
@pytest.mark.parametrize("fault", ["missing", *MODEL_PROOF_FIELDS, "requests"])
def test_each_exposed_selector_needs_a_successful_one_shot(model, fault):
    report = passing_report()
    if fault == "missing":
        del report["model_proofs"][model]
    elif fault == "requests":
        report["model_proofs"][model][fault] = 2
    else:
        report["model_proofs"][model][fault] = False
    assert not generation_allowed(report)


def test_model_proof_set_must_match_the_catalog_exactly():
    report = passing_report()
    report["model_proofs"]["unexposed-model"] = {**dict.fromkeys(MODEL_PROOF_FIELDS, True), "requests": 1}
    assert not generation_allowed(report)
    report.pop("model_proofs")
    assert not generation_allowed(report)


@pytest.mark.parametrize("case", AUTH_RECOVERY_PHASES)
def test_auth_recovery_gate_rejects_extra_attempts_refreshes_and_wrong_phases(case):
    report = passing_report()
    report[case + "_requests"] += 1
    assert not generation_allowed(report)
    report = passing_report()
    report[case + "_refreshes"] += 1
    assert not generation_allowed(report)
    report = passing_report()
    report["auth_recovery_phases"][case].append("responses:refreshed")
    assert not generation_allowed(report)
    report = passing_report()
    report["auth_recovery_phases"][case][0] = "responses:refreshed"
    assert not generation_allowed(report)
    report = passing_report()
    report["auth_recovery_phases"].pop(case)
    assert not generation_allowed(report)


def test_unverified_binary_prevents_even_login(bridge_client):
    client = bridge_client
    client.app.state.bridge.binary_verified = False
    assert client.post(BASE + "/login").status_code == 503
    assert not client.runtimes
    with pytest.raises(ProtocolError):
        verify_binary(__file__)


@pytest.mark.parametrize("model", [row["id"] for row in MODELS])
def test_chat_history_fresh_threads_and_request_id_cache(bridge_client, model):
    client = bridge_client
    runtime = connect(client)
    data = chat(model=model, messages=[{"role": "user", "text": " question \n", "images": [jpeg()]},
                          {"role": "assistant", "text": " displayed answer \n"},
                          {"role": "user", "text": " final ", "images": [jpeg()]}])
    response = client.post(BASE + "/chat", json=data)
    assert response.status_code == 200, response.text
    assert response.json() == {"request_id": data["request_id"], "model": data["model"], "text": "Answer", "incomplete": False, "usage": None}
    assert client.post(BASE + "/chat", json=data).json() == response.json()
    calls = dict(runtime.calls)
    assert calls["thread/start"] == {**thread_params(data["model"]),
                                     "config": {"model_providers.evencomms.base_url": runtime.relay.last.base_url}}
    validated = Chat.model_validate(data)
    assert calls["thread/inject_items"]["items"] == history_items(validated.messages[:-1])
    assert calls["turn/start"] == turn_params(runtime.thread, validated.messages[-1])
    assert [m for m, _ in runtime.calls].count("thread/start") == 1
    data["messages"][-1]["text"] = "different"
    assert client.post(BASE + "/chat", json=data).status_code == 409
    before = runtime.thread
    assert client.post(BASE + "/chat", json=chat()).status_code == 200
    assert runtime.thread != before
    assert not any(m in {"thread/resume", "thread/delete", "thread/unsubscribe"} for m, _ in runtime.calls)


def test_service_accepts_replay_echoes_delivered_after_turn_start_reply(bridge_client):
    client = bridge_client
    runtime = connect(client)
    runtime.mode = "late_replay"
    response = client.post(BASE + "/chat", json=chat(messages=[
        {"role": "user", "text": "Displayed question", "images": [jpeg()]},
        {"role": "assistant", "text": "Displayed old answer"},
        {"role": "user", "text": "New explicit request"},
    ]))
    assert response.status_code == 200 and response.json()["text"] == "Answer"
    assert not runtime.closed
    assert client.get(BASE + "/status").json()["state"] == "connected"
    assert [method for method, _ in runtime.calls].count("turn/start") == 1


@pytest.mark.parametrize("reply_after", [0, 1, 2, 3])
def test_recording_context_echo_order_is_independent_of_turn_start_reply(reply_after):
    async def run():
        data = Chat.model_validate(chat(messages=[
            {"role": "user", "text": "Question", "images": [jpeg()]},
            {"role": "assistant", "text": "Past answer"},
            {"role": "user", "text": "New question"},
        ]))
        generation = Generation()
        generation.thread_id = "fresh-thread"
        items = history_items(data.messages[:-1])
        generation.replay = deque(items)
        for index, item in enumerate([skills_prelude(), *items]):
            if index == reply_after:
                generation.turn_id = "new-turn"  # The RPC reply, not a notification.
            generation.event("rawResponseItem/completed", {
                "threadId": "fresh-thread", "turnId": "auto-compact-0", "item": item,
            })
        assert not generation.replay and not generation.texts
        generation.event("turn/started", {"threadId": "fresh-thread", "turn": {"id": "new-turn"}})
        generation.event("item/completed", {"threadId": "fresh-thread", "turnId": "new-turn",
                                             "item": {"type": "agentMessage", "id": "new-answer", "text": "New answer"}})
        generation.event("rawResponse/completed", {"threadId": "fresh-thread", "turnId": "new-turn"})
        generation.event("turn/completed", {"threadId": "fresh-thread", "turn": {"id": "new-turn", "status": "completed"}})
        assert generation.done.result()["text"] == "New answer"

    asyncio.run(run())


@pytest.mark.parametrize("fault", [
    "function_call", "custom_tool_call", "local_shell_call", "reasoning", "changed_text", "changed_image",
    "changed_role", "foreign_thread", "unknown_recording_context", "stale_turn", "no_injection", "extra_echo",
    "late_prelude", "duplicate_prelude", "unknown_prelude_kind", "oversized_prelude", "tool_in_prelude",
])
@pytest.mark.parametrize("turn_known", [False, True])
def test_recording_context_exception_never_allows_unrequested_or_unsafe_items(fault, turn_known):
    async def run():
        data = Chat.model_validate(chat(messages=[
            {"role": "user", "text": "Question", "images": [jpeg()]},
            {"role": "user", "text": "New question"},
        ]))
        generation = Generation()
        generation.thread_id = "fresh-thread"
        generation.turn_id = "new-turn" if turn_known else None
        items = history_items(data.messages[:-1])
        generation.replay = deque(items)
        params = {"threadId": "fresh-thread", "turnId": "auto-compact-0", "item": copy.deepcopy(items[0])}
        if fault in {"function_call", "custom_tool_call", "local_shell_call", "reasoning"}:
            params["item"] = {"type": fault, "name": "exec_command"}
        elif fault == "changed_text":
            params["item"]["content"][0]["text"] = "Unrequested answer"
        elif fault == "changed_image":
            params["item"]["content"][1]["image_url"] = "different image"
        elif fault == "changed_role":
            params["item"]["role"] = "assistant"
        elif fault == "foreign_thread":
            params["threadId"] = "other-thread"
        elif fault == "unknown_recording_context":
            params["turnId"] = "auto-compact-1"
        elif fault == "stale_turn":
            params["turnId"] = "old-turn"
        elif fault == "no_injection":
            generation.replay = None
        elif fault == "extra_echo":
            generation.event("rawResponseItem/completed", params)
        else:
            if fault == "late_prelude":
                generation.event("rawResponseItem/completed", params)
            params["item"] = skills_prelude()
            if fault == "duplicate_prelude":
                generation.event("rawResponseItem/completed", params)
            elif fault == "unknown_prelude_kind":
                params["item"]["internal_chat_message_metadata_passthrough"]["content_item_kinds"] = ["unknown"]
            elif fault == "oversized_prelude":
                params["item"]["content"][0]["text"] = "x" * 64001
            elif fault == "tool_in_prelude":
                params["item"]["content"][0] = {"type": "function_call", "name": "exec_command"}
        try:
            with pytest.raises(ProtocolError):
                generation.event("rawResponseItem/completed", params)
            assert not generation.texts
        finally:
            generation.done.cancel()

    asyncio.run(run())


@pytest.mark.parametrize("change", [
    {"extra": True}, {"request_id": "not-a-uuid"}, {"model": "not-in-pinned-catalog"},
    {"messages": [{"role": "system", "text": "override"}]},
    {"messages": [{"role": "user", "text": "x" * 8001}]},
    {"messages": [{"role": "user", "text": "x"}] * 21},
    {"messages": [{"role": "user", "images": ["https://evil.invalid/image.jpg"]}]},
    {"messages": [{"role": "user", "images": [jpeg((1281, 1))]}]},
    {"messages": [{"role": "user", "images": [jpeg()] * 4}]},
    {"messages": [{"role": "user", "images": [jpeg()] * 3}] * 3},
    {"messages": [{"role": "assistant", "text": "x", "images": [jpeg()]}, {"role": "user", "text": "next"}]},
])
def test_chat_validation_before_any_inference(bridge_client, change):
    client = bridge_client
    runtime = connect(client)
    response = client.post(BASE + "/chat", json=chat(**change))
    assert response.status_code == 422
    assert "evil" not in response.text
    assert not any(method == "thread/start" for method, _ in runtime.calls)


def test_http_body_and_jpeg_byte_limits(bridge_client):
    client = bridge_client
    runtime = connect(client)
    assert client.post(BASE + "/chat", content=b"x" * (BODY_LIMIT + 1)).status_code == 413
    assert client.post(BASE + "/chat", content=iter([b"x" * (5 * 1024 * 1024)] * 2)).status_code == 413
    assert client.post(BASE + "/chat", json=chat(), headers={"Content-Encoding": "gzip"}).status_code == 415
    big = "data:image/jpeg;base64," + base64.b64encode(b"x" * (1024 * 1024 + 1)).decode()
    assert client.post(BASE + "/chat", json=chat(messages=[{"role": "user", "images": [big]}])).status_code == 422
    assert not any(method == "thread/start" for method, _ in runtime.calls)


@pytest.mark.parametrize("mode", ["tool", "overflow", "error"])
def test_unsafe_events_destroy_session_without_fallback(bridge_client, mode):
    client = bridge_client
    runtime = connect(client)
    runtime.mode = mode
    response = client.post(BASE + "/chat", json=chat())
    assert response.status_code == 502
    assert response.headers[ERROR_HEADER] == {"tool": "tool_rejected", "overflow": "protocol_mismatch",
                                              "error": "runtime_error"}[mode]
    assert "secret" not in response.text
    assert "does not automatically resubmit" in response.text
    assert "Codex may retry during bounded OAuth credential recovery" in response.text
    assert "No paid API fallback" in response.text
    assert runtime.closed
    assert client.get(BASE + "/status").json()["generation_enabled"] is False
    assert [m for m, _ in runtime.calls].count("turn/start") == 1
    assert client.post(BASE + "/chat", json=chat()).status_code == 409
    assert [m for m, _ in runtime.calls].count("turn/start") == 1


@pytest.mark.parametrize("mode", ["provider_fallback", "model_fallback"])
def test_runtime_cannot_switch_provider_or_model(bridge_client, mode):
    client = bridge_client
    runtime = connect(client)
    runtime.mode = mode
    response = client.post(BASE + "/chat", json=chat())
    assert response.status_code == (422 if mode == "model_fallback" else 502)
    assert response.headers[ERROR_HEADER] == ("model_changed" if mode == "model_fallback" else "protocol_mismatch")
    assert runtime.closed
    assert not any(method == "turn/start" for method, _ in runtime.calls)


def test_chatgpt_refresh_update_preserves_ready_session(bridge_client):
    client = bridge_client
    runtime = connect(client)
    runtime.mode = "auth_refreshed"
    assert client.post(BASE + "/chat", json=chat()).status_code == 200
    assert client.get(BASE + "/status").json()["state"] == "connected"
    assert [m for m, _ in runtime.calls].count("turn/start") == 1


@pytest.mark.parametrize("auth_mode", [None, "apiKey", "chatgptAuthTokens", "amazonBedrock", "unexpected"])
def test_non_chatgpt_account_update_aborts_active_generation(bridge_client, auth_mode):
    client = bridge_client
    runtime = connect(client)
    runtime.mode = "hold"

    async def revoke():
        with pytest.raises(ProtocolError):
            runtime.event("account/updated", {"authMode": auth_mode})

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(client.post, BASE + "/chat", json=chat())
        client.portal.call(runtime.started_turn.wait)
        client.portal.call(revoke)
        assert pending.result(timeout=2).status_code == 502
    assert runtime.closed
    assert client.get(BASE + "/status").json() == DISCONNECTED
    assert [m for m, _ in runtime.calls].count("turn/start") == 1


def test_timeout_kills_without_application_resubmission(bridge_client, monkeypatch):
    monkeypatch.setattr("codex_bridge.service.CHAT_SECONDS", 0.03)
    client = bridge_client
    runtime = connect(client)
    runtime.mode = "hold"
    assert client.post(BASE + "/chat", json=chat()).status_code == 504
    assert runtime.closed
    assert [m for m, _ in runtime.calls].count("turn/start") == 1


def test_one_generation_and_delete_cancels_immediately(bridge_client):
    client = bridge_client
    runtime = connect(client)
    runtime.mode = "hold"
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(client.post, BASE + "/chat", json=chat())
        client.portal.call(runtime.started_turn.wait)
        assert client.post(BASE + "/chat", json=chat()).status_code == 429
        assert client.delete(BASE).status_code == 204
        assert pending.result(timeout=2).status_code == 409
    assert runtime.closed
    assert not client.app.state.bridge.sessions


def test_completed_worker_keeps_admission_until_result_handling(bridge_client):
    client = bridge_client
    runtime = connect(client)
    session = client.app.state.bridge.sessions[SID]

    async def finished_worker():
        session.active = asyncio.create_task(asyncio.sleep(0))
        await session.active

    client.portal.call(finished_worker)
    assert client.post(BASE + "/chat", json=chat()).status_code == 429
    assert not any(method == "thread/start" for method, _ in runtime.calls)


def test_cancelled_image_workers_keep_global_admission_slots(bridge_client, monkeypatch):
    client = bridge_client
    entered = [threading.Event(), threading.Event()]
    release = threading.Event()
    calls = []
    original = Chat.model_validate_json

    def blocked(raw):
        index = len(calls)
        calls.append(index)
        entered[index].set()
        assert release.wait(5)
        return original(raw)

    monkeypatch.setattr(Chat, "model_validate_json", staticmethod(blocked))
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            connect(client)
            first = pool.submit(client.post, BASE + "/chat", json=chat())
            assert entered[0].wait(2)
            assert client.delete(BASE).status_code == 204
            assert first.result(timeout=2).status_code == 409
            connect(client)
            second = pool.submit(client.post, BASE + "/chat", json=chat())
            assert entered[1].wait(2)
            connect(client, "2" * 64)
            assert client.post(f"/sessions/{'2' * 64}/chat", json=chat()).status_code == 429
            assert len(client.app.state.bridge.validations) == 2
            release.set()
            assert second.result(timeout=2).status_code == 200
    finally:
        release.set()
    assert len(calls) == 2


def test_eighth_thread_reaps_and_requires_explicit_relogin(bridge_client):
    client = bridge_client
    runtime = connect(client)
    session = client.app.state.bridge.sessions[SID]
    for _ in range(8):
        response = client.post(BASE + "/chat", json=chat())
        assert response.status_code == 200 and response.json()["text"] == "Answer"
    assert runtime.closed
    assert [m for m, _ in runtime.calls].count("thread/start") == 8
    assert client.get(BASE + "/status").json() == DISCONNECTED
    assert SID not in client.app.state.bridge.sessions and not session.results
    assert client.post("/lease", json={"sessions": [SID]}).json() == {"sessions": []}
    assert client.post(BASE + "/chat", json=chat()).status_code == 409
    assert client.post(BASE + "/login").json()["state"] == "pending"


def test_runtime_configuration_and_environment_have_no_inheritance(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-credential")
    monkeypatch.setenv("CODEX_BRIDGE_TOKEN", TOKEN)
    monkeypatch.setenv("CODEX_BRIDGE_TOKEN_FILE", "/run/codex-auth/token")
    monkeypatch.setenv("HTTP_PROXY", "http://unrelated-proxy")
    env = child_environment(tmp_path)
    assert "OPENAI_API_KEY" not in env and "CODEX_BRIDGE_TOKEN" not in env and "HTTP_PROXY" not in env
    assert "CODEX_BRIDGE_TOKEN_FILE" not in env and "/run/codex-auth/token" not in env.values()
    assert env["HOME"] == str(tmp_path) and env["CODEX_HOME"] == str(tmp_path / "codex")
    config = configuration()
    assert config["forced_login_method"] == "chatgpt" and config["cli_auth_credentials_store"] == "ephemeral"
    assert config["model_providers.evencomms.requires_openai_auth"] is True
    assert config["model_providers.evencomms.request_max_retries"] == 0
    assert config["model_providers.evencomms.stream_max_retries"] == 0
    assert config["model_providers.evencomms.base_url"] == "http://127.0.0.1:1/unarmed"
    assert config["model_providers.evencomms.name"] == "OpenAI"
    assert config["model_providers.evencomms.http_headers.version"] == "0.157.1"
    assert UPSTREAM == "https://chatgpt.com/backend-api/codex/responses"
    assert "https://chatgpt.com/backend-api/codex" not in config.values()
    assert not any(key.endswith(("env_key", "experimental_bearer_token")) for key in config)
    assert config["model_provider"] == PROVIDER == thread_params(MODELS[0]["id"])["modelProvider"]
    assert config["mcp_servers"] == {} and config["notify"] == []
    assert not any(key.endswith(("unified_exec", "view_image_tool", "apply_patch_freeform")) for key in config)
    catalog = json.loads(Path(config["model_catalog_json"]).read_text())
    for row in catalog["models"]:
        assert row["tool_mode"] == "direct" and row["shell_type"] == "disabled"
        assert row["experimental_supported_tools"] == [] and row["apply_patch_tool_type"] is None
        assert not row["supports_search_tool"] and not row["supports_experimental_context"]
        assert "token_budget" not in row.get("model_messages", {})


def test_lighter_selector_retains_pinned_metadata_and_is_startup_default():
    config = configuration()
    rows = json.loads(Path(config["model_catalog_json"]).read_text())["models"]
    assert MODELS == [{"id": "gpt-6-luna", "image": True}, {"id": "gpt-6-astra", "image": True}]
    assert config["model"] == "gpt-6-luna"
    luna, astra = rows
    assert luna["display_name"] == "GPT-6-Luna"
    assert luna["description"] == "Fast and affordable model for easier tasks."
    assert luna["default_reasoning_level"] == "medium" and luna["priority"] == 3
    assert [row["effort"] for row in luna["supported_reasoning_levels"]] == ["low", "medium", "high", "xhigh", "max"]
    assert astra["default_reasoning_level"] == "low" and astra["priority"] == 1
    for row in rows:
        assert row["input_modalities"] == ["text", "image"] and row["context_window"] == 272000
        assert row["truncation_policy"] == {"mode": "tokens", "limit": 10000}
        assert row["visibility"] == "list" and row["supported_in_api"] is True
        assert row["support_verbosity"] is True and row["default_verbosity"] == "low"
        assert row["use_responses_lite"] is True and row["default_reasoning_summary"] == "none"
        assert row["availability_nux"] is None and row["upgrade"] is None
        assert "model_messages" not in row


class Process:
    def __init__(self):
        self.stdout = asyncio.StreamReader(limit=WIRE_LIMIT + 1)
        self.stdin = self
        self.returncode = None
        self.pid = 424242
        self.sent = []
        self.logout_error = False

    def write(self, raw):
        data = json.loads(raw)
        self.sent.append(data)
        if "id" in data and data.get("method") in {"initialize", "account/logout"}:
            if data["method"] == "account/logout" and self.logout_error:
                self.stdout.feed_data(json.dumps({"id": data["id"], "error": {"message": "secret-provider-error"}}).encode() + b"\n")
                return
            result = {"userAgent": "evencomms_codex_bridge/0.157.1"} if data["method"] == "initialize" else {}
            self.stdout.feed_data(json.dumps({"id": data["id"], "result": result}).encode() + b"\n")

    async def drain(self):
        pass

    def close(self):
        self.stdout.feed_eof()

    async def wait(self):
        self.returncode = -9
        return -9


@pytest.mark.parametrize("kind", ["approval", "bad_json", "oversize", "unknown_id", "eof"])
def test_rpc_wire_failure_denies_requests_reaps_and_removes_home(monkeypatch, tmp_path, kind):
    async def run():
        process = Process()
        captured = {}
        killed = []
        events = []

        async def spawn(*args, **kwargs):
            captured.update(kwargs)
            return process

        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
        monkeypatch.setattr("codex_bridge.rpc.os.killpg", lambda *args: killed.append(args))
        runtime = Runtime("/fake/codex", lambda m, p: events.append(m), temp_parent=str(tmp_path))
        await runtime.start()
        home = Path(captured["env"]["HOME"])
        assert home.is_dir()
        assert captured["stderr"] == asyncio.subprocess.DEVNULL
        assert captured["cwd"] == home / "work"
        assert captured["start_new_session"] is True
        initialize = process.sent[0]
        assert initialize["params"]["capabilities"]["experimentalApi"] is True
        pending = asyncio.create_task(runtime.call("account/read"))
        await asyncio.sleep(0)
        if kind == "approval":
            process.stdout.feed_data(b'{"id":99,"method":"item/commandExecution/requestApproval","params":{}}\n')
        elif kind == "bad_json":
            process.stdout.feed_data(b"not json\n")
        elif kind == "oversize":
            process.stdout.feed_data(b"x" * (WIRE_LIMIT + 2))
        elif kind == "unknown_id":
            process.stdout.feed_data(b'{"id":999,"result":{}}\n')
        else:
            process.stdout.feed_eof()
        with pytest.raises(ProtocolError):
            await pending
        assert runtime.failed and killed
        if kind == "approval":
            assert any(message.get("error", {}).get("code") == -32601 for message in process.sent)
        await runtime.close()
        await runtime.close()
        assert process.returncode == -9 and not home.exists() and not runtime.pending
        assert runtime.reader.done()

    asyncio.run(run())


def test_logout_rpc_error_is_best_effort_and_still_reaps(monkeypatch, tmp_path):
    async def run():
        process = Process()
        process.logout_error = True

        async def spawn(*args, **kwargs):
            return process

        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
        monkeypatch.setattr("codex_bridge.rpc.os.killpg", lambda *args: None)
        runtime = Runtime("/fake/codex", lambda m, p: None, temp_parent=str(tmp_path))
        await runtime.start()
        directory = Path(runtime.directory.name)
        await runtime.close()
        assert process.returncode == -9 and not directory.exists() and not runtime.pending
        assert len([message for message in process.sent if message.get("method") == "account/logout"]) == 1

    asyncio.run(run())


def test_outbound_rpc_limit_and_pending_call_cap(monkeypatch, tmp_path):
    async def run():
        process = Process()

        async def spawn(*args, **kwargs):
            return process

        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
        monkeypatch.setattr("codex_bridge.rpc.os.killpg", lambda *args: None)
        runtime = Runtime("/fake/codex", lambda m, p: None, temp_parent=str(tmp_path))
        await runtime.start()
        pending = [asyncio.create_task(runtime.call("blocked")) for _ in range(4)]
        await asyncio.sleep(0)
        with pytest.raises(ProtocolError):
            await runtime.call("overflow")
        with pytest.raises(ProtocolError):
            await runtime.send({"method": "large", "params": {"text": "x" * WIRE_LIMIT}})
        await asyncio.gather(*pending, return_exceptions=True)
        await runtime.close()
        assert not runtime.pending

    asyncio.run(run())


def test_cancel_during_process_creation_still_reaps(monkeypatch, tmp_path):
    async def run():
        process = Process()
        entered, release = asyncio.Event(), asyncio.Event()
        killed = []

        async def spawn(*args, **kwargs):
            entered.set()
            await release.wait()
            return process

        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
        monkeypatch.setattr("codex_bridge.rpc.os.killpg", lambda *args: killed.append(args))
        runtime = Runtime("/fake/codex", lambda m, p: None, temp_parent=str(tmp_path))
        task = asyncio.create_task(runtime.start())
        await entered.wait()
        directory = Path(runtime.directory.name)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert killed and process.returncode == -9 and not directory.exists()

    asyncio.run(run())


def test_probe_issuer_cannot_be_a_real_or_redirected_origin(tmp_path):
    async def run():
        runtime = Runtime("/fake/codex", lambda m, p: None, temp_parent=str(tmp_path), probe_origin="https://auth.openai.com")
        with pytest.raises(ProtocolError):
            await runtime.start()
        assert runtime.directory is None and runtime.process is None

    asyncio.run(run())


def test_real_full_pipe_is_killed_reaped_and_drained(monkeypatch, tmp_path):
    async def run():
        original = asyncio.create_subprocess_exec

        async def spawn(*args, **kwargs):
            # Our own credential-free fixture, not Codex or a provider call.
            return await original(sys.executable, "-c",
                                  "import sys; sys.stdin.readline(); sys.stdout.buffer.write(b'x' * (32 * 1024 * 1024)); sys.stdout.flush()",
                                  **kwargs)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
        runtime = Runtime("/fake/codex", lambda m, p: None, temp_parent=str(tmp_path))
        async with asyncio.timeout(5):
            with pytest.raises(ProtocolError):
                await runtime.start()
        assert runtime.process.returncode is not None
        assert runtime.directory is None and not runtime.pending

    asyncio.run(run())


def inference_payload(model=None):
    return {"model": model or MODELS[0]["id"], "stream": True, "store": False,
            "tool_choice": "auto", "parallel_tool_calls": False,
            "reasoning": {"effort": "low" if model == MODELS[1]["id"] else "medium", "context": "all_turns"},
            "include": ["reasoning.encrypted_content"], "text": {"verbosity": "low"},
            "client_metadata": {}, "prompt_cache_key": "thread-one", "input": [
                {"type": "additional_tools", "id": "at_fixture", "role": "developer", "tools": []},
                {"type": "message", "id": "msg_base", "role": "developer",
                 "content": [{"type": "input_text", "text": INSTRUCTIONS}],
                 "internal_chat_message_metadata_passthrough": {"content_item_kinds": ["model.base_instructions"]}},
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Question"}]},
            ]}


class RelayStream(httpx.AsyncByteStream):
    def __init__(self, chunks=(b"data: {}\n\n",)):
        self.chunks = chunks
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk

    async def aclose(self):
        self.closed = True


async def allow_fixture_workspace(account_id):
    assert account_id == "fixture-account"
    return True


async def relay_request(relay, budget=None, *, payload=None, headers=None, target=None, raw=None, encoded=None):
    if raw is None:
        body = encoded if encoded is not None else json.dumps(inference_payload() if payload is None else payload).encode()
        fields = {
            "host": relay.authority, "content-length": str(len(body)), "content-type": "application/json",
            "accept": "text/event-stream", "authorization": "Bearer fixture-private-access",
            "chatgpt-account-id": "fixture-account", "user-agent": "evencomms_codex_bridge/0.157.1",
            "originator": "evencomms_codex_bridge", "thread-id": budget.thread_id if budget else "thread-one",
            "x-client-request-id": budget.thread_id if budget else "thread-one",
            "version": "0.157.1", "x-openai-internal-codex-responses-lite": "true",
        }
        fields.update(headers or {})
        target = target or (budget.path if budget else "/unarmed/responses")
        raw = (f"POST {target} HTTP/1.1\r\n" + "".join(f"{key}: {value}\r\n" for key, value in fields.items()
                                                        if value is not None) + "\r\n").encode() + body
    reader, writer = await asyncio.open_connection("127.0.0.1", int(relay.authority.split(":")[1]))
    try:
        writer.write(raw)
        await writer.drain()
        async with asyncio.timeout(5):
            response = await reader.read()
        head, _, body = response.partition(b"\r\n\r\n")
        return int(head.split(b" ")[1]), head.lower(), body
    finally:
        writer.close()
        await writer.wait_closed()


@pytest.mark.parametrize("content_type", ["text/event-stream", None])
def test_relay_has_no_budget_until_trusted_arm_and_never_uses_host_routing(monkeypatch, content_type):
    async def run():
        calls, checks = [], []

        async def check_workspace(account_id):
            checks.append(account_id)
            return await allow_fixture_workspace(account_id)

        def upstream(request):
            calls.append(request)
            headers = {} if content_type is None else {"content-type": content_type}
            return httpx.Response(200, headers=headers, stream=RelayStream())

        monkeypatch.setenv("HTTPS_PROXY", "http://unrelated.invalid")
        monkeypatch.setenv("OPENAI_API_KEY", "unrelated-key")
        relay = Relay(check_workspace=check_workspace, transport=httpx.MockTransport(upstream))
        await relay.start()
        try:
            assert (await relay_request(relay))[0] == 403
            assert not calls
            budget = relay.arm(MODELS[0]["id"])
            relay.bind(budget, "thread-one")
            assert (await relay_request(relay, budget, target="https://evil.invalid/responses"))[0] == 403
            assert (await relay_request(relay, budget, headers={"host": "evil.invalid"}))[0] == 400
            assert not calls and not checks and budget.attempts == 0
            assert budget.failure is None and budget.upstream_status is None
            status, _, content = await relay_request(relay, budget)
            assert status == 200 and content == b"data: {}\n\n"
            assert checks == ["fixture-account"]
            assert str(calls[0].url) == UPSTREAM
            assert calls[0].headers["host"] == "chatgpt.com"
            assert calls[0].headers["authorization"] == "Bearer fixture-private-access"
            assert calls[0].headers["user-agent"] == "evencomms_codex_bridge/0.157.1"
            assert calls[0].headers["accept-encoding"] == "identity"
            assert calls[0].headers["version"] == "0.157.1"
            assert calls[0].headers["x-openai-internal-codex-responses-lite"] == "true"
            assert "cookie" not in calls[0].headers
        finally:
            await relay.close()
        assert not relay.tasks and relay.client.is_closed

    asyncio.run(run())


@pytest.mark.parametrize("headers", [
    {"origin": "null"}, {"cookie": "secret=value"}, {"connection": "keep-alive"},
    {"transfer-encoding": "chunked"}, {"content-encoding": "gzip"}, {"proxy-authorization": "secret"},
    {"x-unknown": "value"}, {"accept-encoding": "gzip"}, {"authorization": "Basic secret"},
    {"thread-id": "old-thread"}, {"x-client-request-id": "old-thread"}, {"content-length": "99999999"},
])
def test_relay_rejects_unsafe_headers_without_forwarding(headers):
    async def run():
        calls = []
        relay = Relay(check_workspace=allow_fixture_workspace,
                      transport=httpx.MockTransport(lambda request: calls.append(request) or httpx.Response(500)))
        await relay.start()
        try:
            budget = relay.arm(MODELS[0]["id"])
            relay.bind(budget, "thread-one")
            assert (await relay_request(relay, budget, headers=headers))[0] >= 400
            assert not calls and not budget.attempts
        finally:
            await relay.close()

    asyncio.run(run())


@pytest.mark.parametrize("change", [
    {"tools": [{"type": "function", "name": "exec_command"}]}, {"tools": None}, {"model": MODELS[1]["id"]},
    {"model": "unlisted"}, {"background": True}, {"store": True}, {"stream": False},
    {"previous_response_id": "hidden-old-context"}, {"tool_choice": "required"}, {"tool_choice": "none"},
    {"input": [{"type": "function_call", "name": "exec_command"}]},
])
def test_relay_revalidates_model_tools_and_body_and_consumes_invalid_send(change):
    async def run():
        calls = []
        relay = Relay(check_workspace=allow_fixture_workspace,
                      transport=httpx.MockTransport(lambda request: calls.append(request) or httpx.Response(500)))
        await relay.start()
        try:
            budget = relay.arm(MODELS[0]["id"])
            relay.bind(budget, "thread-one")
            assert (await relay_request(relay, budget, payload={**inference_payload(), **change}))[0] == 400
            assert budget.consumed
            assert (await relay_request(relay, budget))[0] == 409
            assert not calls
        finally:
            await relay.close()

    asyncio.run(run())


@pytest.mark.parametrize("malformed", [
    b"Content-Length: 2\r\ncontent-length: 2\r\n\r\n{}",
    b"Content-Length: -1\r\n\r\n{}",
    b"Bad Header: value\r\nContent-Length: 2\r\n\r\n{}",
    b"X-Unknown: " + b"x" * 33000 + b"\r\nContent-Length: 2\r\n\r\n{}",
])
def test_relay_rejects_malformed_or_oversized_framing(malformed):
    async def run():
        calls = []
        relay = Relay(check_workspace=allow_fixture_workspace,
                      transport=httpx.MockTransport(lambda request: calls.append(request) or httpx.Response(500)))
        await relay.start()
        try:
            budget = relay.arm(MODELS[0]["id"])
            relay.bind(budget, "thread-one")
            raw = f"POST {budget.path} HTTP/1.1\r\nHost: {relay.authority}\r\n".encode() + malformed
            assert (await relay_request(relay, raw=raw))[0] >= 400
            assert not calls
        finally:
            await relay.close()

    asyncio.run(run())


@pytest.mark.parametrize("statuses", [[200], [500], [401, 200], [401, 401, 200], [401, 401, 401]])
def test_relay_forwards_only_bounded_401_recovery_and_one_non_401(statuses):
    async def run():
        calls = []

        def upstream(request):
            status = statuses[len(calls)]
            calls.append(request)
            return httpx.Response(status, headers={"content-type": "text/event-stream"}, stream=RelayStream())

        relay = Relay(check_workspace=allow_fixture_workspace, transport=httpx.MockTransport(upstream))
        await relay.start()
        try:
            budget = relay.arm(MODELS[0]["id"])
            relay.bind(budget, "thread-one")
            for status in statuses:
                assert (await relay_request(relay, budget))[0] == status
            # Delayed and repeated native follow-ups cannot earn a new budget.
            await asyncio.sleep(0.03)
            for _ in range(5):
                assert (await relay_request(relay, budget))[0] == 409
            assert len(calls) == len(statuses) == budget.attempts
            assert budget.non_401 == sum(status != 401 for status in statuses) <= 1
            with pytest.raises(Rejected):
                relay.arm(MODELS[0]["id"])
        finally:
            await relay.close()

    asyncio.run(run())


def test_relay_rejects_changed_recovery_input_and_account():
    async def run():
        changed = inference_payload()
        changed["input"][-1]["content"][0]["text"] = "Different authorized input"
        for headers, payload in [({"chatgpt-account-id": "another-account"}, inference_payload()),
                                 ({}, changed)]:
            calls = []
            relay = Relay(check_workspace=allow_fixture_workspace,
                          transport=httpx.MockTransport(lambda request: calls.append(request) or httpx.Response(401)))
            await relay.start()
            try:
                budget = relay.arm(MODELS[0]["id"])
                relay.bind(budget, "thread-one")
                assert (await relay_request(relay, budget))[0] == 401
                assert (await relay_request(relay, budget, headers=headers, payload=payload))[0] == 400
                assert budget.consumed and len(calls) == 1
            finally:
                await relay.close()

    asyncio.run(run())


def test_relay_overlap_stale_paths_and_completion_cannot_rearm_old_work():
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        calls, checks = [], []

        async def check_workspace(account_id):
            checks.append(account_id)
            return await allow_fixture_workspace(account_id)

        async def upstream(request):
            calls.append(request)
            entered.set()
            await release.wait()
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=RelayStream())

        relay = Relay(check_workspace=check_workspace, transport=httpx.MockTransport(upstream))
        await relay.start()
        try:
            old = relay.arm(MODELS[0]["id"])
            relay.bind(old, "old-thread")
            pending = asyncio.create_task(relay_request(relay, old))
            await entered.wait()
            assert old.busy and (await relay_request(relay, old))[0] == 409
            assert old.failure is None
            assert len(calls) == len(checks) == 1
            release.set()
            assert (await pending)[0] == 200
            await relay.finish(old)
            new = relay.arm(MODELS[0]["id"])
            relay.bind(new, "new-thread")
            for _ in range(3):
                assert (await relay_request(relay, old))[0] == 403
            assert new.attempts == 0 and not new.consumed
            assert new.failure is None and new.upstream_status is None
            assert len(checks) == 1
            assert (await relay_request(relay, new))[0] == 200
            assert new.attempts == 1 and old.attempts == 1 and len(calls) == len(checks) == 2
        finally:
            await relay.close()

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["network", "redirect", "compressed", "bad_content_type", "empty_content_type"])
def test_relay_uncertainty_redirect_and_invalid_upstream_consume_budget(failure, caplog):
    async def run():
        calls = []

        def upstream(request):
            calls.append(request)
            if failure == "network":
                raise httpx.ReadError("private provider diagnostic")
            if failure == "redirect":
                return httpx.Response(307, headers={"location": "https://evil.invalid/private"})
            if failure == "compressed":
                return httpx.Response(200, headers={"content-encoding": "gzip"}, stream=RelayStream((b"private",)))
            return httpx.Response(200, headers={"content-type": "" if failure == "empty_content_type" else "text/html"}, content=b"private")

        relay = Relay(check_workspace=allow_fixture_workspace, transport=httpx.MockTransport(upstream))
        await relay.start()
        try:
            budget = relay.arm(MODELS[0]["id"])
            relay.bind(budget, "thread-one")
            status, _, content = await relay_request(relay, budget)
            assert status == 502 and b"private" not in content
            assert (await relay_request(relay, budget))[0] == 409
            assert budget.consumed and len(calls) == 1
            assert budget.failure == {"network": "network_error", "redirect": "unsupported_workspace",
                                      "compressed": "response_encoding", "bad_content_type": "protocol_mismatch",
                                      "empty_content_type": "protocol_mismatch"}[failure]
            assert "private provider diagnostic" not in caplog.text
        finally:
            await relay.close()

    asyncio.run(run())


def test_relay_discards_response_cookies_and_private_error_bodies():
    async def run():
        calls = []

        def upstream(request):
            calls.append(request)
            return httpx.Response(401, headers={"set-cookie": "private=value; Path=/"}, content=b"private account diagnostic")

        relay = Relay(check_workspace=allow_fixture_workspace, transport=httpx.MockTransport(upstream))
        await relay.start()
        try:
            budget = relay.arm(MODELS[0]["id"])
            relay.bind(budget, "thread-one")
            for _ in range(3):
                status, headers, content = await relay_request(relay, budget)
                assert status == 401 and b"set-cookie" not in headers and b"private" not in content
            assert not relay.client.cookies
            assert all("cookie" not in request.headers for request in calls)
        finally:
            await relay.close()

    asyncio.run(run())


def test_relay_deadline_and_close_cancel_owned_forwarding():
    async def run():
        entered = asyncio.Event()

        async def upstream(request):
            entered.set()
            await asyncio.Event().wait()

        relay = Relay(check_workspace=allow_fixture_workspace, transport=httpx.MockTransport(upstream))
        await relay.start()
        expired = relay.arm(MODELS[0]["id"])
        relay.bind(expired, "expired-thread")
        expired.deadline = asyncio.get_running_loop().time() - 1
        assert (await relay_request(relay, expired))[0] == 409
        assert not entered.is_set()
        await relay.finish(expired)
        budget = relay.arm(MODELS[0]["id"])
        relay.bind(budget, "thread-one")
        pending = asyncio.create_task(relay_request(relay, budget))
        await entered.wait()
        await relay.close()
        await asyncio.gather(pending, return_exceptions=True)
        assert not relay.tasks and not budget.tasks and not budget.armed and budget.consumed
        assert relay.client.is_closed

    asyncio.run(run())


@pytest.mark.parametrize("encoded", [
    b"not json", b"[]", b'{"tools":[],"tools":[]}', b'{"tools":[],"value":NaN}',
])
def test_relay_rejects_invalid_and_ambiguous_json(encoded):
    async def run():
        calls = []
        relay = Relay(check_workspace=allow_fixture_workspace,
                      transport=httpx.MockTransport(lambda request: calls.append(request) or httpx.Response(500)))
        await relay.start()
        try:
            budget = relay.arm(MODELS[0]["id"])
            relay.bind(budget, "thread-one")
            assert (await relay_request(relay, budget, encoded=encoded))[0] == 400
            assert not calls and budget.consumed
        finally:
            await relay.close()

    asyncio.run(run())


def test_relay_bounds_streamed_bytes_and_keeps_spent_budget(monkeypatch):
    async def run():
        chunks = RelayStream((b"data: first\n\n", b"x" * 40))
        calls = []

        def upstream(request):
            calls.append(request)
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=chunks)

        monkeypatch.setattr("codex_bridge.relay.RESPONSE_LIMIT", 32)
        relay = Relay(check_workspace=allow_fixture_workspace, transport=httpx.MockTransport(upstream))
        await relay.start()
        try:
            budget = relay.arm(MODELS[0]["id"])
            relay.bind(budget, "thread-one")
            status, _, body = await relay_request(relay, budget)
            assert status == 200 and body == b"data: first\n\n" and chunks.closed
            assert budget.consumed and not budget.completed and budget.non_401 == 1
            assert (await relay_request(relay, budget))[0] == 409 and len(calls) == 1
        finally:
            await relay.close()

    asyncio.run(run())


def test_relay_stream_timeout_closes_stream_without_replay():
    async def run():
        class SlowStream(RelayStream):
            async def __aiter__(self):
                yield b"data: first\n\n"
                await asyncio.Event().wait()

        stream = SlowStream()
        relay = Relay(check_workspace=allow_fixture_workspace, transport=httpx.MockTransport(lambda request: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=stream)))
        await relay.start()
        try:
            budget = relay.arm(MODELS[0]["id"])
            relay.bind(budget, "thread-one")
            budget.deadline = asyncio.get_running_loop().time() + 0.05
            status, _, body = await relay_request(relay, budget)
            assert status == 200 and body == b"data: first\n\n" and stream.closed
            assert budget.consumed and budget.attempts == 1
            assert budget.failure == "timeout"
            assert (await relay_request(relay, budget))[0] == 409
        finally:
            await relay.close()

    asyncio.run(run())


def test_relay_cookie_headers_do_not_enter_debug_logs(caplog):
    async def run():
        async def upstream(reader, writer):
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 401 Unauthorized\r\nSet-Cookie: relay-private-cookie=secret\r\n"
                         b"Content-Length: 0\r\nConnection: close\r\n\r\n")
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(upstream, "127.0.0.1", 0)
        relay = Relay(check_workspace=allow_fixture_workspace,
                      probe_origin="http://127.0.0.1:" + str(server.sockets[0].getsockname()[1]))
        await relay.start()
        try:
            budget = relay.arm(MODELS[0]["id"])
            relay.bind(budget, "thread-one")
            assert (await relay_request(relay, budget))[0] == 401
            assert not relay.client.cookies
        finally:
            await relay.close()
            server.close()
            await server.wait_closed()

    caplog.set_level(logging.DEBUG)
    asyncio.run(run())
    assert "relay-private-cookie" not in caplog.text and "fixture-private-access" not in caplog.text


def test_relay_caps_connections_before_creating_handler_tasks():
    async def run():
        relay = Relay(check_workspace=allow_fixture_workspace, transport=httpx.MockTransport(lambda request: httpx.Response(500)))
        await relay.start()
        writers = []
        try:
            for _ in range(4):
                _, writer = await asyncio.open_connection("127.0.0.1", int(relay.authority.split(":")[1]))
                writers.append(writer)
            assert len(relay.tasks) == 4
            reader, writer = await asyncio.open_connection("127.0.0.1", int(relay.authority.split(":")[1]))
            writers.append(writer)
            async with asyncio.timeout(1):
                assert await reader.read() == b""
            assert len(relay.tasks) == 4
        finally:
            async with asyncio.timeout(2):
                await relay.close()
            for writer in writers:
                writer.close()
        assert not relay.tasks

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["text", "error"])
def test_chat_stream_envelope_and_cached_replay(bridge_client, mode):
    client = bridge_client
    runtime = connect(client)
    runtime.mode = mode
    data = chat()
    response = client.post(BASE + "/chat", json=data, headers={"Accept": "application/x-ndjson"})
    assert response.status_code == 200
    assert response.headers["x-accel-buffering"] == "no"
    events = [json.loads(line) for line in response.text.splitlines() if line]
    if mode == "text":
        assert events[-1]["type"] == "done"
        assert events[-1]["response"]["text"] == "Answer"
        assert events[0] == {"type": "text", "text": "Answer"}
        count = len(runtime.calls)
        replay = client.post(BASE + "/chat", json=data, headers={"Accept": "application/x-ndjson"})
        assert json.loads(replay.text.strip())["type"] == "done"
        assert len(runtime.calls) == count
    else:
        assert events[-1]["type"] == "error"
        assert "secret-provider-error" not in response.text
    assert not client.app.state.bridge.stream_bodies
