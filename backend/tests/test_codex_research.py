import asyncio
import base64
import io
import json
import re
import threading
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from PIL import Image

from backend.codex_research import DEVICE_URL, LEASE_INTERVAL, LEASE_TTL, SESSION_TTL, disconnected
from backend.config import Settings
from backend.main import create_app, digest
from backend.research import BODY_LIMIT, MODEL_TTL, PROVIDER_LIMIT, RESULT_TTL, Chat
from backend.stream import COOKIE


ROOT = "/api/research/codex"
BRIDGE = "http://codex-bridge:8090"
TOKEN = "aB12" * 16
MODEL = "account-model"
SECRET = "private-email@example.test private-provider-token"


def login(client):
    response = client.post("/api/login", json={"password": "test-admin-password"})
    assert response.status_code == 200
    return {"Authorization": "Bearer " + response.json()["token"]}


def parent(headers):
    return digest(headers["Authorization"].split()[1])


def payload(**changes):
    return {"request_id": str(uuid4()), "model": MODEL,
            "messages": [{"role": "user", "text": "Only this explicit question"}], **changes}


def status(**changes):
    return {"state": "connected", "verification_url": None, "user_code": None,
            "generation_enabled": True, **changes}


def answer(data, **changes):
    return {"request_id": data["request_id"], "model": data["model"], "text": "Codex answer",
            "incomplete": False, "usage": {"input_tokens": 2, "output_tokens": 3, "total_tokens": 5}, **changes}


def bridge(request):
    if request.method == "DELETE":
        return httpx.Response(204)
    if request.url.path == "/lease":
        return httpx.Response(200, json={"sessions": json.loads(request.content)["sessions"]})
    if request.url.path.endswith("/models"):
        return httpx.Response(200, json={"models": [{"id": MODEL, "image": True}, {"id": "text-only", "image": False}]})
    if request.url.path.endswith("/chat"):
        return httpx.Response(200, json=answer(json.loads(request.content)))
    return httpx.Response(200, json=status())


def jpeg(size=(2, 3), format="JPEG"):
    buffer = io.BytesIO()
    Image.new("RGB", size, color="red").save(buffer, format=format)
    return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode()


def connect(client, headers):
    response = client.post(ROOT + "/login", headers=headers, json={})
    assert response.status_code == 200
    return response


@pytest.fixture(autouse=True)
def isolated_codex_environment(monkeypatch):
    monkeypatch.setenv("CODEX_BRIDGE_URL", "")
    monkeypatch.setenv("CODEX_BRIDGE_TOKEN", "")
    monkeypatch.setenv("CODEX_BRIDGE_TOKEN_FILE", "")


@pytest.fixture
def make_codex(make_client):
    def make(handler=bridge, **options):
        client = make_client(codex_bridge_url=BRIDGE, codex_bridge_token=TOKEN, **options)
        research = client.app.state.codex_research
        assert research.client._trust_env is False and research.lifecycle_client._trust_env is False
        assert not research.client.follow_redirects and not research.lifecycle_client.follow_redirects
        assert research.client._transport._pool._max_connections == 2
        assert research.lifecycle_client._transport._pool._max_connections == 1
        assert research.client.timeout.read == options.get("openai_timeout", 90)
        assert len(research.credentials.prune_hooks) == 2
        client.portal.call(research.client._transport.aclose)
        client.portal.call(research.lifecycle_client._transport.aclose)
        calls = []

        async def mock(request):
            assert str(request.url).startswith(BRIDGE + "/")
            assert request.headers["authorization"] == "Bearer " + TOKEN
            assert request.headers["accept-encoding"] == "identity"
            assert "cookie" not in request.headers
            if request.url.path != "/lease":
                assert re.fullmatch(r"/sessions/[a-f0-9]{64}(?:/(?:login|status|models|chat))?", request.url.path)
            calls.append(request)
            result = handler(request)
            return await result if hasattr(result, "__await__") else result

        research.client._transport = httpx.MockTransport(mock)
        research.lifecycle_client._transport = httpx.MockTransport(mock)

        def forbidden(request):
            pytest.fail("Codex must never call paid OpenAI, even with a server key configured")

        client.portal.call(client.app.state.research.client.aclose)
        client.app.state.research.client = httpx.AsyncClient(transport=httpx.MockTransport(forbidden))
        return client, login(client), calls

    return make


def test_disabled_and_unmapped_status_do_not_contact_bridge(make_client, make_codex):
    client = make_client(openai_api_key="paid-server-key")
    headers = login(client)
    research = client.app.state.codex_research
    assert research.client is None and research.lifecycle_client is None and research.sweeper is None
    assert len(research.credentials.prune_hooks) == 1
    assert client.get(ROOT + "/status", headers=headers).json() == disconnected(False)
    assert client.delete(ROOT + "/connection", headers=headers).json() == disconnected(False)
    for method, path in [("POST", "/login"), ("GET", "/models"), ("POST", "/chat")]:
        assert client.request(method, ROOT + path, headers=headers, json={}).status_code == 503
    assert client.get("/api/research/status", headers=headers).json() == {"configured": True, "key_source": "server"}
    enabled, headers, calls = make_codex()
    for _ in range(2):
        assert enabled.get(ROOT + "/status", headers=headers).json() == disconnected()
    assert enabled.get(ROOT + "/models", headers=headers).status_code == 409
    assert enabled.post(ROOT + "/chat", headers=headers, json=payload()).status_code == 409
    enabled.portal.call(enabled.app.state.codex_research.renew_leases)
    assert not calls and not enabled.app.state.codex_research.sessions


@pytest.mark.parametrize("url", ["ftp://bridge", "http://bridge/", "http://bridge/path", "http://bridge?",
    "http://bridge?secret=x", "http://bridge#fragment", "http://user@bridge", "http://:password@bridge",
    "http://bridge:0", "http://bridge:65536", "http://bridge:bad", "http://", "https://*.example",
    " http://bridge", "http://bridge\n", "http://bridge\\evil", "http://[::1", "http://%65vil"])
def test_invalid_bridge_origins_fail_closed(url):
    with pytest.raises(ValueError, match="CODEX_BRIDGE_URL"):
        Settings(admin_password="test", codex_bridge_url=url, codex_bridge_token=TOKEN)


@pytest.mark.parametrize("options", [
    {"codex_bridge_url": BRIDGE}, {"codex_bridge_token": TOKEN},
    {"codex_bridge_url": None}, {"codex_bridge_token": None},
    {"codex_bridge_url": BRIDGE, "codex_bridge_token": "a" * 31},
    {"codex_bridge_url": BRIDGE, "codex_bridge_token": "a" * 257},
    {"codex_bridge_url": BRIDGE, "codex_bridge_token": "g" * 64},
    {"codex_bridge_url": BRIDGE, "codex_bridge_token": TOKEN + "\n"},
])
def test_invalid_settings_and_repr(options):
    with pytest.raises(ValueError, match="CODEX_BRIDGE") as error:
        Settings(admin_password="test", **options)
    assert TOKEN not in str(error.value)


@pytest.mark.parametrize("url", [BRIDGE, "https://bridge.example:443", "http://[::1]:8090"])
@pytest.mark.parametrize("length", [32, 256])
def test_environment_origin_and_token(url, length, monkeypatch):
    token = ("aB12" * 64)[:length]
    monkeypatch.setenv("ADMIN_PASSWORD", "test")
    monkeypatch.setenv("CODEX_BRIDGE_URL", url)
    monkeypatch.setenv("CODEX_BRIDGE_TOKEN", token)
    settings = Settings.from_env()
    assert settings.codex_bridge_url == url and settings.codex_bridge_token == token
    assert token not in repr(settings)


@pytest.mark.parametrize("length", [31, 257])
def test_invalid_environment_token_fails_fast(length, monkeypatch):
    token = "a" * length
    monkeypatch.setenv("ADMIN_PASSWORD", "test")
    monkeypatch.setenv("CODEX_BRIDGE_URL", BRIDGE)
    monkeypatch.setenv("CODEX_BRIDGE_TOKEN", token)
    with pytest.raises(ValueError, match="32 to 256 hexadecimal characters") as error:
        Settings.from_env()
    assert token not in str(error.value)


def test_default_settings_do_not_access_a_token_file(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "test")

    def forbidden(*args, **kwargs):
        pytest.fail("Standalone defaults must not access credential files")

    monkeypatch.setattr("backend.private_token.os.open", forbidden)
    settings = Settings.from_env()
    assert settings.codex_bridge_url == settings.codex_bridge_token == ""


def test_backend_consumes_token_file_once_at_startup(tmp_path, monkeypatch, caplog):
    path = tmp_path / "token"
    path.write_text(TOKEN + "\n")
    path.chmod(0o440)
    monkeypatch.setenv("ADMIN_PASSWORD", "test-admin-password")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "db.sqlite3"))
    monkeypatch.setenv("STT_ENABLED", "false")
    monkeypatch.setenv("CODEX_BRIDGE_URL", BRIDGE)
    monkeypatch.setenv("CODEX_BRIDGE_TOKEN_FILE", str(path))
    app = create_app()
    with TestClient(app) as client:
        assert app.state.settings.codex_bridge_token == TOKEN
        assert TOKEN not in repr(app.state.settings)
        assert not app.state.codex_research.sessions
        path.unlink()

        def forbidden(*args):
            pytest.fail("Ordinary requests must not reread the token file")

        monkeypatch.setattr("backend.config.load_token_file", forbidden)
        research = app.state.codex_research
        calls = []

        def handler(request):
            calls.append(request)
            assert request.headers["authorization"] == "Bearer " + TOKEN
            return bridge(request)

        for http_client in (research.client, research.lifecycle_client):
            client.portal.call(http_client._transport.aclose)
            http_client._transport = httpx.MockTransport(handler)
        headers = login(client)
        assert client.get(ROOT + "/status", headers=headers).json() == disconnected()
        assert not calls
        assert connect(client, headers).status_code == 200
        assert client.get(ROOT + "/status", headers=headers).status_code == 200
        assert client.delete(ROOT + "/connection", headers=headers).status_code == 200
    assert TOKEN not in caplog.text


def test_backend_rejects_ambiguous_token_sources_before_reading(monkeypatch):
    monkeypatch.setenv("CODEX_BRIDGE_TOKEN", TOKEN)
    monkeypatch.setenv("CODEX_BRIDGE_TOKEN_FILE", "/private/not-read")
    monkeypatch.setattr("backend.config.load_token_file", lambda *args: pytest.fail("Ambiguous source must not be read"))
    with pytest.raises(ValueError, match="Configure only one") as error:
        Settings.from_env()
    assert TOKEN not in str(error.value) and "/private/not-read" not in str(error.value)


@pytest.mark.parametrize("kind", ["missing", "invalid", "insecure"])
def test_backend_token_file_failures_are_generic(tmp_path, monkeypatch, kind):
    path = tmp_path / "private-token-path"
    if kind != "missing":
        path.write_text(TOKEN if kind == "insecure" else "private invalid content")
        path.chmod(0o444 if kind == "insecure" else 0o440)
    monkeypatch.setenv("ADMIN_PASSWORD", "test")
    monkeypatch.setenv("CODEX_BRIDGE_URL", BRIDGE)
    monkeypatch.setenv("CODEX_BRIDGE_TOKEN_FILE", str(path))
    with pytest.raises(ValueError, match="^Invalid CODEX_BRIDGE_TOKEN_FILE$"):
        Settings.from_env()


def test_operator_only_no_cookie_wearer_query_or_bridge_bearer(make_codex):
    client, operator, calls = make_codex(stream_enabled=True)
    code = client.post("/api/pairings", headers=operator).json()["code"]
    token = client.post("/api/pair", json={"code": code, "name": "Wearer"}).json()["token"]
    wearer = {"Authorization": "Bearer " + token}
    client.post("/api/stream/playback-session", headers=operator)
    cookie = {"Cookie": COOKIE + "=" + client.cookies[COOKIE]}
    for method, path in [("GET", "/status"), ("POST", "/login"), ("DELETE", "/connection"),
                         ("GET", "/models"), ("POST", "/chat")]:
        for headers in ({}, wearer, cookie, {"Authorization": "Bearer " + TOKEN}):
            response = client.request(method, ROOT + path, headers=headers, json={})
            assert response.status_code == 401 and response.headers["cache-control"] == "no-store"
    assert client.get(ROOT + "/status", params={"token": operator["Authorization"].split()[1]}).status_code == 401
    assert not calls


def test_ram_isolation_private_fields_and_idempotent_login(make_codex, caplog):
    def handler(request):
        if request.url.path.endswith(("/login", "/status")):
            return httpx.Response(200, json=status(email=SECRET, access_token=TOKEN))
        return bridge(request)

    client, one, calls = make_codex(handler, openai_api_key="paid-server-key")
    two = login(client)
    response = connect(client, one)
    assert response.json() == {"enabled": True, **status()}
    for name, value in [("cache-control", "no-store"), ("referrer-policy", "no-referrer")]:
        assert response.headers[name] == value
    research = client.app.state.codex_research
    first = research.sessions[parent(one)]
    connect(client, one)
    assert research.sessions[parent(one)] is first
    assert calls[0].url.path == calls[1].url.path
    assert client.get(ROOT + "/status", headers=two).json() == disconnected()
    connect(client, two)
    second = research.sessions[parent(two)]
    assert first.identity != second.identity and first.identity != parent(one)
    assert first.identity not in one["Authorization"] and first.identity != TOKEN
    assert client.post(ROOT + "/chat", headers=one, json=payload()).status_code == 200
    dump = "\n".join(client.app.state.store.db.iterdump())
    for private in (TOKEN, SECRET, first.identity, "Only this explicit question", "Codex answer"):
        assert private not in response.text and private not in dump
    assert TOKEN not in caplog.text and SECRET not in caplog.text
    assert first.identity not in repr(research.sessions)
    assert client.delete(ROOT + "/connection", headers=one).json() == disconnected()
    assert not first.results and first.models is None
    assert research.sessions[parent(two)] is second
    assert client.get(ROOT + "/models", headers=one).status_code == 409


@pytest.mark.parametrize("data", [{"consent": True}, {"url": "https://evil.example"}, {"api_key": SECRET},
                                 {"bridge_token": TOKEN}, [], None, "", 3])
def test_explicit_login_requires_strict_empty_object(make_codex, data):
    client, headers, calls = make_codex()
    response = client.post(ROOT + "/login", headers={**headers, "Content-Type": "application/json"},
                           content=json.dumps(data))
    assert response.status_code == 422 and not calls
    assert SECRET not in response.text and TOKEN not in response.text
    assert client.get(ROOT + "/status", headers=headers).json() == disconnected()
    assert client.get(ROOT + "/models", headers=headers).status_code == 409
    assert client.post(ROOT + "/chat", headers=headers, json=payload()).status_code == 409
    assert not calls and not client.app.state.codex_research.sessions


def test_invalid_logins_do_not_reserve_bridge_capacity(make_codex):
    client, one, calls = make_codex()
    two, three, four = login(client), login(client), login(client)
    research = client.app.state.codex_research
    for headers in (one, two):
        assert client.post(ROOT + "/login", headers=headers, json={"api_key": SECRET}).status_code == 422
        assert client.post(ROOT + "/login", headers={**headers, "Content-Type": "application/json"},
                           content=b"{").status_code == 422
    assert not research.sessions and not research.retired and not calls
    connect(client, three)
    connect(client, four)
    assert len(research.sessions) == 2
    before = dict(research.sessions)
    for headers in (one, two, three, four):
        assert client.post(ROOT + "/login", headers=headers, json=[]).status_code == 422
    assert research.sessions == before and len(calls) == 2
    assert client.post("/api/logout", headers=one).status_code == 204
    assert parent(one) not in research.login_guards


def test_login_body_bounds_and_pending_device_url(make_codex):
    client, headers, calls = make_codex(lambda request: httpx.Response(200, json=status(
        state="pending", verification_url=DEVICE_URL, user_code="ABCD-EFGH", generation_enabled=False)))
    assert client.post(ROOT + "/login", headers=headers).status_code == 415
    assert client.post(ROOT + "/login", headers={**headers, "Content-Type": "application/json"},
                       content=" " * 65537).status_code == 413
    assert client.post(ROOT + "/login", headers={**headers, "Content-Encoding": "gzip"}, json={}).status_code == 415
    assert not calls and not client.app.state.codex_research.sessions
    result = connect(client, headers).json()
    assert result["verification_url"] == DEVICE_URL and result["user_code"] == "ABCD-EFGH"
    assert client.get(ROOT + "/models", headers=headers).status_code == 409
    assert client.post(ROOT + "/chat", headers=headers, json=payload()).status_code == 409
    assert all(request.url.path.endswith(("/login", "/status")) for request in calls)


@pytest.mark.parametrize("change", [
    {"verification_url": "https://evil.example/codex/device"},
    {"verification_url": DEVICE_URL + "?token=" + TOKEN},
    {"verification_url": DEVICE_URL + "#secret"}, {"verification_url": DEVICE_URL + "/"},
    {"verification_url": "https://auth.openai.com.evil.example/codex/device"},
    {"verification_url": "https://auth.openai.com@evil.example/codex/device"},
    {"verification_url": "http://auth.openai.com/codex/device"},
    {"verification_url": "https://auth.openai.com:443/codex/device"},
    {"verification_url": "https://auth.openai.com/codex/%64evice"},
    {"verification_url": [DEVICE_URL]}, {"verification_url": None},
    {"user_code": SECRET}, {"user_code": "a" * 33}, {"user_code": "CODE\n"}, {"user_code": 123},
    {"user_code": None}, {"state": "connected"}, {"state": []}, {"state": "invented"},
    {"generation_enabled": 1},
])
def test_untrusted_status_urls_codes_and_states_rejected(make_codex, change):
    invalid = {**status(state="pending", verification_url=DEVICE_URL, user_code="ABCD-EFGH"), **change}
    client, headers, _ = make_codex(lambda request: bridge(request) if request.method == "DELETE"
                                  else httpx.Response(200, json=invalid))
    response = client.post(ROOT + "/login", headers=headers, json={})
    assert response.status_code == 502
    assert TOKEN not in response.text and SECRET not in response.text and "evil.example" not in response.text


@pytest.mark.parametrize("enabled", [False, True])
def test_generation_gate_and_model_discovery(make_codex, enabled):
    def handler(request):
        if request.url.path.endswith(("/login", "/status")):
            return httpx.Response(200, json=status(generation_enabled=enabled))
        return bridge(request)

    client, headers, calls = make_codex(handler, openai_api_key="paid-server-key")
    connect(client, headers)
    models = client.get(ROOT + "/models", headers=headers)
    response = client.post(ROOT + "/chat", headers=headers, json=payload())
    if not enabled:
        assert models.status_code == response.status_code == 409
        assert not any(request.url.path.endswith(("/models", "/chat")) for request in calls)
    else:
        assert models.json() == {"models": [{"id": MODEL, "image": True}, {"id": "text-only", "image": False}]}
        assert response.status_code == 200
        assert client.post(ROOT + "/chat", headers=headers, json=payload(model="invented")).status_code == 422
        assert client.post(ROOT + "/chat", headers=headers, json=payload(model="text-only",
            messages=[{"role": "user", "images": [jpeg()]}])).status_code == 422
        assert sum(request.url.path.endswith("/chat") for request in calls) == 1
        assert sum(request.url.path.endswith("/models") for request in calls) == 1
        now = client.app.state.credentials.now()
        client.app.state.credentials.now = lambda: now + MODEL_TTL + 1
        assert client.get(ROOT + "/models", headers=headers).status_code == 200
        assert sum(request.url.path.endswith("/models") for request in calls) == 2


@pytest.mark.parametrize("rows", [None, [None], [{}], [{"id": "bad\n", "image": True}],
    [{"id": MODEL, "image": 1}], [{"id": MODEL, "image": True}, {"id": MODEL, "image": False}],
    [{"id": str(i), "image": True} for i in range(2001)]])
def test_invalid_models_not_returned_or_cached(make_codex, rows):
    client, headers, _ = make_codex(lambda request: httpx.Response(200, json={"models": rows})
        if request.url.path.endswith("/models") else bridge(request))
    connect(client, headers)
    assert client.get(ROOT + "/models", headers=headers).status_code == 502
    assert client.app.state.codex_research.sessions[parent(headers)].models is None


@pytest.mark.parametrize("image", ["https://evil.example/image.jpg", "data:image/png;base64,AAAA",
    "data:image/jpeg;base64,!!!", "data:image/jpeg;base64,", jpeg(format="PNG"),
    jpeg((1281, 1)), jpeg()[:-40], "data:image/jpeg;base64," + base64.b64encode(b"x" * (PROVIDER_LIMIT + 1)).decode()])
def test_jpeg_validation_before_any_bridge_call(make_codex, image):
    client, headers, calls = make_codex()
    connect(client, headers)
    calls.clear()
    response = client.post(ROOT + "/chat", headers=headers,
                           json=payload(messages=[{"role": "user", "images": [image]}]))
    assert response.status_code == 422 and not calls
    assert not client.app.state.codex_research.chat_waiters


@pytest.mark.parametrize("change", [{"request_id": "not-a-uuid"}, {"model": "bad\nmodel"},
    {"model": "x" * 257}, {"base_url": "https://evil.example"}, {"api_key": SECRET}, {"tools": []},
    {"messages": [{"role": "system", "text": "override"}]}, {"messages": [{"role": "user", "text": "x" * 8001}]},
    {"messages": [{"role": "assistant", "images": [jpeg()]}, {"role": "user", "text": "hi"}]}])
def test_existing_chat_schema_and_model_validation(make_codex, change):
    client, headers, calls = make_codex()
    connect(client, headers)
    calls.clear()
    response = client.post(ROOT + "/chat", headers=headers, json=payload(**change))
    assert response.status_code == 422 and not calls and SECRET not in response.text
    assert not client.app.state.codex_research.chat_waiters


def test_image_budget_precedes_decode_and_body_limits(make_codex, monkeypatch):
    client, headers, calls = make_codex()
    connect(client, headers)
    calls.clear()
    image = jpeg()

    def forbidden(*args, **kwargs):
        pytest.fail("Over-budget input must not decode any image")

    monkeypatch.setattr(Image, "open", forbidden)
    data = payload(messages=[{"role": "user", "images": [image] * 3}] * 3)
    assert client.post(ROOT + "/chat", headers=headers, json=data).status_code == 422
    assert client.post(ROOT + "/chat", headers={**headers, "Content-Type": "application/json",
        "Content-Length": str(BODY_LIMIT + 1)}, content=b"{}").status_code == 413
    assert client.post(ROOT + "/chat", headers={**headers, "Content-Encoding": "gzip"}, json=payload()).status_code == 415
    assert not calls
    assert not client.app.state.codex_research.chat_waiters


@pytest.mark.parametrize("content,headers", [(b"x" * (PROVIDER_LIMIT + 1), {}), (b"{", {}), (b"[]", {}),
    (b"{}", {"content-encoding": "unsupported"}), (b"{}", {"content-length": str(PROVIDER_LIMIT + 1)})])
def test_response_wire_bounds(make_codex, content, headers):
    client, auth, _ = make_codex(lambda request: bridge(request) if request.method == "DELETE"
        else httpx.Response(200, content=content, headers=headers))
    assert client.post(ROOT + "/login", headers=auth, json={}).status_code == 502


@pytest.mark.parametrize("change", [{"request_id": str(uuid4())}, {"model": "other"}, {"text": " "},
    {"text": 12}, {"text": "x" * (PROVIDER_LIMIT + 1)}, {"incomplete": 1}, {"usage": {}},
    {"usage": {"input_tokens": True, "output_tokens": 2, "total_tokens": 3}},
    {"usage": {"input_tokens": -1, "output_tokens": 2, "total_tokens": 3}}])
def test_chat_response_validation_no_cached_failure(make_codex, change):
    client, headers, calls = make_codex(lambda request: httpx.Response(200,
        json=answer(json.loads(request.content), **change)) if request.url.path.endswith("/chat") else bridge(request))
    connect(client, headers)
    response = client.post(ROOT + "/chat", headers=headers, json=payload())
    assert response.status_code == 502
    assert not client.app.state.codex_research.sessions[parent(headers)].results
    assert sum(request.url.path.endswith("/chat") for request in calls) == 1
    assert not client.app.state.codex_research.chat_waiters


def test_current_chat_wire_allowlist_partial_and_truncation(make_codex):
    def handler(request):
        if request.url.path.endswith("/chat"):
            return httpx.Response(200, json=answer(json.loads(request.content), text="x" * 17000,
                usage={"input_tokens": 2, "output_tokens": 3, "total_tokens": 5, "private": SECRET},
                account=SECRET, trace=SECRET, token=TOKEN))
        return bridge(request)

    client, headers, calls = make_codex(handler)
    connect(client, headers)
    data = payload(messages=[{"role": "user", "text": "still frames only", "images": [jpeg()]},
                             {"role": "assistant", "text": "prior"}, {"role": "user", "text": "now"}])
    response = client.post(ROOT + "/chat", headers=headers, json=data)
    result = response.json()
    assert response.status_code == 200 and result["incomplete"] and len(result["text"]) == 16000
    assert set(result) == {"request_id", "model", "text", "incomplete", "usage"}
    assert set(result["usage"]) == {"input_tokens", "output_tokens", "total_tokens"}
    assert SECRET not in response.text and TOKEN not in response.text
    assert json.loads(calls[-1].content) == Chat(**data).model_dump(mode="json")
    assert client.app.state.store.db.execute("SELECT count(*) FROM messages").fetchone()[0] == 0


@pytest.mark.parametrize("code,expected", [(400, 422), (401, 502), (403, 502), (404, 422), (409, 409),
    (422, 422), (429, 429), (500, 502), (302, 502), (307, 502)])
def test_errors_sanitized_no_backend_resubmission_or_paid_fallback(make_codex, code, expected, caplog):
    def handler(request):
        if request.url.path.endswith("/chat"):
            return httpx.Response(code, text=SECRET + TOKEN + DEVICE_URL + "?private",
                                  headers={"Location": "https://evil.example/", "www-authenticate": TOKEN})
        return bridge(request)

    client, headers, calls = make_codex(handler, openai_api_key="paid-server-key")
    connect(client, headers)
    data = payload()
    for attempt in range(2):
        response = client.post(ROOT + "/chat", headers=headers, json=data)
        assert response.status_code == expected
        assert "plan allowance" in response.text and "No paid API fallback" in response.text
        assert "Codex may retry during sign-in recovery" in response.text
        assert "No automatic retry" not in response.text
        assert SECRET not in response.text and TOKEN not in response.text and "evil.example" not in response.text
        assert "www-authenticate" not in response.headers and "location" not in response.headers
        assert sum(request.url.path.endswith("/chat") for request in calls) == attempt + 1
    assert TOKEN not in caplog.text and SECRET not in caplog.text
    assert not client.app.state.codex_research.sessions[parent(headers)].results
    assert not client.app.state.codex_research.chat_waiters


@pytest.mark.parametrize("exception,expected", [(httpx.ReadTimeout, 504), (httpx.ConnectError, 503)])
def test_network_errors_sanitized(make_codex, exception, expected):
    def handler(request):
        raise exception(SECRET + TOKEN, request=request)

    client, headers, calls = make_codex(handler)
    response = client.post(ROOT + "/login", headers=headers, json={})
    assert response.status_code == expected and SECRET not in response.text and TOKEN not in response.text
    assert sum(request.url.path.endswith("/login") for request in calls) == 1
    assert not client.app.state.codex_research.sessions


def test_dedup_conflict_isolation_cache_limits_ttl_and_new_connection(make_codex):
    client, one, calls = make_codex()
    two = login(client)
    connect(client, one)
    connect(client, two)
    data = payload()
    first = client.post(ROOT + "/chat", headers=one, json=data)
    before = len(calls)
    assert client.post(ROOT + "/chat", headers=one, json={**data, "request_id": data["request_id"].upper()}).json() == first.json()
    assert len(calls) == before
    assert client.post(ROOT + "/chat", headers=one, json={**data, "model": "text-only"}).status_code == 409
    assert client.post(ROOT + "/chat", headers=two, json=data).status_code == 200
    research = client.app.state.codex_research
    first_session = research.sessions[parent(one)]
    second_session = research.sessions[parent(two)]
    assert first_session.results[data["request_id"]][0] != second_session.results[data["request_id"]][0]
    for _ in range(8):
        assert client.post(ROOT + "/chat", headers=one, json=payload()).status_code == 200
    assert len(first_session.results) == 8 and data["request_id"] not in first_session.results
    assert sum(len(session.results) for session in research.sessions.values()) <= 128
    now = research.credentials.now()
    # Isolate result expiry from the independently enforced, shorter bridge lease.
    research.credentials.now = lambda: now + RESULT_TTL
    first_session.lease_until = second_session.lease_until = now + RESULT_TTL + LEASE_TTL
    client.portal.call(research.credentials.prune)
    assert not first_session.results and not second_session.results
    assert client.delete(ROOT + "/connection", headers=one).status_code == 200
    client.portal.call(lambda: asyncio.gather(*research.cleanups.values()))
    connect(client, one)
    assert research.sessions[parent(one)].identity != first_session.identity
    assert client.post(ROOT + "/chat", headers=one, json=data).status_code == 200


def test_two_session_limit_and_shutdown_delete_all(make_codex):
    client, one, calls = make_codex()
    two, three = login(client), login(client)
    connect(client, one)
    connect(client, two)
    research = client.app.state.codex_research
    ids = {session.identity for session in research.sessions.values()}
    assert client.post(ROOT + "/login", headers=three, json={}).status_code == 429
    client.portal.call(research.aclose)
    deleted = {request.url.path.split("/")[2] for request in calls if request.method == "DELETE"}
    assert deleted == ids
    assert research.closed and research.sweeper.done() and research.client.is_closed and research.lifecycle_client.is_closed
    assert not research.sessions and not research.tasks and not research.retired and not research.cleanups
    assert len(research.credentials.prune_hooks) == 1


@pytest.mark.parametrize("action,expected", [("logout", 401), ("expiry", 401), ("disconnect", 200), ("lease", 200)])
def test_revocation_prunes_and_deletes_without_leasing_old_ids(make_codex, action, expected):
    client, headers, calls = make_codex()
    connect(client, headers)
    assert client.post(ROOT + "/chat", headers=headers, json=payload()).status_code == 200
    research = client.app.state.codex_research
    old = research.sessions[parent(headers)]
    if action == "logout":
        assert client.post("/api/logout", headers=headers).status_code == 204
    elif action == "expiry":
        research.credentials.operators[parent(headers)] = research.credentials.now()
    elif action == "disconnect":
        assert client.delete(ROOT + "/connection", headers=headers).json() == disconnected()
    else:
        now = research.credentials.now()
        research.credentials.now = lambda: now + LEASE_TTL
    assert client.get(ROOT + "/status", headers=headers).status_code == expected
    client.portal.call(research.renew_leases)
    client.portal.call(lambda: asyncio.gather(*research.cleanups.values()))
    assert parent(headers) not in research.sessions and not old.results and old.models is None
    assert any(request.method == "DELETE" and old.identity in request.url.path for request in calls)
    assert not any(request.url.path == "/lease" for request in calls)


def test_leases_existing_only_absolute_expiry_and_failed_cleanup_ttl(make_codex):
    def handler(request):
        if request.method == "DELETE":
            return httpx.Response(503, text=SECRET)
        return bridge(request)

    client, one, calls = make_codex(handler)
    two = login(client)
    connect(client, one)
    connect(client, two)
    research = client.app.state.codex_research
    first, second = research.sessions[parent(one)], research.sessions[parent(two)]
    now = research.credentials.now()
    assert first.expires <= now + SESSION_TTL
    research.credentials.now = lambda: now + LEASE_INTERVAL
    client.portal.call(research.renew_leases)
    lease = calls[-1]
    assert lease.url.path == "/lease"
    assert set(json.loads(lease.content)["sessions"]) == {first.identity, second.identity}
    assert first.lease_until == second.lease_until == now + LEASE_INTERVAL + LEASE_TTL
    assert client.delete(ROOT + "/connection", headers=one).json() == disconnected()
    client.portal.call(lambda: asyncio.gather(*research.cleanups.values()))
    assert first.identity in research.retired
    assert client.post(ROOT + "/login", headers=one, json={}).status_code == 429
    client.portal.call(research.renew_leases)
    assert json.loads(calls[-1].content) == {"sessions": [second.identity]}
    research.credentials.now = lambda: now + LEASE_INTERVAL + LEASE_TTL
    client.portal.call(research.credentials.prune)
    assert first.identity not in research.retired
    assert not research.sessions


def test_pending_dedup_cancelled_waiter_and_lifecycle_not_starved(make_codex):
    client, one, calls = make_codex()
    two = login(client)
    connect(client, one)
    connect(client, two)

    async def exercise():
        research = client.app.state.codex_research
        entered, release = asyncio.Queue(), asyncio.Event()
        original = research.client._transport.handler

        async def handler(request):
            if request.url.path.endswith("/chat"):
                entered.put_nowait(True)
                await release.wait()
            return await original(request)

        research.client._transport = httpx.MockTransport(handler)
        data = Chat(**payload())
        first = asyncio.create_task(research.chat(parent(one), research.session(parent(one)), data))
        await asyncio.wait_for(entered.get(), 1)
        duplicates = [asyncio.create_task(research.chat(parent(one), research.session(parent(one)), data)) for _ in range(5)]
        second = asyncio.create_task(research.chat(parent(two), research.session(parent(two)), data))
        try:
            await asyncio.wait_for(entered.get(), 1)
            with pytest.raises(HTTPException) as error:
                await research.chat(parent(one), research.session(parent(one)), Chat(**payload()))
            assert error.value.status_code == 429
            with pytest.raises(HTTPException) as error:
                await research.chat(parent(one), research.session(parent(one)), Chat(**{**data.model_dump(mode="json"), "model": "text-only"}))
            assert error.value.status_code == 409
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            assert len(research.tasks) == 2 and entered.empty()
            await asyncio.wait_for(research.renew_leases(), 1)
            assert calls[-1].url.path == "/lease"
            release.set()
            results = await asyncio.gather(*duplicates, second)
            assert all(result == results[0] for result in results)
            assert sum(request.url.path.endswith("/chat") for request in calls) == 2
            assert not research.tasks and len(research.sessions[parent(one)].results) == 1
        finally:
            release.set()
            await asyncio.gather(first, second, *duplicates, return_exceptions=True)

    client.portal.call(exercise)


@pytest.mark.parametrize("operation", ["login", "status", "models", "chat"])
@pytest.mark.parametrize("action,expected", [("logout", 401), ("expiry", 401), ("disconnect", 409)])
def test_inflight_revocation_discards_late_replies_and_keeps_slot(make_codex, operation, action, expected):
    client, headers, calls = make_codex()
    if operation != "login":
        connect(client, headers)

    async def exercise():
        research = client.app.state.codex_research
        entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        original = research.client._transport.handler

        async def handler(request):
            if request.url.path.endswith("/" + operation):
                entered.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    await release.wait()
            return await original(request)

        research.client._transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client.app), base_url="http://testserver") as api:
            pending = asyncio.create_task(api.request("POST" if operation in {"chat", "login"} else "GET",
                ROOT + "/" + operation, headers=headers, json=payload() if operation == "chat" else {}))
            try:
                await asyncio.wait_for(entered.wait(), 1)
                old = research.sessions[parent(headers)]
                if action == "logout":
                    assert (await api.post("/api/logout", headers=headers)).status_code == 204
                elif action == "expiry":
                    research.credentials.operators[parent(headers)] = research.credentials.now()
                    research.credentials.prune()
                else:
                    assert (await api.delete(ROOT + "/connection", headers=headers)).json() == disconnected()
                await asyncio.wait_for(cancelled.wait(), 1)
                assert parent(headers) in research.tasks and parent(headers) not in research.sessions
                await asyncio.wait_for(research.renew_leases(), 1)
                assert any(request.method == "DELETE" and old.identity in request.url.path for request in calls)
                if action == "disconnect":
                    assert (await api.post(ROOT + "/login", headers=headers, json={})).status_code == 429
                release.set()
                response = await asyncio.wait_for(pending, 1)
                assert response.status_code == expected
                await asyncio.gather(*research.cleanups.values())
                assert not old.results and old.models is None and not research.tasks
                assert not research.sessions and not research.retired
            finally:
                release.set()
                await asyncio.gather(pending, return_exceptions=True)

    client.portal.call(exercise)


def test_pixel_worker_cancellation_admission_and_stale_disconnect(make_codex, monkeypatch):
    client, one, calls = make_codex()
    two = login(client)
    connect(client, one)
    connect(client, two)
    image = jpeg()
    entered, release = threading.Event(), threading.Event()
    original = Image.Image.load

    def load(self, *args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Image.Image, "load", load)

    async def exercise():
        research = client.app.state.codex_research
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client.app), base_url="http://testserver") as api:
            pending = asyncio.create_task(api.post(ROOT + "/chat", headers=one,
                json=payload(messages=[{"role": "user", "images": [image]}])))
            try:
                assert await asyncio.to_thread(entered.wait, 2)
                worker = research.validations[parent(one)]
                assert worker is not None and not research.tasks
                assert (await asyncio.wait_for(api.get(ROOT + "/status", headers=two), 1)).status_code == 200
                pending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await pending
                assert not worker.done() and research.validations[parent(one)] is worker
                assert not research.chat_waiters
                assert (await api.post(ROOT + "/chat", headers=one, json=payload())).status_code == 429
                assert (await api.delete(ROOT + "/connection", headers=one)).status_code == 200
                release.set()
                await asyncio.wait_for(asyncio.shield(worker), 1)
                assert not research.validations and parent(one) not in research.sessions
                assert not any(request.url.path.endswith("/chat") for request in calls)
            finally:
                release.set()
                await asyncio.gather(pending, return_exceptions=True)

    client.portal.call(exercise)


def test_total_deadline_and_no_timeout_result_cache(make_codex):
    client, headers, _ = make_codex(openai_timeout=0.02)
    connect(client, headers)

    async def exercise():
        research = client.app.state.codex_research

        async def handler(request):
            if request.url.path.endswith("/chat"):
                try:
                    await asyncio.sleep(1)
                except asyncio.CancelledError:
                    pass  # Even a transport suppressing cancellation cannot install a result.
            return bridge(request)

        research.client._transport = httpx.MockTransport(handler)
        session = research.session(parent(headers))
        with pytest.raises(HTTPException) as error:
            await research.chat(parent(headers), session, Chat(**payload()))
        assert error.value.status_code == 504
        assert "plan allowance" in error.value.detail and not session.results and not research.tasks

    client.portal.call(exercise)


def test_bridge_cookies_never_cross_concurrent_operator_requests(make_codex):
    client, one, _ = make_codex()
    two = login(client)

    async def exercise():
        research = client.app.state.codex_research
        entered, release = asyncio.Event(), asyncio.Event()
        count = 0

        class SlowStatus(httpx.AsyncByteStream):
            async def __aiter__(self):
                entered.set()
                await release.wait()
                yield json.dumps(status()).encode()

        async def handler(request):
            nonlocal count
            assert "cookie" not in request.headers
            if request.url.path.endswith("/login"):
                count += 1
                if count == 1:
                    return httpx.Response(200, headers={"Set-Cookie": "account=" + TOKEN + "; Path=/"},
                                          stream=SlowStatus())
            return bridge(request)

        research.client._transport = httpx.MockTransport(handler)
        first_session = research.session(parent(one), create=True)
        pending = asyncio.create_task(research.login(parent(one), first_session))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            assert not research.client.cookies
            result = await research.login(parent(two), research.session(parent(two), create=True))
            assert result["state"] == "connected"
            release.set()
            await pending
            assert not research.client.cookies
        finally:
            release.set()
            await asyncio.gather(pending, return_exceptions=True)

    client.portal.call(exercise)


@pytest.mark.parametrize("kind,expected", [("oversized", 502), ("error", 502), ("compressed", 502), ("slow", 504)])
def test_streaming_response_limits_and_unread_error_bodies(make_codex, kind, expected):
    reads = []

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            if kind in {"error", "compressed"}:
                pytest.fail("Do not consume untrusted error bodies or compressed responses")
            if kind == "slow":
                while True:
                    await asyncio.sleep(0.001)
                    yield b" "
            for _ in range(3):
                reads.append(True)
                yield b" " * (PROVIDER_LIMIT // 2 + 1)

    def handler(request):
        if request.method == "DELETE":
            return bridge(request)
        return httpx.Response(500 if kind == "error" else 200, stream=Stream(),
                              headers={"Content-Encoding": "gzip"} if kind == "compressed" else {})

    client, headers, _ = make_codex(handler, openai_timeout=0.03)
    assert client.post(ROOT + "/login", headers=headers, json={}).status_code == expected
    if kind == "oversized":
        assert len(reads) == 2


@pytest.mark.parametrize("state,generation", [("disconnected", True), ("failed", True), ("connected", False)])
def test_status_change_discards_old_models_and_results(make_codex, state, generation):
    current = status()

    def handler(request):
        return httpx.Response(200, json=current) if request.url.path.endswith(("/login", "/status")) else bridge(request)

    client, headers, calls = make_codex(handler)
    connect(client, headers)
    data = payload()
    assert client.post(ROOT + "/chat", headers=headers, json=data).status_code == 200
    research = client.app.state.codex_research
    session = research.sessions[parent(headers)]
    current.update(state=state, generation_enabled=generation)
    assert client.get(ROOT + "/status", headers=headers).json() == {"enabled": True, **current}
    assert not session.results and session.models is None
    assert client.post(ROOT + "/chat", headers=headers, json=data).status_code == 409
    assert sum(request.url.path.endswith("/chat") for request in calls) == 1
    if state != "connected":
        assert parent(headers) not in research.sessions
        client.portal.call(lambda: asyncio.gather(*research.cleanups.values()))
        current.update(state="connected", generation_enabled=True)
        connect(client, headers)
        assert research.sessions[parent(headers)].identity != session.identity
        assert client.post(ROOT + "/chat", headers=headers, json=data).status_code == 200
        assert sum(request.url.path.endswith("/chat") for request in calls) == 2


@pytest.mark.parametrize("operation", ["login", "relogin", "chat"])
@pytest.mark.parametrize("action,expected", [("disconnect", 409), ("logout", 401), ("expiry", 401), ("shutdown", 503)])
def test_revocation_during_body_receipt_cannot_start_or_send(make_codex, operation, action, expected):
    client, headers, calls = make_codex()
    if operation != "login":
        connect(client, headers)
    calls.clear()

    async def exercise():
        research = client.app.state.codex_research
        entered, release = asyncio.Event(), asyncio.Event()

        async def chunks():
            entered.set()
            await release.wait()
            yield json.dumps(payload() if operation == "chat" else {}).encode()

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client.app), base_url="http://testserver") as api:
            pending = asyncio.create_task(api.post(ROOT + ("/chat" if operation == "chat" else "/login"), content=chunks(),
                headers={**headers, "Content-Type": "application/json"}))
            try:
                await asyncio.wait_for(entered.wait(), 1)
                if operation == "login":
                    assert not research.sessions and not calls
                if action == "disconnect":
                    assert (await api.delete(ROOT + "/connection", headers=headers)).status_code == 200
                elif action == "logout":
                    assert (await api.post("/api/logout", headers=headers)).status_code == 204
                elif action == "expiry":
                    research.credentials.operators[parent(headers)] = research.credentials.now()
                    research.credentials.prune()
                else:
                    await research.aclose()
                release.set()
                assert (await asyncio.wait_for(pending, 1)).status_code == expected
                assert all(request.method == "DELETE" for request in calls)
                assert not research.sessions and not research.login_guards
                assert not research.chat_waiters
            finally:
                release.set()
                await asyncio.gather(pending, return_exceptions=True)

    client.portal.call(exercise)


def test_validation_admission_before_body_receipt_is_bounded(make_codex):
    client, one, calls = make_codex()
    two = login(client)
    connect(client, one)
    connect(client, two)

    async def exercise():
        research = client.app.state.codex_research
        entered, release = asyncio.Queue(), asyncio.Event()

        async def chunks():
            entered.put_nowait(True)
            await release.wait()
            yield json.dumps(payload()).encode()

        async def unread():
            pytest.fail("Rejected admission must not receive any body bytes")
            yield b""

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client.app), base_url="http://testserver") as api:
            pending = [asyncio.create_task(api.post(ROOT + "/chat", content=chunks(),
                headers={**headers, "Content-Type": "application/json"})) for headers in (one, two)]
            try:
                for _ in pending:
                    await asyncio.wait_for(entered.get(), 1)
                assert research.validations == {parent(one): None, parent(two): None}
                for headers in (one, two):
                    assert (await api.post(ROOT + "/chat", content=unread(),
                        headers={**headers, "Content-Type": "application/json"})).status_code == 429
                pending[0].cancel()
                with pytest.raises(asyncio.CancelledError):
                    await pending[0]
                assert parent(one) not in research.validations
                assert (await api.post(ROOT + "/chat", headers=one, json=payload())).status_code == 200
                release.set()
                assert (await pending[1]).status_code == 200
                assert not research.validations
                assert sum(request.url.path.endswith("/chat") for request in calls) == 2
            finally:
                release.set()
                await asyncio.gather(*pending, return_exceptions=True)

    client.portal.call(exercise)


@pytest.mark.parametrize("action,expected", [("logout", 401), ("expiry", 401), ("disconnect", 409), ("shutdown", 503)])
def test_revocation_waits_for_pixel_worker_and_never_sends_late(make_codex, monkeypatch, action, expected):
    client, headers, calls = make_codex()
    connect(client, headers)
    image = jpeg()
    entered, release = threading.Event(), threading.Event()
    original = Image.Image.load

    def load(self, *args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Image.Image, "load", load)

    async def exercise():
        research = client.app.state.codex_research
        closing = None
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client.app), base_url="http://testserver") as api:
            pending = asyncio.create_task(api.post(ROOT + "/chat", headers=headers,
                json=payload(messages=[{"role": "user", "images": [image]}])))
            try:
                assert await asyncio.to_thread(entered.wait, 2)
                worker = research.validations[parent(headers)]
                if action == "logout":
                    assert (await api.post("/api/logout", headers=headers)).status_code == 204
                elif action == "disconnect":
                    assert (await api.delete(ROOT + "/connection", headers=headers)).status_code == 200
                elif action == "expiry":
                    research.credentials.operators[parent(headers)] = research.credentials.now()
                    research.credentials.prune()
                else:
                    closing = asyncio.create_task(research.aclose())
                    await asyncio.sleep(0)
                    assert not closing.done()
                assert not research.sessions and not worker.done() and research.validations[parent(headers)] is worker
                release.set()
                assert (await asyncio.wait_for(pending, 1)).status_code == expected
                if closing:
                    await asyncio.wait_for(closing, 1)
                assert worker.done() and not worker.cancelled()
                assert not research.validations and not research.tasks
                assert not research.chat_waiters
                assert not any(request.url.path.endswith("/chat") for request in calls)
            finally:
                release.set()
                await asyncio.gather(pending, *([closing] if closing else []), return_exceptions=True)

    client.portal.call(exercise)


def test_failed_lease_does_not_extend_mapping_or_lease_expired_session(make_codex):
    def handler(request):
        if request.url.path == "/lease" or request.method == "DELETE":
            raise httpx.ReadTimeout(SECRET, request=request)
        return bridge(request)

    client, headers, calls = make_codex(handler)
    connect(client, headers)
    research = client.app.state.codex_research
    session = research.sessions[parent(headers)]
    deadline = session.lease_until
    research.credentials.now = lambda: deadline - LEASE_INTERVAL
    client.portal.call(research.renew_leases)
    assert session.lease_until == deadline and session.cleanup_until == deadline - LEASE_INTERVAL + LEASE_TTL
    research.credentials.now = lambda: deadline
    client.portal.call(research.renew_leases)
    client.portal.call(lambda: asyncio.gather(*research.cleanups.values()))
    assert not research.sessions and session.identity in research.retired
    assert sum(request.url.path == "/lease" for request in calls) == 1
    research.credentials.now = lambda: session.cleanup_until
    client.portal.call(research.credentials.prune)
    assert not research.retired


def test_queued_lease_filters_revoked_mapping_and_late_ack_cannot_restore_it(make_codex):
    client, headers, calls = make_codex()
    connect(client, headers)

    async def exercise():
        research = client.app.state.codex_research
        session = research.session(parent(headers))
        entered, release = asyncio.Event(), asyncio.Event()
        original = research.lifecycle_client._transport.handler

        async def handler(request):
            if request.url.path == "/lease":
                entered.set()
                await release.wait()
            return await original(request)

        research.lifecycle_client._transport = httpx.MockTransport(handler)
        first = asyncio.create_task(research.renew_leases())
        await asyncio.wait_for(entered.wait(), 1)
        queued = asyncio.create_task(research.renew_leases())
        try:
            old_deadline = session.lease_until
            research.clear(parent(headers))
            release.set()
            await asyncio.gather(first, queued, *research.cleanups.values())
            assert not research.sessions and not research.retired and session.lease_until == old_deadline
            assert sum(request.url.path == "/lease" for request in calls) == 1
            assert calls[-1].method == "DELETE"
        finally:
            release.set()
            await asyncio.gather(first, queued, return_exceptions=True)

    client.portal.call(exercise)


def test_sweeper_renews_only_live_and_enforces_absolute_session_expiry(make_codex):
    client, headers, calls = make_codex()
    research = client.app.state.codex_research
    now = research.credentials.now()
    research.credentials.operators[parent(headers)] = now + 2 * SESSION_TTL
    connect(client, headers)
    session = research.sessions[parent(headers)]
    assert now + SESSION_TTL <= session.expires < now + SESSION_TTL + 1
    research.credentials.now = lambda: now + LEASE_INTERVAL + 1

    async def wait_for_lease():
        async with asyncio.timeout(6):
            while not any(request.url.path == "/lease" for request in calls):
                await asyncio.sleep(0.01)

    client.portal.call(wait_for_lease)
    assert session.lease_until == now + LEASE_INTERVAL + 1 + LEASE_TTL
    research.credentials.now = lambda: session.expires
    session.lease_until = session.expires + LEASE_TTL
    assert client.get(ROOT + "/status", headers=headers).json() == disconnected()
    client.portal.call(lambda: asyncio.gather(*research.cleanups.values()))
    assert not research.sessions
    assert any(request.method == "DELETE" and session.identity in request.url.path for request in calls)


def test_http_duplicate_admission_bounds_large_payloads_before_receipt(make_codex, monkeypatch):
    client, headers, calls = make_codex()
    connect(client, headers)
    buffer = io.BytesIO()
    Image.effect_noise((1024, 1024), 100).convert("RGB").save(buffer, format="JPEG", quality=85)
    assert len(buffer.getvalue()) <= PROVIDER_LIMIT
    image = "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode()
    raw = json.dumps(payload(messages=[{"role": "user", "images": [image] * 3}] * 2)).encode()
    assert 3 * PROVIDER_LIMIT < len(raw) <= BODY_LIMIT

    async def exercise():
        research = client.app.state.codex_research
        entered, release = asyncio.Event(), asyncio.Event()
        validated, responses = asyncio.Queue(), asyncio.Queue()
        received = []
        original = research.client._transport.handler
        original_validate = research.validate_chat

        async def handler(request):
            if request.url.path.endswith("/chat"):
                assert not entered.is_set(), "Duplicate HTTP waiters must share one provider operation"
                entered.set()
                await release.wait()
            return await original(request)

        async def validate(*args):
            data = await original_validate(*args)
            validated.put_nowait(True)
            return data

        async def chunks(index):
            received.append(index)
            yield raw

        research.client._transport = httpx.MockTransport(handler)
        monkeypatch.setattr(research, "validate_chat", validate)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client.app), base_url="http://testserver") as api:
            async def send(index):
                response = await api.post(ROOT + "/chat", content=chunks(index),
                                          headers={**headers, "Content-Type": "application/json"})
                responses.put_nowait(response)
                return response

            pending = [asyncio.create_task(send(0))]
            try:
                await asyncio.wait_for(entered.wait(), 3)
                pending.extend(asyncio.create_task(send(index)) for index in range(1, 16))
                for _ in range(14):
                    assert (await asyncio.wait_for(responses.get(), 3)).status_code == 429
                for _ in range(2):
                    await asyncio.wait_for(validated.get(), 3)
                assert validated.empty() and len(received) == 2
                assert research.chat_waiters == {parent(headers): 2}
                assert not research.validations and len(research.tasks) == 1
                release.set()
                results = await asyncio.gather(*pending)
                successes = [response.json() for response in results if response.status_code == 200]
                assert len(successes) == 2 and successes[0] == successes[1]
                assert sum(request.url.path.endswith("/chat") for request in calls) == 1
                assert not research.chat_waiters
            finally:
                release.set()
                await asyncio.gather(*pending, return_exceptions=True)

    client.portal.call(exercise)


def test_http_waiter_cancellation_frees_admission_but_not_provider_slot(make_codex):
    client, headers, calls = make_codex()
    connect(client, headers)

    async def exercise():
        research = client.app.state.codex_research
        entered, release = asyncio.Event(), asyncio.Event()
        original = research.client._transport.handler

        async def handler(request):
            if request.url.path.endswith("/chat"):
                assert not entered.is_set()
                entered.set()
                await release.wait()
            return await original(request)

        research.client._transport = httpx.MockTransport(handler)
        data = payload()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client.app), base_url="http://testserver") as api:
            pending = asyncio.create_task(api.post(ROOT + "/chat", headers=headers, json=data))
            replacement = None
            try:
                await asyncio.wait_for(entered.wait(), 1)
                pending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await pending
                assert not research.chat_waiters and len(research.tasks) == 1
                replacement = asyncio.create_task(api.post(ROOT + "/chat", headers=headers, json=data))
                async with asyncio.timeout(1):
                    while not research.chat_waiters or research.validations:
                        await asyncio.sleep(0.001)
                assert research.chat_waiters == {parent(headers): 1} and len(research.tasks) == 1
                assert (await api.post("/api/logout", headers=headers)).status_code == 204
                assert (await asyncio.wait_for(replacement, 1)).status_code == 401
                assert not research.chat_waiters and not research.tasks and not research.sessions
                assert not any(request.url.path.endswith("/chat") for request in calls)
            finally:
                release.set()
                await asyncio.gather(pending, *([replacement] if replacement else []), return_exceptions=True)

    client.portal.call(exercise)


def test_http_waiter_accounting_survives_repeated_session_replacement(make_codex, monkeypatch):
    client, headers, calls = make_codex()
    connect(client, headers)

    async def exercise():
        research = client.app.state.codex_research
        validated = asyncio.Queue()
        releases = {}
        original_validate = research.validate_chat

        async def validate(parent, session, request, body):
            data = await original_validate(parent, session, request, body)
            release = releases.setdefault(session.identity, asyncio.Event())
            validated.put_nowait(session)
            await release.wait()
            return data

        async def unread():
            pytest.fail("Full HTTP waiter admission must reject before body receipt")
            yield b""

        monkeypatch.setattr(research, "validate_chat", validate)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client.app), base_url="http://testserver") as api:
            for _ in range(3):
                releases[research.sessions[parent(headers)].identity] = asyncio.Event()
                old_request = asyncio.create_task(api.post(ROOT + "/chat", headers=headers, json=payload()))
                new_request = None
                try:
                    old = await asyncio.wait_for(validated.get(), 1)
                    assert not research.validations and research.chat_waiters == {parent(headers): 1}
                    assert (await api.delete(ROOT + "/connection", headers=headers)).status_code == 200
                    await asyncio.gather(*research.cleanups.values())
                    assert (await api.post(ROOT + "/login", headers=headers, json={})).status_code == 200
                    new_request = asyncio.create_task(api.post(ROOT + "/chat", headers=headers, json=payload()))
                    new = await asyncio.wait_for(validated.get(), 1)
                    assert new is not old and research.chat_waiters == {parent(headers): 2}
                    assert (await api.post(ROOT + "/chat", content=unread(),
                        headers={**headers, "Content-Type": "application/json"})).status_code == 429
                    releases[old.identity].set()
                    assert (await asyncio.wait_for(old_request, 1)).status_code == 409
                    assert research.chat_waiters == {parent(headers): 1}
                    releases[new.identity].set()
                    assert (await asyncio.wait_for(new_request, 1)).status_code == 200
                    assert not research.chat_waiters
                finally:
                    for release in releases.values():
                        release.set()
                    await asyncio.gather(old_request, *([new_request] if new_request else []), return_exceptions=True)
        assert sum(request.url.path.endswith("/chat") for request in calls) == 3

    client.portal.call(exercise)


def test_global_http_waiter_limit_includes_revoked_sessions(make_codex, monkeypatch):
    client, one, calls = make_codex()
    two, three = login(client), login(client)
    connect(client, one)
    connect(client, two)

    async def exercise():
        research = client.app.state.codex_research
        validated, release = asyncio.Queue(), asyncio.Event()
        original_validate = research.validate_chat

        async def validate(identity, *args):
            data = await original_validate(identity, *args)
            if identity != parent(three):
                validated.put_nowait(True)
                await release.wait()
            return data

        async def unread():
            pytest.fail("Global HTTP waiter admission must reject before body receipt")
            yield b""

        monkeypatch.setattr(research, "validate_chat", validate)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client.app), base_url="http://testserver") as api:
            pending = []
            try:
                for headers in (one, one, two, two):
                    pending.append(asyncio.create_task(api.post(ROOT + "/chat", headers=headers, json=payload())))
                    await asyncio.wait_for(validated.get(), 1)
                assert research.chat_waiters == {parent(one): 2, parent(two): 2}
                assert not research.validations
                assert (await api.post("/api/logout", headers=one)).status_code == 204
                await asyncio.gather(*research.cleanups.values())
                assert (await api.post(ROOT + "/login", headers=three, json={})).status_code == 200
                assert (await api.post(ROOT + "/chat", content=unread(),
                    headers={**three, "Content-Type": "application/json"})).status_code == 429
                pending[0].cancel()
                with pytest.raises(asyncio.CancelledError):
                    await pending[0]
                assert sum(research.chat_waiters.values()) == 3
                assert (await api.post(ROOT + "/chat", headers=three, json=payload())).status_code == 200
                release.set()
                results = await asyncio.gather(*pending[1:])
                assert results[0].status_code == 401
                assert all(response.status_code in {200, 429} for response in results[1:])
                assert not research.chat_waiters
            finally:
                release.set()
                await asyncio.gather(*pending, return_exceptions=True)

    client.portal.call(exercise)


def test_lease_ack_renews_only_subset_and_immediately_releases_missing_capacity(make_codex):
    acknowledged = []

    def handler(request):
        if request.url.path == "/lease":
            return httpx.Response(200, json={"sessions": acknowledged})
        if request.method == "DELETE":
            return httpx.Response(503, text=SECRET)
        return bridge(request)

    client, one, calls = make_codex(handler)
    two, three = login(client), login(client)
    connect(client, one)
    connect(client, two)
    assert client.post(ROOT + "/chat", headers=two, json=payload()).status_code == 200
    research = client.app.state.codex_research
    first, second = research.sessions[parent(one)], research.sessions[parent(two)]
    acknowledged.append(first.identity)
    now = research.credentials.now()
    research.credentials.now = lambda: now + LEASE_INTERVAL
    before = len(calls)
    client.portal.call(research.renew_leases)
    assert first.lease_until == now + LEASE_INTERVAL + LEASE_TTL
    assert parent(two) not in research.sessions and second.identity not in research.retired
    assert not second.results and second.models is None and second.status == disconnected()
    assert len(calls) == before + 1 and calls[-1].url.path == "/lease"
    connect(client, three)
    assert len(research.sessions) == 2
    assert not any(request.method == "DELETE" for request in calls)


@pytest.mark.parametrize("lease_before_login", [False, True])
@pytest.mark.parametrize("login_before_ack", [False, True])
def test_missing_lease_ack_before_or_during_login_cannot_reap_creation(
        make_codex, lease_before_login, login_before_ack):
    client, headers, calls = make_codex()
    if lease_before_login:
        connect(client, headers)

    async def exercise():
        research = client.app.state.codex_research
        session = research.session(parent(headers), create=True)
        deadline = session.lease_until
        now = research.credentials.now()
        research.credentials.now = lambda: now + LEASE_INTERVAL
        login_entered, lease_entered = asyncio.Event(), asyncio.Event()
        create, acknowledge = asyncio.Event(), asyncio.Event()
        # A previous acknowledged session has already been reaped in the relogin case.
        remote = set()

        async def handler(request):
            calls.append(request)
            if request.url.path == "/lease":
                renewed = sorted(remote.intersection(json.loads(request.content)["sessions"]))
                lease_entered.set()
                await acknowledge.wait()
                return httpx.Response(200, json={"sessions": renewed})
            identity = request.url.path.split("/")[2]
            if request.url.path.endswith("/login"):
                login_entered.set()
                await create.wait()
                remote.add(identity)
            elif request.method == "DELETE":
                remote.discard(identity)
            return bridge(request)

        research.client._transport = httpx.MockTransport(handler)
        research.lifecycle_client._transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client.app), base_url="http://testserver") as api:
            pending = renewing = None
            try:
                if lease_before_login:
                    renewing = asyncio.create_task(research.renew_leases())
                    await asyncio.wait_for(lease_entered.wait(), 1)
                pending = asyncio.create_task(api.post(ROOT + "/login", headers=headers, json={}))
                await asyncio.wait_for(login_entered.wait(), 1)
                if not lease_before_login:
                    renewing = asyncio.create_task(research.renew_leases())
                    await asyncio.wait_for(lease_entered.wait(), 1)
                assert session.started and not remote
                if login_before_ack:
                    create.set()
                    response = await asyncio.wait_for(pending, 1)
                    assert response.status_code == 200
                acknowledge.set()
                await asyncio.wait_for(renewing, 1)
                assert research.sessions.get(parent(headers)) is session
                assert session.lease_until == deadline
                assert not research.retired and not research.cleanups
                if not login_before_ack:
                    assert not pending.done() and not research.tasks[parent(headers)].done()
                    create.set()
                    response = await asyncio.wait_for(pending, 1)
                assert response.status_code == 200 and response.json() == {"enabled": True, **status()}
                assert all(private not in response.text for private in (session.identity, TOKEN, SECRET))
                assert remote == {session.identity} and not research.tasks
                await research.renew_leases()
                assert session.lease_until == now + LEASE_INTERVAL + LEASE_TTL
                remote.clear()
                await research.renew_leases()
                assert not research.sessions and not research.retired and not research.cleanups
                assert not any(request.method == "DELETE" for request in calls)
            finally:
                create.set()
                acknowledge.set()
                await asyncio.gather(*(task for task in (pending, renewing) if task), return_exceptions=True)
                await asyncio.gather(*research.cleanups.values())

    client.portal.call(exercise)


@pytest.mark.parametrize("action,expected", [("logout", 401), ("disconnect", 409)])
def test_missing_lease_ack_preserves_late_login_cleanup_and_capacity(make_codex, action, expected):
    client, one, calls = make_codex()
    two, three = login(client), login(client)

    async def exercise():
        research = client.app.state.codex_research
        entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        deleted = asyncio.Queue()
        remote = set()

        async def handler(request):
            calls.append(request)
            if request.url.path == "/lease":
                return httpx.Response(200, json={
                    "sessions": sorted(remote.intersection(json.loads(request.content)["sessions"]))})
            identity = request.url.path.split("/")[2]
            if request.url.path.endswith("/login"):
                if not entered.is_set():
                    entered.set()
                    try:
                        await release.wait()
                    except asyncio.CancelledError:
                        cancelled.set()
                        await release.wait()
                remote.add(identity)
            elif request.method == "DELETE":
                remote.discard(identity)
                deleted.put_nowait(identity)
            return bridge(request)

        research.client._transport = httpx.MockTransport(handler)
        research.lifecycle_client._transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client.app), base_url="http://testserver") as api:
            pending = asyncio.create_task(api.post(ROOT + "/login", headers=one, json={}))
            try:
                await asyncio.wait_for(entered.wait(), 1)
                old = research.session(parent(one))
                deadline = old.lease_until
                now = research.credentials.now()
                research.credentials.now = lambda: now + LEASE_INTERVAL
                await research.renew_leases()
                assert research.sessions.get(parent(one)) is old and not cancelled.is_set()
                assert old.lease_until == deadline and not remote
                assert (await api.post(ROOT + "/login", headers=two, json={})).status_code == 200
                assert (await api.post(ROOT + "/login", headers=three, json={})).status_code == 429
                if action == "logout":
                    assert (await api.post("/api/logout", headers=one)).status_code == 204
                else:
                    assert (await api.delete(ROOT + "/connection", headers=one)).json() == disconnected()
                await asyncio.wait_for(cancelled.wait(), 1)
                assert await asyncio.wait_for(deleted.get(), 1) == old.identity
                assert old.identity not in remote and old.identity in research.retired
                assert parent(one) in research.tasks and not research.tasks[parent(one)].done()
                assert len(research.sessions) + len(research.retired) == 2
                assert (await api.post(ROOT + "/login", headers=three, json={})).status_code == 429
                if action == "disconnect":
                    assert (await api.post(ROOT + "/login", headers=one, json={})).status_code == 429
                release.set()
                response = await asyncio.wait_for(pending, 1)
                assert response.status_code == expected
                assert all(private not in response.text for private in (old.identity, TOKEN, SECRET))
                await asyncio.gather(*research.cleanups.values())
                assert await asyncio.wait_for(deleted.get(), 1) == old.identity
                assert deleted.empty() and old.identity not in remote
                assert not research.retired and not research.cleanups and not research.tasks
                assert (await api.post(ROOT + "/login", headers=three, json={})).status_code == 200
                assert len(remote) == len(research.sessions) == 2
            finally:
                release.set()
                await asyncio.gather(pending, return_exceptions=True)
                await asyncio.gather(*research.cleanups.values())

    client.portal.call(exercise)


def test_missing_lease_ack_cancels_chat_and_discards_stale_reply(make_codex):
    client, headers, calls = make_codex()
    connect(client, headers)

    async def exercise():
        research = client.app.state.codex_research
        entered, cancelled = asyncio.Event(), asyncio.Event()

        async def handler(request):
            calls.append(request)
            if request.url.path == "/lease":
                return httpx.Response(200, json={"sessions": []})
            if request.url.path.endswith("/chat"):
                entered.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.set()
            return bridge(request)

        research.client._transport = httpx.MockTransport(handler)
        research.lifecycle_client._transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client.app), base_url="http://testserver") as api:
            pending = asyncio.create_task(api.post(ROOT + "/chat", headers=headers, json=payload()))
            try:
                await asyncio.wait_for(entered.wait(), 1)
                old = research.session(parent(headers))
                await research.renew_leases()
                assert (await asyncio.wait_for(pending, 1)).status_code == 409
                assert cancelled.is_set() and not old.results and old.models is None
                assert not research.sessions and not research.retired and not research.cleanups
                assert not research.tasks and not research.chat_waiters
                assert not any(request.method == "DELETE" for request in calls)
            finally:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)

    client.portal.call(exercise)


@pytest.mark.parametrize("acknowledge_old", [True, False])
def test_late_lease_ack_neither_renews_nor_clears_replacement(make_codex, acknowledge_old):
    client, headers, calls = make_codex()
    connect(client, headers)

    async def exercise():
        research = client.app.state.codex_research
        entered, release = asyncio.Event(), asyncio.Event()
        old = research.session(parent(headers))
        old_deadline = old.lease_until
        now = research.credentials.now()
        research.credentials.now = lambda: now + LEASE_INTERVAL
        original = research.lifecycle_client._transport.handler

        async def handler(request):
            if request.url.path == "/lease":
                entered.set()
                await release.wait()
                return httpx.Response(200, json={"sessions": [old.identity] if acknowledge_old else []})
            return await original(request)

        research.lifecycle_client._transport = httpx.MockTransport(handler)
        renewing = asyncio.create_task(research.renew_leases())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client.app), base_url="http://testserver") as api:
            try:
                await asyncio.wait_for(entered.wait(), 1)
                assert (await api.delete(ROOT + "/connection", headers=headers)).status_code == 200
                research.credentials.now = lambda: now + LEASE_INTERVAL + 10
                assert (await api.post(ROOT + "/login", headers=headers, json={})).status_code == 200
                new = research.session(parent(headers))
                new_deadline = new.lease_until
                release.set()
                await asyncio.gather(renewing, *research.cleanups.values())
                assert research.sessions[parent(headers)] is new
                assert new.lease_until == new_deadline and old.lease_until == old_deadline
                assert not research.retired and not research.cleanups
                assert [request.url.path for request in calls if request.method == "DELETE"] == [f"/sessions/{old.identity}"]
            finally:
                release.set()
                await asyncio.gather(renewing, return_exceptions=True)

    client.portal.call(exercise)


@pytest.mark.parametrize("kind", ["missing", "extra_field", "not_list", "duplicates", "unknown", "mixed",
    "short", "uppercase", "newline", "nonstring", "legacy_204", "bad_json", "array", "oversized", "encoded"])
def test_invalid_lease_ack_never_partially_applies_or_extends_local_deadlines(make_codex, kind, caplog):
    def handler(request):
        if request.url.path != "/lease":
            return bridge(request)
        identities = json.loads(request.content)["sessions"]
        if kind == "legacy_204":
            return httpx.Response(204)
        if kind == "bad_json":
            return httpx.Response(200, content=b"{")
        if kind == "oversized":
            return httpx.Response(200, content=b" " * (PROVIDER_LIMIT + 1))
        if kind == "encoded":
            return httpx.Response(200, json={"sessions": identities}, headers={"Content-Encoding": "unsupported"})
        variants = {
            "missing": {}, "extra_field": {"sessions": [], "private": SECRET},
            "not_list": {"sessions": identities[0]}, "duplicates": {"sessions": [identities[0]] * 2},
            "unknown": {"sessions": ["a" * 64]}, "mixed": {"sessions": [identities[0], "a" * 64]},
            "short": {"sessions": ["a" * 63]}, "uppercase": {"sessions": ["F" * 64]},
            "newline": {"sessions": [identities[0] + "\n"]}, "nonstring": {"sessions": [None]}, "array": [],
        }
        return httpx.Response(200, json=variants[kind])

    client, one, _ = make_codex(handler)
    two = login(client)
    connect(client, one)
    connect(client, two)
    research = client.app.state.codex_research
    sessions = dict(research.sessions)
    deadlines = {parent: session.lease_until for parent, session in sessions.items()}
    now = research.credentials.now()
    research.credentials.now = lambda: now + LEASE_INTERVAL
    client.portal.call(research.renew_leases)
    assert research.sessions == sessions and not research.retired and not research.cleanups
    for parent, session in sessions.items():
        assert session.lease_until == deadlines[parent]
        assert session.cleanup_until == now + LEASE_INTERVAL + LEASE_TTL
    assert SECRET not in caplog.text and TOKEN not in caplog.text


def test_lease_ack_stream_is_bounded_before_json_validation(make_codex):
    reads = []

    class Oversized(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(3):
                reads.append(True)
                yield b" " * (PROVIDER_LIMIT // 2 + 1)

    client, headers, _ = make_codex(lambda request: httpx.Response(200, stream=Oversized())
        if request.url.path == "/lease" else bridge(request))
    connect(client, headers)
    research = client.app.state.codex_research
    session = research.sessions[parent(headers)]
    deadline = session.lease_until
    now = research.credentials.now()
    research.credentials.now = lambda: now + LEASE_INTERVAL
    client.portal.call(research.renew_leases)
    assert session.lease_until == deadline and research.sessions[parent(headers)] is session
    assert len(reads) == 2


@pytest.mark.parametrize("acknowledged", [True, False])
def test_ack_arriving_after_local_expiry_cannot_extend_lease(make_codex, acknowledged):
    def handler(request):
        if request.url.path == "/lease":
            research.credentials.now = lambda: deadline + 1
            return httpx.Response(200, json={"sessions": [session.identity] if acknowledged else []})
        if request.method == "DELETE":
            return httpx.Response(503, text=SECRET)
        return bridge(request)

    client, headers, _ = make_codex(handler)
    connect(client, headers)
    research = client.app.state.codex_research
    session = research.sessions[parent(headers)]
    deadline = session.lease_until
    research.credentials.now = lambda: deadline - 1
    client.portal.call(research.renew_leases)
    client.portal.call(lambda: asyncio.gather(*research.cleanups.values()))
    assert not research.sessions and session.lease_until == deadline
    # Acknowledged remote renewal needs cleanup/TTL fallback; an omitted ID is
    # already reaped, even when local expiry occurred while awaiting the ACK.
    assert (session.identity in research.retired) is acknowledged
    assert session.cleanup_until == deadline - 1 + LEASE_TTL


def test_sweeper_releases_failed_login_capacity_without_status_polling(make_codex):
    def handler(request):
        if request.url.path.endswith("/login"):
            return httpx.Response(200, json=status(state="pending", generation_enabled=False))
        if request.url.path == "/lease":
            return httpx.Response(200, json={"sessions": []})
        pytest.fail("No status polling or redundant cleanup is needed for a reaped bridge session")

    client, one, calls = make_codex(handler)
    two, three = login(client), login(client)
    connect(client, one)
    connect(client, two)
    assert client.post(ROOT + "/login", headers=three, json={}).status_code == 429
    research = client.app.state.codex_research
    now = research.credentials.now()
    research.credentials.now = lambda: now + LEASE_INTERVAL + 1

    async def wait_for_reaping():
        async with asyncio.timeout(6):
            while research.sessions:
                await asyncio.sleep(0.01)

    client.portal.call(wait_for_reaping)
    assert not research.retired and not research.cleanups
    assert [request.url.path for request in calls if not request.url.path.endswith("/login")] == ["/lease"]
    connect(client, three)
    # Reap the final mocked session so shutdown also performs no redundant DELETE.
    client.portal.call(research.renew_leases)
