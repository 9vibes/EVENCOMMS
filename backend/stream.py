"""Fixed-path live media access; publisher and reader credentials never mix."""

import asyncio
import hashlib
import json
import logging
import re
import secrets
import time
from datetime import datetime

import anyio
import httpx
from fastapi import Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, StrictStr, ValidationError

from .config import media_host


COOKIE = "evencomms_playback"
COOKIE_PATH = "/api/stream/live/"
PLAYBACK_URL = COOKIE_PATH + "index.m3u8"
PLAYBACK_TTL = 300
MEDIA_LIMIT = 64 * 1024 * 1024
FILE = re.compile(r"[A-Za-z0-9_-]{1,160}\.(?:m3u8|mp4|m4s|ts)")
TRACKS = {"H264", "H265", "AV1", "VP8", "VP9", "MPEG4Video", "MPEG1Video",
          "MJPEG", "Opus", "MPEG4Audio", "MPEG-4 Audio", "MPEG1Audio", "AC3",
          "G711", "G722", "LPCM"}
CONTENT_HEADERS = {"content-type", "content-length", "content-range", "accept-ranges",
                   "etag", "last-modified"}


class MediaAuth(BaseModel):
    model_config = ConfigDict(extra="ignore")
    user: StrictStr
    password: StrictStr
    action: StrictStr
    path: StrictStr
    protocol: StrictStr
    id: StrictStr | None = None


class MediaResponse(StreamingResponse):
    def __init__(self, upstream):
        self.upstream = upstream
        super().__init__(self.chunks(), status_code=upstream.status_code,
                         headers={key: value for key, value in upstream.headers.items()
                                  if key in CONTENT_HEADERS})

    async def chunks(self):
        size = 0
        deadline = asyncio.get_running_loop().time() + 30
        # Forward available bytes without aggregating small low-latency parts.
        chunks = self.upstream.aiter_bytes()
        try:
            while True:
                async with asyncio.timeout_at(deadline):
                    chunk = await anext(chunks, None)
                if chunk is None:
                    return
                size += len(chunk)
                if size > MEDIA_LIMIT:
                    return
                yield chunk
        except (httpx.HTTPError, TimeoutError):
            # Headers have already been sent. End playback without exposing upstream errors.
            return

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            # Also close if the browser disconnects before the iterator starts.
            with anyio.CancelScope(shield=True):
                await self.upstream.aclose()


class Stream:
    def __init__(self, config, store):
        self.config = config
        if config.stream_enabled:
            logging.getLogger(__name__).warning(
                "Streaming requires a private /internal/media/auth callback. Bind bare development "
                "servers to 127.0.0.1; deployments must block /internal at ingress and keep the backend port private.")
        row = store.db.execute("SELECT publisher_secret, reader_secret FROM stream_settings WHERE id = 1").fetchone()
        self.publisher_secret, self.reader_secret = row
        self.api = httpx.AsyncClient(timeout=3, trust_env=False, follow_redirects=False)
        self.hls = httpx.AsyncClient(timeout=httpx.Timeout(10, connect=3, pool=3),
                                    trust_env=False, follow_redirects=False,
                                    limits=httpx.Limits(max_connections=100, max_keepalive_connections=20))
        self.lock = asyncio.Lock()
        self.now = time.monotonic
        self.cached = None
        self.cached_until = 0
        self.sample = None

    async def aclose(self):
        await self.api.aclose()
        await self.hls.aclose()

    async def status(self):
        async with self.lock:
            if self.cached is not None and self.now() < self.cached_until:
                return self.cached
            result = dict(enabled=self.config.stream_enabled, media_available=False, online=False,
                          publisher_session_id=None, started_at=None, tracks=[], bitrate_mbps=None)
            sample = None
            if self.config.stream_enabled:
                try:
                    async with asyncio.timeout(3):
                        async with self.api.stream("GET", self.config.media_api_url + "/v3/paths/get/live/stream",
                                                   headers={"accept-encoding": "identity"}) as response:
                            if response.status_code == 404:
                                result["media_available"] = True
                            elif response.status_code == 200:
                                if response.headers.get("content-encoding", "identity") != "identity":
                                    raise ValueError()
                                raw = bytearray()
                                async for chunk in response.aiter_bytes(8192):
                                    if len(raw) + len(chunk) > 65536:
                                        raise ValueError()
                                    raw.extend(chunk)
                                data = json.loads(raw)
                                if not isinstance(data, dict) or type(data.get("ready")) is not bool:
                                    raise ValueError()
                                result["media_available"] = True
                                result["online"] = data["ready"]
                                if data["ready"]:
                                    source = data.get("source")
                                    source_id = source.get("id") if isinstance(source, dict) else None
                                    started = data.get("readyTime")
                                    if isinstance(started, str) and len(started) <= 64:
                                        try:
                                            parsed = datetime.fromisoformat(started.replace("Z", "+00:00"))
                                            if parsed.tzinfo is not None:
                                                result["started_at"] = started
                                        except ValueError:
                                            pass
                                    if isinstance(source_id, str) and 0 < len(source_id) <= 256 and result["started_at"]:
                                        identity = json.dumps([source_id, started]).encode()
                                        result["publisher_session_id"] = hashlib.sha256(identity).hexdigest()
                                    tracks = data.get("tracks")
                                    if isinstance(tracks, list):
                                        result["tracks"] = [track for track in tracks[:32]
                                                            if isinstance(track, str) and track in TRACKS]
                                    count = data.get("bytesReceived")
                                    session = result["publisher_session_id"]
                                    now = self.now()
                                    if session and type(count) is int and 0 <= count < 2**64:
                                        sample = (session, count, now)
                                        if self.sample:
                                            previous, last_count, last_time = self.sample
                                            if session == previous and count >= last_count and now > last_time:
                                                result["bitrate_mbps"] = (count - last_count) * 8 / (now - last_time) / 1_000_000
                except (httpx.HTTPError, TimeoutError, ValueError, RecursionError):
                    pass
            self.sample = sample
            self.cached = result
            self.cached_until = self.now() + 1
            return result


def register_stream_routes(app, operator, bearer, digest, body):
    @app.get("/api/stream/status", dependencies=[Depends(operator)])
    async def status():
        return await app.state.stream.status()

    @app.get("/api/stream/settings", dependencies=[Depends(operator)])
    async def settings():
        stream = app.state.stream
        config = stream.config
        return dict(enabled=config.stream_enabled,
                    server_url=f"rtmp://{media_host(config.public_host)}:{config.rtmp_port}/live",
                    stream_key="stream?user=publisher&pass=" + stream.publisher_secret,
                    playback_url=PLAYBACK_URL, rtmp_port=config.rtmp_port)

    @app.post("/api/stream/playback-session", dependencies=[Depends(operator)])
    async def playback_session(request: Request, response: Response):
        if not app.state.settings.stream_enabled:
            raise HTTPException(503, "Streaming is disabled")
        credentials = app.state.credentials
        credentials.prune()
        parent = digest(bearer(request))
        ttl = min(PLAYBACK_TTL, int(credentials.operators.get(parent, 0) - credentials.now()))
        if ttl <= 0:
            raise HTTPException(401, "Invalid or expired operator token")
        # One live cookie per operator bounds state to the existing operator limit.
        credentials.playbacks = {key: value for key, value in credentials.playbacks.items() if value[0] != parent}
        token = secrets.token_urlsafe(32)
        credentials.playbacks[digest(token)] = (parent, min(credentials.now() + ttl, credentials.operators[parent]))
        response.set_cookie(COOKIE, token, max_age=ttl, path=COOKIE_PATH, httponly=True,
                            samesite="strict", secure=app.state.settings.cookie_secure)
        return {"expires_in": ttl}

    @app.post("/api/logout", dependencies=[Depends(operator)], status_code=204)
    async def logout(request: Request):
        credentials = app.state.credentials
        credentials.operators.pop(digest(bearer(request)), None)
        credentials.prune()
        response = Response(status_code=204)
        response.delete_cookie(COOKIE, path=COOKIE_PATH, httponly=True, samesite="strict",
                               secure=app.state.settings.cookie_secure)
        return response

    @app.post("/internal/media/auth", status_code=204)
    async def media_auth(request: Request):
        raw = await body(request, 65536, "application/json")
        try:
            data = MediaAuth.model_validate_json(raw)
        except ValidationError:
            raise HTTPException(401, "Media authentication failed") from None
        stream = app.state.stream
        expected = None
        if (data.user, data.action, data.protocol) == ("publisher", "publish", "rtmp"):
            expected = stream.publisher_secret
        elif (data.user, data.action, data.protocol) == ("reader", "read", "hls"):
            expected = stream.reader_secret
        if (not stream.config.stream_enabled or data.path != "live/stream" or expected is None
                or not secrets.compare_digest(digest(data.password), digest(expected))):
            raise HTTPException(401, "Media authentication failed")
        return Response(status_code=204)

    @app.get(COOKIE_PATH + "{file:path}")
    async def live(file: str, request: Request):
        credentials = app.state.credentials
        credentials.prune()
        token = request.cookies.get(COOKIE, "")
        if not token or len(token) > 256 or digest(token) not in credentials.playbacks:
            raise HTTPException(401, "Playback session required")
        if not app.state.settings.stream_enabled:
            raise HTTPException(503, "Streaming is disabled")
        if not FILE.fullmatch(file):
            raise HTTPException(404, "Media file not found")
        if len(request.scope.get("query_string", b"")) > 256:
            raise HTTPException(400, "Invalid HLS query")
        query = request.query_params.multi_items()
        if len(query) > 3 or len({key for key, _ in query}) != len(query):
            raise HTTPException(400, "Invalid HLS query")
        for key, value in query:
            if not ((key in {"_HLS_msn", "_HLS_part"} and re.fullmatch(r"[0-9]{1,20}", value))
                    or (key == "_HLS_skip" and value in {"YES", "v2"})):
                raise HTTPException(400, "Invalid HLS query")
        headers = {"accept-encoding": "identity"}
        for key in ("range", "if-range"):
            if key in request.headers:
                if len(request.headers[key]) > 256:
                    raise HTTPException(400, "Invalid media request header")
                headers[key] = request.headers[key]
        stream = app.state.stream
        upstream = None
        response = None
        try:
            async with asyncio.timeout(10):
                upstream = await stream.hls.send(stream.hls.build_request(
                    "GET", stream.config.media_hls_url + "/live/stream/" + file,
                    params=query, headers=headers), stream=True, follow_redirects=False,
                    auth=httpx.BasicAuth("reader", stream.reader_secret))
            if upstream.status_code not in {200, 206, 304, 404, 416}:
                raise HTTPException(502, "Media request failed")
            if upstream.status_code in {404, 416}:
                headers = {key: value for key, value in upstream.headers.items()
                           if key in {"content-range", "accept-ranges"}}
                raise HTTPException(upstream.status_code, "Media file unavailable", headers=headers)
            if upstream.status_code == 304:
                return Response(status_code=304, headers={key: value for key, value in upstream.headers.items()
                                                         if key in CONTENT_HEADERS})
            length = upstream.headers.get("content-length")
            if ((length is not None and (not length.isascii() or not length.isdecimal() or len(length) > 12
                                        or int(length) > MEDIA_LIMIT))
                    or upstream.headers.get("content-encoding", "identity") != "identity"):
                raise HTTPException(502, "Invalid media response")
            response = MediaResponse(upstream)
        except (httpx.HTTPError, TimeoutError):
            raise HTTPException(503, "Media unavailable") from None
        finally:
            if upstream is not None and response is None:
                with anyio.CancelScope(shield=True):
                    await upstream.aclose()
        return response
