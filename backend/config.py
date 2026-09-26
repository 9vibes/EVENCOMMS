import os
from dataclasses import dataclass, field
from math import isfinite
from pathlib import Path
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parent.parent


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
    max_sessions: int = 100
    max_messages_per_session: int = 1000

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

    @classmethod
    def from_env(cls):
        enabled = os.getenv("STT_ENABLED", "true").lower()
        if enabled not in {"true", "false", "1", "0"}:
            raise ValueError("STT_ENABLED must be true or false")
        return cls(
            admin_password=os.getenv("ADMIN_PASSWORD", ""),
            database_path=Path(os.getenv("DATABASE_PATH", str(ROOT / "data/evencomms.sqlite3"))),
            frontend_dist=Path(os.getenv("FRONTEND_DIST", str(ROOT / "frontend/dist"))),
            allowed_origins=tuple(value.strip().rstrip("/") for value in
                                  os.getenv("ALLOWED_ORIGINS", "").split(",") if value.strip()),
            stt_enabled=enabled in {"true", "1"},
            stt_model=os.getenv("STT_MODEL", "base.en"),
            model_cache=Path(os.getenv("MODEL_CACHE", str(ROOT / "data/models"))),
            stt_timeout=float(os.getenv("STT_TIMEOUT", "90")),
            ollama_url=os.getenv("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/"),
            ollama_model=os.getenv("OLLAMA_MODEL", "llama3.2:3b"),
            ollama_timeout=float(os.getenv("OLLAMA_TIMEOUT", "30")),
            max_sessions=int(os.getenv("MAX_SESSIONS", "100")),
            max_messages_per_session=int(os.getenv("MAX_MESSAGES_PER_SESSION", "1000")),
        )
