import asyncio
import base64
import io
import json
import threading
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from PIL import Image

from backend.config import Settings
from backend.main import digest
from backend.research import (
    BODY_LIMIT, MODEL_TTL, PROVIDER_LIMIT, RESULT_TTL, TEXT_LIMIT, Chat, Connection,
)
from backend.stream import COOKIE


ROOT = "/api/research"
MODEL = "account-model-2026"
SECRET = "sk-test-only-never-a-real-credential"


def login(client):
    token = client.post("/api/login", json={"password": "test-admin-password"}).json()["token"]
    return {"Authorization": "Bearer " + token}


def parent(headers):
    return digest(headers["Authorization"].split()[1])


def payload(**changes):
    return {"request_id": str(uuid4()), "model": MODEL,
            "messages": [{"role": "user", "text": "Explicit private question"}], **changes}


def answer(**changes):
    return {"status": "completed", "output": [
        {"type": "reasoning", "summary": [{"text": "private trace"}]},
        {"type": "message", "role": "assistant", "content": [
            {"type": "output_text", "text": "Research answer", "annotations": [{"secret": "trace"}]}]},
    ], "usage": {"input_tokens": 12, "output_tokens": 3, "total_tokens": 15,
                 "output_tokens_details": {"reasoning_tokens": 1}}, **changes}


def provider(request):
    if request.url.path == "/v1/models":
        return httpx.Response(200, json={"data": [{"id": MODEL}, {"id": "z-model"}, {"id": MODEL}]})
    return httpx.Response(200, json=answer())


@pytest.fixture
def make_research(make_client):
    def make(handler=provider, **options):
        client = make_client(**options)
        research = client.app.state.research
        assert not research.client.follow_redirects
        assert not research.client._trust_env
        assert research.client.timeout.read == options.get("openai_timeout", 90)
        client.portal.call(research.client.aclose)
        calls = []

        async def mock(request):
            assert request.url.scheme == "https"
            assert request.url.host == "api.openai.com"
            assert request.url.path in {"/v1/models", "/v1/responses"}
            calls.append(request)
            response = handler(request)
            return await response if hasattr(response, "__await__") else response

        research.client = httpx.AsyncClient(transport=httpx.MockTransport(mock),
                                           trust_env=False, follow_redirects=False)
        return client, login(client), calls
    return make


def jpeg(size=(2, 3), format="JPEG"):
    buffer = io.BytesIO()
    Image.new("RGB", size, color="red").save(buffer, format=format)
    return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode()


@pytest.fixture
def blocked_pixels(monkeypatch):
    image = jpeg()
    entered = threading.Event()
    release = threading.Event()
    original = Image.Image.load

    def load(self, *args, **kwargs):
        entered.set()
        assert release.wait(5), "Pixel validation did not get released"
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Image.Image, "load", load)
    try:
        yield image, entered, release
    finally:
        release.set()


def connect(client, headers, key=SECRET):
    return client.post(ROOT + "/connection", headers=headers, json={"api_key": key})


def test_long_answer_can_be_sent_as_followup_history(make_research):
    def handler(request):
        if request.url.path == "/v1/models":
            return provider(request)
        return httpx.Response(200, json=answer(output=[{"type": "message", "role": "assistant",
            "content": [{"type": "output_text", "text": "x" * 17000}]}]))
    client, headers, _ = make_research(handler)
    assert connect(client, headers).status_code == 200
    response = client.post(ROOT + "/chat", headers=headers, json=payload())
    assert response.status_code == 200
    result = response.json()
    assert len(result["text"]) == 16000 and result["incomplete"]
    followup = payload(messages=[{"role": "assistant", "text": result["text"]},
                                 {"role": "user", "text": "Continue"}])
    assert client.post(ROOT + "/chat", headers=headers, json=followup).status_code == 200


def test_auth_isolation_all_routes(make_research):
    client, operator, calls = make_research(stream_enabled=True)
    code = client.post("/api/pairings", headers=operator).json()["code"]
    token = client.post("/api/pair", json={"code": code, "name": "Wearer"}).json()["token"]
    wearer = {"Authorization": "Bearer " + token}
    client.post("/api/stream/playback-session", headers=operator)
    cookie = {"Cookie": COOKIE + "=" + client.cookies[COOKIE]}
    for method, path in [("GET", "/status"), ("POST", "/connection"), ("DELETE", "/connection"),
                         ("GET", "/models"), ("POST", "/chat")]:
        for headers in ({}, wearer, cookie):
            assert client.request(method, ROOT + path, headers=headers).status_code == 401
    assert not calls


def test_connection_is_ram_only_isolated_and_validated_before_replacement(make_research, caplog):
    def handler(request):
        if request.headers["authorization"] == "Bearer rejected-secret":
            return httpx.Response(401, text="rejected-secret " + SECRET)
        return provider(request)

    client, one, calls = make_research(handler)
    two = login(client)
    assert client.get(ROOT + "/status", headers=one).json() == {"configured": False, "key_source": None}
    assert not calls
    assert client.get(ROOT + "/models", headers=one).status_code == 409
    assert client.post(ROOT + "/chat", headers=one, json=payload()).status_code == 409
    response = connect(client, one)
    assert response.json() == {"configured": True, "key_source": "session"}
    assert response.headers["cache-control"] == "no-store"
    assert calls[-1].method == "GET" and calls[-1].url.path == "/v1/models"
    assert calls[-1].headers["authorization"] == "Bearer " + SECRET
    assert SECRET not in response.text
    assert SECRET not in repr(Connection(api_key=SECRET))
    assert client.get(ROOT + "/status", headers=two).json() == {"configured": False, "key_source": None}
    assert connect(client, one, "rejected-secret").status_code == 502
    research = client.app.state.research
    assert research.sessions[parent(one)].key == SECRET
    assert SECRET not in repr(research.sessions)
    assert SECRET not in "\n".join(client.app.state.store.db.iterdump())
    assert SECRET not in caplog.text
    assert connect(client, two, "second-test-key").status_code == 200
    assert client.post("/api/logout", headers=one).status_code == 204
    assert parent(one) not in research.sessions
    assert research.sessions[parent(two)].key == "second-test-key"
    assert digest(SECRET) not in research.model_cache
    assert client.get(ROOT + "/status", headers=one).status_code == 401


def test_environment_fallback_disconnect_and_expiry(make_research):
    client, one, calls = make_research(openai_api_key="server-test-key")
    two = login(client)
    research = client.app.state.research
    server = {"configured": True, "key_source": "server"}
    assert client.get(ROOT + "/status", headers=one).json() == server
    assert not calls
    assert "server-test-key" not in repr(client.app.state.settings)
    connect(client, one)
    assert client.get(ROOT + "/models", headers=two).status_code == 200
    assert calls[-1].headers["authorization"] == "Bearer server-test-key"
    assert client.post(ROOT + "/chat", headers=one, json=payload()).status_code == 200
    old_session = research.sessions[parent(one)]
    assert old_session.results
    assert client.delete(ROOT + "/connection", headers=one).json() == server
    assert not old_session.key and not old_session.results
    connect(client, one)
    old_session = research.sessions[parent(one)]
    credentials = client.app.state.credentials
    now = credentials.now()
    credentials.operators[parent(one)] = now
    # Any auth route prunes research for all expired operators, not just its caller.
    assert client.get("/api/sessions", headers=two).status_code == 200
    assert not old_session.key and not old_session.results
    assert parent(one) not in research.sessions
    assert digest(SECRET) not in research.model_cache
    assert client.get(ROOT + "/status", headers=one).status_code == 401


@pytest.mark.parametrize("change", [
    {"base_url": "https://evil.example"}, {"url": "http://127.0.0.1"}, {"api_key": "bad\nkey"},
    {"api_key": ""}, {"api_key": "x" * 513}, {"api_key": 1}, {"api_key": " key "},
])
def test_connection_schema_no_external_targets(make_research, change):
    client, headers, calls = make_research()
    response = client.post(ROOT + "/connection", headers=headers, json={"api_key": SECRET, **change})
    assert response.status_code == 422
    assert SECRET not in response.text
    assert not calls


def test_models_discovery_cache_key_scope_and_ttl(make_research):
    client, one, calls = make_research()
    two = login(client)
    connect(client, one)
    for _ in range(2):
        assert client.get(ROOT + "/models", headers=one).json() == {"models": [{"id": MODEL}, {"id": "z-model"}]}
    assert len(calls) == 1
    connect(client, two, "second-test-key")
    assert len(calls) == 2
    credentials = client.app.state.credentials
    now = credentials.now()
    credentials.now = lambda: now + MODEL_TTL + 1
    client.get(ROOT + "/models", headers=one)
    assert len(calls) == 3
    assert digest("second-test-key") not in client.app.state.research.model_cache
    unknown = client.post(ROOT + "/chat", headers=one, json=payload(model="invented-model"))
    assert unknown.status_code == 422
    assert len(calls) == 3


@pytest.mark.parametrize("data", [None, [], {}, {"data": None}, {"data": [None]}, {"data": [{}]},
    {"data": [{"id": 1}]}, {"data": [{"id": ""}]}, {"data": [{"id": "bad\nmodel"}]},
    {"data": [{"id": "x" * 257}]}, {"data": [{"id": f"model-{i}"} for i in range(2001)]},
])
def test_invalid_model_list_never_installs_candidate_key(make_research, data):
    client, headers, _ = make_research(lambda request: httpx.Response(200, json=data))
    response = connect(client, headers)
    assert response.status_code == 502
    assert client.get(ROOT + "/status", headers=headers).json()["configured"] is False
    assert not client.app.state.research.model_cache


def test_model_list_limit_boundary_and_empty_account(make_research):
    rows = [{"id": f"model-{i:04}"} for i in range(2000)]
    client, headers, _ = make_research(lambda request: httpx.Response(200, json={"data": rows + rows[:1]}))
    assert connect(client, headers).status_code == 200
    assert client.get(ROOT + "/models", headers=headers).json() == {"models": rows}
    rows.clear()
    assert connect(client, headers).status_code == 200
    assert client.get(ROOT + "/models", headers=headers).json() == {"models": []}


@pytest.mark.parametrize("content,headers", [
    (b"x" * (PROVIDER_LIMIT + 1), {}), (b"{", {}), (b"[]", {}),
    (b'{}', {"content-encoding": "unsupported"}),
])
def test_provider_response_wire_bounds(make_research, content, headers):
    client, auth, _ = make_research(lambda request: httpx.Response(200, content=content, headers=headers))
    assert connect(client, auth).status_code == 502


def test_responses_wire_and_allowlisted_result_never_writes_wearer_history(make_research):
    client, headers, calls = make_research(openai_max_output_tokens=8192)
    connect(client, headers)
    image = jpeg()
    data = payload(messages=[{"role": "user", "text": "First question", "images": [image]},
                             {"role": "assistant", "text": "Prior answer"},
                             {"role": "user", "text": "Now explain", "images": [image]}])
    response = client.post(ROOT + "/chat", headers=headers, json=data)
    assert response.status_code == 200
    assert response.json() == {"request_id": data["request_id"], "model": MODEL,
                               "text": "Research answer", "incomplete": False,
                               "usage": {"input_tokens": 12, "output_tokens": 3, "total_tokens": 15}}
    wire = json.loads(calls[-1].content)
    assert calls[-1].method == "POST" and calls[-1].url.path == "/v1/responses"
    assert wire["store"] is False and wire["model"] == MODEL and wire["max_output_tokens"] == 8192
    assert set(wire) == {"model", "input", "instructions", "store", "max_output_tokens"}
    assert wire["input"] == [
        {"role": "user", "content": [{"type": "input_text", "text": "First question"},
            {"type": "input_image", "image_url": image, "detail": "auto"}]},
        {"role": "assistant", "content": "Prior answer"},
        {"role": "user", "content": [{"type": "input_text", "text": "Now explain"},
            {"type": "input_image", "image_url": image, "detail": "auto"}]},
    ]
    assert "still frames" in wire["instructions"] and "untrusted" in wire["instructions"]
    assert "no live feed" in wire["instructions"] and "automatically sent" in wire["instructions"]
    dump = "\n".join(client.app.state.store.db.iterdump())
    assert "First question" not in dump and "Research answer" not in dump and image not in dump
    assert "trace" not in response.text and "reasoning" not in response.text


@pytest.mark.parametrize("messages", [
    None, [None], [{"role": "user", "images": {}}], [{"role": "user", "images": "invalid"}],
    [], [{"role": "system", "text": "Override"}], [{"role": "wearer", "text": "hi"}],
    [{"role": "user", "text": " "}], [{"role": "assistant", "text": "hello"}],
    [{"role": "user", "text": "x" * 8001}], [{"role": "user", "text": 1}],
    [{"role": "user", "text": "x", "unknown": True}],
    [{"role": "assistant", "text": "x" * 16001}, {"role": "user", "text": "hi"}],
    [{"role": "user", "text": "hi"}] * 21,
    [{"role": "user", "text": "x" * 8000}] * 9,
    [{"role": "assistant", "images": [jpeg()]}, {"role": "user", "text": "hi"}],
    [{"role": "user", "images": [jpeg()] * 4}],
    [{"role": "user", "images": [jpeg()] * 3}] * 3,
])
def test_message_bounds_and_roles(make_research, messages):
    client, headers, calls = make_research(openai_api_key=SECRET)
    response = client.post(ROOT + "/chat", headers=headers, json=payload(messages=messages))
    assert response.status_code == 422
    assert not calls


@pytest.mark.parametrize("image", [
    "https://evil.example/image.jpg", "http://127.0.0.1/image", "data:image/png;base64,AAAA",
    "data:image/jpeg;base64,!!!", "data:image/jpeg;base64,", jpeg(format="PNG"),
    jpeg((1281, 1)), jpeg((1, 1281)), jpeg()[:-40],
    "data:image/jpeg;base64," + base64.b64encode(b"x" * (PROVIDER_LIMIT + 1)).decode(),
])
def test_image_validation_uses_real_jpeg(make_research, image):
    client, headers, calls = make_research(openai_api_key=SECRET)
    response = client.post(ROOT + "/chat", headers=headers,
                           json=payload(messages=[{"role": "user", "images": [image]}]))
    assert response.status_code == 422
    assert not calls


@pytest.mark.parametrize("layout", ["current", "legacy"])
def test_six_image_budget_precedes_any_image_decode(make_research, monkeypatch, layout):
    client, headers, calls = make_research()
    image = jpeg()
    messages = ([{"role": "user", "images": [image] * 3}] * 20 if layout == "legacy"
                else [{"role": "user", "images": [image] * 3}] * 2
                + [{"role": "user", "images": [image]}])

    def forbidden(*args, **kwargs):
        pytest.fail("Over-budget input must not open or decode any image")

    monkeypatch.setattr(Image, "open", forbidden)
    monkeypatch.setattr(Image.Image, "load", forbidden)
    response = client.post(ROOT + "/chat", headers=headers, json=payload(messages=messages))
    assert response.status_code == 422  # Also enforced without a provider key.
    assert not calls and not client.app.state.research.validations


def test_message_budget_precedes_any_image_decode(make_research, monkeypatch):
    client, headers, calls = make_research()
    messages = [{"role": "user", "images": [jpeg()]}] + [{"role": "user", "text": "x"}] * 20

    def forbidden(*args, **kwargs):
        pytest.fail("Over-budget input must not open or decode any image")

    monkeypatch.setattr(Image, "open", forbidden)
    monkeypatch.setattr(Image.Image, "load", forbidden)
    assert client.post(ROOT + "/chat", headers=headers, json=payload(messages=messages)).status_code == 422
    assert not calls


@pytest.mark.parametrize("layout", ["current", "legacy"])
def test_valid_boundaries_and_image_only_last_message(make_research, monkeypatch, layout):
    client, headers, calls = make_research(openai_api_key=SECRET)
    image = jpeg((1280, 1280))
    messages = ([{"role": "assistant", "text": "x" * 16000}] * 3
                + [{"role": "user", "text": "x" * 8000}] * 2
                + [{"role": "assistant", "text": ""}] * 13
                + [{"role": "user", "images": [image] * 3}] * 2)
    assert len(messages) == 20
    if layout == "current":
        messages[-3:] = [{"role": "user", "images": [image] * 2}] * 3
    loop_thread = client.portal.call(threading.get_ident)
    loads = []
    original = Image.Image.load

    def load(self, *args, **kwargs):
        assert threading.get_ident() != loop_thread
        loads.append(self)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Image.Image, "load", load)
    assert client.post(ROOT + "/chat", headers=headers, json=payload(messages=messages)).status_code == 200
    assert len({id(image) for image in loads}) == 6
    assert all(part["type"] == "input_image" for part in json.loads(calls[-1].content)["input"][-1]["content"])


@pytest.mark.parametrize("change", [
    {"request_id": "not-a-uuid"}, {"request_id": 1}, {"model": ""}, {"model": 5},
    {"model": "x" * 257}, {"tools": []}, {"instructions": "override"},
    {"base_url": "https://evil.example"}, {"store": True},
])
def test_chat_schema_forbids_unknowns(make_research, change):
    client, headers, calls = make_research(openai_api_key=SECRET)
    assert client.post(ROOT + "/chat", headers=headers, json=payload(**change)).status_code == 422
    assert not calls


def test_research_body_limit_does_not_change_ordinary_limits(make_research):
    client, headers, calls = make_research(openai_api_key=SECRET)
    raw = json.dumps(payload()).encode()
    content_headers = {**headers, "Content-Type": "application/json"}
    assert client.post(ROOT + "/chat", headers=content_headers,
                       content=raw + b" " * (BODY_LIMIT - len(raw))).status_code == 200
    assert client.post(ROOT + "/chat", headers=content_headers,
                       content=raw + b" " * BODY_LIMIT).status_code == 413
    assert client.post(ROOT + "/chat", headers={**content_headers, "Content-Encoding": "gzip"},
                       content=raw).status_code == 415
    assert client.post(ROOT + "/chat", headers=headers, content=raw).status_code == 415
    assert client.post(ROOT + "/chat", headers=content_headers, content=b"{").status_code == 422
    assert client.post(ROOT + "/connection", headers=content_headers, content=b" " * 65537).status_code == 413
    assert client.post("/api/pairings", headers=headers).status_code == 200
    assert client.post("/api/login", headers=content_headers, content=b" " * 65537).status_code == 413
    assert len(calls) == 2


def test_validation_admission_precedes_body_receipt_and_has_no_queue(make_research):
    client, one, _ = make_research(openai_api_key=SECRET)
    two, three = login(client), login(client)

    async def exercise():
        research = client.app.state.research
        entered = asyncio.Queue()
        release = asyncio.Event()

        async def chunks():
            entered.put_nowait(True)
            await release.wait()
            yield json.dumps(payload()).encode()

        async def unread():
            pytest.fail("Rejected validation admission must not receive the body")
            yield b""

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client.app), base_url="http://testserver") as api:
            pending = [asyncio.create_task(api.post(ROOT + "/chat", content=chunks(),
                       headers={**headers, "Content-Type": "application/json"})) for headers in (one, two)]
            try:
                for _ in pending:
                    await asyncio.wait_for(entered.get(), 1)
                assert research.validations == {parent(one): None, parent(two): None}
                for headers in (one, two, three):
                    response = await asyncio.wait_for(api.post(ROOT + "/chat", content=unread(),
                        headers={**headers, "Content-Type": "application/json"}), 1)
                    assert response.status_code == 429
                pending[0].cancel()
                with pytest.raises(asyncio.CancelledError):
                    await pending[0]
                assert parent(one) not in research.validations
                assert (await api.post(ROOT + "/chat", headers=three, json=payload())).status_code == 200
                release.set()
                assert (await asyncio.wait_for(pending[1], 1)).status_code == 200
                assert not research.validations
            finally:
                release.set()
                await asyncio.gather(*pending, return_exceptions=True)

    client.portal.call(exercise)


def test_cancelled_pixel_waiter_holds_admission_without_blocking_other_routes(make_research, blocked_pixels):
    client, one, calls = make_research(openai_api_key=SECRET)
    two = login(client)
    image, entered, release = blocked_pixels

    async def exercise():
        research = client.app.state.research
        data = payload(messages=[{"role": "user", "images": [image]}])
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client.app), base_url="http://testserver") as api:
            pending = asyncio.create_task(api.post(ROOT + "/chat", headers=one, json=data))
            try:
                assert await asyncio.to_thread(entered.wait, 2)
                worker = research.validations[parent(one)]
                assert worker is not None and not worker.done() and not research.tasks
                # These must complete while the pixel loader is blocked on a thread Event.
                for path in (ROOT + "/status", "/api/stream/status", "/api/sessions"):
                    assert (await asyncio.wait_for(api.get(path, headers=two), 1)).status_code == 200
                assert (await asyncio.wait_for(api.post(ROOT + "/chat", headers=two, json=payload()), 1)).status_code == 200
                assert len(calls) == 2
                pending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await pending
                assert not worker.done() and research.validations[parent(one)] is worker
                assert (await api.post(ROOT + "/chat", headers=one, json=data)).status_code == 429
                assert len(calls) == 2
                release.set()
                await asyncio.wait_for(asyncio.shield(worker), 1)
                assert not research.validations and not research.sessions[parent(one)].results
                assert (await api.post(ROOT + "/chat", headers=one, json=data)).status_code == 200
                assert len(calls) == 3
            finally:
                release.set()
                await asyncio.gather(pending, return_exceptions=True)

    client.portal.call(exercise)


@pytest.mark.parametrize("stage", ["upload", "pixels", "worker_ready"])
@pytest.mark.parametrize("action,expected", [("logout", 401), ("expiry", 401), ("disconnect", 409)])
def test_validation_revocation_prevents_late_provider_calls(make_research, blocked_pixels, stage, action, expected):
    client, headers, calls = make_research()
    assert connect(client, headers).status_code == 200
    assert client.post(ROOT + "/chat", headers=headers, json=payload()).status_code == 200
    calls.clear()
    image, entered, release = blocked_pixels

    async def exercise():
        research = client.app.state.research
        identity = parent(headers)
        session = research.sessions[identity]
        assert session.key and session.results
        uploaded = asyncio.Event()
        finish_upload = asyncio.Event()
        raw = json.dumps(payload(messages=[{"role": "user", "images": [image]}])).encode()

        async def chunks():
            yield raw[:10]
            uploaded.set()
            if stage == "upload":
                await finish_upload.wait()
            yield raw[10:]

        def revoke_ready_worker(done):
            assert done.done() and not done.cancelled()
            # Run after worker completion but before the shielded HTTP waiter resumes.
            if action == "disconnect":
                research.clear(identity)
            elif action == "logout":
                research.credentials.operators.pop(identity)
            else:
                research.credentials.operators[identity] = research.credentials.now()
            research.credentials.prune()

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client.app), base_url="http://testserver") as api:
            pending = asyncio.create_task(api.post(ROOT + "/chat", content=chunks(),
                headers={**headers, "Content-Type": "application/json"}))
            try:
                if stage == "upload":
                    await asyncio.wait_for(uploaded.wait(), 1)
                    assert research.validations[identity] is None
                else:
                    assert await asyncio.to_thread(entered.wait, 2)
                worker = research.validations[identity]
                if stage == "worker_ready":
                    worker.add_done_callback(revoke_ready_worker)
                else:
                    if action == "logout":
                        assert (await api.post("/api/logout", headers=headers)).status_code == 204
                    elif action == "disconnect":
                        assert (await api.delete(ROOT + "/connection", headers=headers)).status_code == 200
                    else:
                        research.credentials.operators[identity] = research.credentials.now()
                        research.credentials.prune()
                    assert not session.key and not session.results
                    assert identity in research.validations
                    if worker is not None:
                        assert not worker.done()
                finish_upload.set()
                release.set()
                assert (await asyncio.wait_for(pending, 1)).status_code == expected
                if stage == "upload":
                    assert not entered.is_set()  # Rechecked before decoding, not just before billing.
                else:
                    assert worker.done() and not worker.cancelled()
                assert not session.key and not session.results
                assert not calls and not research.sessions and not research.model_cache
                assert not research.validations and not research.tasks
            finally:
                finish_upload.set()
                release.set()
                await asyncio.gather(pending, return_exceptions=True)

    client.portal.call(exercise)


@pytest.mark.parametrize("cancel_waiter", [False, True])
def test_shutdown_waits_for_pixel_worker_without_cancelling_it(make_research, blocked_pixels, cancel_waiter):
    client, headers, calls = make_research(openai_api_key=SECRET)
    image, entered, release = blocked_pixels

    async def exercise():
        research = client.app.state.research
        closing = None
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client.app), base_url="http://testserver") as api:
            pending = asyncio.create_task(api.post(ROOT + "/chat", headers=headers,
                json=payload(messages=[{"role": "user", "images": [image]}])))
            try:
                assert await asyncio.to_thread(entered.wait, 2)
                worker = research.validations[parent(headers)]
                if cancel_waiter:
                    pending.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await pending
                closing = asyncio.create_task(research.aclose())
                await asyncio.sleep(0)
                assert research.closed and not closing.done()
                assert not worker.done() and research.validations[parent(headers)] is worker
                release.set()
                await asyncio.wait_for(closing, 1)
                if not cancel_waiter:
                    assert (await asyncio.wait_for(pending, 1)).status_code == 503
                assert worker.done() and not worker.cancelled()
                assert not calls and not research.validations and not research.tasks
                assert not research.sessions and not research.model_cache
                assert research.sweeper.done() and research.client.is_closed
            finally:
                release.set()
                await asyncio.gather(pending, *([closing] if closing else []), return_exceptions=True)

    client.portal.call(exercise)


@pytest.mark.parametrize("status,expected", [(400, 422), (401, 502), (403, 502), (404, 422),
    (422, 422), (429, 429), (500, 502), (503, 502), (302, 502), (307, 502)])
def test_provider_errors_are_sanitized_not_operator_401_and_never_retried(make_research, status, expected):
    def handler(request):
        if request.url.path == "/v1/models":
            return provider(request)
        return httpx.Response(status, text=SECRET + " Explicit private question", headers={
            "Location": "https://evil.example/", "www-authenticate": SECRET, "x-secret": SECRET})

    client, headers, calls = make_research(handler, openai_api_key=SECRET)
    data = payload()
    for attempt in range(2):
        response = client.post(ROOT + "/chat", headers=headers, json=data)
        assert response.status_code == expected
        assert SECRET not in response.text and "Explicit private question" not in response.text
        assert "www-authenticate" not in response.headers and "location" not in response.headers
        assert "retrying manually may incur another charge" in response.text
        assert len(calls) == 2 + attempt
        assert not client.app.state.research.sessions[parent(headers)].results
    assert client.get(ROOT + "/status", headers=headers).status_code == 200


@pytest.mark.parametrize("exception,expected", [(httpx.ReadTimeout, 504), (httpx.ConnectError, 503)])
def test_provider_network_errors_sanitized(make_research, exception, expected):
    def handler(request):
        raise exception(SECRET + " private question", request=request)
    client, headers, calls = make_research(handler)
    response = connect(client, headers)
    assert response.status_code == expected and SECRET not in response.text
    assert len(calls) == 1


@pytest.mark.parametrize("change", [
    {"status": "failed"}, {"status": []}, {"error": {"message": SECRET}}, {"output": []},
    {"output": None}, {"output": [None]},
    {"output": [{"type": "message", "role": "assistant", "content": []}]},
    {"output": [{"type": "message", "role": "user", "content": []}]},
    {"output": [{"type": "message", "role": "assistant", "content": [
        {"type": "output_text", "text": 42}]}]},
    {"output": [{"type": "message", "role": "assistant", "content": [
        {"type": "output_text", "text": "x" * (TEXT_LIMIT + 1)}]}]},
    {"usage": {}}, {"usage": {"input_tokens": True, "output_tokens": 2, "total_tokens": 3}},
])
def test_malformed_or_empty_chat_response(make_research, change):
    def handler(request):
        return provider(request) if request.url.path == "/v1/models" else httpx.Response(200, json=answer(**change))
    client, headers, calls = make_research(handler, openai_api_key=SECRET)
    response = client.post(ROOT + "/chat", headers=headers, json=payload())
    assert response.status_code == 502 and SECRET not in response.text
    assert len(calls) == 2


def test_refusal_incomplete_and_optional_usage(make_research):
    def handler(request):
        return provider(request) if request.url.path == "/v1/models" else httpx.Response(200, json=answer(
            status="incomplete", usage=None, output=[{"type": "message", "role": "assistant", "content": [
                {"type": "refusal", "refusal": "Cannot help with that."}]}]))
    client, headers, _ = make_research(handler, openai_api_key=SECRET)
    response = client.post(ROOT + "/chat", headers=headers, json=payload())
    assert response.status_code == 200
    assert response.json()["text"] == "Cannot help with that."
    assert response.json()["incomplete"] is True and response.json()["usage"] is None


def test_success_dedup_per_operator_conflict_ttl_and_connection_change(make_research):
    client, one, calls = make_research(openai_api_key=SECRET)
    two = login(client)
    data = payload()
    first = client.post(ROOT + "/chat", headers=one, json=data)
    assert first.status_code == 200
    assert client.post(ROOT + "/chat", headers=one, json=data).json() == first.json()
    assert len(calls) == 2
    assert client.post(ROOT + "/chat", headers=one, json={**data, "model": "z-model"}).status_code == 409
    assert client.post(ROOT + "/chat", headers=two, json=data).status_code == 200
    assert len(calls) == 3
    connect(client, one, "new-test-key")
    assert client.post(ROOT + "/chat", headers=one, json=data).status_code == 200
    assert len(calls) == 5
    credentials = client.app.state.credentials
    now = credentials.now()
    credentials.now = lambda: now + RESULT_TTL
    assert client.post(ROOT + "/chat", headers=one, json=data).status_code == 200
    assert len(calls) == 7
    assert not client.app.state.research.sessions[parent(two)].results


def test_cache_bounds_across_operators_and_logout(make_research):
    client, headers, calls = make_research(openai_api_key=SECRET)
    research = client.app.state.research
    data = payload()
    client.post(ROOT + "/chat", headers=headers, json=data)
    for _ in range(8):
        assert client.post(ROOT + "/chat", headers=headers, json=payload()).status_code == 200
    assert len(research.sessions[parent(headers)].results) == 8
    assert data["request_id"] not in research.sessions[parent(headers)].results
    assert len(calls) == 10

    async def exercise():
        for index in range(100):
            identity = digest(f"synthetic-operator-{index}")
            research.credentials.operators[identity] = research.credentials.now() + 1000
            if index == 99:  # The real logged-in operator already occupies one slot.
                with pytest.raises(HTTPException) as error:
                    research.session(identity)
                assert error.value.status_code == 429
                break
            session = research.session(identity)
            for _ in range(8):
                await research.chat(identity, session, Chat(**payload()))
        assert sum(len(session.results) for session in research.sessions.values()) == 128
        assert len(research.model_cache) <= 100
        for session in research.sessions.values():
            assert len(session.results) <= 8

    client.portal.call(exercise)
    assert client.post("/api/logout", headers=headers).status_code == 204
    assert parent(headers) not in research.sessions


def test_pending_dedup_cancellation_and_global_limits(make_research):
    async def exercise(client, one, two, three):
        research = client.app.state.research
        entered = asyncio.Queue()
        release = asyncio.Event()

        async def handler(request):
            if request.url.path == "/v1/responses":
                await entered.put(True)
                await release.wait()
            return provider(request)

        research.client._transport = httpx.MockTransport(handler)
        data = Chat(**payload())
        first = asyncio.create_task(research.chat(parent(one), research.session(parent(one)), data))
        await asyncio.wait_for(entered.get(), 1)
        duplicates = [asyncio.create_task(research.chat(parent(one), research.session(parent(one)), data))
                      for _ in range(10)]
        await asyncio.sleep(0)
        assert len(research.tasks) == 1
        changed = Chat(**{**data.model_dump(mode="json"), "model": "z-model"})
        with pytest.raises(HTTPException) as error:
            await research.chat(parent(one), research.session(parent(one)), changed)
        assert error.value.status_code == 409
        with pytest.raises(HTTPException) as error:
            await research.chat(parent(one), research.session(parent(one)), Chat(**payload()))
        assert error.value.status_code == 429
        second = asyncio.create_task(research.chat(parent(two), research.session(parent(two)), data))
        await asyncio.wait_for(entered.get(), 1)
        with pytest.raises(HTTPException) as error:
            await research.chat(parent(three), research.session(parent(three)), data)
        assert error.value.status_code == 429
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert len(research.tasks) == 2
        assert entered.empty()  # No fanout from ten duplicates or cancelled HTTP waiting.
        release.set()
        results = await asyncio.gather(*duplicates, second)
        assert all(result == results[0] for result in results)
        assert not research.tasks
        assert len(research.sessions[parent(one)].results) == 1

    client, one, _ = make_research(openai_api_key=SECRET)
    client.portal.call(exercise, client, one, login(client), login(client))


@pytest.mark.parametrize("operation", ["chat", "connection"])
def test_disconnect_during_body_upload_cannot_reconnect_or_send(make_research, operation):
    client, headers, calls = make_research(openai_api_key=SECRET)

    async def exercise():
        entered = asyncio.Event()
        release = asyncio.Event()
        data = payload() if operation == "chat" else {"api_key": "candidate-test-key"}
        raw = json.dumps(data).encode()

        async def chunks():
            yield raw[:10]
            entered.set()
            await release.wait()
            yield raw[10:]

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client.app), base_url="http://testserver") as api:
            pending = asyncio.create_task(api.post(ROOT + "/" + operation, content=chunks(),
                                                  headers={**headers, "Content-Type": "application/json"}))
            await asyncio.wait_for(entered.wait(), 1)
            assert (await api.delete(ROOT + "/connection", headers=headers)).status_code == 200
            release.set()
            assert (await asyncio.wait_for(pending, 1)).status_code == 409
            assert not calls and not client.app.state.research.sessions

    client.portal.call(exercise)


def test_idle_expiry_sweeper_cancels_work_without_another_http_request(make_research):
    client, headers, _ = make_research()
    connect(client, headers)

    async def exercise():
        research = client.app.state.research
        entered = asyncio.Event()

        async def handler(request):
            entered.set()
            await asyncio.Event().wait()

        research.client._transport = httpx.MockTransport(handler)
        session = research.session(parent(headers))
        pending = asyncio.create_task(research.chat(parent(headers), session, Chat(**payload())))
        await asyncio.wait_for(entered.wait(), 1)
        research.credentials.operators[parent(headers)] = research.credentials.now()
        with pytest.raises(HTTPException) as error:
            await asyncio.wait_for(pending, 6)
        assert error.value.status_code == 401
        assert not session.key and not session.results
        assert not research.sessions and not research.tasks and not research.model_cache

    client.portal.call(exercise)


def test_disconnect_keeps_operator_slot_until_cancelled_provider_exits(make_research):
    client, headers, _ = make_research(openai_api_key=SECRET)

    async def exercise():
        research = client.app.state.research
        entered = asyncio.Event()
        cancelling = asyncio.Event()
        release = asyncio.Event()

        async def handler(request):
            if request.url.path == "/v1/responses":
                entered.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelling.set()
                    await release.wait()
            return provider(request)

        research.client._transport = httpx.MockTransport(handler)
        pending = asyncio.create_task(research.chat(parent(headers), research.session(parent(headers)), Chat(**payload())))
        await asyncio.wait_for(entered.wait(), 1)
        research.clear(parent(headers))
        await asyncio.wait_for(cancelling.wait(), 1)
        replacement = research.session(parent(headers))
        with pytest.raises(HTTPException) as error:
            await research.chat(parent(headers), replacement, Chat(**payload()))
        assert error.value.status_code == 429
        assert len(research.tasks) == 1
        release.set()
        with pytest.raises(HTTPException) as error:
            await pending
        assert error.value.status_code == 409
        assert not research.tasks and not replacement.results

    client.portal.call(exercise)


@pytest.mark.parametrize("operation", ["chat", "models", "connection"])
@pytest.mark.parametrize("action,expected", [("logout", 401), ("expiry", 401), ("disconnect", 409)])
def test_inflight_revocation_cancels_work_and_cannot_resurrect_state(make_research, operation, action, expected):
    client, headers, calls = make_research(openai_api_key="server-test-key")

    async def exercise():
        research = client.app.state.research
        entered = asyncio.Event()
        cancelled = asyncio.Event()

        async def handler(request):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                # Even an upstream transport which completes during cancellation cannot restore state.
                return provider(request)

        research.client._transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client.app), base_url="http://testserver") as api:
            if operation == "models":
                pending = asyncio.create_task(api.get(ROOT + "/models", headers=headers))
            else:
                data = payload() if operation == "chat" else {"api_key": SECRET}
                pending = asyncio.create_task(api.post(ROOT + "/" + operation, headers=headers, json=data))
            await asyncio.wait_for(entered.wait(), 1)
            old = research.sessions[parent(headers)]
            if action == "logout":
                assert (await api.post("/api/logout", headers=headers)).status_code == 204
            elif action == "disconnect":
                assert (await api.delete(ROOT + "/connection", headers=headers)).json() == {
                    "configured": True, "key_source": "server"}
            else:
                research.credentials.operators[parent(headers)] = research.credentials.now()
                research.credentials.prune()
            response = await asyncio.wait_for(pending, 1)
            assert response.status_code == expected
            assert cancelled.is_set()
            assert not old.key and not old.results
            assert parent(headers) not in research.sessions
            assert not research.tasks and not research.model_cache

    client.portal.call(exercise)


def test_total_deadline_and_shutdown_cancel_tracked_work(make_research):
    client, headers, calls = make_research(openai_api_key=SECRET, openai_timeout=0.02)

    async def exercise():
        research = client.app.state.research

        class SlowStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                while True:
                    await asyncio.sleep(0.001)
                    yield b" "

        research.client._transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=SlowStream()))
        with pytest.raises(HTTPException) as error:
            await research.chat(parent(headers), research.session(parent(headers)), Chat(**payload()))
        assert error.value.status_code == 504
        assert not research.tasks
        session = research.session(parent(headers))
        pending = asyncio.create_task(research.chat(parent(headers), session, Chat(**payload())))
        await asyncio.sleep(0)
        await research.aclose()
        with pytest.raises(HTTPException) as error:
            await pending
        assert error.value.status_code == 503
        assert not research.tasks and not research.sessions and not research.model_cache
        assert research.sweeper.done()

    client.portal.call(exercise)


@pytest.mark.parametrize("options", [
    {"openai_timeout": 0}, {"openai_timeout": -1}, {"openai_timeout": 110.01},
    {"openai_timeout": float("nan")}, {"openai_timeout": float("inf")},
    {"openai_max_output_tokens": 255}, {"openai_max_output_tokens": 8193},
    {"openai_max_output_tokens": True}, {"openai_max_output_tokens": 2048.5},
])
def test_config_bounds(options):
    with pytest.raises(ValueError, match="OPENAI"):
        Settings(admin_password="test", **options)


def test_config_defaults_environment_and_repr(monkeypatch):
    settings = Settings(admin_password="test")
    assert settings.openai_api_key == ""
    assert settings.openai_timeout == 90 and settings.openai_max_output_tokens == 2048
    monkeypatch.setenv("ADMIN_PASSWORD", "test")
    monkeypatch.setenv("OPENAI_API_KEY", SECRET)
    monkeypatch.setenv("OPENAI_TIMEOUT", "110")
    monkeypatch.setenv("OPENAI_MAX_OUTPUT_TOKENS", "256")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://ignored.example")
    settings = Settings.from_env()
    assert settings.openai_api_key == SECRET and SECRET not in repr(settings)
    assert settings.openai_timeout == 110 and settings.openai_max_output_tokens == 256
    assert not hasattr(settings, "openai_base_url")
