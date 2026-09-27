"""Backend -> real bridge -> pinned native runtime -> real relay, loopback only."""

import asyncio
import base64
import contextlib
import io
import json
import logging
from pathlib import Path
import re
import shutil
from urllib.parse import parse_qs
from uuid import uuid4

import httpx
from PIL import Image
import pytest

from backend.config import Settings
from backend.main import create_app as backend_app, digest
from codex_bridge.errors import ERROR_HEADER, Failure
from codex_bridge.generation import Generation
from codex_bridge.policy import DEVICE_URL, INSTRUCTIONS, MODELS, VERSION, generation_allowed
from codex_bridge.probe import run_probe, verify_binary
from codex_bridge.relay import Relay
from codex_bridge.rpc import ProtocolError, Runtime, WIRE_LIMIT
from codex_bridge.service import RETRY_WARNING, Session, create_app as bridge_app


ROOT = "/api/research/codex"
BRIDGE = "http://codex-bridge:8090"
TOKEN = "aB12" * 16
PASSWORD = "offline-native-admin-password"
PAID_KEY = "offline-paid-key-must-not-be-used"
EMAIL = "offline-native@example.invalid"
PRIVATE = "offline-native-provider-secret-message"
ACCESS = "offline-native-access"
REFRESH = "offline-native-refresh"
ACCOUNT = "offline-native-account"
PROMPT = "Private explicit native question."
ANSWER = "Offline native answer."
REGIONAL_ORIGIN = "https://private-workspace.example.invalid"


@pytest.fixture(scope="module")
def pinned_native_probe(tmp_path_factory):
    binary = shutil.which("codex")
    if binary is None:
        pytest.skip("Put the pinned native Codex binary on PATH to run the offline integration")
    try:
        verify_binary(binary)
    except (OSError, ProtocolError):
        pytest.skip("Offline integration requires the SHA-256-pinned native Codex binary")
    parent = tmp_path_factory.mktemp("native-chat-probe")
    transport = httpx.AsyncHTTPTransport.handle_async_request

    async def loopback_only(self, request):
        assert request.url.scheme == "http" and request.url.host == "127.0.0.1", "Non-loopback HTTP is forbidden"
        return await transport(self, request)

    async def prove():
        async with asyncio.timeout(75):
            return await run_probe(binary, temp_parent=str(parent))

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", loopback_only)
        report = asyncio.run(prove())
        assert report["generation_enabled"] is True and generation_allowed(report), report
        assert report["live_account_verified"] is False
        assert {"model/verification", "turn/moderationMetadata", "model/safetyBuffering/updated",
                "item/reasoning/textDelta", "item/agentMessage/delta"} <= set(report["observed_events"])
        assert report["tool_upstream_requests"] == report["tool_upstream_non_401"] == 1
        assert report["tool_blocked_requests"] >= 1
        assert not list(parent.iterdir())
        yield binary, report


@pytest.mark.parametrize("model,case,code", [
    pytest.param("gpt-6-luna", "text", None, id="luna-text-metadata"),
    pytest.param("gpt-6-astra", "text", None, id="astra-text-metadata"),
    pytest.param("gpt-6-luna", "jpeg", None, id="jpeg-history-metadata"),
    pytest.param("gpt-6-luna", "quota", "rate_limit", id="http-429-quota"),
    pytest.param("gpt-6-astra", "model", "model_unavailable", id="http-404-model"),
    pytest.param("gpt-6-luna", "failed", "context_limit", id="sse-structured-failure"),
    pytest.param("gpt-6-astra", "tool", "tool_rejected", id="unexpected-tool"),
    pytest.param("gpt-6-luna", "regional_us", "unsupported_workspace", id="regional-us-no-forward"),
    pytest.param("gpt-6-astra", "regional_us_cr", "unsupported_workspace", id="regional-us-cr-no-forward"),
    pytest.param("gpt-6-luna", "regional_origin", "unsupported_workspace", id="regional-origin-no-forward"),
    pytest.param("gpt-6-luna", "routing_refresh_us", "unsupported_workspace", id="regional-us-after-refresh"),
    pytest.param("gpt-6-astra", "routing_refresh_us_cr", "unsupported_workspace", id="regional-us-cr-after-refresh"),
    pytest.param("gpt-6-luna", "routing_refresh_missing", "unsupported_workspace", id="routing-missing-after-refresh"),
    pytest.param("gpt-6-astra", "routing_refresh_malformed", "unsupported_workspace", id="routing-malformed-after-refresh"),
    pytest.param("gpt-6-luna", "routing_refresh_fetch_error", "unsupported_workspace", id="routing-fetch-error-after-refresh"),
])
def test_offline_pinned_native_chat(pinned_native_probe, tmp_path, caplog, model, case, code):
    binary, report = pinned_native_probe
    caplog.set_level(logging.DEBUG)
    refresh_routing = case.startswith("routing_refresh_")
    blocked_routing = case.startswith("regional_") or refresh_routing
    expected_requests = 2 if refresh_routing else 0 if blocked_routing else 1
    messages = [{"role": "user", "text": " Displayed private question.\n"},
                {"role": "assistant", "text": " Displayed private answer.\n"},
                {"role": "user", "text": PROMPT}]
    if case == "jpeg" or blocked_routing:
        output = io.BytesIO()
        Image.new("RGB", (2, 3), (123, 45, 67)).save(output, format="JPEG")
        image = "data:image/jpeg;base64," + base64.b64encode(output.getvalue()).decode()
        messages[0]["images"] = [image]
        messages[-1]["images"] = [image]
    data = {"request_id": str(uuid4()), "model": model, "messages": messages}
    claims = {"email": EMAIL, "https://api.openai.com/auth": {
        "chatgpt_user_id": "offline-native-user", "chatgpt_account_id": ACCOUNT, "chatgpt_plan_type": "pro"}}
    jwt = "e30." + base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=") + ".offline"

    async def run():
        approved, polled = asyncio.Event(), asyncio.Event()
        handlers, errors = set(), []
        requests, exchanges, upstream, runtimes, directories, generations, calls = [], [], [], [], [], [], []
        bridge_requests, bridge_responses, public_responses, paid_requests = [], [], [], []
        routing_reads, discovered = [], []

        async def respond(reader, writer):
            handlers.add(asyncio.current_task())
            try:
                assert len(handlers) <= 4
                async with asyncio.timeout(10):
                    lines = (await reader.readuntil(b"\r\n\r\n")).decode("ascii").split("\r\n")
                    headers = {key.lower(): value.strip() for key, value in
                               (line.split(":", 1) for line in lines[1:] if ":" in line)}
                    size = int(headers.get("content-length", "0"))
                    assert 0 <= size <= WIRE_LIMIT and "transfer-encoding" not in headers
                    raw = await reader.readexactly(size)
                    method, path, _ = lines[0].split(" ")
                    path = path.split("?", 1)[0]
                    requests.append((method, path))
                    status, content_type, value = 200, "application/json", {}
                    if method == "POST" and path == "/api/accounts/deviceauth/usercode":
                        value = {"device_auth_id": "offline-device", "user_code": "123456789", "interval": "1"}
                    elif method == "POST" and path == "/api/accounts/deviceauth/token":
                        assert json.loads(raw) == {"device_auth_id": "offline-device", "user_code": "123456789"}
                        if approved.is_set():
                            value = {"authorization_code": "offline-code", "code_challenge": "offline", "code_verifier": "offline"}
                        else:
                            status = 403
                            polled.set()
                    elif method == "POST" and path == "/oauth/token":
                        grant = (json.loads(raw) if headers.get("content-type", "").startswith("application/json")
                                 else {key: values[0] for key, values in parse_qs(raw.decode()).items()})
                        assert approved.is_set()
                        if grant["grant_type"] == "authorization_code":
                            assert not exchanges and grant["code"] == "offline-code"
                            value = {"id_token": jwt, "access_token": ACCESS, "refresh_token": REFRESH}
                        else:
                            assert refresh_routing and grant["grant_type"] == "refresh_token"
                            assert exchanges == ["authorization_code"] and grant["refresh_token"] == REFRESH
                            value = {"access_token": ACCESS + "-refreshed", "refresh_token": REFRESH + "-refreshed"}
                        exchanges.append(grant["grant_type"])
                    elif method == "GET" and path.endswith("/accounts/check"):
                        entry = {"id": ACCOUNT, "workspace_backend_origin": "https://chatgpt.com",
                                 "account_routing_override": "NO_CONSTRAINT"}
                        if case.startswith("regional_") or refresh_routing and "refresh_token" in exchanges:
                            entry["workspace_backend_origin"] = REGIONAL_ORIGIN
                            if case.endswith("_us_cr"):
                                entry["account_routing_override"] = "us_cr"
                            elif case.endswith("_us"):
                                entry["account_routing_override"] = "us"
                            elif case.endswith("_missing"):
                                del entry["workspace_backend_origin"]
                            elif case.endswith("_malformed"):
                                entry["account_routing_override"] = "private-unknown-route"
                            elif case.endswith("_fetch_error"):
                                status = 403
                        value = {"accounts": [entry]} if status == 200 else {"error": {"message": PRIVATE}}
                    elif method == "POST" and path == "/oauth/revoke":
                        pass
                    elif method == "GET" and path.endswith(("/config/bundle", "/settings/user")):
                        pass
                    elif method == "POST" and path == "/responses":
                        request = json.loads(raw)
                        upstream.append(request)
                        assert len(upstream) <= expected_requests and exchanges == ["authorization_code"]
                        assert headers["authorization"] == "Bearer " + ACCESS
                        assert headers["chatgpt-account-id"] == ACCOUNT
                        assert headers["version"] == VERSION == "0.157.1"
                        assert headers["x-openai-internal-codex-responses-lite"] == "true"
                        assert headers["originator"] == "evencomms_codex_bridge"
                        assert headers["user-agent"].startswith("evencomms_codex_bridge/" + VERSION)
                        assert headers["accept"] == "text/event-stream" and headers["accept-encoding"] == "identity"
                        assert headers["thread-id"] == headers["x-client-request-id"] == generations[0].thread_id
                        assert "cookie" not in headers and PAID_KEY not in raw.decode() + str(headers)
                        assert "x-openai-account-routing-override" not in headers
                        # Check native Lite bytes independently of the relay decoder and policy history helper.
                        assert set(request) == {"model", "input", "tool_choice", "parallel_tool_calls", "reasoning",
                                                "store", "stream", "include", "prompt_cache_key", "text", "client_metadata"}
                        assert request["model"] == model and request["tool_choice"] == "auto"
                        assert request["stream"] is True and request["store"] is False
                        assert request["parallel_tool_calls"] is False
                        assert request["reasoning"] == {"effort": "medium" if model == "gpt-6-luna" else "low",
                                                        "context": "all_turns"}
                        assert request["include"] == ["reasoning.encrypted_content"]
                        assert request["text"] == {"verbosity": "low"}
                        prefix, base, skills, *history = request["input"]
                        assert prefix == {"type": "additional_tools", "id": prefix["id"], "role": "developer", "tools": []}
                        assert re.fullmatch(r"at_[A-Za-z0-9_-]+", prefix["id"])
                        assert base["role"] == "developer" and base["content"] == [{"type": "input_text", "text": INSTRUCTIONS}]
                        assert base["internal_chat_message_metadata_passthrough"]["content_item_kinds"] == ["model.base_instructions"]
                        assert skills["role"] == "developer"
                        assert skills["internal_chat_message_metadata_passthrough"]["content_item_kinds"] == ["host_skills.instructions"]
                        assert all(item["type"] == "message" for item in request["input"][1:])
                        normalized = [{"role": item["role"], "content": [
                            {key: value for key, value in part.items() if key in {"type", "text", "image_url"}}
                            for part in item["content"]]} for item in history]
                        expected = [{"role": message["role"], "content": [
                            {"type": "output_text" if message["role"] == "assistant" else "input_text", "text": message["text"]},
                            *({"type": "input_image", "image_url": image} for image in message.get("images", [])),
                        ]} for message in messages]
                        assert normalized == expected
                        secret_message = " ".join((PRIVATE, EMAIL, ACCESS, REFRESH, PROMPT))
                        if refresh_routing:
                            status, value = 401, {"error": {"message": PRIVATE}}
                        elif case in {"quota", "model"}:
                            status = 429 if case == "quota" else 404
                            value = {"error": {"code": "insufficient_quota" if case == "quota" else "model_not_found",
                                               "type": "usage_limit_reached" if case == "quota" else "invalid_request_error",
                                               "message": secret_message}}
                        else:
                            content_type = "text/event-stream"
                            events = [{"type": "response.created", "response": {"id": "resp_offline"}}]
                            if case == "failed":
                                events.append({"type": "response.failed", "response": {"id": "resp_offline", "status": "failed",
                                    "error": {"code": "context_length_exceeded", "message": secret_message}}})
                            else:
                                item = ({"type": "function_call", "id": "fc_offline", "call_id": "call_offline",
                                         "name": "exec_command", "arguments": '{"cmd":"false"}'} if case == "tool" else
                                        {"type": "message", "id": "msg_offline", "role": "assistant", "phase": "final_answer",
                                         "content": [{"type": "output_text", "text": ANSWER}]})
                                output = [item]
                                if case != "tool":
                                    reasoning = {"type": "reasoning", "id": "rs_offline", "summary": [],
                                                 "content": [{"type": "reasoning_text", "text": PRIVATE}]}
                                    output = [reasoning, item]
                                    events.extend([
                                        {"type": "response.metadata", "response_id": "resp_offline", "metadata": {
                                            "openai_verification_recommendation": ["trusted_access_for_cyber"],
                                            "openai_chatgpt_moderation_metadata": {"presentation": "inline", "private": PRIVATE}}},
                                        {"type": "response.metadata", "response_id": "resp_offline", "metadata": {
                                            "type": "safety_buffering", "use_cases": ["cyber"], "reasons": ["user_risk"], "retry_model": None}},
                                        {"type": "response.output_item.added", "output_index": 0,
                                         "item": {"type": "reasoning", "id": "rs_offline", "summary": []}},
                                        {"type": "response.reasoning_text.delta", "item_id": "rs_offline", "output_index": 0,
                                         "content_index": 0, "delta": PRIVATE},
                                        {"type": "response.output_item.done", "output_index": 0, "item": reasoning},
                                    ])
                                index = len(output) - 1
                                events.append({"type": "response.output_item.added", "output_index": index,
                                               "item": item if case == "tool" else {**item, "content": []}})
                                if case != "tool":
                                    events.extend({"type": "response.output_text.delta", "item_id": "msg_offline", "output_index": index,
                                                   "content_index": 0, "delta": delta} for delta in ("Offline native ", "answer."))
                                events.extend([
                                    {"type": "response.output_item.done", "output_index": index, "item": item},
                                    {"type": "response.completed", "response": {"id": "resp_offline", "status": "completed",
                                        "output": output, "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}}},
                                ])
                            value = "".join("data: " + json.dumps(event) + "\n\n" for event in events)
                    else:
                        raise AssertionError("Unexpected offline fixture route")
                    body = (value if content_type == "text/event-stream" else json.dumps(value)).encode()
                    writer.write((f"HTTP/1.1 {status} Fixture\r\nContent-Type: {content_type}\r\nConnection: close\r\n"
                                  f"Content-Length: {len(body)}\r\nSet-Cookie: private={PRIVATE}\r\n\r\n").encode() + body)
                    await writer.drain()
            except (ConnectionError, asyncio.IncompleteReadError):
                pass
            except Exception:
                errors.append("offline fixture rejected request")
            finally:
                writer.close()
                with contextlib.suppress(Exception):
                    await writer.wait_closed()
                handlers.discard(asyncio.current_task())

        server = await asyncio.start_server(respond, "127.0.0.1", 0, limit=65536)
        origin = "http://127.0.0.1:" + str(server.sockets[0].getsockname()[1])

        class FixtureRuntime(Runtime):
            async def call(self, method, params=None, **kwargs):
                assert method in {"initialize", "account/read", "account/login/start", "account/logout",
                                  "thread/start", "thread/inject_items", "turn/start"}
                calls.append((method, params))
                if method == "initialize":
                    directories.append(Path(self.directory.name))
                if method == "turn/start":
                    generation = self.on_event.__self__.generation
                    assert type(generation) is Generation
                    generations.append(generation)
                checking = method == "account/read" and self.relay.current is not None and self.relay.current.busy
                if checking:
                    routing_reads.append(len(upstream))
                result = await super().call(method, params, **kwargs)
                if checking:
                    discovered.append(result.get("workspaceRouting"))
                if method == "account/login/start":
                    # Only adapt the fixture issuer's displayed URL. Both API validators
                    # and the production Session.event callback remain untouched.
                    assert result["verificationUrl"] == origin + "/codex/device"
                    result = {**result, "verificationUrl": DEVICE_URL}
                return result

        def factory(*args, **kwargs):
            runtime = FixtureRuntime(*args, probe_origin=origin, probe_upstream=origin,
                                     probe_config={"chatgpt_base_url": origin, "openai_base_url": origin}, **kwargs)
            runtimes.append(runtime)
            return runtime

        async def verified_probe(requested_binary, **kwargs):
            assert requested_binary == binary and generation_allowed(report)
            return report  # One actual module-scoped native proof, never a manufactured gate.

        async def record_request(request):
            assert str(request.url).startswith(BRIDGE + "/")
            assert request.headers["authorization"] == "Bearer " + TOKEN
            assert request.headers["accept-encoding"] == "identity" and "cookie" not in request.headers
            assert request.url.path == "/lease" or re.fullmatch(r"/sessions/[a-f0-9]{64}(?:/(?:login|status|models|chat))?", request.url.path)
            bridge_requests.append(request)

        async def record_response(response):
            await response.aread()
            bridge_responses.append(response)

        async def no_paid_request(request):
            paid_requests.append(True)
            raise AssertionError("Paid API fallback must not be called")

        bridge = bridge_app(token=TOKEN, token_file="", binary=binary, runtime_factory=factory,
                            probe=verified_probe, temp_parent=str(tmp_path))
        backend = backend_app(Settings(
            admin_password=PASSWORD, database_path=tmp_path / "db.sqlite3", frontend_dist=tmp_path / "dist",
            model_cache=tmp_path / "models", stt_enabled=False, stream_enabled=False,
            openai_api_key=PAID_KEY, openai_timeout=30, codex_bridge_url=BRIDGE, codex_bridge_token=TOKEN,
        ))
        try:
            async with asyncio.timeout(45), bridge.router.lifespan_context(bridge), backend.router.lifespan_context(backend):
                research = backend.state.codex_research
                for client in (research.client, research.lifecycle_client):
                    await client._transport.aclose()
                    client._transport = httpx.ASGITransport(app=bridge)
                    client.event_hooks["request"].append(record_request)
                    client.event_hooks["response"].append(record_response)
                backend.state.research.client.event_hooks["request"].append(no_paid_request)
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=backend), base_url="http://backend",
                                             trust_env=False) as api, httpx.AsyncClient(
                        transport=httpx.ASGITransport(app=bridge), base_url=BRIDGE, trust_env=False) as private:
                    auth = {"Authorization": "Bearer " + TOKEN}
                    for method, path in (("GET", "/ready"), ("GET", "/sessions/" + "1" * 64 + "/status"),
                                         ("POST", "/sessions/" + "1" * 64 + "/chat")):
                        for headers in ({}, {"Authorization": "Bearer " + TOKEN.swapcase()}):
                            assert (await private.request(method, path, headers=headers)).status_code == 401
                    ready = await private.get("/ready", headers=auth)
                    assert ready.json() == {"binary_verified": True, "generation_enabled": True, "active_sessions": 0}
                    public_responses.append(ready)
                    for headers in ({}, auth):
                        assert (await api.get(ROOT + "/status", headers=headers)).status_code == 401
                        assert (await api.post(ROOT + "/chat", headers=headers, json=data)).status_code == 401
                    assert not runtimes and not requests and not bridge_requests
                    login = await api.post("/api/login", json={"password": PASSWORD})
                    assert login.status_code == 200
                    operator = {"Authorization": "Bearer " + login.json()["token"]}
                    parent = digest(login.json()["token"])
                    assert (await api.get("/api/research/status", headers=operator)).json() == {"configured": True, "key_source": "server"}
                    pending = await api.post(ROOT + "/login", headers=operator, json={})
                    public_responses.append(pending)
                    assert pending.status_code == 200 and pending.json() == {
                        "enabled": True, "state": "pending", "verification_url": DEVICE_URL,
                        "user_code": "123456789", "generation_enabled": False}
                    backend_session = research.sessions[parent]
                    session = bridge.state.bridge.sessions[backend_session.identity]
                    runtime = session.runtime
                    assert type(session) is Session and runtime.on_event == session.event
                    assert type(runtime.relay) is Relay and runtime.relay.transport is None
                    assert runtime.relay.upstream == origin + "/responses"
                    await polled.wait()
                    assert not exchanges and not upstream and not session.login_done.is_set()
                    assert (await api.get(ROOT + "/status", headers=operator)).json() == pending.json()
                    assert not list(tmp_path.rglob("auth.json"))
                    approved.set()  # Synthetic loopback approval only, after the public pending state.
                    await session.login_task
                    connected = await api.get(ROOT + "/status", headers=operator)
                    public_responses.append(connected)
                    assert connected.json() == {"enabled": True, "state": "connected", "verification_url": None,
                                                "user_code": None, "generation_enabled": True}
                    catalog = await api.get(ROOT + "/models", headers=operator)
                    assert catalog.status_code == 200 and catalog.json() == {"models": sorted(MODELS, key=lambda row: row["id"])}
                    response = await api.post(ROOT + "/chat", headers=operator, json=data)
                    public_responses.append(response)
                    assert not errors
                    assert len(generations) == 1 and len(upstream) == expected_requests
                    generation = generations[0]
                    budget = generation.budget
                    assert budget.model == model and budget.attempts == expected_requests
                    assert budget.non_401 == (0 if blocked_routing else 1)
                    assert budget.consumed and not budget.armed and not budget.tasks
                    assert runtime.relay.forwarded_requests == expected_requests
                    assert runtime.relay.forwarded_non_401 == budget.non_401
                    assert routing_reads == ([0, 1, 2] if refresh_routing else [0])
                    if not blocked_routing or refresh_routing:
                        assert discovered[0] == {"chatgptAccountId": ACCOUNT, "backendOrigin": "https://chatgpt.com",
                                                 "accountRoutingOverride": "NO_CONSTRAINT"}
                    if case.endswith(("_us", "_us_cr", "_origin")):
                        assert discovered[-1] == {"chatgptAccountId": ACCOUNT, "backendOrigin": REGIONAL_ORIGIN,
                            "accountRoutingOverride": "us_cr" if case.endswith("_us_cr") else
                                                      "us" if case.endswith("_us") else "NO_CONSTRAINT"}
                    assert runtime.relay.current is None
                    assert [method for method, _ in calls].count("thread/start") == 1
                    assert [method for method, _ in calls].count("turn/start") == 1
                    thread = dict(calls)["thread/start"]
                    assert thread["model"] == model and thread["modelProvider"] == "evencomms"
                    assert thread["allowProviderModelFallback"] is False and thread["dynamicTools"] == []
                    bridge_chat = [item for item in bridge_responses if item.request.url.path.endswith("/chat")]
                    assert len(bridge_chat) == 1
                    if code is None:
                        assert response.status_code == bridge_chat[0].status_code == 200
                        assert response.json() == {"request_id": data["request_id"], "model": model,
                                                   "text": ANSWER, "incomplete": False, "usage": None}
                        assert not runtime.failed and runtime.process.returncode is None
                        assert generation.raw_completed == 1 and generation.delta_length == len(ANSWER)
                        assert not generation.replay and generation.failure is None
                        assert ERROR_HEADER not in bridge_chat[0].headers
                        duplicate = await api.post(ROOT + "/chat", headers=operator, json=data)
                        assert duplicate.json() == response.json() and len(upstream) == 1
                    else:
                        failure = Failure(code)
                        assert bridge_chat[0].status_code == failure.status
                        assert bridge_chat[0].headers[ERROR_HEADER] == code
                        assert bridge_chat[0].json() == {"detail": str(failure) + RETRY_WARNING}
                        assert response.status_code == failure.status != 401
                        assert "No paid API fallback" in response.json()["detail"]
                        assert runtime.failed and runtime.failure == generation.failure == code
                        assert runtime.process.returncode is not None and not session.results and not backend_session.results
                        assert budget.upstream_status == (401 if refresh_routing else None if blocked_routing else
                                                          {"quota": 429, "model": 404, "failed": 200, "tool": 200}[case])
                        if blocked_routing:
                            assert budget.failure == "unsupported_workspace"
                        if case == "failed":
                            assert budget.failure is None  # Classified by native Generation, not an HTTP error shortcut.
                    assert not research.tasks and not research.validations and not research.chat_waiters
                    assert not list(tmp_path.rglob("auth.json"))
                    assert (await api.get("/api/research/status", headers=operator)).status_code == 200
                    await api.delete(ROOT + "/connection", headers=operator)
                    await asyncio.gather(*list(research.cleanups.values()))
                    assert not research.sessions and not research.retired and not bridge.state.bridge.sessions
                    assert (await api.post("/api/logout", headers=operator)).status_code == 204
                    assert (await api.get(ROOT + "/status", headers=operator)).status_code == 401
                    dump = "\n".join(backend.state.store.db.iterdump())
                    private_values = (TOKEN, PASSWORD, PAID_KEY, EMAIL, PRIVATE, ACCESS, REFRESH, ACCOUNT, jwt, REGIONAL_ORIGIN,
                                      "offline-native-user", login.json()["token"], *(message["text"] for message in messages),
                                      *(image for message in messages for image in message.get("images", [])))
                    surface = "\n".join(item.text + str(dict(item.headers)) for item in public_responses + bridge_responses)
                    for value in private_values:
                        assert value not in surface + caplog.text + dump
                    assert ANSWER not in dump and not paid_requests
                    assert len(upstream) == expected_requests
                    assert exchanges == (["authorization_code", "refresh_token"] if refresh_routing else ["authorization_code"])
                    assert requests.count(("POST", "/api/accounts/deviceauth/usercode")) == 1
                    assert sum(item.url.path.endswith("/chat") for item in bridge_requests) == 1
        finally:
            server.close()
            await server.wait_closed()
            pending_handlers = tuple(handlers)
            for task in pending_handlers:
                task.cancel()
            await asyncio.gather(*pending_handlers, return_exceptions=True)
            assert not handlers
            assert all(runtime.process is None or runtime.process.returncode is not None for runtime in runtimes)
            assert all(not runtime.pending and runtime.relay.closed and not runtime.relay.tasks for runtime in runtimes)
            assert all(not directory.exists() for directory in directories)
        assert not errors and not paid_requests
        assert research.closed and research.sweeper.done() and not research.cleanups
        assert bridge.state.bridge.closed and bridge.state.bridge.sweeper.done()
        if code is not None:
            assert "[codex:" + code + "]" in response.json()["detail"]

    asyncio.run(run())
