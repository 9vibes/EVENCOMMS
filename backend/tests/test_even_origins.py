import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from backend.config import Settings
from backend.main import create_app


def preflight(client, origin, path='/health', method='GET'):
    return client.options(path, headers={
        'Origin': origin, 'Access-Control-Request-Method': method,
        'Access-Control-Request-Headers': 'Content-Type,Authorization',
    })


@pytest.mark.parametrize('port', [1, 63233, 63234, 65535])
def test_localhost_pairing_and_authenticated_socket(make_client, port):
    client = make_client(allow_even_localhost=True)
    origin = f'http://127.0.0.1:{port}'
    for path, method in [('/health', 'GET'), ('/api/pair', 'POST')]:
        response = preflight(client, origin, path, method)
        assert response.status_code == 200
        assert response.headers['access-control-allow-origin'] == origin
        assert 'access-control-allow-credentials' not in response.headers
    response = client.get('/health', headers={'Origin': origin})
    assert response.headers['access-control-allow-origin'] == origin
    operator = client.post('/api/login', json={'password': 'test-admin-password'}).json()['token']
    code = client.post('/api/pairings', headers={'Authorization': f'Bearer {operator}'}).json()['code']
    response = client.post('/api/pair', headers={'Origin': origin}, json={'code': code, 'name': 'Even'})
    assert response.status_code == 200
    assert response.headers['access-control-allow-origin'] == origin
    with client.websocket_connect('/api/wearer', headers={'Origin': origin}) as socket:
        socket.send_json({'type': 'auth', 'token': response.json()['token']})
        assert socket.receive_json()['type'] == 'ready'
    with client.websocket_connect('/api/wearer', headers={'Origin': origin}) as socket:
        socket.send_json({'type': 'auth', 'token': 'invalid'})
        with pytest.raises(WebSocketDisconnect) as error:
            socket.receive_json()
        assert error.value.code == 4401
    with pytest.raises(WebSocketDisconnect) as error:
        with client.websocket_connect('/api/wearer?token=invalid', headers={'Origin': origin}):
            pass
    assert error.value.code == 4403


@pytest.mark.parametrize('origin', [
    'null', 'https://evil.example', 'http://127.0.0.1.evil.example:63233',
    'http://127.0.0.1:63233@evil.example', 'http://evil.example@127.0.0.1:63233',
    'http://localhost:63233', 'http://127.0.0.2:63233', 'http://[::1]:63233',
    'https://127.0.0.1:63233', 'http://127.0.0.1', 'http://127.0.0.1:0',
    'http://127.0.0.1:65536', 'http://127.0.0.1:063233',
    'http://127.0.0.1:63233/', 'http://127.0.0.1:63233?x=1',
    'http://127.0.0.1:63233#fragment', 'http://127.0.0.1:63233\n',
])
def test_localhost_opt_in_does_not_allow_other_origins(make_client, origin):
    client = make_client(allow_even_localhost=True)
    assert preflight(client, origin).status_code == 400
    assert 'access-control-allow-origin' not in client.get('/health', headers={'Origin': origin}).headers
    with pytest.raises(WebSocketDisconnect) as error:
        with client.websocket_connect('/api/wearer', headers={'Origin': origin}):
            pass
    assert error.value.code == 4403


def test_localhost_is_disabled_by_default(make_client):
    client = make_client()
    origin = 'http://127.0.0.1:63233'
    assert preflight(client, origin).status_code == 400
    with pytest.raises(WebSocketDisconnect) as error:
        with client.websocket_connect('/api/wearer', headers={'Origin': origin}):
            pass
    assert error.value.code == 4403


def test_explicit_origin_still_works_with_opt_in(make_client):
    origin = 'https://stone.example'
    client = make_client(allow_even_localhost=True, allowed_origins=(origin,))
    assert preflight(client, origin).headers['access-control-allow-origin'] == origin


@pytest.mark.parametrize('enabled', ['true', 'false'])
def test_environment_configures_cors_and_socket(monkeypatch, tmp_path, enabled):
    monkeypatch.setenv('ADMIN_PASSWORD', 'test-password')
    monkeypatch.setenv('DATABASE_PATH', str(tmp_path / 'db.sqlite3'))
    monkeypatch.setenv('ALLOW_EVEN_LOCALHOST', enabled)
    monkeypatch.setenv('ALLOWED_ORIGINS', '')
    monkeypatch.setenv('CODEX_BRIDGE_TOKEN', '')
    monkeypatch.setenv('CODEX_BRIDGE_TOKEN_FILE', '')
    monkeypatch.setenv('CODEX_BRIDGE_URL', '')
    assert Settings.from_env().allow_even_localhost is (enabled == 'true')
    with TestClient(create_app()) as client:
        origin = 'http://127.0.0.1:63233'
        assert preflight(client, origin).status_code == (200 if enabled == 'true' else 400)
        with pytest.raises(WebSocketDisconnect) as error:
            with client.websocket_connect('/api/wearer', headers={'Origin': origin}) as socket:
                socket.send_json({'type': 'auth', 'token': 'invalid'})
                socket.receive_json()
        assert error.value.code == (4401 if enabled == 'true' else 4403)


def test_invalid_setting_rejected(monkeypatch):
    monkeypatch.setenv('ALLOW_EVEN_LOCALHOST', 'yes')
    with pytest.raises(ValueError, match='ALLOW_EVEN_LOCALHOST'):
        create_app()
    with pytest.raises(ValueError, match='ALLOW_EVEN_LOCALHOST'):
        Settings(admin_password='test', allow_even_localhost='false')
