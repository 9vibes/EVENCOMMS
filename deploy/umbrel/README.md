# Umbrel Staging Package

This is a **local staging package, not a published store release**. Neither a
local-only image reference nor these templates should be installed from a store.
The installable, digest-pinned package lives in
[KNS-Umbrel](https://github.com/9vibes/KNS-Umbrel/tree/master/kunas-evencomms).
The app ID remains `kunas-evencomms`; web port `28097` and RTMP port `21936` are
unchanged. Consult the GitHub release page and canonical store package for
published references. These templates are not evidence of an actual installation.

**Version 0.3.0 requires deployment of all services.** It adds OpenAI Research,
selected still-frame captures, 1x to 4x digital zoom/pan, and LL-HLS with 200 ms
parts and a one-second browser live-sync target. The `0.1.0` image lacks streaming;
`0.2.1` uses ordinary fMP4 HLS. The `evencomms:0.3.0` tag here is a local build, not the
published GHCR reference. Use the KNS-Umbrel package for a normal installation;
these development files do not update the live store automatically.

## Stage Locally

1. From the EVENCOMMS repository root, run `docker build -t evencomms:0.3.0 .`
   on the target host. The frontend package and lockfile must be present.
2. If building elsewhere, use `docker save evencomms:0.3.0` and `docker load`
   to transfer the image to the Umbrel Docker daemon. Build for the target
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
   disabled by default here. Research also requires your own OpenAI API key;
   prefer the HTTPS in-app sign-in connection or a private server environment.
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
application network; `server` and `mediamtx` join only private. This bridge is
not `internal: true`, so published RTMP and backend model downloads can work.
Backend `8000`, HLS `8888` and media API `9997` are not published. Both media and
web wait for the image's backend liveness check, without a dependency cycle.
MediaMTX's minimal image has no shell-based runtime health check. Web uses the
official Alpine image's BusyBox `wget` against `/health`. All services have
bounded JSON-file logs; nginx access logging is disabled.

The application process runs as UID/GID `10001:10001`. A short-lived, networkless
root `data_init` service initializes `/data`, `/data/models`, and existing
SQLite database/WAL/SHM files, and installs the two managed files into `/config`.
Symlinks and hard-linked database/config files are rejected;
ownership is not changed recursively. Restored model-cache files must retain
UID/GID `10001:10001`. Use a dedicated app directory. The database and model cache
persist under `${APP_DATA_DIR}/data`; the root filesystem is read-only.
Update in place, never uninstall to upgrade. Back up and preserve the entire data
directory, including SQLite sidecars, conversations, pairings, model cache and
persistent stream secrets, along with the existing Umbrel application password.
Only bundled managed proxy/media configs are replaced. Operator sessions and
UI-entered Research keys are intentionally lost on restart; Research history is
browser RAM only, clears on reload/sign-out/New chat and is not restored from backups.

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
phone conversations. No Ollama service or cloud provider is installed here.
Set `OLLAMA_URL` to a reachable local service address on the appropriate Docker
network or LAN; `127.0.0.1` inside this container is not the Umbrel host. Restrict
Ollama with network/firewall rules since its API is normally unauthenticated.

## Research Settings

Research is optional and never automatically replies to the wearer. Connect your
own key in the HTTPS UI for the current login, or provide `OPENAI_API_KEY` through
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

Provider tests use MockTransport and browser stubs, not live OpenAI. Real LL-HLS
smoke tests use synthetic media; native Safari, physical glasses/phones and the
actual Umbrel host still need acceptance testing over the intended TLS path.

## Release Promotion

1. Review frontend/hardware acceptance, security and canonical store metadata.
   Preserve app ID `kunas-evencomms`, `APP_HOST`, web port `28097`, RTMP port
   `21936`, password and existing data paths; do not create a replacement app.
2. Build and test the image for each advertised platform with Debian/Python 3.12
   and Node 24. Verify STT wheels, a cold model download, cache reuse, volume
   permissions and phone/TLS operation. Do not advertise untested GPU support.
3. For a new version, publish the release image to a
   registry you control, confirm it is publicly pullable, and record its immutable
   digest. The release workflow publishes
   tested amd64 images; the regular CI workflow does not publish.
4. In the canonical KNS-Umbrel release package, pin **both** application/init
   image references to `ghcr.io/9vibes/evencomms:0.3.0@sha256:<verified-digest>`
   and do not use `pull_policy: never`. Keep these EVENCOMMS templates local-only;
   do not promote a local tag or invent a digest before publication.
5. Verify the release workflow's separate 0.1.0 and 0.2.1 upgrade checks, then test
   a clean pull/install and an in-place Umbrel update with existing data. Submit
   the canonical store update separately and record actual results, not assumed
   hardware or live-provider success.

`/health` confirms only HTTP liveness, not models, Ollama, authentication,
storage health, or readiness for a live conversation. A staging manifest and
static YAML checks are not proof of a successful Umbrel installation.
