# Umbrel Staging Package

This is the **0.4.0 local staging candidate, not a published store release**.
Neither local-only image references nor these templates should be installed from a store.
The installable, digest-pinned package lives in
[KNS-Umbrel](https://github.com/9vibes/KNS-Umbrel/tree/master/kunas-evencomms).
The app ID remains `kunas-evencomms`; web port `28097` and RTMP port `21936` are
unchanged. Consult the GitHub release page and canonical store package for
published references. These templates are not evidence of an actual installation.

**Version 0.4.0 requires deployment of the full stack and both images.** It adds
experimental Codex Research, with an isolated bridge running by default but
account use explicitly opt-in. Startup runs synthetic loopback probe processes;
it does not log in to a real account or send real inference requests. Operators
must select Codex, complete device sign-in, choose a model and Send. The API
provider remains the UI default and existing OpenAI key overrides are preserved.
A failed bridge or safety probe does not block ordinary app startup or other features.

The local tags are `evencomms:0.4.0` and `evencomms-codex:0.4.0`. Planned publication
uses the same existing GHCR package for `ghcr.io/9vibes/evencomms:0.4.0` and
`ghcr.io/9vibes/evencomms:0.4.0-codex`; these names are not proof of publication.
Use KNS-Umbrel for a normal installation after verified image promotion; these
development files do not update the live store automatically. The 0.3.0 features
remain: OpenAI API Research, selected stills, 1x to 4x digital zoom/pan and LL-HLS
with 200 ms parts and a one-second browser live-sync target, not a guarantee.
The `0.1.0` image lacks streaming; `0.2.1` uses ordinary fMP4 HLS.

## Stage Locally

1. From the EVENCOMMS repository root, run `docker build -t evencomms:0.4.0 .`
   and `docker build -f deploy/codex/Dockerfile -t evencomms-codex:0.4.0 .`
   on the target host. The frontend and bridge packages and lockfiles must be present.
2. If building elsewhere, use `docker save evencomms:0.4.0 evencomms-codex:0.4.0`
   and `docker load` to transfer both images to the Umbrel Docker daemon. Build for the target
   architecture; a tag alone does not make an image multi-architecture.
3. Use these files only in a separate local/test Umbrel app staging area under
   app ID `kunas-evencomms`, following that Umbrel version's development workflow.
   Do not copy this unpublishable package into the live KNS store.
4. The init service installs both bundled `infra/` files into
   `${APP_DATA_DIR}/config` automatically. The image is the source of truth; no
   manual template copying or `envsubst` is needed. Nginx's `$http_host` and
   upgrade variables remain literal. Config directories use mode `0755` and
   managed files `0644`, readable by nginx UID 101. Both consumers mount the
   whole config directory read-only, avoiding missing-file bind-mount races.
   Existing managed configs are refreshed on initialization; do not customize
   them in place. Unrelated files and the persistent data volume are untouched.
   The same networkless initializer creates `${APP_DATA_DIR}/codex-auth/token`
   once with a random 64-character hexadecimal service token. The dedicated
   directory is `10001:10002`, mode `0750`; its token file is mode `0440` with the
   same ownership. Only init mounts it writable at `/codex-auth`; backend and
   bridge mount it read-only at `/run/codex-auth`. It never enters shared `/config`,
   nginx or MediaMTX. No manual credential setup or app-password reuse is needed.
5. Supply the environment through the staging launcher. Umbrel must supply
   `APP_DATA_DIR` and a nonempty `APP_PASSWORD`. The device's `.local` HTTP origin
   is allowed by default. Override `ALLOWED_ORIGINS` for IP access or other
   browser/phone origins, including the external HTTPS origin.
   Set `COOKIE_SECURE=true` behind TLS. `PUBLIC_HOST` defaults to
   `${DEVICE_DOMAIN_NAME:-umbrel.local}`; override it with a LAN/VPN hostname or
   IP reachable by the encoder, without scheme, port or path. `RTMP_PORT=21936`
   is separate from SteamLab's 21935 but still requires an actual host port check.
   Set `RTMP_BIND` to the host's LAN/VPN IP where possible; this staging overlay
   defaults to `0.0.0.0`, unlike standalone's loopback default. Firewall TCP ingest
   to trusted encoders. Never publicly port-forward plaintext RTMP.
   Configure an existing local `OLLAMA_URL` if wanted; Ollama suggestions are
   disabled by default here. API Research optionally uses your own OpenAI API key;
   prefer the HTTPS in-app sign-in connection or a private server environment.
   Experimental Codex instead uses an eligible ChatGPT account, after explicit
   operator actions. No new enable flag or secret metadata field is required.
   The backend settings in the root README apply to this package too.
6. Recreate the full staging stack through Umbrel's development launcher, not
   just `server`. Verify startup, authentication, pairing, socket reconnect,
   deletion and a real transcription on the intended hardware before calling it
   deployable.
   Follow the [OBS and stream verification guide](../../docs/streaming.md) for
   live playback, invalid credentials, private ports and callback isolation.

The compose file is an **Umbrel overlay**, not a standalone compose project:
Umbrel supplies `app_proxy`'s image/network and exposes manifest port `28097`.
Use the root `compose.yml` for standalone deployment. The proxy target follows
Umbrel's `kunas-evencomms_web_1:8080` service naming. Check the generated container name
in your Umbrel version and update the target if its naming differs.
Never target the raw backend: only `web` blocks `/internal` and `/internal/` for
every method. Only `web` joins both the default app-proxy network and the private
application network. `mediamtx` joins only `private`; `server` joins `private`
and the internal `codex_link` network. The existing `private` network is not
`internal: true`, so published RTMP and backend model downloads can work.
Codex joins only `codex_link` and the separate non-internal `codex_egress` bridge
for outbound authentication/inference; this is not an OpenAI-only egress firewall.
Backend `8000`, Codex `8001`, HLS `8888` and media API `9997` are not published.
Both media and web wait for the backend liveness check, without a dependency cycle.
MediaMTX's minimal image has no shell-based runtime health check. Web uses the
official Alpine image's BusyBox `wget` against `/health`. All services have
bounded JSON-file logs; nginx access logging is disabled.

`server` and `codex-bridge` each depend only on successful `data_init`, never on
each other's health. Codex runs as `10002:10002` with init/reaping, a read-only
root, a 256 MiB `/tmp` tmpfs (`noexec,nosuid,nodev`), 1 GiB memory and total
memory-plus-swap limit, one CPU, 128 PIDs, all capabilities dropped,
`no-new-privileges`, and disabled core dumps. It has no `/data`, `/config`, host
home, personal Codex cache or Docker socket mount. The health start period is
90 seconds for the bounded 75-second startup proof. Neither `data_init` nor
`codex-bridge` receives `APP_PASSWORD`; only the backend maps it to `ADMIN_PASSWORD`.

The application process runs as UID/GID `10001:10001`. A short-lived, networkless
root `data_init` service initializes `/data`, `/data/models`, and existing
SQLite database/WAL/SHM files, installs the two managed files into `/config`, and
provisions the separate `/codex-auth` service credential.
Symlinks and hard-linked database/config files are rejected;
ownership is not changed recursively. Restored model-cache files must retain
UID/GID `10001:10001`. Use a dedicated app directory. The database and model cache
persist under `${APP_DATA_DIR}/data`; the root filesystem is read-only.
Update in place, never uninstall to upgrade. Back up and preserve the entire data
directory, including SQLite sidecars, conversations, pairings, model cache and
persistent stream secrets, along with the existing Umbrel application password.
Also back up `${APP_DATA_DIR}/codex-auth` privately and restore its ownership and
modes above; initialization preserves its existing token rather than rotating it.
This is a local service secret, not a ChatGPT account token. OAuth credentials
are RAM-only and must not be added to persistent storage or backups.
Only bundled managed proxy/media configs are replaced. Operator sessions,
UI-entered Research keys and Codex account logins are intentionally lost on
restart. Browser Research history clears on reload/sign-out/New chat and is not
restored from backups; submitted Codex history also remains in ephemeral bridge
threads until runtime cleanup.

## Authentication And Networking

`APP_PASSWORD` is mapped to the backend's mandatory `ADMIN_PASSWORD`; use the
generated password displayed by Umbrel to sign in. `PROXY_AUTH_ADD: "false"`
is intentional **only because EVENCOMMS supplies its own authentication**.
Phone/Even Hub clients cannot depend on an Umbrel browser login cookie.
It does not make the API public: operator endpoints require a login token and
wearer endpoints require a paired wearer token. Protect the generated password.

Use a trusted TLS reverse proxy reachable from the phone. Forward HTTP and
WebSocket Upgrade traffic without rewriting paths; see the root README for a
Caddy example. Preserve the original Host. The backend ignores forwarded
headers, so explicitly allow the external HTTPS origin even when frontend and
API appear same-origin to the browser. Also allow the actual packaged Even app
origin, and whitelist the HTTPS backend origin in the Even package network
permissions. Do not substitute `*` or disable origin checks.

Umbrel's ordinary LAN HTTP URL may require its own explicit origin because the
app proxy can rewrite Host. HTTP is only for isolated staging, not private
phone conversations. TLS is a manual deployment prerequisite, not provisioned by
the Codex service. Remote plain HTTP cannot initiate device sign-in in the UI.
No Ollama service is installed and no cloud account is connected automatically.
Set `OLLAMA_URL` to a reachable local service address on the appropriate Docker
network or LAN; `127.0.0.1` inside this container is not the Umbrel host. Restrict
Ollama with network/firewall rules since its API is normally unauthenticated.

## Research Settings

Research is optional and never automatically replies to the wearer. API mode
remains selected by default. Connect your own key in the HTTPS UI for the current
login, or provide `OPENAI_API_KEY` through
a private server environment shared by authenticated operators. Do not add a
plain key field to the Umbrel manifest, use `VITE_` variables, commit credentials
or persist them in browser storage. Sign-in keys clear on logout, expiry, explicit
Research disconnect or server restart; removing one falls back to any server key.

Nonsecret settings match the staging Compose defaults: `OPENAI_TIMEOUT=90`
seconds (greater than zero, at most 110) and `OPENAI_MAX_OUTPUT_TOKENS=2048`
(256..8192). The model list comes from the connected OpenAI account, but does not
guarantee Responses/vision compatibility. Explicit Send submits displayed history
and selected JPEG stills, never continuous video/audio. API use is billable;
`store: false` is not a zero-retention guarantee. No web-search tools are enabled.
See [Research limits and privacy](../../docs/research.md).

For **ChatGPT account (Experimental Codex)**, the service is already installed
and privately authenticated by `CODEX_BRIDGE_TOKEN_FILE=/run/codex-auth/token`
in both backend and bridge. Do not also set `CODEX_BRIDGE_TOKEN`: conflicting
token sources are rejected. Do not put either secret in manifest/UI metadata.
Choose this provider explicitly, sign in on OpenAI's device page, select a model
and Send. Connecting and model selection never send questions or frames.

An eligible plan/workspace is required; pinned models are not entitlement
guarantees. Requests consume ChatGPT Codex allowance and may incur purchased-credit
costs. Account/workspace privacy controls apply, not the API `store: false`
contract. There is no automatic paid API, model or provider fallback. Two signed-in
operators and eight fresh sends per login bound retained RAM; disconnect, expiry
or restart loses the login. See [Codex setup, limits and privacy](../../docs/codex.md).

Provider tests use MockTransport and browser stubs, not live OpenAI. Real LL-HLS
smoke tests use synthetic media; native Safari, physical glasses/phones and the
actual Umbrel host still need acceptance testing over the intended TLS path.

## Release Promotion

1. Review frontend/hardware acceptance, security and canonical store metadata.
   Preserve app ID `kunas-evencomms`, `APP_HOST`, web port `28097`, RTMP port
   `21936`, password and existing data paths; do not create a replacement app.
2. Build and test both images for Linux AMD64. The app uses Debian/Python 3.12
   and Node 24; the bridge retains its pinned official Codex 0.157.1 dependency
   and binary hashes. Verify STT wheels, cold model download, cache reuse, private
   token provisioning/reuse and container isolation. Do not infer ARM64 or GPU support.
3. Require CI and the bridge's authenticated private `GET /ready` generation
   gate to pass, not just `/health`. Idle readiness must report verified binary,
   enabled generation and zero active account sessions after the synthetic probe.
   Publish the exact tested images as `ghcr.io/9vibes/evencomms:0.4.0` and
   `ghcr.io/9vibes/evencomms:0.4.0-codex` in the same existing public package.
   Confirm anonymous pulls of both digests and record the actual release run.
   The regular CI workflow does not publish.
4. Only after verification, update the canonical KNS-Umbrel package: pin both
   application/init references to `ghcr.io/9vibes/evencomms:0.4.0@sha256:<verified-app-digest>`
   and the bridge to `ghcr.io/9vibes/evencomms:0.4.0-codex@sha256:<verified-codex-digest>`.
   Do not use `pull_policy: never` there. Keep these source templates local-only;
   never invent digests or promote local tags before publication.
5. Verify upgrades from published prior images, including 0.3.0, then test
   a clean pull/install and an in-place Umbrel update with existing data. Submit
   the canonical store update separately and record actual results, not assumed
   hardware or live-provider success.

`/health` confirms only HTTP liveness, not models, Ollama, authentication,
storage health, or readiness for a live conversation. The bridge's private
authenticated `/ready` reports its safety gate, not live ChatGPT entitlement or
provider availability. A staging manifest and static YAML checks establish
neither successful CI/publication nor an actual Umbrel installation. Physical
hardware, real-account login/inference and the intended TLS path need separate
acceptance evidence.
