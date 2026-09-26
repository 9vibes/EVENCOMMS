"""Isolated real-server fixture: python -m backend.e2e_server."""

import os
from pathlib import Path
from tempfile import TemporaryDirectory

import uvicorn

from .config import ROOT
from .main import create_app


def main():
    with TemporaryDirectory(prefix="evencomms-e2e-") as directory:
        os.environ.update(
            DATABASE_PATH=str(Path(directory) / "test.sqlite3"),
            MODEL_CACHE=str(Path(directory) / "models"),
            FRONTEND_DIST=str(ROOT / "frontend" / "dist"),
            STT_ENABLED="false",
            # Keep the suggestion UI available, but never target a live model.
            OLLAMA_URL="http://127.0.0.1:1",
            OLLAMA_MODEL="e2e-browser-route-only",
            OLLAMA_TIMEOUT="1",
            ALLOWED_ORIGINS="",
        )
        uvicorn.run(create_app(), host="127.0.0.1", port=8765, log_level="warning")


if __name__ == "__main__":
    main()
