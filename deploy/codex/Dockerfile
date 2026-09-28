FROM node:22-bookworm-slim AS codex
WORKDIR /opt/codex
COPY codex_bridge/package.json codex_bridge/package-lock.json ./
RUN npm ci --ignore-scripts --no-audit --no-fund \
    && node -e 'const fs = require("fs"); const arch = process.arch; const target = {x64: "x86_64", arm64: "aarch64"}[arch]; if (!target) throw Error("Unsupported architecture"); const root = `node_modules/@openai/codex-linux-${arch}`; const pkg = JSON.parse(fs.readFileSync(`${root}/package.json`)); if (pkg.version !== `0.157.1-linux-${arch}`) throw Error("Wrong Codex version"); fs.cpSync(`${root}/vendor/${target}-unknown-linux-musl`, "/opt/codex/runtime", {recursive: true});'

FROM python:3.12-slim-bookworm
LABEL org.opencontainers.image.title="EVENCOMMS Experimental Codex Bridge" \
      org.opencontainers.image.description="Isolated experimental ChatGPT account connection; account use requires explicit operator actions" \
      org.opencontainers.image.version="0.4.4" \
      org.opencontainers.image.codex-version="0.157.1"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /opt/bridge
RUN pip install --no-cache-dir fastapi==0.141.1 uvicorn==0.54.0 pydantic==2.13.5 pillow==12.3.0 httpx==0.28.1 \
    && groupadd --gid 10002 bridge \
    && useradd --uid 10002 --gid 10002 --no-create-home --home-dir /nonexistent bridge
COPY --from=codex /opt/codex/runtime/ /opt/codex/
COPY codex_bridge /opt/bridge/codex_bridge
RUN ln -s /opt/codex/bin/codex /usr/local/bin/codex \
    && python -c 'from codex_bridge.probe import verify_binary; verify_binary("/usr/local/bin/codex")' \
    && test "$(/usr/local/bin/codex --version)" = "codex-cli 0.157.1"
USER 10002:10002
# Do not publish this port. Compose supplies read-only root, tmpfs, limits and
# a private backend link plus egress; no app volumes or Docker socket.
EXPOSE 8001
CMD ["uvicorn", "codex_bridge.service:app", "--host", "0.0.0.0", "--port", "8001", "--workers", "1", "--no-access-log", "--no-proxy-headers", "--limit-concurrency", "16", "--timeout-keep-alive", "5"]
