import asyncio
import hashlib
import json
import os
import secrets
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from uuid import UUID

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, StrictStr, ValidationError, field_validator
from starlette.exceptions import HTTPException as StarletteHTTPException

from .config import Settings, env_bool
from .origins import EvenCORSMiddleware, is_even_localhost_origin
from .codex_research import CodexResearch, register_codex_research_routes
from .research import Research, register_research_routes
from .services import Transcriber, suggest
from .store import Store
from .stream import Stream, register_stream_routes


JSON_LIMIT = 65536
AUDIO_LIMIT = 480000
AUTH_TIMEOUT = 5


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class Credentials:
    def __init__(self):
        self.operators: dict[str, float] = {}
        self.pairings: dict[str, float] = {}
        self.playbacks: dict[str, tuple[str, float]] = {}
        self.rates: dict[tuple[str, str], tuple[float, int]] = {}
        self.now = time.monotonic
        self.prune_hooks = []

    def prune(self):
        now = self.now()
        self.operators = {key: expiry for key, expiry in self.operators.items() if expiry > now}
        self.pairings = {key: expiry for key, expiry in self.pairings.items() if expiry > now}
        self.playbacks = {key: (parent, expiry) for key, (parent, expiry) in self.playbacks.items()
                          if expiry > now and self.operators.get(parent, 0) > now}
        self.rates = {key: value for key, value in self.rates.items() if value[0] > now}
        for hook in self.prune_hooks:
            hook()

    def throttle(self, route: str, request: Request):
        self.prune()
        host = request.client.host if request.client else "unknown"
        # Global caps bound both guessing and the number of per-address counters.
        for key, limit in (((route, "global"), 60), ((route, "ip:" + host), 5)):
            expires, count = self.rates.get(key, (self.now() + 60, 0))
            if count >= limit:
                raise HTTPException(429, "Too many attempts; retry in one minute", headers={"Retry-After": "60"})
            self.rates[key] = (expires, count + 1)


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Login(Input):
    password: StrictStr


class Pair(Input):
    code: StrictStr
    name: StrictStr

    @field_validator("name")
    @classmethod
    def name_valid(cls, value):
        value = value.strip()
        if not 1 <= len(value) <= 80:
            raise ValueError("Name must be 1..80 characters")
        return value


class Send(Input):
    text: StrictStr
    client_id: UUID

    @field_validator("text")
    @classmethod
    def text_valid(cls, value):
        value = value.strip()
        if not 1 <= len(value) <= 4000:
            raise ValueError("Text must be 1..4000 characters")
        return value


async def body(request: Request, limit: int, content_type: str) -> bytes:
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != content_type:
        raise HTTPException(415, f"Expected {content_type}")
    if request.headers.get("content-encoding", "identity").lower() != "identity":
        raise HTTPException(415, "Compressed request bodies are not supported")
    size = request.headers.get("content-length")
    if size is not None:
        try:
            length = int(size)
        except ValueError:
            raise HTTPException(400, "Invalid Content-Length") from None
        if length < 0:
            raise HTTPException(400, "Invalid Content-Length")
        if length > limit:
            raise HTTPException(413, "Request body too large")
    data = bytearray()
    try:
        async with asyncio.timeout(10):
            async for chunk in request.stream():
                if len(data) + len(chunk) > limit:
                    raise HTTPException(413, "Request body too large")
                data.extend(chunk)
    except TimeoutError:
        raise HTTPException(408, "Request body timed out") from None
    return bytes(data)


async def parse(request: Request, model):
    raw = await body(request, JSON_LIMIT, "application/json")
    try:
        return model.model_validate_json(raw)
    except ValidationError:
        raise HTTPException(422, "Invalid request; check required fields and length limits") from None


def bearer(request: Request) -> str:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token or len(token) > 256:
        raise HTTPException(401, "Bearer authentication required", headers={"WWW-Authenticate": "Bearer"})
    return token


async def operator(request: Request):
    credentials = request.app.state.credentials
    credentials.prune()
    token_hash = digest(bearer(request))
    if credentials.operators.get(token_hash, 0) <= credentials.now():
        raise HTTPException(401, "Invalid or expired operator token")


async def wearer(request: Request) -> str:
    row = request.app.state.store.wearer(digest(bearer(request)))
    if row is None:
        raise HTTPException(401, "Invalid wearer token")
    return row["id"]


@dataclass
class Peer:
    socket: WebSocket
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


async def close_socket(socket: WebSocket, code: int):
    try:
        await asyncio.wait_for(socket.close(code=code), 2)
    except (RuntimeError, OSError, TimeoutError, WebSocketDisconnect):
        pass


async def notify(app: FastAPI, message: dict):
    peers = app.state.peers
    peer = peers.get(message["session_id"])
    if peer:
        try:
            async with asyncio.timeout(2):
                async with peer.lock:
                    await peer.socket.send_json({"type": "message", "message": message})
        except (RuntimeError, OSError, TimeoutError, WebSocketDisconnect):
            if peers.get(message["session_id"]) is peer:
                peers.pop(message["session_id"], None)
            await close_socket(peer.socket, 1013)


def create_app(settings: Settings | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app):
        config = settings or Settings.from_env()
        app.state.settings = config
        app.state.store = Store(config)
        app.state.credentials = Credentials()
        app.state.peers = {}
        app.state.transcriber = Transcriber(config)
        app.state.ollama = httpx.AsyncClient(timeout=httpx.Timeout(config.ollama_timeout), trust_env=False)
        app.state.stream = Stream(config, app.state.store)
        app.state.research = Research(config, app.state.credentials)
        app.state.codex_research = CodexResearch(config, app.state.credentials)
        try:
            yield
        finally:
            await app.state.codex_research.aclose()
            await app.state.research.aclose()
            for peer in list(app.state.peers.values()):
                await close_socket(peer.socket, 1001)
            await app.state.ollama.aclose()
            await app.state.stream.aclose()
            app.state.store.db.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    # Read only origins here; the mandatory password is validated during startup.
    origins = settings.allowed_origins if settings else tuple(
        value.strip().rstrip("/") for value in os.getenv("ALLOWED_ORIGINS", "").split(",")
        if value.strip())
    allow_even_localhost = (settings.allow_even_localhost if settings else
                            env_bool("ALLOW_EVEN_LOCALHOST", "false"))
    app.add_middleware(EvenCORSMiddleware, allow_even_localhost=allow_even_localhost,
                       allow_origins=list(origins), allow_credentials=False,
                       allow_methods=["GET", "POST", "DELETE"], allow_headers=["Authorization", "Content-Type"])

    @app.middleware("http")
    async def privacy_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request, exc):
        return JSONResponse({"detail": str(exc.detail)}, status_code=exc.status_code, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        return JSONResponse({"detail": "Invalid request"}, status_code=422)

    @app.exception_handler(Exception)
    async def unexpected_error(request, exc):
        return JSONResponse({"detail": "Internal server error"}, status_code=500)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.post("/api/login")
    async def login(request: Request):
        credentials = app.state.credentials
        credentials.throttle("login", request)
        data = await parse(request, Login)
        if not secrets.compare_digest(digest(data.password), digest(app.state.settings.admin_password)):
            raise HTTPException(401, "Invalid password")
        if len(credentials.operators) >= 100:
            raise HTTPException(429, "Operator token limit reached")
        token = secrets.token_urlsafe(32)
        credentials.operators[digest(token)] = credentials.now() + 8 * 60 * 60
        return {"token": token}

    @app.get("/api/status", dependencies=[Depends(operator)])
    async def status():
        config = app.state.settings
        return {"stt_enabled": config.stt_enabled, "stt_model": config.stt_model,
                "ai_configured": bool(config.ollama_url and config.ollama_model),
                "ollama_model": config.ollama_model}

    @app.post("/api/pairings", dependencies=[Depends(operator)])
    async def pairing():
        credentials = app.state.credentials
        credentials.prune()
        if len(credentials.pairings) >= 100:
            raise HTTPException(429, "Pairing limit reached; wait for unused codes to expire")
        while True:
            code = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(8))
            if digest(code) not in credentials.pairings:
                break
        credentials.pairings[digest(code)] = credentials.now() + 300
        return {"code": code, "expires_in": 300}

    @app.post("/api/pair")
    async def pair(request: Request):
        credentials = app.state.credentials
        credentials.throttle("pair", request)
        data = await parse(request, Pair)
        code_hash = digest(data.code.strip().upper())
        if credentials.pairings.get(code_hash, 0) <= credentials.now():
            raise HTTPException(401, "Invalid or expired pairing code")
        token = secrets.token_urlsafe(32)
        session_id = app.state.store.create_session(data.name, digest(token))
        del credentials.pairings[code_hash]
        return {"token": token, "session_id": session_id}

    @app.get("/api/sessions", dependencies=[Depends(operator)])
    async def sessions():
        return [dict(row, connected=row["id"] in app.state.peers) for row in app.state.store.sessions()]

    @app.get("/api/sessions/{session_id}/messages", dependencies=[Depends(operator)])
    async def messages(session_id: str):
        return app.state.store.messages(session_id)

    @app.post("/api/sessions/{session_id}/reply", dependencies=[Depends(operator)])
    async def reply(session_id: str, request: Request):
        data = await parse(request, Send)
        message, created = app.state.store.message(session_id, "operator", data.text, str(data.client_id))
        if created:
            await notify(app, message)
        return message

    @app.post("/api/sessions/{session_id}/suggest", dependencies=[Depends(operator)])
    async def suggestion(session_id: str):
        history = app.state.store.messages(session_id, last=20)
        text = await suggest(app.state.ollama, app.state.settings, history)
        # A conversation deleted while the model was running must not be revived.
        app.state.store.session(session_id)
        return {"text": text}

    @app.delete("/api/sessions/{session_id}", dependencies=[Depends(operator)], status_code=204)
    async def delete(session_id: str):
        app.state.store.delete(session_id)
        peer = app.state.peers.pop(session_id, None)
        if peer:
            await close_socket(peer.socket, 4004)
        return Response(status_code=204)

    @app.get("/api/me")
    async def me(session_id: str = Depends(wearer)):
        return {"session_id": session_id, "messages": app.state.store.messages(session_id)}

    @app.post("/api/messages")
    async def send(request: Request, session_id: str = Depends(wearer)):
        data = await parse(request, Send)
        message, created = app.state.store.message(session_id, "wearer", data.text, str(data.client_id))
        if created:
            await notify(app, message)
        return message

    @app.post("/api/transcribe")
    async def transcribe(request: Request, session_id: str = Depends(wearer)):
        app.state.transcriber.available()
        pcm = await body(request, AUDIO_LIMIT, "application/octet-stream")
        if not pcm or len(pcm) % 2:
            raise HTTPException(422, "Expected nonempty PCM s16le audio with an even byte length")
        text = await app.state.transcriber.transcribe(pcm)
        app.state.store.session(session_id)
        return {"text": text}

    @app.websocket("/api/wearer")
    async def socket(websocket: WebSocket):
        config = app.state.settings
        origin = websocket.headers.get("origin")
        same_origin = ("https" if websocket.url.scheme == "wss" else "http") + "://" + websocket.headers.get("host", "")
        allowed = (origin in {same_origin, *config.allowed_origins}
                   or (config.allow_even_localhost and is_even_localhost_origin(origin)))
        if not allowed or websocket.query_params:
            await websocket.close(code=4403)
            return
        await websocket.accept()
        peer = None
        session_id = None
        try:
            try:
                raw = await asyncio.wait_for(websocket.receive_text(), AUTH_TIMEOUT)
                if len(raw) > 4096:
                    raise ValueError()
                auth = json.loads(raw)
                if (not isinstance(auth, dict) or auth.get("type") != "auth"
                        or not isinstance(auth.get("token"), str) or not 1 <= len(auth["token"]) <= 256):
                    raise ValueError()
                row = app.state.store.wearer(digest(auth["token"]))
                if row is None:
                    raise ValueError()
            except (TimeoutError, ValueError, KeyError, TypeError):
                await close_socket(websocket, 4401)
                return
            session_id = row["id"]
            peer = Peer(websocket)
            previous = app.state.peers.get(session_id)
            try:
                async with peer.lock:
                    app.state.peers[session_id] = peer
                    await asyncio.wait_for(websocket.send_json({"type": "ready", "session_id": session_id,
                        "messages": app.state.store.messages(session_id)}), 2)
            finally:
                if previous:
                    await close_socket(previous.socket, 4009)
            while True:
                raw = await websocket.receive_text()
                if len(raw) > 1024 or json.loads(raw) != {"type": "ping"}:
                    await close_socket(websocket, 1008)
                    return
                async with asyncio.timeout(2):
                    async with peer.lock:
                        await websocket.send_json({"type": "pong"})
        except (WebSocketDisconnect, RuntimeError, OSError):
            pass
        except (ValueError, KeyError, TypeError):
            await close_socket(websocket, 1008)
        except TimeoutError:
            await close_socket(websocket, 1013)
        finally:
            if session_id and app.state.peers.get(session_id) is peer:
                app.state.peers.pop(session_id, None)

    register_stream_routes(app, operator, bearer, digest, body)
    register_research_routes(app, operator, bearer, digest, body)
    register_codex_research_routes(app, operator, bearer, digest, body)

    @app.get("/{path:path}", include_in_schema=False)
    async def frontend(path: str):
        if path in {"api", "internal", "health"} or path.startswith(("api/", "internal/")):
            raise HTTPException(404, "Not found")
        root = app.state.settings.frontend_dist.resolve()
        target = (root / (path or "index.html")).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            raise HTTPException(404, "Not found")
        return FileResponse(target)

    return app


app = create_app()
