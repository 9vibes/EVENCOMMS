# syntax=docker/dockerfile:1
FROM node:24-bookworm-slim AS frontend
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim-bookworm AS runtime
LABEL org.opencontainers.image.version="0.4.2"
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DATABASE_PATH=/data/evencomms.sqlite3 \
    MODEL_CACHE=/data/models \
    HF_HOME=/data/models/huggingface \
    XDG_CACHE_HOME=/data/models/.cache \
    FRONTEND_DIST=/app/frontend/dist
WORKDIR /app
# CTranslate2 uses OpenMP; Debian/glibc supports its prebuilt wheels.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 evencomms \
    && useradd --uid 10001 --gid 10001 --create-home evencomms \
    && install -d -m 0700 -o 10001 -g 10001 /data /data/models
COPY pyproject.toml ./
COPY backend/ ./backend/
COPY infra/ ./infra/
RUN pip install '.[stt]'
COPY --from=frontend /build/frontend/dist/ ./frontend/dist/
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).close()"]
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--ws", "websockets", "--ws-max-size", "8192", "--ws-max-queue", "8", "--no-access-log", "--no-proxy-headers"]
