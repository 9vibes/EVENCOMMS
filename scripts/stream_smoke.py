"""Publish synthetic RTMP and verify real authenticated HLS on an isolated running stack."""

import argparse
from collections import deque
from http.cookies import SimpleCookie
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request


LIVE = "/api/stream/live/"
MANIFEST = LIVE + "index.m3u8"
COOKIE = "evencomms_playback"


class SmokeFailure(Exception):
    pass


def require(condition, message):
    if not condition:
        raise SmokeFailure(message)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class API:
    def __init__(self, base):
        self.base = base
        # Never forward credentials through environment proxies or redirects.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def request(self, path, *, method="GET", token=None, cookie=None, data=None,
                headers=None, expected=200, deadline=None):
        headers = dict(headers or {})
        if token:
            headers["Authorization"] = "Bearer " + token
        if cookie:
            headers["Cookie"] = COOKIE + "=" + cookie
        if data is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(data).encode()
        timeout = 5 if deadline is None else min(5, deadline - time.monotonic())
        require(timeout > 0, "HTTP polling deadline exceeded")
        request = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            response = self.opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            raw = response.read(8 * 1024 * 1024 + 1)
            require(len(raw) <= 8 * 1024 * 1024, "HTTP response exceeded smoke-test size limit")
            if expected is not None:
                require(response.status == expected,
                        f"Expected HTTP {expected}, received {response.status}")
            return response.status, response.headers, raw

    def json(self, path, **kwargs):
        _, _, raw = self.request(path, **kwargs)
        return json.loads(raw)


class Publisher:
    def __init__(self, url):
        self.errors = deque(maxlen=8)
        self.process = subprocess.Popen([
            "ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error",
            "-re", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25",
            "-re", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
            "-map", "0:v:0", "-map", "1:a:0", "-c:v", "libx264",
            "-pix_fmt", "yuv420p", "-profile:v", "baseline", "-preset", "ultrafast",
            "-tune", "zerolatency", "-g", "25", "-c:a", "aac", "-t", "120",
            "-f", "flv", url,
        ], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            env={key: value for key, value in os.environ.items()
                 if key not in {"ADMIN_PASSWORD", "FFREPORT"}})
        # FFmpeg errors can contain the complete RTMP credential. Drain a bounded
        # private buffer, never print it or a CalledProcessError/TimeoutExpired.
        self.reader = threading.Thread(target=self.drain, daemon=True)
        self.reader.start()
        self.watchdog = threading.Timer(125, self.kill)
        self.watchdog.daemon = True
        self.watchdog.start()

    def drain(self):
        while chunk := self.process.stderr.read(1024):
            self.errors.append(chunk)

    def kill(self):
        if self.process.poll() is None:
            self.process.kill()

    def close(self):
        self.watchdog.cancel()
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.kill()
        self.process.wait(timeout=3)
        self.reader.join(timeout=3)
        self.process.stderr.close()
        self.errors.clear()


def wait_status(api, token, online, publisher=None):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if publisher:
            require(publisher.process.poll() is None, "Synthetic publisher exited unexpectedly")
        status = api.json("/api/stream/status", token=token, deadline=deadline)
        if status.get("media_available") is True and status.get("online") is online:
            return status
        time.sleep(min(0.5, max(0, deadline - time.monotonic())))
    raise SmokeFailure("Publisher status did not reach the expected state within 30 seconds")


def playback_cookie(api, token):
    _, headers, raw = api.request("/api/stream/playback-session", method="POST", token=token)
    body = json.loads(raw)
    require(set(body) == {"expires_in"} and type(body["expires_in"]) is int
            and 0 < body["expires_in"] <= 300, "Invalid playback-session response")
    jar = SimpleCookie()
    jar.load(headers.get("Set-Cookie", ""))
    require(COOKIE in jar, "Playback cookie was not issued")
    cookie = jar[COOKIE]
    require(cookie.value and cookie["httponly"] and cookie["samesite"].lower() == "strict"
            and cookie["path"] == LIVE and int(cookie["max-age"]) == body["expires_in"],
            "Playback cookie scope, flags or lifetime are incorrect")
    require(bool(cookie["secure"]) == api.base.startswith("https://"),
            "COOKIE_SECURE must match the test origin scheme")
    return cookie.value


def media_path(parent, reference):
    # Do not follow upstream origins, traversal, or credential-bearing URLs.
    require(re.fullmatch(r"[A-Za-z0-9_-]{1,160}\.(?:m3u8|mp4|m4s)", reference),
            "Unexpected or credential-bearing HLS reference")
    return urllib.parse.urljoin(parent, reference)


def check_hls(api, cookie):
    deadline = time.monotonic() + 30

    def fetch(path):
        while time.monotonic() < deadline:
            status, headers, raw = api.request(path, cookie=cookie, expected=None, deadline=deadline)
            if status == 200:
                require(raw, "Empty HLS response")
                return headers, raw
            require(status in {404, 502, 503}, "HLS request was rejected")
            time.sleep(min(0.5, max(0, deadline - time.monotonic())))
        raise SmokeFailure("HLS files were not ready within 30 seconds")

    _, master = fetch(MANIFEST)
    master = master.decode("utf-8")
    require(master.startswith("#EXTM3U") and "#EXT-X-STREAM-INF:" in master,
            "Expected an HLS master playlist")
    children = [line.strip() for line in master.splitlines() if line and not line.startswith("#")]
    require(children, "Master playlist has no child")
    child = media_path(MANIFEST, children[0])
    while time.monotonic() < deadline:
        _, playlist = fetch(child)
        playlist = playlist.decode("utf-8")
        require(playlist.startswith("#EXTM3U"), "Invalid HLS child playlist")
        init = re.search(r'#EXT-X-MAP:URI="([^"]+)"', playlist)
        segments = [line.strip() for line in playlist.splitlines() if line and not line.startswith("#")]
        if init and segments:
            break
        time.sleep(min(0.5, max(0, deadline - time.monotonic())))
    else:
        raise SmokeFailure("HLS child did not contain initialization and media within 30 seconds")
    init_path = media_path(child, init.group(1))
    media = media_path(child, segments[-1])
    for path, boxes in [(init_path, (b"ftyp", b"moov")), (media, (b"moof", b"mdat"))]:
        headers, raw = fetch(path)
        require("mp4" in headers.get_content_type() and all(box in raw for box in boxes),
                "Expected real fMP4 initialization/media bytes and content type")
        if headers.get("Content-Length"):
            require(int(headers["Content-Length"]) == len(raw), "Incorrect media Content-Length")
        if path == init_path:
            status, ranged_headers, ranged = api.request(
                path, cookie=cookie, headers={"Range": "bytes=0-31"}, expected=None, deadline=deadline)
            if status == 206:
                require(ranged == raw[:32] and ranged_headers.get("Content-Range") == f"bytes 0-31/{len(raw)}",
                        "Incorrect partial media response")
                require(ranged_headers.get_content_type() == headers.get_content_type(),
                        "Range response changed media content type")
                if ranged_headers.get("Content-Length"):
                    require(int(ranged_headers["Content-Length"]) == 32, "Incorrect range Content-Length")
            else:
                require(status == 200 and ranged == raw and not ranged_headers.get("Content-Range")
                        and headers.get("Accept-Ranges", "").lower() != "bytes",
                        "Upstream range behavior is inconsistent")
                print("HLS upstream returned the complete file for Range (supported fallback).")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, help="Explicit isolated public nginx origin; no default target")
    parser.add_argument("--rtmp-host", default="127.0.0.1", help="Encoder destination, not advertised PUBLIC_HOST")
    parser.add_argument("--rtmp-port", type=int, default=21936)
    parser.add_argument("--browser", action="store_true", help="Also run real Chromium playback checks")
    args = parser.parse_args()
    base = urllib.parse.urlsplit(args.base_url)
    require(base.scheme in {"http", "https"} and base.hostname and base.port != 0
            and not base.username and not base.password and base.path in {"", "/"}
            and not base.query and not base.fragment, "--base-url must be an HTTP(S) origin without credentials")
    require(re.fullmatch(r"[A-Za-z0-9.:-]+", args.rtmp_host) and 0 < args.rtmp_port < 65536,
            "Invalid RTMP host or port")
    password = os.environ.get("ADMIN_PASSWORD")
    require(password, "ADMIN_PASSWORD is required in the environment")
    require(shutil.which("ffmpeg"), "FFmpeg must be installed with lavfi, libx264 and AAC support")
    if args.browser:
        require(shutil.which("node"), "Node is required for --browser")
    api = API(args.base_url.rstrip("/"))
    tokens = []
    publisher = None
    observer = None
    online_session = None
    stage = "login and safety preflight"
    try:
        operator = api.json("/api/login", method="POST", data={"password": password})["token"]
        tokens.append(operator)
        status = api.json("/api/stream/status", token=operator)
        require(status.get("online") is False, "Refusing to publish: source is already online or status is invalid")
        require(status.get("enabled") is True and status.get("media_available") is True,
                "Streaming must be enabled and MediaMTX available")

        stage = "authentication and public ingress"
        routes = [("GET", "/api/stream/status"), ("GET", "/api/stream/settings"),
                  ("POST", "/api/stream/playback-session"), ("POST", "/api/logout")]
        for method, path in routes:
            for token in (None, "invalid-smoke-token"):
                api.request(path, method=method, token=token, expected=401)
        for token in (None, operator):
            api.request(MANIFEST, token=token, expected=401)
        for path in ("/internal", "/internal/", "/internal/media/auth"):
            for method in ("GET", "POST", "PUT", "DELETE", "OPTIONS"):
                api.request(path, method=method, expected=404)
        settings = api.json("/api/stream/settings", token=operator)
        require(settings.get("enabled") is True and settings.get("playback_url") == MANIFEST,
                "Unexpected stream settings")
        key = settings["stream_key"]
        require(re.fullmatch(r"stream\?user=publisher&pass=[A-Za-z0-9_-]+", key), "Unexpected publishing key format")
        cookie = playback_cookie(api, operator)
        for method, path in routes:
            api.request(path, method=method, cookie=cookie, expected=401)
        api.request("/api/stream/playback-session", method="POST", cookie=cookie,
                    headers={"Origin": "https://csrf.invalid"}, expected=401)
        _, headers, _ = api.request("/api/stream/playback-session", method="OPTIONS", expected=400,
                                    headers={"Origin": "https://csrf.invalid",
                                             "Access-Control-Request-Method": "POST",
                                             "Access-Control-Request-Headers": "authorization"})
        require(not headers.get("Access-Control-Allow-Origin"), "Untrusted CORS origin was allowed")
        renewed = playback_cookie(api, operator)
        require(renewed != cookie, "Playback renewal did not rotate the cookie")
        api.request(MANIFEST, cookie=cookie, expected=401)
        cookie = renewed
        observer = api.json("/api/login", method="POST", data={"password": password})["token"]
        tokens.append(observer)

        host = f"[{args.rtmp_host}]" if ":" in args.rtmp_host else args.rtmp_host
        destination = f"rtmp://{host}:{args.rtmp_port}/live/"
        stage = "wrong-key RTMP rejection"
        require(api.json("/api/stream/status", token=observer).get("online") is False,
                "Refusing to publish: another source became online")
        wrong = Publisher(destination + "stream?user=publisher&pass=" + secrets.token_urlsafe(32))
        try:
            try:
                result = wrong.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                raise SmokeFailure("Wrong-key publisher was not rejected within 10 seconds") from None
            require(result != 0, "Wrong-key publisher unexpectedly succeeded")
        finally:
            wrong.close()
        require(api.json("/api/stream/status", token=observer).get("online") is False,
                "Refusing to publish: source became online during wrong-key test")

        stage = "synthetic RTMP and authenticated HLS"
        publisher = Publisher(destination + key)
        status = wait_status(api, observer, True, publisher)
        online_session = status.get("publisher_session_id")
        require(online_session and "H264" in status.get("tracks", [])
                and any(track in status["tracks"] for track in ("MPEG4Audio", "MPEG-4 Audio")),
                "Publisher status does not report the expected H.264/AAC feed")
        check_hls(api, cookie)
        print("RTMP rejection, H.264/AAC ingest, authentication and real fMP4 HLS passed.")

        if args.browser:
            stage = "real browser playback"
            script = Path(__file__).resolve().parents[1] / "frontend/scripts/check-stream.mjs"
            # Only environment carries browser login credentials; suppress debug
            # output that could otherwise include HTTP headers or browser commands.
            env = {**os.environ, "STREAM_TEST_URL": api.base, "ADMIN_PASSWORD": password,
                   "DEBUG": "", "PWDEBUG": "0"}
            browser = subprocess.Popen(["node", str(script)], env=env, start_new_session=True,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                result = browser.wait(timeout=85)
            finally:
                if browser.poll() is None:
                    browser.terminate()
                    try:
                        browser.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(browser.pid, signal.SIGKILL)
                        browser.wait(timeout=3)
            require(result == 0, "Real browser smoke failed (run check-stream.mjs directly for its safe stage label)")
            print("Real Chromium playback, responsive layout, cookie recovery and tab lifecycle passed.")

        stage = "logout and publisher independence"
        _, headers, _ = api.request("/api/logout", method="POST", token=operator, cookie=cookie, expected=204)
        deleted = SimpleCookie()
        deleted.load(headers.get("Set-Cookie", ""))
        require(COOKIE in deleted and deleted[COOKIE]["max-age"] == "0" and deleted[COOKIE]["path"] == LIVE,
                "Logout did not expire the playback cookie")
        for method, path in routes:
            api.request(path, method=method, token=operator, expected=401)
        api.request(MANIFEST, cookie=cookie, expected=401)
        tokens.remove(operator)
        status = wait_status(api, observer, True, publisher)
        require(status.get("publisher_session_id") == online_session, "Browser logout changed the publisher session")
    except SmokeFailure:
        raise
    except Exception:
        raise SmokeFailure(f"Failed during {stage}; raw diagnostics suppressed to protect credentials") from None
    finally:
        try:
            if publisher:
                publisher.close()
                if observer and online_session:
                    wait_status(api, observer, False)
        finally:
            for token in tokens:
                try:
                    api.request("/api/logout", method="POST", token=token, expected=204)
                except Exception:
                    pass
    print("Stream smoke passed; own publisher stopped and source confirmed offline.")


if __name__ == "__main__":
    def interrupted(signum, frame):
        raise SmokeFailure("Stream smoke interrupted")

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        main()
    except SmokeFailure as error:
        print(f"Stream smoke failed: {error}", file=sys.stderr)
        sys.exit(1)
    except Exception:
        print("Stream smoke failed; raw diagnostics suppressed to protect credentials", file=sys.stderr)
        sys.exit(1)
