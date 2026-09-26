# EVENCOMMS

Local communication between an Even glasses wearer and a browser operator.
The wearer edits a private draft, sends a question, and reads the operator's
reply on the glasses. English speech transcription runs locally; an existing
local Ollama server can suggest a reply, but **the operator must review and send
it**. There is no cloud inference integration and no automatic AI reply.

Version `0.1.0` is an early release. The installable Umbrel package is maintained
in [KNS-Umbrel](https://github.com/9vibes/KNS-Umbrel/tree/master/kunas-evencomms).
The files in `deploy/umbrel` remain local-development templates, not the store's
digest-pinned release package. See [the protocol](docs/protocol.md) for API
contracts. Physical G2/phone behavior still needs hardware acceptance testing.

## First-Version Interaction

- The glasses screen switches between the unsent draft and latest operator reply,
  with swipe navigation for longer text. The phone mirrors both.
- A single tap finalizes preceding speech and deletes the draft's last word.
  Capture pauses during correction; use Start listening to speak a replacement.
- A continuous hold freezes capture; releasing finalizes preceding speech and
  sends once. Failed transcription must be retried or discarded explicitly,
  not silently omitted from a partially sent question.
- Double tap exits, following the required Even convention; it is not Send.
- A contextual menu provides pause/resume for capture.

These controls are implemented in the client. Validate the SDK's actual
hold/release events and the pause/resume/exit behavior on supported glasses
before relying on the app. Short local utterances produce transcription chunks;
continuous, word-by-word partial updates are **not guaranteed**.

## Umbrel Installation

Add `https://github.com/9vibes/KNS-Umbrel` to Umbrel's community app stores, then
install **EVENCOMMS** (`kunas-evencomms`). The initial published image targets
Linux x86-64 (amd64), uses CPU speech recognition, and needs no NVIDIA runtime.
ARM devices are not supported by this release image.

Open the app on port `28097` and sign in with the generated application password
shown by Umbrel; no username is required. The browser simulator works without
glasses. The model is downloaded on first transcription, not during installation.
Allow time for that download and keep the model cache for subsequent offline use.

The package defaults to allowing `http://<device-domain>:28097` for WebSockets.
If using an IP address, another hostname, or a trusted HTTPS proxy, configure the
full browser origin in `ALLOWED_ORIGINS`. Include the packaged Even app's origin
when it differs. Do not use wildcards. Manual replies work without Ollama; set a
reachable `OLLAMA_URL` and installed `OLLAMA_MODEL` to enable AI suggestions.

Read the [store setup guide](https://github.com/9vibes/KNS-Umbrel/blob/master/kunas-evencomms/README.md)
for persistent storage, settings, TLS and phone packaging. Adding the package to
the store is not the same as installing it on a particular Stone.

## Standalone Deployment

Requirements: Docker Engine with Compose v2, a compatible CPU, disk/RAM for the
selected speech model, and the complete frontend source plus its committed
`package-lock.json`. The multi-stage image runs `npm ci` and `npm run build`
under Node 24, then installs `pip install '.[stt]'` under Debian-based Python
3.12 slim and serves `frontend/dist` with FastAPI. Alpine/musl is not used:
CTranslate2's prebuilt wheels require a supported glibc platform.

1. Create a local `.env` from `.env.example` and set a long, unique
   `ADMIN_PASSWORD`. The blank example is deliberate: Compose refuses to start
   without a nonempty password. Keep this file private and out of version control.
2. From this directory, build and start:

```sh
docker compose build
docker compose up -d
docker compose ps
curl --fail http://127.0.0.1:28097/health
```

3. Open `http://127.0.0.1:28097/` for the operator console, sign in, and generate
   a one-use pairing code. In another client open
   `http://127.0.0.1:28097/glasses.html?simulate=1` and pair for **typed browser
   simulation**. This does not promise browser microphone capture or validate
   real glasses audio.

The locally built tag is `evencomms:0.1.0`; no registry pull is needed or claimed.
The API binds port `8000` inside the container. The published host port is
`28097`, bound to loopback by default. For isolated LAN testing set
`BIND_ADDRESS` to the host's LAN IP (or `0.0.0.0` to listen on all IPv4 interfaces)
and recreate the service. Firewall it to trusted clients. Use TLS for phone use
and private conversations; plain HTTP exposes passwords and bearer tokens.

The server runs as UID/GID `10001:10001`, with a read-only root filesystem and
writable temporary storage. A new named `data` volume inherits the image's
owned `/data` and `/data/models` directories. If replacing it with a bind mount
or restoring an existing volume, prepare ownership for `10001:10001` first.
Both SQLite and the model cache persist in `/data`. Do not scale replicas or
increase workers: operator tokens, pairing codes, sockets and the STT busy slot
are process-local. The configured single worker is required.

`/health` is **liveness only**, not a model warmup, Ollama connectivity or storage
readiness test. First transcription downloads the selected model unless already
cached; it can exceed the request timeout. Models remain cached across restarts.
Acquire/cache models before an offline deployment. The app does not persist raw
audio, but model downloads need network access on a cold cache.

## TLS And Origins

Terminate TLS in a trusted external reverse proxy. For example, Caddy running
on the same host can proxy a DNS name with a valid certificate:

```caddyfile
comms.example.net {
    reverse_proxy 127.0.0.1:28097
}
```

For a containerized proxy, connect it to the app's Docker network and proxy
`server:8000` instead; the proxy's own loopback is not the host. Keep the backend
unpublished or loopback-only and allow external access only through TLS.
Preserve Host, forward WebSocket Upgrade, and do not strip `/api` or other
paths. Set proxy response timeouts above `STT_TIMEOUT` and `OLLAMA_TIMEOUT`;
allow the 480000-byte audio request plus framing. Avoid logging credentials,
request bodies, transcription text or replies at the proxy.

Set this in `.env` and recreate the service:

```dotenv
ALLOWED_ORIGINS=https://comms.example.net
```

**This is required even for a same-origin browser using HTTPS/WSS.** Uvicorn runs
with `--no-proxy-headers`, so behind TLS termination the backend sees HTTP/WS,
not the browser's HTTPS/WSS origin. Its WebSocket same-origin check alone will
not match. Add the public HTTPS origin explicitly rather than enabling blanket
proxy trust. When the packaged Even app has a different origin, include that
actual origin too, comma-separated. Origins are scheme + host + optional port,
with no paths or wildcards; HTTPS origins are used here, not `wss://` URLs.

Origin checks and CORS do not replace authentication. With forwarded headers
ignored, requests through a proxy share its source address for IP throttling;
global throttles also apply. The runtime uses bounded WebSocket input:
`--ws-max-size 8192 --ws-max-queue 8 --no-access-log --no-proxy-headers`.

## Even Hub

Use the official `@evenrealities/even_hub_sdk` integration, SDK **0.0.16 or later**,
and Even app **2.2.10 or later** with compatible, up-to-date glasses firmware.
Check device/firmware compatibility on real hardware; browser simulation is
not evidence of microphone or gesture support.

The glasses entry point is `/glasses.html`. Use the Even Hub developer workflow
and its development QR to load the phone-reachable URL, not a localhost URL
that points back at the phone. The browser simulation entry point is
`/glasses.html?simulate=1`; do not use simulation mode to test glasses hardware.

For packaging from `frontend`, the frontend pack script uses the externally
reachable HTTPS backend origin:

```sh
npm ci
npm run build
EVENCOMMS_ORIGIN=https://comms.example.net npm run pack
```

The generated Even package must explicitly whitelist that HTTPS backend origin
in its network permissions (and the corresponding WebSocket destination if
required by the packaging schema). `EVENCOMMS_ORIGIN` is a package/build setting,
not a backend runtime setting. Rebuild/repack if it changes. Confirm the generated
manifest contains the intended origin before installing; never use a wildcard.
Allow the **actual packaged app origin** in backend `ALLOWED_ORIGINS` for its
cross-origin HTTP and WebSocket requests. Do not guess that origin from the
backend URL: inspect the SDK/development client's Origin or packaging output.
The frontend and pack script must be present in the complete source checkout;
deployment files alone do not provide them.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `ADMIN_PASSWORD` | Required | Operator login password; no default credential. |
| `BIND_ADDRESS` | `127.0.0.1` | Standalone host interface for port 28097, not an API setting. |
| `ALLOWED_ORIGINS` | Empty | Exact, comma-separated HTTP(S) browser/packaged app origins. |
| `STT_ENABLED` | `true` | `true`/`false` or `1`/`0`; disable for text-only use. |
| `STT_MODEL` | `base.en` | Faster Whisper model name or local model directory. |
| `STT_TIMEOUT` | `90` | Transcription request deadline in seconds. |
| `OLLAMA_URL` | See below | Existing local Ollama API base URL, not `/api/chat`. |
| `OLLAMA_MODEL` | `llama3.2:3b` | Model already installed in that Ollama server. |
| `OLLAMA_TIMEOUT` | `30` | Suggestion deadline in seconds. |
| `MAX_SESSIONS` | `100` | Stored conversation limit. |
| `MAX_MESSAGES_PER_SESSION` | `1000` | Stored messages per conversation. |
| `DATABASE_PATH` | `/data/evencomms.sqlite3` in image | SQLite database, including adjacent WAL files. |
| `MODEL_CACHE` | `/data/models` in image | Persistent speech model storage. |
| `FRONTEND_DIST` | `/app/frontend/dist` in image | Built frontend served by FastAPI. |

Compose fixes the container data paths; change volume mappings and environment
together if customizing them. Outside Docker the backend defaults to repository
`data/`, `data/models` and `frontend/dist`. Timeouts and limits must be positive
and finite. Defaults above are configuration choices, not measured hardware
capacity or latency guarantees.

Standalone Compose uses `http://host.docker.internal:11434` for an existing host
Ollama instance, including a Linux host-gateway mapping. Ollama must listen on an
interface reachable from Docker, with firewall protection; its loopback-only
default may not work. Use a Docker service name on a shared network or a trusted
LAN address instead when appropriate. The bare backend default is
`http://127.0.0.1:11434`; inside a container this means the container itself.
Umbrel staging leaves `OLLAMA_URL` empty. An empty URL or model disables
suggestions. No deployment here installs Ollama or pulls an Ollama model.

STT initially uses CPU/int8 and English, not GPU. No CUDA configuration or
untested speed/RAM guarantee is provided. Benchmark latency, memory and thermal
behavior on the target hardware before tuning models or considering GPU work.

## Limits And Recovery

- Audio requests are PCM s16le, 16 kHz mono, at most 15 seconds / 480000 bytes.
  Transcription accepts one active job; concurrent work receives `429` rather
  than entering an unbounded backend queue.
- A timed-out transcription returns `504`, but its worker thread cannot be
  cancelled and stays busy until completion. Disabling STT/unavailable models
  returns `503`. A healthy `/health` response does not contradict these errors.
- The client segments speech at silence or 10 seconds, pauses when two audio jobs
  are pending, and retains at most three bounded chunks including a pause flush.
  Oversized SDK frames are explicitly rejected with a warning, not silently lost.
  Pending audio/retry buffers are memory-only and are lost on reload or exit.
- Message text is limited to 4000 characters; sends use client UUIDs for
  idempotent retry. A connection loss is not proof that a send failed: reconcile
  acknowledged messages before resubmitting. A new wearer socket replaces the
  previous socket for the same conversation.
- Pairing codes last five minutes and are single use. Operator tokens last
  eight hours and are lost on server restart. Wearer pairing persists until its
  conversation is deleted. A server restart requires operator login again.
- Ollama failure does not prevent a manual operator reply. Suggestions use
  bounded recent conversation context and remain editable, never auto-sent.

## Privacy And Data

Unsent drafts belong to the wearer and are not shown to the operator or Ollama.
Audio is sent to the local backend for transcription and is not written as raw
audio to application storage. Sent conversation text is stored in SQLite; only
an explicit suggestion request sends bounded recent text to the configured
local Ollama instance. This is not end-to-end encryption from the server.

Wearer drafts/tokens use browser `localStorage`; operator authentication uses
`sessionStorage`. These are not encrypted vaults: protect browser profiles,
phone access and shared devices. Use the wearer's **clear device** control to
remove local pairing/draft state. Local clearing does not delete the server
conversation or revoke a token copied elsewhere. Delete the conversation from
the operator console to remove stored messages and revoke its wearer token.
Remote deletion cannot reliably clear storage on a disconnected client.

Deletion is logical application deletion, **not a forensic erase guarantee**.
SQLite/WAL remnants, host snapshots, backups, browser storage remnants and swap
may retain data. Manage backup retention and host/disk encryption separately.
Pending audio is not backed up and is lost on reload. Avoid recording bodies or
tokens in infrastructure logs. Keep `.env`, app data and backups private.

For a consistent filesystem backup, stop the server first, back up the entire
`/data` volume (database plus sidecar files and optionally model cache), then
restart. Restore with UID/GID `10001:10001` ownership. `docker compose down`
preserves the named volume; `docker compose down -v` destroys it. Neither that
command nor conversation deletion removes independent backups.

## Development And Verification

On Debian/glibc Python 3.12, install `pip install '.[stt,test]'`, then run
`pytest`. In `frontend`, run `npm ci`, `npm test`, and `npm run build` using
Node 24. For typed-only backend development, `pip install '.[test]'` plus
`STT_ENABLED=false` avoids installing speech runtime dependencies. A local
Uvicorn invocation must also use a single worker and the socket limits above.

For a local development loop, run the backend on port 8000 with a configured
`ADMIN_PASSWORD`, then `npm run dev` from `frontend`. Vite proxies `/api` and
WebSockets to that backend. Open `http://localhost:5173/` for the operator and
`http://localhost:5173/glasses.html?simulate=1` for typed simulation.

Run repeatable browser tests against an isolated real backend:

```sh
npm run build
npx playwright install chromium
npm run test:e2e
```

These commands run from `frontend`; `python3` must have the backend test/runtime
dependencies. Set `EVENCOMMS_PYTHON` to a virtualenv Python if needed, and
`CHROMIUM_PATH` to use an existing Chromium. Test databases are temporary and
model inference is disabled. The only mocked HTTP response in browser tests is
the AI suggestion. See [browser tests](frontend/e2e/README.md).

For the official desktop simulator, run `npm run simulate` alongside Vite.
The simulator needs a supported desktop OS and its native GUI/WebKit libraries;
Alpine/musl containers are not a validated simulator target. The package command
has been validated against SDK 0.0.16; it stamps a minimum Even app version of
2.2.10. The checked-in manifest has no deployment-specific origin: packaging
generates an origin-specific manifest/config in temporary storage without
changing the normal server build. Never deploy an example-origin test package.

The CI workflow checks backend tests, frontend tests/build, browser end-to-end
tests, Even packaging, standalone Compose configuration and an image build/smoke
test. CI does not fetch speech models or verify glasses hardware. The separate
release workflow runs CI, builds an amd64 image, tests real CPU transcription
with synthetic speech and persisted data/model cache, then publishes that exact
image to GHCR. See [release checks](scripts/README.md). Before deployment, verify a
real utterance, model cache reuse, pairing/reconnect, gesture ordering, deletion,
TLS/CORS/network permissions, and an operator-approved AI reply on target
hardware. Report the actual test results, not a fixed historical test count.
