# Live Stream (0.2.0)

One RTMP publisher feeds MediaMTX at `live/stream`; operator browsers use an
authenticated same-origin HLS proxy. No recording, transcoding or analysis.
The original conversation API and wearer client remain unchanged.

Version `0.1.0` does **not** contain STREAM. Update to `0.2.0` and redeploy **all
services**, including the new public `web` proxy and MediaMTX. Back up the app,
then update it in place rather than uninstalling: existing conversations,
pairings, application password and model cache remain in place. The store's init
service installs the bundled nginx/MediaMTX configuration automatically. Do not
run the new topology against the old image.

## API

- `GET /api/stream/status` (operator Bearer) -> `{enabled, media_available,
  online, publisher_session_id, started_at, tracks, bitrate_mbps}`. Session/time
   fields are nullable strings; tracks is a string array. Only allowlisted
   telemetry, no credentials or raw MediaMTX responses.
   `bitrate_mbps` is a nullable number calculated from successive byte-count
   samples, not a guaranteed encoder setting. `online` describes the publisher,
   not successful browser decoding.
- `GET /api/stream/settings` (operator Bearer) -> `{enabled, server_url,
  stream_key, playback_url, rtmp_port}`. Show credentials only on explicit reveal;
  never persist them in browser storage. Key example:
  `stream?user=publisher&pass=<random persistent publishing secret>`.
- `POST /api/stream/playback-session` (operator Bearer) -> `{expires_in}` and a
  short-lived, HttpOnly, SameSite=Strict cookie scoped to `/api/stream/live/`.
   Cookie privileges are read-only playback and expire with the operator session.
   Its lifetime is at most 300 seconds, capped by the remaining operator lifetime.
  Use `credentials: 'same-origin'` on this request. Refresh before expiry while
  the stream tab is mounted. `COOKIE_SECURE=true` for HTTPS deployments.
- `GET /api/stream/live/{file}` (playback cookie only) proxies fixed MediaMTX
  HLS files with server-only reader credentials. No URL tokens; restrict filenames
  and HLS query parameters. Support ranges and always close upstream responses.
- `POST /api/logout` (operator Bearer) -> 204, revokes the operator token and all
  linked playback sessions, expires the playback cookie.
- `POST /internal/media/auth` (MediaMTX only) -> 204 or 401. Public nginx blocks
  `/internal` and `/internal/`; the backend port is never published in Compose.
  MediaMTX posts user/password/action/path/protocol/id plus other fields. Ignore
  extra fields and accept a null connection ID. Permit only publisher/rtmp/publish
  or reader/hls/read, with distinct constant-time-checked secrets and fixed path.

## Deployment

```text
Browser -> [TLS proxy / Umbrel app_proxy] -> web:8080 -> server:8000
Encoder -> host TCP 21936 -> mediamtx:1935
server -> mediamtx:9997 (status), mediamtx:8888 (HLS with private reader secret)
mediamtx -> server:8000/internal/media/auth (publish/read authentication)
```

`web` nginx is the only public HTTP entry. Standalone maps host `28097` to
`web:8080`; Umbrel targets `kunas-evencomms_web_1:8080`. Nginx proxies all other
paths, including static files, API, HLS and WebSockets, to `server:8000` with
original Host (`$http_host`), cookies and mapped WebSocket Upgrade headers. It
returns 404 for exact `/internal` and prefix `/internal/` for **all methods**.
Never publish backend `8000` or point another public proxy directly at it.

Nginx has a 1 MiB request-body limit, 180-second read/send timeouts, a five-second
connect timeout, no request/response buffering and no access log. Docker DNS is
refreshed so a replaced backend can receive new requests without recreating nginx.
It runs as `101:101` with a read-only
root, `/tmp` tmpfs for PID and all temporary paths, and bypasses the official
image entrypoint. Backend data ownership, root-filesystem protection, init and
image health check are retained. Web and MediaMTX wait for backend health;
backend health does not depend on media. Web's BusyBox `wget --spider` checks
`/health`. MediaMTX runs its binary directly, with no invented shell health check.
All Compose services use bounded JSON-file logging (three 10 MiB files each).

Only `web`, `server` and MediaMTX share the private application bridge; in Umbrel,
only `web` additionally joins the default network to receive app-proxy requests.
The private bridge is deliberately **not** `internal: true`: published RTMP must
remain reachable and the backend may need model downloads/Ollama access. Do not
attach untrusted containers. MediaMTX publishes only TCP RTMP; HLS `8888` and API
`9997` are private. Only API actions bypass MediaMTX's HTTP auth callback, so
network isolation of the unauthenticated media control API is mandatory.
Metrics, pprof, playback server, RTSP, WebRTC and SRT are disabled.

The backend deliberately ignores forwarded client-IP headers. Through this
nginx ingress, clients therefore share its five-attempts-per-minute limit for
each of login and pairing, including successful attempts. After a burst, wait
one minute before retrying. Status polling and playback do not consume this
budget. Larger multi-user deployments need a reviewed trusted-proxy rate-limit
configuration; do not enable blanket forwarded-header trust as a workaround.

The infrastructure images are pinned: official `nginx:1.28-alpine` at index
`sha256:a8b39bd9cf0f83869a2162827a0caf6137ddf759d50a171451b335cecc87d236`, and
`bluenviron/mediamtx:1.12.3` at
`sha256:3634a1eed1288b93e8d22ed74694eae96d483fcf676cac5d0c91830ad1b86b3d`.

### Standalone Setup

1. Follow the root [build instructions](../README.md#standalone-deployment).
   Set a unique `ADMIN_PASSWORD` in a private `.env`. For a remote encoder, set
   `PUBLIC_HOST` to the Docker host's reachable LAN/VPN hostname or IP, **without
   scheme, port or path**. The `localhost` default is only useful on that host.
2. Set `RTMP_BIND` to the host's LAN/VPN IP for remote ingest; standalone defaults
   to `127.0.0.1`. `BIND_ADDRESS` separately controls web access on `28097` and
   also defaults to loopback. Changing one does not change the other.
3. Check that TCP `21936` is available on the **actual deployment host**, using
   `ss -ltn` and `docker ps --format '{{.Names}} {{.Ports}}'`. This candidate
   avoids SteamLab's `21935` but is not a reservation or a verified free port.
   If needed change `RTMP_PORT`; Compose uses it for both the host mapping and
   the advertised encoder URL. Restrict ingest with Docker-aware firewall rules.
4. Leave `STREAM_ENABLED=true` (Compose default), or set `false` to reject
   publishing/playback. Disabling the feature does not remove the media container
   or published RTMP port. Bare backend development defaults to `false`.
5. For HTTPS, set `COOKIE_SECURE=true` and include the exact HTTPS origin in
   `ALLOWED_ORIGINS`. The default `false` is for isolated HTTP testing only.
   Configure the external TLS proxy to reach `web`, never the raw backend.
6. Run `docker compose build` then `docker compose up -d --force-recreate` from
   the repository root. This rebuilds the application and redeploys the stack;
   do not replace the frontend alone or reuse an old release image.

`MEDIA_API_URL=http://mediamtx:9997` and `MEDIA_HLS_URL=http://mediamtx:8888` are
fixed private service origins in Compose. The backend accepts only HTTP(S)
origins without credentials, paths (including trailing `/`), query or fragment.
Do not substitute browser URLs or publish these ports to fix connectivity.

### Umbrel Staging

Use [the staging instructions](../deploy/umbrel/README.md) for local development,
or the digest-pinned KNS-Umbrel package for installation. Local builds use
`evencomms:0.2.0`. The init service copies the image's bundled `infra/` files into
`${APP_DATA_DIR}/config` at startup. Both consumers mount the whole directory
read-only and wait for initialization through the backend health dependency.
No manual copying, platform template expansion or nginx entrypoint rendering is
needed. Literal nginx `$` variables are preserved. These two files are managed:
changes to them are replaced on the next initialization; other files are untouched.

Umbrel supplies `APP_PASSWORD` and `APP_DATA_DIR`. `PUBLIC_HOST` defaults to
`DEVICE_DOMAIN_NAME`, falling back to `umbrel.local`; set a reachable LAN/VPN
hostname or IP if the encoder cannot resolve that name. The staging RTMP bind
defaults to `0.0.0.0`; prefer an explicit LAN/VPN `RTMP_BIND` and firewall it.
`app_proxy` must target `kunas-evencomms_web_1` port `8080`, never `server`.

### Local Development

Explicitly bind a bare backend to `127.0.0.1:8000`, not `0.0.0.0`, especially
when streaming is enabled. Vite already proxies the API/WebSockets but excludes
`/internal`, so the local Vite loop needs no additional nginx. This is not a
production topology. Full-stack media testing is simplest with Compose; its
private Docker service names are not normally resolvable from a host process.

## OBS Setup

1. Sign in to the operator console and open **STREAM**. Under **Encoder
   configuration**, explicitly **Reveal credentials**. Settings are fetched
   only on reveal, not included in status polling or persisted in browser storage.
2. In OBS, choose Settings -> Stream -> Service: Custom. Copy the displayed
   **RTMP server** (`rtmp://<PUBLIC_HOST>:<RTMP_PORT>/live`) into Server, and the
   entire **Stream key** (`stream?user=publisher&pass=<secret>`) into Stream Key.
   Do not strip the query string, add a second `/stream`, or substitute the web
   port. The key already carries authentication; leave OBS's separate auth off.
3. Use H.264 video and AAC audio, with a **one-second keyframe interval**. Choose
   bitrate/resolution appropriate to your LAN and browser. There is no
   transcoding: unsupported encoder codecs will not be repaired by the server.
4. Start streaming. One publisher is accepted at `live/stream`; a second cannot
   replace an active publisher (`overridePublisher: no`). Stop the old encoder
   before switching sources.
5. Wait for **Source ready** and browser playback. HLS uses in-memory fMP4,
   seven approximately one-second segments; keyframe cadence can lengthen them.
   Expect **seconds of latency**, not real-time/WebRTC latency. Autoplay is muted;
   use manual Play if blocked, then enable audio/fullscreen as needed.

Stopping the encoder ends ingest. Leaving STREAM stops browser playback and
polling, not OBS. No recording, stored video, transcoding or stream analysis is
provided. `hlsDirectory: ''` retains the short HLS window in memory only.

## Security And Recovery

**RTMP is plaintext even when the console uses HTTPS.** Both the publishing key
and media are exposed to network observers. Use a trusted LAN or encrypted VPN;
do not publicly port-forward TCP `21936`. HTTP console access likewise exposes
login credentials: use TLS for real deployments and secure playback cookies.

Publisher and separate server-only reader secrets are generated once and persist
in the existing SQLite database. Hiding credentials, logging out, deleting a
conversation, restarting or changing the operator password does **not** rotate
the publisher secret. Backups include these secrets. Protect the database,
clipboard, OBS profile and any screen capture of revealed credentials. Never
put real keys/tokens in logs, issue reports, shell commands or shared screenshots.
Keep reverse-proxy/app-proxy access logs and request-body diagnostics disabled
or securely redacted; Compose's log size bounds are not secret redaction.

There is **no implemented rotation control or automatic key rotation**. If a key
is compromised, immediately stop MediaMTX and block ingest. Replacing persisted
credentials requires a deliberate, manually reviewed recovery procedure and
encoder reconfiguration; a safe rotation workflow is future work. Do not delete
the database or volume as a shortcut: it also holds conversations and pairing
state. Enabling/disabling streaming is not credential rotation, and should not
be treated as a way to disconnect an already accepted publisher.

Playback uses a short-lived HttpOnly, SameSite=Strict cookie scoped to
`/api/stream/live/`, not URL tokens. Logout revokes linked playback sessions.
The reader secret stays on the backend; media status exposes only allowlisted
telemetry. Keep backend/media ports private even with these authentication checks.

## Deployment Checks

- Confirm `docker compose ps` shows healthy server/web, and running MediaMTX.
  `/health` is backend liveness only, not proof of ingest, auth or HLS readiness.
- Through the actual browser-facing URL, verify `/internal`, `/internal/` and
  `/internal/media/auth` return 404 for GET, POST, PUT, DELETE and OPTIONS. For
  example: `curl -i -X POST http://127.0.0.1:28097/internal/media/auth`. Verify
  backend `8000`, HLS `8888` and API `9997` have no host publication. Check the
  intended RTMP bind/port from a trusted encoder, not just from inside Docker.
- Verify static UI, login, pairing and WebSocket reconnect still work through
  nginx and any external proxy, including preservation of Host with its port.
- Confirm valid OBS credentials publish, an intentionally wrong key fails and a
  second publisher cannot displace the first. An unauthenticated browser must
  not read settings, status or `/api/stream/live/index.m3u8`.
- Verify playback, cookie renewal, logout, leaving/re-entering STREAM, encoder
  reconnect, and media restart recovery on the target browser. Check cookie
  `Secure` under HTTPS. Test with actual video/audio, not only status telemetry.
- If media is unavailable, check private service DNS, config mounts and the
  `/internal/media/auth` callback without publishing it. If source is ready but
  playback fails, check codec support, playback cookies, TLS settings and browser
  errors. A stale `PUBLIC_HOST` affects the encoder URL, not container routing.

Static Compose/nginx checks cannot prove an Umbrel installation, actual port
availability, container-image runtime compatibility or end-to-end media behavior.

## Browser Behavior

OPERATOR / STREAM are accessible top-level tabs. Switching retains the operator
draft and conversation selection but stops browser media requests when leaving
STREAM. It never stops the encoder. HLS.js handles MSE, native HLS is the fallback;
muted autoplay, manual play, offline/reconnect/error states and fullscreen controls
are available. The live view fills the main content width, not a sidebar column.
