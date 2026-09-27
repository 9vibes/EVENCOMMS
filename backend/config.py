import os
import re
from dataclasses import dataclass, field
from ipaddress import ip_address
from math import isfinite
from pathlib import Path
from urllib.parse import urlsplit

from .private_token import load_token_file


ROOT = Path(__file__).resolve().parent.parent


def media_host(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 253:
        raise ValueError("Invalid host")
    host = value[1:-1] if value.startswith("[") and value.endswith("]") else value
    try:
        address = ip_address(host)
    except ValueError:
        if (not re.fullmatch(r"[A-Za-z0-9.-]+", value)
                or re.fullmatch(r"[0-9.]+", value)
                or any(not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                       for label in value.removesuffix(".").split("."))):
            raise ValueError("Invalid host") from None
        return value
    if "%" in host or (value.startswith("[") and address.version != 6):
        raise ValueError("Invalid host")
    return f"[{address}]" if address.version == 6 else str(address)


def env_bool(name: str, default: str) -> bool:
    value = os.getenv(name, default).lower()
    if value not in {"true", "false", "1", "0"}:
        raise ValueError(f"{name} must be true or false")
    return value in {"true", "1"}


@dataclass(frozen=True)
class Settings:
    admin_password: str = field(repr=False)
    database_path: Path = ROOT / "data/evencomms.sqlite3"
    frontend_dist: Path = ROOT / "frontend/dist"
    allowed_origins: tuple[str, ...] = ()
    stt_enabled: bool = True
    stt_model: str = "base.en"
    model_cache: Path = ROOT / "data/models"
    stt_timeout: float = 90
    ollama_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "llama3.2:3b"
    ollama_timeout: float = 30
    openai_api_key: str = field(default="", repr=False)
    openai_timeout: float = 90
    openai_max_output_tokens: int = 2048
    codex_bridge_url: str = ""
    codex_bridge_token: str = field(default="", repr=False)
    max_sessions: int = 100
    max_messages_per_session: int = 1000
    stream_enabled: bool = False
    public_host: str = "localhost"
    rtmp_port: int = 21936
    media_api_url: str = "http://mediamtx:9997"
    media_hls_url: str = "http://mediamtx:8888"
    cookie_secure: bool = False

    def __post_init__(self):
        if not self.admin_password or not self.admin_password.strip():
            raise ValueError("ADMIN_PASSWORD must be set to a nonempty password")
        for origin in self.allowed_origins:
            parsed = urlsplit(origin)
            if (parsed.scheme not in {"http", "https"} or not parsed.netloc
                    or parsed.path or parsed.query or parsed.fragment
                    or parsed.username or parsed.password or "*" in origin):
                raise ValueError("ALLOWED_ORIGINS must contain explicit HTTP(S) origins")
        limits = (self.max_sessions, self.max_messages_per_session,
                  self.stt_timeout, self.ollama_timeout)
        if any(not isfinite(value) or value <= 0 for value in limits):
            raise ValueError("Limits and timeouts must be finite and positive")
        if not isfinite(self.openai_timeout) or not 0 < self.openai_timeout <= 110:
            raise ValueError("OPENAI_TIMEOUT must be finite, positive and at most 110 seconds")
        if type(self.openai_max_output_tokens) is not int or not 256 <= self.openai_max_output_tokens <= 8192:
            raise ValueError("OPENAI_MAX_OUTPUT_TOKENS must be an integer from 256 to 8192")
        if type(self.stream_enabled) is not bool or type(self.cookie_secure) is not bool:
            raise ValueError("STREAM_ENABLED and COOKIE_SECURE must be booleans")
        if type(self.rtmp_port) is not int or not 1 <= self.rtmp_port <= 65535:
            raise ValueError("RTMP_PORT must be an integer from 1 to 65535")
        try:
            media_host(self.public_host)
        except ValueError:
            raise ValueError("PUBLIC_HOST must be an IPv4, IPv6 or DNS host without a port or path") from None
        if (not isinstance(self.codex_bridge_url, str) or not isinstance(self.codex_bridge_token, str)
                or bool(self.codex_bridge_url) != bool(self.codex_bridge_token)):
            raise ValueError("CODEX_BRIDGE_URL and CODEX_BRIDGE_TOKEN must be configured together")
        if self.codex_bridge_token and not re.fullmatch(r"[0-9A-Fa-f]{32,256}", self.codex_bridge_token):
            raise ValueError("CODEX_BRIDGE_TOKEN must contain 32 to 256 hexadecimal characters")
        origins = [("MEDIA_API_URL", self.media_api_url), ("MEDIA_HLS_URL", self.media_hls_url)]
        if self.codex_bridge_url:
            origins.append(("CODEX_BRIDGE_URL", self.codex_bridge_url))
        for name, value in origins:
            try:
                if (not isinstance(value, str) or len(value) > 2048
                        or not re.fullmatch(r"https?://(?:[A-Za-z0-9.-]+|\[[0-9A-Fa-f:.]+\])(?::[0-9]{1,5})?", value)):
                    raise ValueError()
                parsed = urlsplit(value)
                media_host(parsed.hostname)
                if parsed.port is not None and not 1 <= parsed.port <= 65535:
                    raise ValueError()
            except ValueError:
                raise ValueError(f"{name} must be an HTTP(S) origin without userinfo, path or query") from None

    @classmethod
    def from_env(cls):
        token = os.getenv("CODEX_BRIDGE_TOKEN", "")
        token_file = os.getenv("CODEX_BRIDGE_TOKEN_FILE", "")
        if token and token_file:
            raise ValueError("Configure only one of CODEX_BRIDGE_TOKEN or CODEX_BRIDGE_TOKEN_FILE")
        if token_file:
            token = load_token_file(token_file)
        return cls(
            admin_password=os.getenv("ADMIN_PASSWORD", ""),
            database_path=Path(os.getenv("DATABASE_PATH", str(ROOT / "data/evencomms.sqlite3"))),
            frontend_dist=Path(os.getenv("FRONTEND_DIST", str(ROOT / "frontend/dist"))),
            allowed_origins=tuple(value.strip().rstrip("/") for value in
                                  os.getenv("ALLOWED_ORIGINS", "").split(",") if value.strip()),
            stt_enabled=env_bool("STT_ENABLED", "true"),
            stt_model=os.getenv("STT_MODEL", "base.en"),
            model_cache=Path(os.getenv("MODEL_CACHE", str(ROOT / "data/models"))),
            stt_timeout=float(os.getenv("STT_TIMEOUT", "90")),
            ollama_url=os.getenv("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/"),
            ollama_model=os.getenv("OLLAMA_MODEL", "llama3.2:3b"),
            ollama_timeout=float(os.getenv("OLLAMA_TIMEOUT", "30")),
            openai_api_key=os.getenv("OPENAI_API_KEY", ""),
            openai_timeout=float(os.getenv("OPENAI_TIMEOUT", "90")),
            openai_max_output_tokens=int(os.getenv("OPENAI_MAX_OUTPUT_TOKENS", "2048")),
            codex_bridge_url=os.getenv("CODEX_BRIDGE_URL", ""),
            codex_bridge_token=token,
            max_sessions=int(os.getenv("MAX_SESSIONS", "100")),
            max_messages_per_session=int(os.getenv("MAX_MESSAGES_PER_SESSION", "1000")),
            stream_enabled=env_bool("STREAM_ENABLED", "false"),
            public_host=os.getenv("PUBLIC_HOST", "localhost"),
            rtmp_port=int(os.getenv("RTMP_PORT", "21936")),
            media_api_url=os.getenv("MEDIA_API_URL", "http://mediamtx:9997"),
            media_hls_url=os.getenv("MEDIA_HLS_URL", "http://mediamtx:8888"),
            cookie_secure=env_bool("COOKIE_SECURE", "false"),
        )
