import asyncio
import base64
import json
import sqlite3

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.requests import ClientDisconnect

from backend.config import Settings
from backend.main import create_app, digest
from backend.stream import COOKIE, COOKIE_PATH, MEDIA_LIMIT, PLAYBACK_URL, MediaResponse


def login(client, password="test-admin-password"):
    token = client.post("/api/login", json={"password": password}).json()["token"]
    return {"Authorization": "Bearer " + token}


@pytest.fixture
def make_stream(make_client):
    def make(api=None, hls=None, **options):
        options.setdefault("stream_enabled", True)
        client = make_client(**options)
        stream = client.app.state.stream
        client.portal.call(stream.aclose)
        stream.api = httpx.AsyncClient(transport=httpx.MockTransport(
            api or (lambda request: httpx.Response(404))), timeout=3, trust_env=False)
        stream.hls = httpx.AsyncClient(transport=httpx.MockTransport(
            hls or (lambda request: httpx.Response(200, content=b"media"))), timeout=10, trust_env=False)
        return client, login(client)
    return make


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.reads = 0
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            self.reads += 1
            if isinstance(chunk, Exception):
                raise chunk
            yield chunk

    async def aclose(self):
        self.closed = True


def test_stream_auth_isolation(make_stream):
    client, operator = make_stream()
    code = client.post("/api/pairings", headers=operator).json()["code"]
    token = client.post("/api/pair", json={"code": code, "name": "Wearer"}).json()["token"]
    wearer = {"Authorization": "Bearer " + token}
    routes = [("GET", "/api/stream/status"), ("GET", "/api/stream/settings"),
              ("POST", "/api/stream/playback-session"), ("POST", "/api/logout")]
    for method, path in routes:
        for headers in ({}, wearer):
            assert client.request(method, path, headers=headers).status_code == 401
    for headers in ({}, wearer, operator):
        assert client.get(PLAYBACK_URL, headers=headers).status_code == 401
    assert client.get(PLAYBACK_URL, params={"token": operator["Authorization"].split()[1]}).status_code == 401
    assert client.post("/api/stream/playback-session", headers=operator).status_code == 200
    cookie = {"Cookie": COOKIE + "=" + client.cookies[COOKIE]}
    for method, path in routes + [("GET", "/api/me"), ("GET", "/api/sessions")]:
        assert client.request(method, path, headers=cookie).status_code == 401
    assert client.get(PLAYBACK_URL).status_code == 200
    assert client.post("/internal/media/auth", headers=operator, json={}).status_code == 401


@pytest.mark.parametrize("secure", [False, True])
def test_cookie_flags_renewal_and_logout(make_stream, secure):
    client, operator = make_stream(cookie_secure=secure)
    credentials = client.app.state.credentials
    now = credentials.now()
    credentials.now = lambda: now
    response = client.post("/api/stream/playback-session", headers=operator)
    assert response.json() == {"expires_in": 300}
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie
    assert "Path=" + COOKIE_PATH in cookie and "Max-Age=300" in cookie
    assert ("Secure" in cookie) == secure
    old = response.cookies[COOKIE]
    parent = digest(operator["Authorization"].split()[1])
    assert credentials.playbacks == {digest(old): (parent, now + 300)}
    for _ in range(5):
        renewed = client.post("/api/stream/playback-session", headers=operator).cookies[COOKIE]
    assert renewed != old and len(credentials.playbacks) == 1
    assert client.get(PLAYBACK_URL, headers={"Cookie": COOKIE + "=" + old}).status_code == 401
    assert client.get(PLAYBACK_URL, headers={"Cookie": COOKIE + "=" + renewed}).status_code == 200
    other = login(client)
    other_cookie = client.post("/api/stream/playback-session", headers=other).cookies[COOKIE]
    response = client.post("/api/logout", headers=operator)
    assert response.status_code == 204 and not response.content
    assert "Max-Age=0" in response.headers["set-cookie"]
    assert "Path=" + COOKIE_PATH in response.headers["set-cookie"]
    assert ("Secure" in response.headers["set-cookie"]) == secure
    assert parent not in credentials.operators
    assert len(credentials.playbacks) == 1
    assert client.get(PLAYBACK_URL, headers={"Cookie": COOKIE + "=" + renewed}).status_code == 401
    assert client.get(PLAYBACK_URL, headers={"Cookie": COOKIE + "=" + other_cookie}).status_code == 200
    assert client.get("/api/stream/settings", headers=operator).status_code == 401
    assert client.post("/api/stream/playback-session", headers=operator).status_code == 401


def test_cookie_and_parent_expiry_prune(make_stream):
    client, operator = make_stream()
    credentials = client.app.state.credentials
    now = credentials.now()
    credentials.now = lambda: now
    parent = digest(operator["Authorization"].split()[1])
    credentials.operators[parent] = now + 42.9
    response = client.post("/api/stream/playback-session", headers=operator)
    assert response.json() == {"expires_in": 42}
    assert credentials.playbacks[digest(response.cookies[COOKIE])][1] <= credentials.operators[parent]
    credentials.now = lambda: now + 42
    assert client.get(PLAYBACK_URL).status_code == 401
    assert not credentials.playbacks
    assert client.post("/api/stream/playback-session", headers=operator).status_code == 401
    credentials.operators[parent] = now + 1000
    assert client.post("/api/stream/playback-session", headers=operator).json() == {"expires_in": 300}
    credentials.now = lambda: now + 342
    assert client.get(PLAYBACK_URL).status_code == 401
    assert not credentials.playbacks
    client.post("/api/stream/playback-session", headers=operator)
    credentials.operators[parent] = now + 342
    assert client.get(PLAYBACK_URL).status_code == 401
    assert not credentials.playbacks and parent not in credentials.operators


def test_settings_contract_and_disabled_stream(make_stream):
    client, operator = make_stream(stream_enabled=False, public_host="2001:db8::1")
    stream = client.app.state.stream
    response = client.get("/api/stream/settings", headers=operator)
    assert response.json() == dict(enabled=False, server_url="rtmp://[2001:db8::1]:21936/live",
                                   stream_key="stream?user=publisher&pass=" + stream.publisher_secret,
                                   playback_url=PLAYBACK_URL, rtmp_port=21936)
    assert stream.reader_secret not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert client.post("/api/stream/playback-session", headers=operator).status_code == 503
    assert client.get("/api/stream/status", headers=operator).json() == dict(
        enabled=False, media_available=False, online=False, publisher_session_id=None,
        started_at=None, tracks=[], bitrate_mbps=None)
    assert client.post("/internal/media/auth", json=dict(user="publisher", password=stream.publisher_secret,
        action="publish", path="live/stream", protocol="rtmp", id=None)).status_code == 401


@pytest.mark.parametrize("user,action,protocol", [("publisher", "publish", "rtmp"), ("reader", "read", "hls")])
def test_media_auth_parsed_fields_and_isolation(make_stream, user, action, protocol):
    client, operator = make_stream()
    stream = client.app.state.stream
    secret = stream.publisher_secret if user == "publisher" else stream.reader_secret
    payload = dict(user=user, password=secret, action=action, path="live/stream", protocol=protocol,
                   id=None, ip="172.18.0.2", query="untrusted=extra", token="ignored")
    response = client.post("/internal/media/auth", json=payload)
    assert response.status_code == 204 and not response.content
    for key, value in [("password", "wrong"), ("password", operator["Authorization"].split()[1]),
                       ("password", stream.reader_secret if user == "publisher" else stream.publisher_secret),
                       ("protocol", "rtsp"), ("protocol", "hls" if protocol == "rtmp" else "rtmp"),
                       ("action", "read" if action == "publish" else "publish"), ("action", "api"),
                       ("path", "other"), ("path", "/live/stream"), ("path", "live/stream/"),
                       ("path", "live/stream?user=publisher"), ("user", "other"), ("password", None),
                       ("id", 123), ("user", 123)]:
        response = client.post("/internal/media/auth", json={**payload, key: value})
        assert response.status_code == 401
        assert secret not in response.text
    for key in ("user", "password", "action", "path", "protocol"):
        assert client.post("/internal/media/auth", json={k: v for k, v in payload.items() if k != key}).status_code == 401


def test_media_auth_body_bounds(make_stream):
    client, _ = make_stream()
    for data, status in [(b"{" + b" " * 65536, 413), (b"invalid", 401), (b"null", 401), (b"[]", 401)]:
        assert client.post("/internal/media/auth", content=data,
                           headers={"content-type": "application/json"}).status_code == status
    assert client.post("/internal/media/auth", content=b"{}").status_code == 415
    assert client.post("/internal/media/auth", json={}, headers={"content-encoding": "gzip"}).status_code == 415


def test_proxy_fixed_target_reader_credentials_ranges_and_queries(make_stream):
    requests = []
    content = Chunks([b"first", b"second"])

    def hls(request):
        requests.append(request)
        return httpx.Response(206, stream=content, headers={"content-type": "video/mp4",
            "content-length": "11", "content-range": "bytes 0-10/20", "accept-ranges": "bytes",
            "etag": '"segment"', "last-modified": "Fri, 25 Sep 2026 12:00:00 GMT",
            "set-cookie": "upstream=secret", "location": "https://other.example/",
            "www-authenticate": "Basic realm=secret", "x-internal": "secret"})

    client, operator = make_stream(hls=hls)
    client.post("/api/stream/playback-session", headers=operator)
    response = client.get(COOKIE_PATH + "part_123_1.mp4?_HLS_msn=123&_HLS_part=1&_HLS_skip=v2",
                          headers={**operator, "Range": "bytes=0-10", "If-Range": '"segment"',
                                   "X-Injected": "private"})
    assert response.status_code == 206 and response.content == b"firstsecond"
    assert content.closed and content.reads == 2
    request = requests[0]
    assert str(request.url) == "http://mediamtx:8888/live/stream/part_123_1.mp4?_HLS_msn=123&_HLS_part=1&_HLS_skip=v2"
    auth = base64.b64decode(request.headers["authorization"].split()[1]).decode()
    assert auth == "reader:" + client.app.state.stream.reader_secret
    assert client.app.state.stream.publisher_secret not in auth
    assert "cookie" not in request.headers and "x-injected" not in request.headers
    assert request.headers["range"] == "bytes=0-10" and request.headers["if-range"] == '"segment"'
    assert request.headers["accept-encoding"] == "identity"
    for key in ("content-type", "content-range", "accept-ranges", "etag", "last-modified"):
        assert key in response.headers
    for key in ("set-cookie", "location", "www-authenticate", "x-internal"):
        assert key not in response.headers


@pytest.mark.parametrize("file", ["index.m3u8", "main.m3u8", "init.mp4", "seg7.mp4", "part7.m4s", "seg7.ts"])
def test_proxy_supported_files(make_stream, file):
    client, operator = make_stream()
    client.post("/api/stream/playback-session", headers=operator)
    assert client.get(COOKIE_PATH + file).status_code == 200


@pytest.mark.parametrize("path", ["%2e%2e/secret.mp4", "%2e%2e%2fsecret.mp4", "sub/index.m3u8",
    "%252e%252e%252findex.m3u8", "%5csecret.mp4", "https://other.example/index.m3u8",
    "index.html", "index", "../settings", "file.mp4%00", ".hidden.mp4", "a..mp4", "x" * 161 + ".mp4"])
def test_proxy_traversal_blocked(make_stream, path):
    def fail(request):
        pytest.fail("Invalid path reached MediaMTX")
    client, operator = make_stream(hls=fail)
    client.post("/api/stream/playback-session", headers=operator)
    assert client.get(COOKIE_PATH + path).status_code in {401, 404}


@pytest.mark.parametrize("query", ["url=http://other.example", "user=reader", "pass=secret", "token=secret",
    "_HLS_msn=-1", "_HLS_msn=1.5", "_HLS_part=", "_HLS_skip=true", "_HLS_skip=NO",
    "_HLS_msn=" + "1" * 21, "_HLS_msn=1&_HLS_msn=2", "_HLS_msn=1&_HLS_part=0&_HLS_skip=YES&a=1",
    "_HLS_msn=" + "1" * 300])
def test_proxy_query_validation(make_stream, query):
    def fail(request):
        pytest.fail("Invalid query reached MediaMTX")
    client, operator = make_stream(hls=fail)
    client.post("/api/stream/playback-session", headers=operator)
    assert client.get(PLAYBACK_URL + "?" + query).status_code == 400


@pytest.mark.parametrize("status,expected", [(301, 502), (302, 502), (307, 502), (401, 502),
                                           (500, 502), (404, 404), (416, 416), (304, 304)])
def test_proxy_status_allowlist_and_no_redirects(make_stream, status, expected):
    content = Chunks([b"secret upstream detail"])
    requests = []
    def hls(request):
        requests.append(request)
        return httpx.Response(status, stream=content,
                              headers={"location": "https://public.example/", "content-range": "bytes */100"})
    client, operator = make_stream(hls=hls)
    client.post("/api/stream/playback-session", headers=operator)
    response = client.get(PLAYBACK_URL)
    assert response.status_code == expected
    assert len(requests) == 1 and content.closed
    assert content.reads == 0 and "secret" not in response.text
    if status == 304:
        assert not response.content
    assert "location" not in response.headers
    if status == 416:
        assert response.headers["content-range"] == "bytes */100"


@pytest.mark.parametrize("error", [httpx.ReadTimeout, httpx.ConnectError])
def test_proxy_unavailable_is_private(make_stream, error):
    def hls(request):
        raise error("sensitive upstream detail")
    client, operator = make_stream(hls=hls)
    client.post("/api/stream/playback-session", headers=operator)
    response = client.get(PLAYBACK_URL)
    assert response.status_code == 503 and "sensitive" not in response.text


@pytest.mark.parametrize("headers", [{"content-length": str(MEDIA_LIMIT + 1)}, {"content-length": "-1"},
                                    {"content-length": "bad"}, {"content-encoding": "gzip"}])
def test_proxy_rejects_oversized_or_encoded_response(make_stream, headers):
    content = Chunks([b"secret"])
    client, operator = make_stream(hls=lambda request: httpx.Response(200, stream=content, headers=headers))
    client.post("/api/stream/playback-session", headers=operator)
    response = client.get(PLAYBACK_URL)
    assert response.status_code == 502 and "secret" not in response.text
    assert content.reads == 0 and content.closed


def test_proxy_unknown_length_cap_and_midstream_failure(make_stream, monkeypatch):
    monkeypatch.setattr("backend.stream.MEDIA_LIMIT", 65536)
    for chunks in ([b"x" * 65536] * 4, [b"x" * 65536, httpx.ReadTimeout("sensitive detail")]):
        content = Chunks(chunks)
        client, operator = make_stream(hls=lambda request: httpx.Response(200, stream=content))
        client.post("/api/stream/playback-session", headers=operator)
        response = client.get(PLAYBACK_URL)
        assert response.content == b"x" * 65536
        assert content.reads == 2 and content.closed


@pytest.mark.parametrize("disconnect", [None, "start", "body"])
def test_response_streams_incrementally_and_closes_on_disconnect(disconnect):
    async def run():
        content = Chunks([b"x" * 65536] * 3)
        upstream = httpx.Response(200, stream=content)
        response = MediaResponse(upstream)
        sent = 0

        async def send(message):
            nonlocal sent
            if disconnect == "start" and message["type"] == "http.response.start":
                raise OSError("Browser disconnected")
            if message["type"] == "http.response.body" and message.get("body"):
                sent += 1
                assert content.reads == sent
                if disconnect == "body":
                    raise OSError("Browser disconnected")

        async def receive():
            await asyncio.sleep(10)

        try:
            await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
        except ClientDisconnect:
            assert disconnect
        assert content.closed and sent == {None: 3, "start": 0, "body": 1}[disconnect]
    asyncio.run(run())


def test_status_offline_recovery_identity_and_bitrate(make_stream):
    calls = []
    reply = [httpx.Response(404)]
    def api(request):
        assert str(request.url) == "http://mediamtx:9997/v3/paths/get/live/stream"
        assert "authorization" not in request.headers and "cookie" not in request.headers
        calls.append(request)
        if isinstance(reply[0], Exception):
            raise reply[0]
        return reply[0]
    client, operator = make_stream(api=api)
    stream = client.app.state.stream
    clock = [100.0]
    stream.now = lambda: clock[0]
    def status(value=None):
        if value is not None:
            clock[0] += 2
            reply[0] = value
        return client.get("/api/stream/status", headers=operator).json()
    offline = status()
    assert offline["enabled"] and offline["media_available"] and not offline["online"]
    assert status() == offline and len(calls) == 1
    for value in (httpx.ConnectError("sensitive"), httpx.ReadTimeout("sensitive"), httpx.Response(500),
                  httpx.Response(302, headers={"location": "https://other.example/"}),
                  httpx.Response(200, json={"ready": "true"}), httpx.Response(200, content=b"x" * 65537)):
        result = status(value)
        assert not result["media_available"] and not result["online"]
        assert result["publisher_session_id"] is None and result["bitrate_mbps"] is None
    data = dict(ready=True, readyTime="2026-09-26T12:00:00Z", source={"id": "publisher-one", "secret": "private"},
                tracks=["H264", "MPEG4Audio", "secret", {"invalid": True}], bytesReceived=1000,
                readers=[{"id": "private"}], password="private")
    first = status(httpx.Response(200, json=data))
    assert first["media_available"] and first["online"]
    assert first["started_at"] == data["readyTime"]
    assert first["tracks"] == ["H264", "MPEG4Audio"] and first["bitrate_mbps"] is None
    assert first["publisher_session_id"] and "publisher-one" not in json.dumps(first)
    assert "private" not in json.dumps(first) and "secret" not in json.dumps(first)
    second = status(httpx.Response(200, json={**data, "bytesReceived": 1001000}))
    assert second["publisher_session_id"] == first["publisher_session_id"]
    assert second["bitrate_mbps"] == 4.0
    reset = status(httpx.Response(200, json=data))
    assert reset["bitrate_mbps"] is None
    replacement = status(httpx.Response(200, json={**data, "source": {"id": "publisher-two"}}))
    assert replacement["publisher_session_id"] != first["publisher_session_id"]
    assert replacement["bitrate_mbps"] is None
    restarted = status(httpx.Response(200, json={**data, "readyTime": "2026-09-26T12:01:00Z"}))
    assert restarted["publisher_session_id"] != first["publisher_session_id"]
    stopped = status(httpx.Response(200, json={**data, "ready": False}))
    assert stopped == offline
    recovered = status(httpx.Response(200, json=data))
    assert recovered == first


def test_status_cache_nonoverlap(make_stream):
    calls = []
    async def api(request):
        calls.append(request)
        await asyncio.sleep(0.02)
        return httpx.Response(200, json={"ready": False})
    client, _ = make_stream(api=api)
    async def concurrent():
        return await asyncio.gather(*(client.app.state.stream.status() for _ in range(20)))
    results = client.portal.call(concurrent)
    assert len(calls) == 1 and all(result == results[0] for result in results)
    assert client.app.state.stream.api.timeout.read <= 3


def test_status_total_timeout_and_cleanup(make_stream):
    class Slow(Chunks):
        async def __aiter__(self):
            try:
                await asyncio.sleep(10)
                yield b'{}'
            finally:
                self.cancelled = True

    content = Slow([])
    client, operator = make_stream(api=lambda request: httpx.Response(200, stream=content))
    response = client.get("/api/stream/status", headers=operator)
    assert response.status_code == 200 and not response.json()["media_available"]
    assert response.elapsed.total_seconds() < 4
    assert content.closed and content.cancelled


@pytest.mark.parametrize("encoded", [False, True])
def test_status_body_limit_and_encoding(make_stream, encoded):
    content = Chunks([b"x" * 8192] * 20)
    client, operator = make_stream(api=lambda request: httpx.Response(
        200, stream=content, headers={"content-encoding": "gzip"} if encoded else {}))
    response = client.get("/api/stream/status", headers=operator)
    assert response.status_code == 200 and not response.json()["media_available"]
    assert content.closed and content.reads == (0 if encoded else 9)


def test_persistent_secrets_and_010_database_migration(tmp_path):
    path = tmp_path / "old.sqlite3"
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE sessions (id TEXT PRIMARY KEY, name TEXT NOT NULL,
                token_hash TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL);
            CREATE TABLE messages (seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
                session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                role TEXT NOT NULL CHECK (role IN ('wearer', 'operator')), text TEXT NOT NULL,
                created_at TEXT NOT NULL, client_id TEXT NOT NULL, UNIQUE(session_id, role, client_id));
            INSERT INTO sessions VALUES ('old-session', 'Existing wearer', 'old-hash', '2026-01-01');
            INSERT INTO messages VALUES (1, 'old-message', 'old-session', 'wearer', 'Keep this', '2026-01-01', 'old-client');
        """)
    config = Settings(admin_password="test", database_path=path, stt_enabled=False, stream_enabled=True)
    previous = None
    for _ in range(2):
        with TestClient(create_app(config)) as client:
            stream = client.app.state.stream
            current = (stream.publisher_secret, stream.reader_secret)
            assert current[0] != current[1]
            assert all(len(base64.urlsafe_b64decode(secret + "=")) == 32 for secret in current)
            if previous is not None:
                assert current == previous
                assert client.get("/api/stream/settings", headers=old_operator).status_code == 401
                assert client.get(PLAYBACK_URL, headers={"Cookie": COOKIE + "=" + old_cookie}).status_code == 401
            previous = current
            operator = login(client, "test")
            settings = client.get("/api/stream/settings", headers=operator).json()
            assert current[0] in settings["stream_key"] and current[1] not in json.dumps(settings)
            store = client.app.state.store
            assert store.sessions()[0]["name"] == "Existing wearer"
            assert store.messages("old-session")[0]["text"] == "Keep this"
            assert store.wearer("old-hash")["id"] == "old-session"
            assert store.db.execute("SELECT count(*) FROM stream_settings").fetchone()[0] == 1
            old_operator = operator
            old_cookie = client.post("/api/stream/playback-session", headers=operator).cookies[COOKIE]
        assert stream.api.is_closed and stream.hls.is_closed


@pytest.mark.parametrize("options", [
    {"stream_enabled": "false"}, {"stream_enabled": 1}, {"cookie_secure": "true"}, {"cookie_secure": 0},
    {"rtmp_port": 0}, {"rtmp_port": 65536}, {"rtmp_port": True}, {"rtmp_port": 21936.0},
    *({"public_host": value} for value in ["", "https://host", "host:123", "host/path", "host?query", "host#fragment",
       "user@host", "host name", "host\n", "-bad.example", "bad_.example", "256.1.1.1", "[127.0.0.1]", "[::1]:21936",
       "fe80::1%eth0", "[::1", ".", "host..", "x" * 64 + ".example"]),
    *({name: value} for name in ("media_api_url", "media_hls_url") for value in [
        "ftp://mediamtx", "http://", "http://user:secret@mediamtx:9997", "http://mediamtx/path",
        "http://mediamtx/", "http://mediamtx?x=1", "http://mediamtx#fragment", "http://mediamtx?",
        "http://mediamtx:", "http://mediamtx:0", "http://mediamtx:65536", "http://mediamtx\n",
        "http://mediamtx\\evil", "http://bad_host", "http://[::1]:bad"]),
])
def test_invalid_stream_configuration(options):
    with pytest.raises(ValueError):
        Settings(admin_password="test", **options)


@pytest.mark.parametrize("host", ["localhost", "umbrel.local", "device-name.lan", "192.168.1.4", "::1", "[::1]"])
def test_valid_stream_configuration(host):
    config = Settings(admin_password="test", public_host=host, media_api_url="http://[::1]:9997",
                      media_hls_url="https://media.internal:8888")
    assert config.public_host == host


def test_stream_environment_defaults_and_validation(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "test")
    names = ["STREAM_ENABLED", "PUBLIC_HOST", "RTMP_PORT", "MEDIA_API_URL", "MEDIA_HLS_URL", "COOKIE_SECURE"]
    for name in names:
        monkeypatch.delenv(name, raising=False)
    config = Settings.from_env()
    assert not config.stream_enabled and not config.cookie_secure
    assert config.public_host == "localhost" and config.rtmp_port == 21936
    assert config.media_api_url == "http://mediamtx:9997" and config.media_hls_url == "http://mediamtx:8888"
    for name in ("STREAM_ENABLED", "COOKIE_SECURE"):
        monkeypatch.setenv(name, "yes")
        with pytest.raises(ValueError, match=name):
            Settings.from_env()
        monkeypatch.setenv(name, "true")
    config = Settings.from_env()
    assert config.stream_enabled and config.cookie_secure
