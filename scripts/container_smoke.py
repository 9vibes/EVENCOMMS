"""Exercise a local image through Docker; an optional PCM fixture enables real STT."""

import argparse
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import time
import urllib.error
import urllib.request
from uuid import uuid4


def docker(*args, stdin=None, env=None, check=True):
    result = subprocess.run(
        ["docker", *args], input=stdin, text=True, capture_output=True,
        timeout=180, env={**os.environ, **(env or {})},
    )
    if check and result.returncode:
        # Do not dump commands, stdin, or container logs containing credentials.
        raise RuntimeError(f"Docker {args[0]} failed (exit {result.returncode})")
    return result.stdout.strip()


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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, help="Local Docker image tag or ID (never pulled)")
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
    name = "evencomms-smoke-" + uuid4().hex[:12]
    volume = name + "-data"
    password = secrets.token_urlsafe(32)
    environment = {"ADMIN_PASSWORD": password}
    explicit_origin = "https://smoke.example.test"
    created_volume = False
    try:
        docker("volume", "create", volume)
        created_volume = True
        # Same image, root only for the dedicated data-volume permission initializer.
        docker("run", "--rm", "--name", name + "-init", "--pull=never",
               "--user", "0:0", "--read-only", "--network", "none",
               "--security-opt", "no-new-privileges:true",
               "--mount", f"type=volume,source={volume},target=/data,volume-nocopy",
               image_id, "python", "-m", "backend.init_data")
        saved_message = None
        wearer = None
        cache = None
        for attempt in range(2):
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
                   "--env", "STT_TIMEOUT=140", image_id, env=environment)
            info = json.loads(docker("inspect", name))[0]
            assert info["Image"] == image_id
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
                code = request(base, "/api/pairings", token=operator, data={})["code"]
                wearer = request(base, "/api/pair", data={"code": code, "name": "Release smoke"})
                saved_message = request(base, "/api/messages", token=wearer["token"],
                                        data={"text": "Meet at the north entrance", "client_id": str(uuid4())})
                if pcm is not None:
                    docker("exec", name, "python", "-c",
                           'from pathlib import Path; assert not any(p.is_file() for p in Path("/data/models").rglob("*"))')
            else:
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
        print("Smoke passed: non-root, read-only, auth, WebSocket origins, database persistence"
              + (", real CPU STT and persistent model cache" if pcm is not None else " (STT skipped)"))
    finally:
        docker("rm", "--force", name, name + "-init", check=False)
        if created_volume:
            docker("volume", "rm", volume, check=False)


if __name__ == "__main__":
    main()
