import asyncio
import struct
import sys
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException

from backend.config import Settings
from backend.services import Transcriber, suggest


def test_whisper_lazy_load_cpu_int8_english_and_vad(monkeypatch, tmp_path):
    loads = []
    calls = []

    class Array:
        def __init__(self, data):
            self.data = data

        def astype(self, dtype):
            assert dtype == "float32"
            return self

        def __truediv__(self, divisor):
            return [sample / divisor for sample in self.data]

    def frombuffer(pcm, dtype):
        assert dtype == "<i2"
        return Array(struct.unpack("<hh", pcm))

    class Model:
        def __init__(self, name, **options):
            loads.append((name, options))

        def transcribe(self, audio, **options):
            calls.append((audio, options))
            return iter([SimpleNamespace(text=" Hello "), SimpleNamespace(text=" world ")]), None

    monkeypatch.setitem(sys.modules, "numpy", SimpleNamespace(frombuffer=frombuffer, float32="float32"))
    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=Model))
    settings = Settings(admin_password="test", model_cache=tmp_path / "models")
    transcriber = Transcriber(settings)
    assert transcriber.model is None
    assert loads == []
    for _ in range(2):
        assert asyncio.run(transcriber.transcribe(struct.pack("<hh", -32768, 16384))) == "Hello world"
    assert loads == [("base.en", {"device": "cpu", "compute_type": "int8", "download_root": str(tmp_path / "models")})]
    assert calls == [([-1.0, 0.5], {"language": "en", "vad_filter": True, "beam_size": 1})] * 2


def test_cancelled_asr_request_does_not_release_running_thread(tmp_path):
    import threading

    settings = Settings(admin_password="test", model_cache=tmp_path)
    transcriber = Transcriber(settings)
    release = threading.Event()
    started = threading.Event()

    def run(pcm):
        started.set()
        release.wait(2)
        return "Draft"

    transcriber.run = run

    async def exercise():
        task = asyncio.create_task(transcriber.transcribe(b"\0\0"))
        try:
            assert await asyncio.to_thread(started.wait, 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert transcriber.busy
            with pytest.raises(HTTPException) as error:
                await transcriber.transcribe(b"\0\0")
            assert error.value.status_code == 429
        finally:
            release.set()
        async with asyncio.timeout(2):
            while transcriber.busy:
                await asyncio.sleep(0.001)

    asyncio.run(exercise())


def test_ollama_has_total_deadline():
    class SlowStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            while True:
                await asyncio.sleep(0.001)
                yield b" "

    async def exercise():
        settings = Settings(admin_password="test", ollama_timeout=0.02)
        transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=SlowStream()))
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(HTTPException) as error:
                await suggest(client, settings, [])
            assert error.value.status_code == 504

    asyncio.run(exercise())


def test_unconfigured_ollama_does_not_connect():
    async def exercise():
        transport = httpx.MockTransport(lambda request: pytest.fail("Unexpected upstream request"))
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(HTTPException) as error:
                await suggest(client, Settings(admin_password="test", ollama_model=""), [])
            assert error.value.status_code == 503

    asyncio.run(exercise())


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan")])
def test_invalid_timeout_config_fails_closed(timeout):
    with pytest.raises(ValueError, match="finite and positive"):
        Settings(admin_password="test", stt_timeout=timeout)


def test_env_config_and_password_repr(monkeypatch, tmp_path):
    monkeypatch.setenv("ADMIN_PASSWORD", "never-print-this")
    monkeypatch.setenv("STT_ENABLED", "false")
    monkeypatch.setenv("STT_MODEL", "tiny.en")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "sqlite.db"))
    monkeypatch.setenv("MODEL_CACHE", str(tmp_path / "cache"))
    monkeypatch.setenv("FRONTEND_DIST", str(tmp_path / "dist"))
    monkeypatch.setenv("ALLOWED_ORIGINS", "https://even.example/, https://second.example")
    monkeypatch.setenv("OLLAMA_URL", "http://ollama:11434/")
    settings = Settings.from_env()
    assert not settings.stt_enabled
    assert settings.stt_model == "tiny.en"
    assert settings.database_path == tmp_path / "sqlite.db"
    assert settings.model_cache == tmp_path / "cache"
    assert settings.frontend_dist == tmp_path / "dist"
    assert settings.allowed_origins == ("https://even.example", "https://second.example")
    assert settings.ollama_url == "http://ollama:11434"
    assert "never-print-this" not in repr(settings)
    monkeypatch.setenv("STT_ENABLED", "perhaps")
    with pytest.raises(ValueError, match="STT_ENABLED"):
        Settings.from_env()
