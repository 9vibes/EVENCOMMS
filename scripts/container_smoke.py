"""Exercise a local image through Docker; an optional PCM fixture enables real STT."""

import argparse
import json
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from uuid import uuid4


def docker(*args, stdin=None, env=None, check=True, timeout=180):
    try:
        result = subprocess.run(
            ["docker", *args], input=stdin, text=True, capture_output=True,
            timeout=timeout, env={**os.environ, **(env or {})},
        )
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError(f"Docker {args[0]} could not complete; private diagnostics suppressed") from None
    if check and result.returncode:
        # Do not dump commands, stdin, or container logs containing credentials.
        raise RuntimeError(f"Docker {args[0]} failed (exit {result.returncode})")
    return result.stdout.strip()


# Run inside the offline initializer. Neither the token nor its hash leaves it.
INIT_CHECK = """
import hashlib
import json
import re
import stat
import subprocess
import sys
from pathlib import Path

root = Path('/codex-auth')
token = root / 'token'
before = hashlib.sha256(token.read_bytes()).digest() if token.exists() else None
for _ in range(2):
    result = subprocess.run([sys.executable, '-m', 'backend.init_data',
                             '--config-dir', '/config', '--codex-auth-dir', '/codex-auth'],
                            capture_output=True, timeout=30)
    assert result.returncode == 0
    for path, mode in ((root, 0o750), (token, 0o440)):
        info = path.lstat()
        assert (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) == (10001, 10002, mode)
    assert root.is_dir() and stat.S_ISREG(token.lstat().st_mode) and token.stat().st_nlink == 1
    raw = token.read_bytes()
    assert re.fullmatch(rb'[0-9a-f]{64}', raw)
    current = hashlib.sha256(raw).digest()
    assert before is None or before == current
    before = current
    assert {path.name for path in root.iterdir()} == {'token'}
    assert {path.name for path in Path('/config').iterdir()} == {'nginx.conf', 'mediamtx.yml'}
    for name in ('nginx.conf', 'mediamtx.yml'):
        assert Path('/config', name).read_bytes() == Path('/app/infra', name).read_bytes()
print(json.dumps({'private_init': True, 'token_preserved': True}))
"""


def request(base, path, *, token=None, data=None, expected=200, timeout=150):
    headers = {}
    if token:
        headers["Authorization"] = "Bearer " + token
    if data is not None:
        if isinstance(data, bytes):
            headers["Content-Type"] = "application/octet-stream"
        else:
            headers["Content-Type"] = "application/json"
            data = json.dumps(data).encode()
    req = urllib.request.Request(base + path, headers=headers, data=data)
    try:
        response = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        if response.status != expected:
            raise RuntimeError(f"{path}: expected HTTP {expected}, got {response.status}")
        raw = response.read()
        return json.loads(raw) if response.headers.get_content_type() == "application/json" else raw


def socket_check(container, token, session_id):
    # Test origin/Host semantics without requiring a separate proxy or host dependencies.
    # Pass the bearer credential on stdin, never in CLI arguments or the URL.
    docker("exec", "-i", container, "python", "-c", """
import asyncio
import json
import sys
from websockets.legacy.client import connect

credentials = json.load(sys.stdin)

async def check():
    for host, origin in [
        ("127.0.0.1:8000", "http://127.0.0.1:8000"),
        ("rewritten-backend:8000", "https://smoke.example.test"),
    ]:
        async with connect("ws://" + host + "/api/wearer", host="127.0.0.1", port=8000,
                           origin=origin, open_timeout=10, close_timeout=5) as socket:
            await socket.send(json.dumps({"type": "auth", "token": credentials["token"]}))
            ready = json.loads(await asyncio.wait_for(socket.recv(), 10))
            assert ready["type"] == "ready"
            assert ready["session_id"] == credentials["session_id"]
            await socket.send('{"type":"ping"}')
            assert json.loads(await asyncio.wait_for(socket.recv(), 10)) == {"type": "pong"}

asyncio.run(check())
""", stdin=json.dumps({"token": token, "session_id": session_id}))


def cache_snapshot(container):
    return json.loads(docker("exec", container, "python", "-c", """
import json
from pathlib import Path
root = Path("/data/models")
# Ignore mutable download bookkeeping (refs and locks), not model payloads.
files = {str(path.relative_to(root)): [path.stat().st_size, path.stat().st_mtime_ns]
         for path in root.rglob("*") if path.is_file()
         and ("snapshots" in path.parts or "blobs" in path.parts)}
assert any(name.endswith("model.bin") and stat[0] > 1000000 for name, stat in files.items())
print(json.dumps(files))
"""))


def stream_secret_hashes(container):
    return json.loads(docker("exec", container, "python", "-c", """
import hashlib
import json
import sqlite3
with sqlite3.connect('/data/evencomms.sqlite3') as database:
    exists = database.execute("SELECT 1 FROM sqlite_master WHERE name='stream_settings'").fetchone()
    row = database.execute('SELECT publisher_secret, reader_secret FROM stream_settings WHERE id=1').fetchone() if exists else None
print(json.dumps([hashlib.sha256(value.encode()).hexdigest() for value in row] if row else None))
"""))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, help="Local Docker image tag or ID (never pulled)")
    parser.add_argument("--upgrade-from", help="Optional local prior image for the first boot (never pulled)")
    parser.add_argument("--speech-pcm", type=Path,
                        help="Raw mono 16 kHz s16le speech mentioning 'north entrance'; enables real base.en STT")
    args = parser.parse_args()
    pcm = args.speech_pcm.read_bytes() if args.speech_pcm else None
    if pcm is not None and (not pcm or len(pcm) % 2 or len(pcm) > 480000):
        parser.error("Speech must be nonempty raw PCM s16le, at most 15 seconds at 16 kHz")

    image = json.loads(docker("image", "inspect", args.image))[0]
    if image["Os"] != "linux" or image["Architecture"] != "amd64":
        raise RuntimeError("Smoke test requires a linux/amd64 image")
    if image["Config"]["User"] != "10001:10001":
        raise RuntimeError("Image must default to user 10001:10001")
    image_id = image["Id"]
    first_image = image_id
    if args.upgrade_from:
        previous = json.loads(docker("image", "inspect", args.upgrade_from))[0]
        if (previous["Os"] != "linux" or previous["Architecture"] != "amd64"
                or previous["Config"]["User"] != "10001:10001"):
            raise RuntimeError("Upgrade source must be a linux/amd64 app image using 10001:10001")
        first_image = previous["Id"]
    name = "evencomms-smoke-" + uuid4().hex[:12]
    volume = name + "-data"
    config_volume = name + "-config"
    auth_volume = name + "-codex-auth"
    password = secrets.token_urlsafe(32)
    environment = {"ADMIN_PASSWORD": password}
    explicit_origin = "https://smoke.example.test"
    created_volumes = []
    try:
        for owned_volume in (volume, config_volume, auth_volume):
            # Track our unique name even if Docker creates it but the CLI times out.
            created_volumes.append(owned_volume)
            docker("volume", "create", owned_volume)
        # Same offline, read-only root initializer used at install and upgrade time.
        initializer = (
            "run", "--rm", "--name", name + "-init", "--pull=never",
            "--user", "0:0", "--read-only", "--network", "none",
            "--security-opt", "no-new-privileges:true",
            "--mount", f"type=volume,source={volume},target=/data,volume-nocopy",
            "--mount", f"type=volume,source={config_volume},target=/config,volume-nocopy",
            "--mount", f"type=volume,source={auth_volume},target=/codex-auth,volume-nocopy",
            image_id,
        )
        snapshot_script = """
import hashlib
import json
import stat
from pathlib import Path
root = Path('/data')
snapshot = {}
for path in sorted(root.rglob('*')):
    if path.is_file():
        info = path.stat()
        with path.open('rb') as content:
            digest = hashlib.file_digest(content, 'sha256').hexdigest()
        snapshot[str(path.relative_to(root))] = [
            digest, info.st_size, info.st_mtime_ns, stat.S_IMODE(info.st_mode),
            info.st_uid, info.st_gid,
        ]
print(json.dumps(snapshot, sort_keys=True))
"""
        saved_message = None
        wearer = None
        cache = None
        stream_secrets = None
        for attempt in range(2):
            boot_image = first_image if attempt == 0 else image_id
            if attempt:
                docker(*initializer, "python", "-c", """
from pathlib import Path
for name in ('nginx.conf', 'mediamtx.yml'):
    Path('/config', name).write_bytes(b'stale config from previous release')
marker = Path('/data/models/smoke-preserved-custom-file')
marker.write_bytes(b'custom model cache content')
marker.chmod(0o640)
""")
            before_init = json.loads(docker(*initializer, "python", "-c", snapshot_script))
            # Existing SQLite files are deliberately tightened to the app user's 0600.
            for path, info in before_init.items():
                if path in {"evencomms.sqlite3", "evencomms.sqlite3-wal", "evencomms.sqlite3-shm"}:
                    info[3:] = [0o600, 10001, 10001]
            docker(*initializer, "python", "-c", INIT_CHECK)
            assert json.loads(docker(*initializer, "python", "-c", snapshot_script)) == before_init
            docker("run", "--rm", "--name", name + "-init", "--pull=never",
                   "--user", "101:101", "--read-only", "--network", "none", "--cap-drop", "ALL",
                   "--security-opt", "no-new-privileges:true",
                    "--mount", f"type=volume,source={config_volume},target=/config,readonly,volume-nocopy",
                    "--mount", f"type=volume,source={auth_volume},target=/run/codex-auth,readonly,volume-nocopy",
                   image_id, "python", "-c", """
import os
import stat
from pathlib import Path
assert os.getuid() == os.getgid() == 101
target = Path('/config')
assert stat.S_IMODE(target.stat().st_mode) == 0o755
assert {p.name for p in target.iterdir()} == {'nginx.conf', 'mediamtx.yml'}
for name in ('nginx.conf', 'mediamtx.yml'):
    installed = target / name
    assert installed.read_bytes() == Path('/app/infra', name).read_bytes()
    assert stat.S_IMODE(installed.stat().st_mode) == 0o644
assert b'$http_host' in (target / 'nginx.conf').read_bytes()
try:
    Path('/run/codex-auth/token').read_bytes()
except PermissionError:
    pass
else:
    raise AssertionError('UID 101 must not read the private bridge token')
""")
            print(f"Starting {'fresh' if attempt == 0 else 'replacement'} container", flush=True)
            docker("run", "--detach", "--name", name, "--pull=never", "--init",
                   "--read-only", "--cap-drop", "ALL",
                   "--security-opt", "no-new-privileges:true",
                   "--tmpfs", "/tmp:rw,noexec,nosuid,size=256m",
                   "--mount", f"type=volume,source={volume},target=/data,volume-nocopy",
                   "--publish", "127.0.0.1::8000",
                   "--env", "ADMIN_PASSWORD", "--env", "OLLAMA_URL=",
                   "--env", f"ALLOWED_ORIGINS={explicit_origin}",
                   "--env", f"STT_ENABLED={'true' if pcm is not None else 'false'}",
                   "--env", "STT_TIMEOUT=140", boot_image, env=environment)
            info = json.loads(docker("inspect", name))[0]
            assert info["Image"] == boot_image
            assert info["HostConfig"]["ReadonlyRootfs"]
            port = info["NetworkSettings"]["Ports"]["8000/tcp"][0]["HostPort"]
            base = "http://127.0.0.1:" + port
            deadline = time.monotonic() + 90
            while True:
                try:
                    assert request(base, "/health", timeout=3) == {"status": "ok"}
                    break
                except (OSError, RuntimeError):
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Container did not become healthy within 90 seconds") from None
                    time.sleep(1)
            docker("exec", name, "python", "-c", """
import os
from pathlib import Path
import faster_whisper
assert os.getuid() == os.getgid() == 10001
assert Path("/data/evencomms.sqlite3").is_file()
assert os.access("/data/models", os.W_OK)
""")
            for page in ("/", "/glasses.html"):
                assert b"<html" in request(base, page).lower()
            request(base, "/api/sessions", expected=401)
            request(base, "/api/me", expected=401)
            operator = request(base, "/api/login", data={"password": password})["token"]
            status = request(base, "/api/status", token=operator)
            assert status["stt_enabled"] == (pcm is not None)
            assert status["stt_model"] == "base.en"
            if attempt == 0:
                stream_secrets = stream_secret_hashes(name)
                code = request(base, "/api/pairings", token=operator, data={})["code"]
                wearer = request(base, "/api/pair", data={"code": code, "name": "Release smoke"})
                saved_message = request(base, "/api/messages", token=wearer["token"],
                                        data={"text": "Meet at the north entrance", "client_id": str(uuid4())})
                if pcm is not None:
                    docker("exec", name, "python", "-c",
                           'from pathlib import Path; assert not any(p.is_file() for p in Path("/data/models").rglob("*"))')
            else:
                if stream_secrets is not None:
                    assert stream_secret_hashes(name) == stream_secrets
                assert request(base, "/api/me", token=wearer["token"])["messages"] == [saved_message]
                assert any(row["id"] == wearer["session_id"]
                           for row in request(base, "/api/sessions", token=operator))
                assert request(base, f'/api/sessions/{wearer["session_id"]}/messages',
                               token=operator) == [saved_message]
                if cache is not None:
                    assert cache_snapshot(name) == cache
            socket_check(name, wearer["token"], wearer["session_id"])
            if pcm is not None:
                print("Checking real CPU base.en transcription", flush=True)
                request(base, "/api/transcribe", token=wearer["token"], data=b"x", expected=422)
                text = request(base, "/api/transcribe", token=wearer["token"], data=pcm)["text"]
                words = set(re.findall(r"[a-z]+", text.lower()))
                if not words.intersection({"north", "entrance"}):
                    raise RuntimeError("STT did not recognize any expected fixture words")
                current_cache = cache_snapshot(name)
                if cache is not None:
                    assert all(current_cache.get(path) == stat for path, stat in cache.items())
                cache = current_cache
            docker("stop", "--time", "15", name)
            docker("rm", name)
        print("Smoke passed: non-root, read-only, auth, WebSocket origins, database persistence,"
              " image-managed configs readable by UID 101, private token denied to UID 101,"
              " idempotent private initializer, config upgrade and initializer data preservation"
              + (", real CPU STT and persistent model cache" if pcm is not None else " (STT skipped)"))
    finally:
        cleanup = [("rm", "--force", name, name + "-init")]
        cleanup.extend(("volume", "rm", owned_volume) for owned_volume in reversed(created_volumes))
        for command in cleanup:
            try:
                docker(*command, check=False)
            except RuntimeError:
                print("Warning: Docker smoke resource cleanup failed", flush=True)


if __name__ == "__main__":
    def interrupted(signum, frame):
        raise RuntimeError("Container smoke interrupted")

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        main()
    except Exception:
        print("Container smoke failed; private diagnostics suppressed", file=sys.stderr)
        sys.exit(1)
