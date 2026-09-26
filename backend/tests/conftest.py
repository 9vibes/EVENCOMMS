from contextlib import ExitStack

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.config import Settings
from backend.main import create_app


@pytest.fixture
def make_client(tmp_path):
    with ExitStack() as stack:
        def make(ollama=None, **options):
            options.setdefault("database_path", tmp_path / "db.sqlite3")
            options.setdefault("frontend_dist", tmp_path / "dist")
            config = Settings(admin_password="test-admin-password", **options)
            app = create_app(config)
            client = stack.enter_context(TestClient(app))
            app.state.transcriber.run = lambda pcm: "Unsent speech draft"
            client.portal.call(app.state.ollama.aclose)
            handler = ollama or (lambda request: httpx.Response(200, json={
                "message": {"content": "Please take the next left."}}))
            app.state.ollama = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            return client

        yield make


@pytest.fixture
def client(make_client):
    return make_client()


@pytest.fixture
def operator(client):
    response = client.post("/api/login", json={"password": "test-admin-password"})
    assert response.status_code == 200
    return {"Authorization": "Bearer " + response.json()["token"]}


@pytest.fixture
def pair_wearer(client, operator):
    def pair(name="Wearer"):
        code = client.post("/api/pairings", headers=operator).json()["code"]
        response = client.post("/api/pair", json={"code": code, "name": name})
        assert response.status_code == 200
        data = response.json()
        return data["session_id"], {"Authorization": "Bearer " + data["token"]}

    return pair
