# Umbrel Staging Package

This is a **local staging package, not a published store release**. Neither a
local-only image reference nor these templates should be installed from a store.
The installable, digest-pinned package lives in
[KNS-Umbrel](https://github.com/9vibes/KNS-Umbrel/tree/master/kunas-evencomms).
The app ID is `kunas-evencomms`. Store metadata/assets must be completed during
promotion.

**STREAM is unreleased and requires rebuilding/redeploying all services.** The
released `0.1.0` image lacks streaming. The unchanged `evencomms:0.1.0` tag here
means a fresh local build of current source, not the published GHCR image.
Do not modify the published KNS-Umbrel package for this staging deployment;
promotion requires a future tagged release and separate review.

## Stage Locally

1. From the EVENCOMMS repository root, run `docker build -t evencomms:0.1.0 .`
   on the target host. The frontend package and lockfile must be present.
2. If building elsewhere, use `docker save evencomms:0.1.0` and `docker load`
   to transfer the image to the Umbrel Docker daemon. Build for the target
   architecture; a tag alone does not make an image multi-architecture.
3. Use these files only in a separate local/test Umbrel app staging area under
   app ID `kunas-evencomms`, following that Umbrel version's development workflow.
   Do not copy this unpublishable package into the live KNS store.
4. Synchronize **both** config templates into `${APP_DATA_DIR}/config` before
   starting or redeploying. Use the staging launcher's exported `APP_DATA_DIR`:

   ```sh
   mkdir -p "${APP_DATA_DIR:?}/config"
   cp deploy/umbrel/nginx.conf.template "${APP_DATA_DIR}/config/nginx.conf"
   cp deploy/umbrel/mediamtx.yml.template "${APP_DATA_DIR}/config/mediamtx.yml"
   chmod 755 "${APP_DATA_DIR}/config"
   chmod 644 "${APP_DATA_DIR}/config/nginx.conf" "${APP_DATA_DIR}/config/mediamtx.yml"
   ```

   These templates are literal configuration, contain no secrets, and match
   `infra/`. Do not run unrestricted `envsubst`: nginx's `$http_host` and upgrade
   variables must survive unchanged. If the platform renders templates, verify
   these exact destination files exist and match before deployment; absent bind
   sources can become directories. Repeat synchronization after template changes.
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
   Configure an existing local `OLLAMA_URL` if wanted; AI is disabled by default
   here. The backend settings in the root README apply to this package too.
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
root `data_init` service initializes only `/data`, `/data/models`, and existing
SQLite database/WAL/SHM files. Symlinks and hard-linked database files are rejected;
ownership is not changed recursively. Restored model-cache files must retain
UID/GID `10001:10001`. Use a dedicated app directory. The database and model cache
persist under `${APP_DATA_DIR}/data`; the root filesystem is read-only.

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

## Release Promotion

1. Finish frontend/hardware acceptance, security review, store metadata (real
   repository/support URLs, icon/screenshots and required gallery fields),
   and choose the final store-prefixed app ID. Update `id` and `APP_HOST` together.
2. Build and test the image for each advertised platform with Debian/Python 3.12
   and Node 24. Verify STT wheels, a cold model download, cache reuse, volume
   permissions and phone/TLS operation. Do not advertise untested GPU support.
3. For a future version (not released `0.1.0`), publish the release image to a
   registry you control, confirm it is publicly pullable, and record its immutable
   digest. The release workflow publishes
   tested amd64 images; the regular CI workflow does not publish.
4. Replace **both** `evencomms:0.1.0` image references with the published
   `registry/owner/image:<new-version>@sha256:<verified-digest>` and remove `pull_policy:
   never`. Do not promote a local-only tag or a made-up GHCR reference.
5. Test a clean pull/install and an upgrade with existing data, then submit the
   completed package to the intended store in a separate reviewed change.

`/health` confirms only HTTP liveness, not models, Ollama, authentication,
storage health, or readiness for a live conversation. A staging manifest and
static YAML checks are not proof of a successful Umbrel installation.
