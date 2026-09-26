import asyncio
import json
import threading
import time
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request
from starlette.websockets import WebSocketDisconnect

from backend.config import Settings
from backend.main import AUDIO_LIMIT, JSON_LIMIT, body, create_app, digest


ORIGIN = {"origin": "http://testserver"}


def send(client, headers, text="Hello", client_id=None, session_id=None):
    path = f"/api/sessions/{session_id}/reply" if session_id else "/api/messages"
    return client.post(path, headers=headers, json={"text": text, "client_id": client_id or str(uuid4())})


def auth_socket(socket, headers):
    socket.send_json({"type": "auth", "token": headers["Authorization"].split()[1]})
    return socket.receive_json()


def test_startup_fails_closed(monkeypatch):
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    with pytest.raises(ValueError, match="ADMIN_PASSWORD"):
        with TestClient(create_app()):
            pass
    with pytest.raises(ValueError, match="ADMIN_PASSWORD"):
        Settings(admin_password="  ")


def test_status_and_public_health(client, operator):
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/api/status", headers=operator).json() == {
        "stt_enabled": True, "stt_model": "base.en", "ai_configured": True,
        "ollama_model": "llama3.2:3b",
    }
    assert client.get("/docs").status_code == 404


def test_private_responses_are_not_cacheable(client, operator):
    for path, headers in (("/api/sessions", operator), ("/api/me", {}), ("/health", {})):
        response = client.get(path, headers=headers)
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert response.headers["x-content-type-options"] == "nosniff"


def test_auth_isolation_all_routes(client, operator, pair_wearer):
    session_id, wearer = pair_wearer()
    operator_routes = [
        ("GET", "/api/status"), ("GET", "/api/sessions"), ("POST", "/api/pairings"),
        ("GET", f"/api/sessions/{session_id}/messages"),
        ("POST", f"/api/sessions/{session_id}/reply"),
        ("POST", f"/api/sessions/{session_id}/suggest"),
        ("DELETE", f"/api/sessions/{session_id}"),
    ]
    wearer_routes = [("GET", "/api/me"), ("POST", "/api/messages"), ("POST", "/api/transcribe")]
    for method, path in operator_routes:
        for headers in ({}, wearer):
            assert client.request(method, path, headers=headers).status_code == 401
    for method, path in wearer_routes:
        for headers in ({}, operator):
            assert client.request(method, path, headers=headers).status_code == 401
    assert client.get("/api/me", params={"token": wearer["Authorization"].split()[1]}).status_code == 401
    assert client.get("/api/me", headers={"Authorization": "Basic anything"}).status_code == 401


def test_wearer_cannot_access_another_conversation(client, operator, pair_wearer):
    first, one = pair_wearer("One")
    second, two = pair_wearer("Two")
    sent = send(client, one, "Private to One").json()
    assert client.get("/api/me", headers=two).json() == {"session_id": second, "messages": []}
    assert client.get(f"/api/sessions/{first}/messages", headers=two).status_code == 401
    assert client.get(f"/api/sessions/{first}/messages", headers=operator).json() == [sent]
    assert client.post("/api/messages", headers=two, json={
        "text": "Inject", "client_id": str(uuid4()), "session_id": first}).status_code == 422


def test_login_expiry_and_no_plaintext_tokens(client, operator, pair_wearer):
    session_id, wearer = pair_wearer()
    credentials = client.app.state.credentials
    operator_token = operator["Authorization"].split()[1]
    wearer_token = wearer["Authorization"].split()[1]
    assert operator_token not in credentials.operators
    assert digest(operator_token) in credentials.operators
    row = client.app.state.store.session(session_id)
    assert row["token_hash"] == digest(wearer_token)
    assert wearer_token not in tuple(row)
    start = credentials.now()
    credentials.now = lambda: start + 8 * 60 * 60 + 1
    assert client.get("/api/sessions", headers=operator).status_code == 401
    assert client.get("/api/me", headers=wearer).status_code == 200


def test_pairing_expiry_and_single_use(client, operator):
    credentials = client.app.state.credentials
    now = credentials.now()
    credentials.now = lambda: now
    data = client.post("/api/pairings", headers=operator).json()
    assert data["expires_in"] == 300
    assert len(data["code"]) == 8
    assert data["code"] not in credentials.pairings
    payload = {"code": data["code"], "name": "Test"}
    credentials.now = lambda: now + 300
    assert client.post("/api/pair", json=payload).status_code == 401
    payload["code"] = client.post("/api/pairings", headers=operator).json()["code"]
    assert client.post("/api/pair", json=payload).status_code == 200
    assert client.post("/api/pair", json=payload).status_code == 401


@pytest.mark.parametrize("route,payload", [
    ("/api/login", {"password": "wrong"}),
    ("/api/pair", {"code": "wrong", "name": "Test"}),
])
def test_bruteforce_is_rate_limited(client, route, payload):
    for _ in range(5):
        assert client.post(route, json=payload).status_code == 401
    response = client.post(route, json=payload)
    assert response.status_code == 429
    assert response.headers["retry-after"] == "60"
    now = client.app.state.credentials.now()
    client.app.state.credentials.now = lambda: now + 61
    assert client.post(route, json=payload).status_code == 401


def test_rate_limiter_global_cap_and_forwarded_header(client):
    for _ in range(5):
        response = client.post("/api/login", headers={"x-forwarded-for": str(uuid4())},
                               json={"password": "wrong"})
        assert response.status_code == 401
    assert client.post("/api/login", headers={"x-forwarded-for": "another"},
                       json={"password": "wrong"}).status_code == 429
    credentials = client.app.state.credentials
    credentials.rates = {("login", "global"): (credentials.now() + 60, 60)}
    assert client.post("/api/login", json={"password": "wrong"}).status_code == 429


def test_idempotency_per_role_and_session(client, operator, pair_wearer, monkeypatch):
    monkeypatch.setattr("backend.store.timestamp", lambda: "2026-09-26T12:00:00.000000Z")
    first, one = pair_wearer("One")
    second, two = pair_wearer("Two")
    client_id = str(uuid4())
    question = send(client, one, " Hello ", client_id).json()
    assert question["text"] == "Hello"
    assert question["role"] == "wearer"
    assert set(question) == {"id", "session_id", "role", "text", "created_at", "client_id"}
    assert send(client, one, "Hello", client_id).json() == question
    assert send(client, one, "Changed", client_id).status_code == 409
    reply = send(client, operator, "Answer", client_id, first).json()
    assert reply["id"] != question["id"]
    assert send(client, operator, "Answer", client_id, first).json() == reply
    other = send(client, two, "Other", client_id).json()
    assert other["session_id"] == second
    assert client.get("/api/me", headers=one).json()["messages"] == [question, reply]
    assert [row["id"] for row in client.get("/api/sessions", headers=operator).json()] == [second, first]


@pytest.mark.parametrize("payload", [
    {"text": " ", "client_id": str(uuid4())},
    {"text": "x" * 4001, "client_id": str(uuid4())},
    {"text": "OK", "client_id": "invalid"},
    {"text": 123, "client_id": str(uuid4())},
    {"text": "OK"},
    {"text": "OK", "client_id": str(uuid4()), "role": "operator"},
])
def test_message_validation(client, pair_wearer, payload):
    _, wearer = pair_wearer()
    response = client.post("/api/messages", headers=wearer, json=payload)
    assert response.status_code == 422
    assert isinstance(response.json()["detail"], str)
    assert client.get("/api/me", headers=wearer).json()["messages"] == []


def test_trimmed_text_boundary_and_uuid_normalization(client, pair_wearer):
    _, wearer = pair_wearer()
    client_id = str(uuid4())
    result = send(client, wearer, " " + "x" * 4000 + " ", client_id.upper())
    assert result.status_code == 200
    assert len(result.json()["text"]) == 4000
    assert send(client, wearer, "x" * 4000, client_id).json() == result.json()


def test_session_and_message_limits(make_client):
    with_limit = make_client(max_sessions=1, max_messages_per_session=1)
    token = with_limit.post("/api/login", json={"password": "test-admin-password"}).json()["token"]
    operator = {"Authorization": "Bearer " + token}
    code = with_limit.post("/api/pairings", headers=operator).json()["code"]
    paired = with_limit.post("/api/pair", json={"code": code, "name": "First"}).json()
    wearer = {"Authorization": "Bearer " + paired["token"]}
    next_code = with_limit.post("/api/pairings", headers=operator).json()["code"]
    payload = {"code": next_code, "name": "Second"}
    assert with_limit.post("/api/pair", json=payload).status_code == 409
    client_id = str(uuid4())
    original = send(with_limit, wearer, "First", client_id)
    assert original.status_code == 200
    assert send(with_limit, wearer, "First", client_id).json() == original.json()
    assert send(with_limit, wearer).status_code == 409
    assert send(with_limit, operator, session_id=paired["session_id"]).status_code == 409
    assert with_limit.delete(f"/api/sessions/{paired['session_id']}", headers=operator).status_code == 204
    assert with_limit.post("/api/pair", json=payload).status_code == 200


def test_bounded_outstanding_credentials(client, operator):
    credentials = client.app.state.credentials
    expiry = credentials.now() + 300
    credentials.pairings = {str(n): expiry for n in range(100)}
    assert client.post("/api/pairings", headers=operator).status_code == 429
    credentials.operators.update({str(n): expiry for n in range(100)})
    assert client.post("/api/login", json={"password": "test-admin-password"}).status_code == 429


@pytest.mark.parametrize("name", ["", " ", "x" * 81, 123])
def test_pair_name_validation(client, operator, name):
    code = client.post("/api/pairings", headers=operator).json()["code"]
    assert client.post("/api/pair", json={"code": code, "name": name}).status_code == 422
    assert client.post("/api/pair", json={"code": code, "name": "Valid"}).status_code == 200


def test_json_body_limits_and_error_shape(client, pair_wearer):
    _, wearer = pair_wearer()
    headers = {**wearer, "content-type": "application/json"}
    for raw, status in [(b"{" + b" " * JSON_LIMIT, 413), (b"{invalid", 422), (b"\xff", 422)]:
        result = client.post("/api/messages", headers=headers, content=raw)
        assert result.status_code == status
        assert isinstance(result.json()["detail"], str)
    assert client.post("/api/messages", headers=wearer, content=b"{}").status_code == 415
    assert client.post("/api/messages", headers={**headers, "content-encoding": "gzip"}, content=b"{}").status_code == 415
    assert client.post("/api/messages", headers={**headers, "content-length": str(JSON_LIMIT + 1)}, content=b"{}").status_code == 413
    assert client.post("/api/messages", headers={**headers, "content-length": "bad"}, content=b"{}").status_code == 400


@pytest.mark.parametrize("limit", [JSON_LIMIT, AUDIO_LIMIT])
def test_stream_limit_stops_before_reading_whole_body(limit):
    reads = []

    async def receive():
        reads.append(1)
        if len(reads) > 2:
            pytest.fail("Read past the body limit")
        return {"type": "http.request", "body": b"x" * (limit // 2 + 1), "more_body": True}

    request = Request({"type": "http", "headers": [(b"content-type", b"application/octet-stream")]}, receive)
    with pytest.raises(HTTPException) as error:
        asyncio.run(body(request, limit, "application/octet-stream"))
    assert error.value.status_code == 413
    assert len(reads) == 2


def test_transcription_is_draft_only(client, operator, pair_wearer):
    session_id, wearer = pair_wearer()
    response = client.post("/api/transcribe", headers={**wearer, "content-type": "application/octet-stream"},
                           content=b"\0\0" * 8000)
    assert response.json() == {"text": "Unsent speech draft"}
    assert client.get("/api/me", headers=wearer).json()["messages"] == []
    assert client.get(f"/api/sessions/{session_id}/messages", headers=operator).json() == []
    assert client.app.state.store.db.execute("SELECT count(*) FROM messages").fetchone()[0] == 0
    assert send(client, wearer, response.json()["text"]).status_code == 200


@pytest.mark.parametrize("size,status", [(0, 422), (3, 422), (AUDIO_LIMIT, 200), (AUDIO_LIMIT + 2, 413)])
def test_audio_limits(client, pair_wearer, size, status):
    _, wearer = pair_wearer()
    assert client.post("/api/transcribe", headers={**wearer, "content-type": "application/octet-stream"},
                       content=b"\0" * size).status_code == status


def test_disabled_stt_manual_path(make_client):
    client = make_client(stt_enabled=False)
    token = client.post("/api/login", json={"password": "test-admin-password"}).json()["token"]
    operator = {"Authorization": "Bearer " + token}
    code = client.post("/api/pairings", headers=operator).json()["code"]
    token = client.post("/api/pair", json={"code": code, "name": "Manual"}).json()["token"]
    wearer = {"Authorization": "Bearer " + token}
    assert client.get("/api/status", headers=operator).json()["stt_enabled"] is False
    assert client.post("/api/transcribe", headers=wearer, content=b"\0\0").status_code == 503
    assert send(client, wearer, "Manually typed text").status_code == 200


def test_asr_timeout_keeps_slot_and_errors_are_private(client, pair_wearer):
    _, wearer = pair_wearer()
    transcriber = client.app.state.transcriber
    object.__setattr__(transcriber.settings, "stt_timeout", 0.03)
    release = threading.Event()
    calls = []

    def slow(pcm):
        calls.append(threading.get_ident())
        release.wait(2)
        raise RuntimeError("secret model path and audio")

    transcriber.run = slow
    headers = {**wearer, "content-type": "application/octet-stream"}
    try:
        response = client.post("/api/transcribe", headers=headers, content=b"\0\0")
        assert response.status_code == 504
        assert "secret" not in response.text
        assert client.post("/api/transcribe", headers=headers, content=b"\0\0").status_code == 429
        assert len(calls) == 1
        assert calls[0] != threading.get_ident()
    finally:
        release.set()
    deadline = time.monotonic() + 2
    while transcriber.busy and time.monotonic() < deadline:
        time.sleep(0.005)
    assert not transcriber.busy
    response = client.post("/api/transcribe", headers=headers, content=b"\0\0")
    assert response.status_code == 503
    assert "secret" not in response.text


def test_reply_websocket_replay_replacement_and_deletion(client, operator, pair_wearer):
    session_id, wearer = pair_wearer()
    question = send(client, wearer).json()
    with client.websocket_connect("/api/wearer", headers=ORIGIN) as first:
        assert auth_socket(first, wearer) == {"type": "ready", "session_id": session_id, "messages": [question]}
        assert client.get("/api/sessions", headers=operator).json()[0]["connected"] is True
        first.send_json({"type": "ping"})
        assert first.receive_json() == {"type": "pong"}
        reply = send(client, operator, "Next left", session_id=session_id).json()
        assert first.receive_json() == {"type": "message", "message": reply}
        with client.websocket_connect("/api/wearer", headers=ORIGIN) as second:
            assert auth_socket(second, wearer)["messages"] == [question, reply]
            with pytest.raises(WebSocketDisconnect) as error:
                first.receive_json()
            assert error.value.code == 4009
            assert client.get("/api/sessions", headers=operator).json()[0]["connected"] is True
            client_id = str(uuid4())
            latest = send(client, wearer, "Thanks", client_id).json()
            assert second.receive_json() == {"type": "message", "message": latest}
            assert send(client, wearer, "Thanks", client_id).json() == latest
            second.send_json({"type": "ping"})
            assert second.receive_json() == {"type": "pong"}
            assert client.delete(f"/api/sessions/{session_id}", headers=operator).status_code == 204
            with pytest.raises(WebSocketDisconnect) as error:
                second.receive_json()
            assert error.value.code == 4004
    assert client.get("/api/me", headers=wearer).status_code == 401
    assert client.get(f"/api/sessions/{session_id}/messages", headers=operator).status_code == 404
    assert client.get("/api/sessions", headers=operator).json() == []
    assert client.app.state.store.db.execute("SELECT count(*) FROM messages").fetchone()[0] == 0
    assert client.app.state.store.db.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0


def test_websocket_auth_and_timeout(client, operator, monkeypatch):
    monkeypatch.setattr("backend.main.AUTH_TIMEOUT", 0.03)
    with client.websocket_connect("/api/wearer", headers=ORIGIN) as socket:
        with pytest.raises(WebSocketDisconnect) as error:
            socket.receive_json()
        assert error.value.code == 4401
    with client.websocket_connect("/api/wearer", headers=ORIGIN) as socket:
        with pytest.raises(WebSocketDisconnect) as error:
            auth_socket(socket, operator)
        assert error.value.code == 4401


@pytest.mark.parametrize("auth", [{"type": "ping"}, {"type": "auth", "token": []}, [],
                                 {"type": "auth", "token": "x" * 5000}])
def test_websocket_invalid_auth(client, auth):
    with client.websocket_connect("/api/wearer", headers=ORIGIN) as socket:
        socket.send_json(auth)
        with pytest.raises(WebSocketDisconnect) as error:
            socket.receive_json()
        assert error.value.code == 4401


@pytest.mark.parametrize("headers,path", [
    ({}, "/api/wearer"), ({"origin": "null"}, "/api/wearer"),
    ({"origin": "https://evil.example"}, "/api/wearer"),
    (ORIGIN, "/api/wearer?token=never-in-the-url"),
])
def test_websocket_origin_and_query_rejection(client, headers, path):
    with pytest.raises(WebSocketDisconnect) as error:
        with client.websocket_connect(path, headers=headers):
            pass
    assert error.value.code == 4403


def test_websocket_notification_only(client, pair_wearer):
    _, wearer = pair_wearer()
    with client.websocket_connect("/api/wearer", headers=ORIGIN) as socket:
        auth_socket(socket, wearer)
        socket.send_json({"type": "message", "text": "Do not persist"})
        with pytest.raises(WebSocketDisconnect) as error:
            socket.receive_json()
        assert error.value.code == 1008
    assert client.get("/api/me", headers=wearer).json()["messages"] == []


def test_explicit_extra_origin_and_cors(make_client):
    client = make_client(allowed_origins=("https://packaged.example",))
    with client.websocket_connect("/api/wearer", headers={"origin": "https://packaged.example"}) as socket:
        socket.send_json({"type": "auth", "token": "invalid"})
        with pytest.raises(WebSocketDisconnect) as error:
            socket.receive_json()
        assert error.value.code == 4401
    headers = {"origin": "https://packaged.example", "access-control-request-method": "POST",
               "access-control-request-headers": "Authorization,Content-Type"}
    response = client.options("/api/messages", headers=headers)
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "https://packaged.example"
    assert "access-control-allow-credentials" not in response.headers
    assert client.options("/api/messages", headers={**headers, "origin": "https://evil.example"}).status_code == 400
    with pytest.raises(ValueError, match="ALLOWED_ORIGINS"):
        Settings(admin_password="test", allowed_origins=("*",))


def test_suggestion_is_editable_not_sent_and_context_is_bounded(client, operator, pair_wearer):
    session_id, wearer = pair_wearer()
    requests = []

    def upstream(request):
        requests.append(json.loads(request.content))
        assert str(request.url) == "http://127.0.0.1:11434/api/chat"
        return httpx.Response(200, json={"message": {"content": " Suggested reply "}})

    client.portal.call(client.app.state.ollama.aclose)
    client.app.state.ollama = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    for n in range(25):
        assert send(client, wearer, f"{n}:" + "x" * 3990).status_code == 200
    before = client.get("/api/me", headers=wearer).json()["messages"]
    response = client.post(f"/api/sessions/{session_id}/suggest", headers=operator)
    assert response.json() == {"text": "Suggested reply"}
    context = requests[0]["messages"][1:-1]
    assert len(context) <= 20
    assert sum(len(item["content"]) for item in context) <= 12000
    assert context[-1]["content"].startswith("24:")
    assert "Unsent speech draft" not in json.dumps(requests)
    assert client.get("/api/me", headers=wearer).json()["messages"] == before


@pytest.mark.parametrize("kind,status", [("timeout", 504), ("connection", 503), ("status", 502),
                                        ("invalid", 502), ("oversized", 502)])
def test_ollama_errors_are_private(client, operator, pair_wearer, kind, status):
    session_id, wearer = pair_wearer()

    def upstream(request):
        if kind == "timeout":
            raise httpx.ReadTimeout("sensitive upstream detail", request=request)
        if kind == "connection":
            raise httpx.ConnectError("sensitive upstream detail", request=request)
        if kind == "status":
            return httpx.Response(500, text="sensitive upstream detail")
        if kind == "oversized":
            return httpx.Response(200, content=b"x" * 65537)
        return httpx.Response(200, json={"error": "sensitive upstream detail"})

    client.portal.call(client.app.state.ollama.aclose)
    client.app.state.ollama = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    response = client.post(f"/api/sessions/{session_id}/suggest", headers=operator)
    assert response.status_code == status
    assert "sensitive" not in response.text
    assert isinstance(response.json()["detail"], str)
    assert client.get("/api/me", headers=wearer).json()["messages"] == []


def test_persistence_restart_and_deletion(tmp_path):
    settings = Settings(admin_password="test", database_path=tmp_path / "persistent.sqlite3", stt_enabled=False)
    with TestClient(create_app(settings)) as client:
        token = client.post("/api/login", json={"password": "test"}).json()["token"]
        operator = {"Authorization": "Bearer " + token}
        code = client.post("/api/pairings", headers=operator).json()["code"]
        pair = client.post("/api/pair", json={"code": code, "name": "Persistent"}).json()
        wearer = {"Authorization": "Bearer " + pair["token"]}
        message = send(client, wearer).json()
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/sessions", headers=operator).status_code == 401
        assert client.get("/api/me", headers=wearer).json()["messages"] == [message]
        token = client.post("/api/login", json={"password": "test"}).json()["token"]
        operator = {"Authorization": "Bearer " + token}
        assert client.delete(f"/api/sessions/{pair['session_id']}", headers=operator).status_code == 204
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/me", headers=wearer).status_code == 401


def test_static_routes_never_swallow_api(make_client, tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<h1>Operator</h1>")
    (dist / "glasses.html").write_text("<h1>Glasses</h1>")
    (dist / "asset.js").write_text("console.log('asset')")
    (tmp_path / "secret").write_text("private")
    (dist / "symlink").symlink_to(tmp_path / "secret")
    client = make_client(frontend_dist=dist)
    assert client.get("/").text == "<h1>Operator</h1>"
    assert client.get("/glasses.html").text == "<h1>Glasses</h1>"
    assert client.get("/asset.js").status_code == 200
    for path in ("/api", "/api/missing", "/api/missing.html", "/missing", "/symlink", "/%2e%2e/secret"):
        response = client.get(path)
        assert response.status_code == 404
        assert isinstance(response.json()["detail"], str)
    assert client.post("/api/missing").status_code == 405
