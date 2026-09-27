# EVENCOMMS

Local communication between an Even glasses wearer and a browser operator.
The wearer edits a private draft, sends a question, and reads the operator's
reply on the glasses. English speech transcription runs locally; an existing
local Ollama server can suggest a reply, but **the operator must review and send
it**. Wearer assistance remains local and human-approved. The separate optional
RESEARCH tab can send an operator's questions and selected still frames to OpenAI;
it never automatically replies to the glasses.

Version `0.4.0` adds an **experimental ChatGPT account connection through Codex**
for operator-only Research. Source files alone do not establish successful CI,
image publication or an Umbrel installation. Consult the
[release page](https://github.com/9vibes/EVENCOMMS/releases) for actual artifacts
and check results. The release image names share the existing public package:
`ghcr.io/9vibes/evencomms:0.4.0` for the app and
`ghcr.io/9vibes/evencomms:0.4.0-codex` for the isolated bridge.
The installable Umbrel package is maintained
in [KNS-Umbrel](https://github.com/9vibes/KNS-Umbrel/tree/master/kunas-evencomms).
The files in `deploy/umbrel` remain local-development templates, not the store's
digest-pinned release package. See [the protocol](docs/protocol.md) for API
contracts. Physical G2/phone behavior still needs hardware acceptance testing.

Codex uses eligible ChatGPT plan allowance rather than API-key billing, with no
automatic API, provider or model fallback and no model-entitlement guarantee.
The `0.4.0` Umbrel source package installs its isolated service by default,
automatically provisioning a private service token. Startup runs synthetic
loopback safety probes, not real account login or inference. Account use requires
explicit Research provider selection, device sign-in, model selection and Send.
The UI still defaults to API mode; existing optional OpenAI keys are unchanged.
Standalone `compose.yml` has no Codex service unless you add `compose.codex.yml`.
See [Codex setup, allowance and privacy](docs/codex.md).

The features introduced in `0.3.0` remain: API Research, selected video-frame
captures, 1x to 4x digital zoom/pan, and Low-Latency HLS with 200 ms parts and a
one-second browser live-sync target, not a latency guarantee.

**OPERATOR / STREAM** switches between conversations and a full-width live RTMP
feed with authenticated HLS playback, without recording or transcoding. Updating
from `0.1.0` or `0.2.1` requires redeploying the full stack, including nginx and MediaMTX,
not just replacing the frontend. Keep the existing app installation and back up
its data first; do not uninstall. The app ID, web port, password, conversations,
wearer pairings and model cache are preserved. Managed proxy/media configuration
is installed automatically by the image's init service. See the
[stream deployment and OBS guide](docs/streaming.md).

## Operator And Wearer Interaction

### Research API Mode

The **RESEARCH** tab beside STREAM provides an OpenAI chat, an available-model
selector, and a live preview with **Capture frame**. A capture includes video
pixels at the current zoom/pan, not page controls, and stays in the draft until
you explicitly choose **Send to OpenAI**. Text-only questions work without a feed.

Connect your own OpenAI API key for the current sign-in over HTTPS/localhost,
or configure `OPENAI_API_KEY` on the server. Keys entered in the UI stay only in
server memory for that operator session. Available model IDs come from OpenAI;
choose a Responses/vision-compatible model rather than assuming every ID supports
images. OpenAI API billing and retention rules apply, separately from ChatGPT.

Research chat/images remain in browser memory and clear on reload, logout or New chat.
No frames, microphone audio, stream keys or wearer messages are sent automatically.
Send includes the displayed history and its attached stills; no live feed is sent
to OpenAI and no web-search tools are enabled. Requests use `store: false`, not a
zero-retention guarantee. Provider testing uses MockTransport and browser stubs,
not live OpenAI calls. See
[Research setup and privacy](docs/research.md).

### Experimental Codex Mode

Choose **ChatGPT account (Experimental Codex)** in Research, then **Sign in with
ChatGPT**. Complete device login on OpenAI's site; never paste account passwords,
cookies or OAuth tokens into EVENCOMMS. Remote sign-in requires HTTPS, configured
manually with explicit origins; exact localhost HTTP is for isolated development.
Choose a pinned model explicitly and use **Send via Codex** to submit displayed
history and selected stills. Signing in and choosing a model do not submit them.

An eligible plan/workspace is required, and plan limits, purchased credits and
account/workspace data controls apply. This is not unlimited API access or the
API `store: false` contract. At most two signed-in operators and eight fresh
sends per login bound retained RAM. Account tokens and submitted threads are
ephemeral; disconnect, expiry or restart loses the login. Browser New chat does
not erase previously submitted bridge threads. A failed bridge/probe disables
Codex generation without blocking the rest of the app or falling back to API
billing. See the [Codex guide](docs/codex.md) before using sensitive captures.

### Wearer Controls

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
install **EVENCOMMS** (`kunas-evencomms`). Check that package's version and verified
digests rather than assuming this source candidate is already available there.
The release target is Linux x86-64 (amd64), with CPU speech recognition and no
NVIDIA runtime. ARM support is not established by the pinned Codex ARM64 asset.

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

In the `0.4.0` source package, `data_init` provisions a separate private
`codex-auth/token`, mounted read-only by backend and bridge, never by nginx or
MediaMTX. No app-password reuse or manual bridge credential setup is needed.
The normal server depends only on `data_init`, not bridge health. No public
ports are added. See the [source staging guide](deploy/umbrel/README.md) for
isolation, backup permissions and release-promotion requirements.

## Standalone Deployment

Requirements: Docker Engine with Compose v2, a compatible CPU, disk/RAM for the
selected speech model, and the complete frontend source plus its committed
`package-lock.json`. The multi-stage image runs `npm ci` and `npm run build`
under Node 24, then installs `pip install '.[stt]'` under Debian-based Python
3.12 slim and serves `frontend/dist` with FastAPI behind nginx. Alpine/musl is not
used for the application image (nginx uses Alpine):
CTranslate2's prebuilt wheels require a supported glibc platform.

1. Create a local `.env` from `.env.example` and set a long, unique
   `ADMIN_PASSWORD`. The blank example is deliberate: Compose refuses to start
   without a nonempty password. Keep this file private and out of version control.
   For a remote encoder, also set `PUBLIC_HOST` to the reachable LAN/VPN hostname
   or IP (no scheme, port or path) and `RTMP_BIND` to the host's LAN/VPN IP.
2. From this directory, build and start:

```sh
docker compose build
docker compose up -d --force-recreate
docker compose ps
curl --fail http://127.0.0.1:28097/health
```

3. Open `http://127.0.0.1:28097/` for the operator console, sign in, and generate
   a one-use pairing code. In another client open
   `http://127.0.0.1:28097/glasses.html?simulate=1` and pair for **typed browser
   simulation**. This does not promise browser microphone capture or validate
   real glasses audio.

The locally built tag is `evencomms:0.4.0`; no registry pull is needed or claimed.
This default stack does not install/start Codex. The optional standalone
`compose.codex.yml` overlay builds `evencomms-codex:0.4.0` and uses a manually
generated private `.env` service token, unlike Umbrel's automatic provisioning.
Follow [the Codex deployment guide](docs/codex.md#standalone-opt-in) to opt in.
The API binds port `8000` inside the private network and is **never published**.
The only HTTP entry is `web` nginx on container port `8080`, published as host
`28097`, bound to loopback by default. For isolated LAN testing set
`BIND_ADDRESS` to the host's LAN IP (or `0.0.0.0` to listen on all IPv4 interfaces)
and recreate the services. RTMP has a separate loopback-default `RTMP_BIND` and
TCP port `RTMP_PORT=21936`; HLS `8888` and media API `9997` stay unpublished.
Firewall access to trusted clients. Use TLS for phone use
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
`web:8080` instead; the proxy's own loopback is not the host. Never target
`server:8000`, including through Umbrel's `app_proxy`: that bypasses nginx's
all-method blocks for exact `/internal` and the `/internal/` prefix. Keep the
backend unpublished and allow external browser access only through TLS.
Preserve Host, forward WebSocket Upgrade, and do not strip `/api` or other
paths. Set proxy response timeouts above `STT_TIMEOUT`, `OLLAMA_TIMEOUT` and
`OPENAI_TIMEOUT`; allow up to 9 MiB for Research chat requests and the 480000-byte
audio request plus framing. Backend route-specific limits still apply. Avoid
logging credentials, request bodies, transcription text or replies at the proxy.

Set this in `.env` and recreate the service:

```dotenv
ALLOWED_ORIGINS=https://comms.example.net
COOKIE_SECURE=true
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
| `STREAM_ENABLED` | `true` in Compose; `false` bare backend | Enable the single RTMP feed and authenticated HLS playback. |
| `PUBLIC_HOST` | `localhost`; Umbrel device domain in staging | Encoder-reachable LAN/VPN hostname or IP only, without scheme, port or path; must be set for remote ingest. |
| `RTMP_BIND` | `127.0.0.1` standalone; `0.0.0.0` Umbrel staging | Host RTMP bind interface, independent of web `BIND_ADDRESS`; prefer a specific LAN/VPN IP. |
| `RTMP_PORT` | `21936` | Published TCP ingest port and the port advertised to encoders; check host availability. |
| `MEDIA_API_URL` | `http://mediamtx:9997` | Backend-only media API origin, fixed in Compose; never publish. |
| `MEDIA_HLS_URL` | `http://mediamtx:8888` | Backend-only HLS origin, fixed in Compose; never publish. |
| `COOKIE_SECURE` | `false` | Set `true` behind TLS for the HttpOnly playback cookie. |
| `ALLOWED_ORIGINS` | Empty | Exact, comma-separated HTTP(S) browser/packaged app origins. |
| `STT_ENABLED` | `true` | `true`/`false` or `1`/`0`; disable for text-only use. |
| `STT_MODEL` | `base.en` | Faster Whisper model name or local model directory. |
| `STT_TIMEOUT` | `90` | Transcription request deadline in seconds. |
| `OLLAMA_URL` | See below | Existing local Ollama API base URL, not `/api/chat`. |
| `OLLAMA_MODEL` | `llama3.2:3b` | Model already installed in that Ollama server. |
| `OLLAMA_TIMEOUT` | `30` | Suggestion deadline in seconds. |
| `OPENAI_API_KEY` | Empty | Optional shared server-managed key for Research; alternatively connect a sign-in-only key in the HTTPS UI. Never expose through frontend environment variables. |
| `OPENAI_TIMEOUT` | `90` | Research provider deadline, greater than zero and at most 110 seconds. |
| `OPENAI_MAX_OUTPUT_TOKENS` | `2048` | API-mode Research output cap, 256..8192; API usage is billable. Does not cap Codex tokens. |
| `CODEX_BRIDGE_URL` | Empty; `http://codex-bridge:8001` in Umbrel/standalone Codex overlay | Private bridge origin; no public port or browser setting. |
| `CODEX_BRIDGE_TOKEN` | Empty | Standalone opt-in service secret in private `.env`, not an OpenAI credential or app password. |
| `CODEX_BRIDGE_TOKEN_FILE` | Empty; `/run/codex-auth/token` in Umbrel | Read-only private service-secret file used by backend and bridge; mutually exclusive with `CODEX_BRIDGE_TOKEN`. |
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
- Research requests allow 20 messages, 6 JPEGs total and 3 per user turn, each
  at most 1 MiB and 1280 pixels per side, within a 9 MiB chat-body limit. Two
  off-event-loop validation slots and two separate provider-call slots each
  allow one request per operator; excess work receives `429`, not an unbounded
  CPU queue. History is never silently shortened to fit.
- Experimental Codex adds at most two account sessions and eight fresh sends per
  login to bound RAM. Its independent two-worker validation pool can run alongside
  API validation. No automatic model/provider/API fallback; allowance and account
  entitlement remain subject to OpenAI. See [Codex limits](docs/codex.md#operator-steps).
- Ollama failure does not prevent a manual operator reply. Suggestions use
  bounded recent conversation context and remain editable, never auto-sent.

## Privacy And Data

Unsent drafts belong to the wearer and are not shown to the operator or Ollama.
Audio is sent to the local backend for transcription and is not written as raw
audio to application storage. Sent conversation text is stored in SQLite; only
an explicit suggestion request sends bounded recent text to the configured
local Ollama instance. This is not end-to-end encryption from the server.

Research is a separate, opt-in cloud workflow. In API mode, Send to OpenAI submits
its displayed chat history and selected JPEG still frames to OpenAI, not to the wearer. It does
not send continuous video/audio, stream keys or wearer drafts. Research history
and captures are browser-memory-only; a bounded server result cache supports
recent retries. UI-entered API keys are isolated per sign-in in server RAM and
clear on logout, expiry, explicit Research disconnect or restart. A server
environment key is shared by authenticated operators. Provider billing/retention policies still apply even
with `store: false`. Use a private deployment for sensitive footage.

Experimental Codex instead uses ChatGPT allowance and account/workspace data
controls. Its official runtime and private relay handle OAuth credentials only
in isolated bridge RAM; the browser/backend never receive account tokens.
Submitted history and images remain in ephemeral threads until runtime cleanup,
not just until New chat. Startup probes use synthetic loopback fixtures, never a
real account. Retention, quota, cleanup and retry caveats are in the
[Codex privacy guide](docs/codex.md#privacy-and-lifetime).

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
Stream publisher and reader secrets persist in the same database. The publisher
key is revealed only on request in STREAM; playback uses a short-lived HttpOnly
cookie. Neither secret rotation nor recording is implemented. See
[stream security and recovery](docs/streaming.md#security-and-recovery).

For a consistent filesystem backup, stop the server first, back up the entire
`/data` volume (database plus sidecar files and optionally model cache), then
restart. Restore with UID/GID `10001:10001` ownership. `docker compose down`
preserves the named volume; `docker compose down -v` destroys it. Neither that
command nor conversation deletion removes independent backups.
For Umbrel, also back up the separate private `${APP_DATA_DIR}/codex-auth`
directory with its service token, preserving `10001:10002` ownership, directory
mode `0750` and token mode `0440`. Never persist or back up OAuth credentials.

## Development And Verification

On Debian/glibc Python 3.12, install `pip install '.[stt,test]'`, then run
`python -m pytest` from the repository root so tests use the source tree and its
bundled infrastructure files. In `frontend`, run `npm ci`, `npm test`, and `npm run build` using
Node 24. For typed-only backend development, `pip install '.[test]'` plus
`STT_ENABLED=false` avoids installing speech runtime dependencies. A local
Uvicorn invocation must also use a single worker and the socket limits above.

For a local development loop, explicitly bind the backend to `127.0.0.1:8000`
(`uvicorn backend.main:app --host 127.0.0.1 --port 8000`, with the single-worker
socket/logging flags above) with a configured
`ADMIN_PASSWORD`, then `npm run dev` from `frontend`. Vite proxies `/api` and
WebSockets to that backend. Open `http://localhost:5173/` for the operator and
`http://localhost:5173/glasses.html?simulate=1` for typed simulation.
Vite does not proxy `/internal`; no additional nginx is needed for this local
development loop. Do not expose a bare streaming backend on a LAN/public bind.

Run repeatable browser tests against an isolated real backend:

```sh
npm run build
npx playwright install chromium
npm run test:e2e
```

These commands run from `frontend`; `python3` must have the backend test/runtime
dependencies. Set `EVENCOMMS_PYTHON` to a virtualenv Python if needed, and
`CHROMIUM_PATH` to use an existing Chromium. Test databases are temporary and
model inference is disabled. The conversation browser tests mock the AI suggestion;
the separate Research smoke stubs `/api/research/**`, while authentication and
video playback remain real. Backend provider tests use MockTransport. Neither
suite verifies live OpenAI model compatibility, billing or retention. See
[browser tests](frontend/e2e/README.md) and [Research smoke checks](scripts/README.md#research-real-frame-browser-smoke).

For the official desktop simulator, run `npm run simulate` alongside Vite.
The simulator needs a supported desktop OS and its native GUI/WebKit libraries;
Alpine/musl containers are not a validated simulator target. The package command
has been validated against SDK 0.0.16; it stamps a minimum Even app version of
2.2.10. The checked-in manifest has no deployment-specific origin: packaging
generates an origin-specific manifest/config in temporary storage without
changing the normal server build. Never deploy an example-origin test package.

The verification pipeline covers backend tests, frontend tests/build, browser
end-to-end tests, Even packaging, Compose configuration and image smoke tests.
Codex checks use the pinned real binary with synthetic loopback OAuth/inference,
not a live account. Its authenticated private `GET /ready` must pass the actual
generation gate with zero idle account sessions; `/health` alone is insufficient.
The `0.4.0` release must pass CI, CPU transcription with synthetic speech,
persistence/upgrade checks and isolated bridge checks before publishing the
exact tested amd64 images. The canonical store update follows verified anonymous
pulls of both digests. See [release checks](scripts/README.md); this checklist is
not a claim that the candidate has passed GitHub CI or been published. Before
deployment, verify a real utterance, model cache reuse, pairing/reconnect, gesture ordering, deletion,
TLS/CORS/network permissions, and an operator-approved AI reply on target
hardware. Report the actual test results, not a fixed historical test count.
