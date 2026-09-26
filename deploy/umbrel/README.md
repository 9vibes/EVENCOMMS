# Umbrel Staging Package

This is a **local staging package, not a published store release**. Neither a
GHCR image nor a store listing is claimed. It does not modify `KNS-Umbrel`.
The app ID is `kunas-evencomms`. Store metadata/assets must be completed during
promotion.

## Stage Locally

1. From the EVENCOMMS repository root, run `docker build -t evencomms:0.1.0 .`
   on the target host. The frontend package and lockfile must be present.
2. If building elsewhere, use `docker save evencomms:0.1.0` and `docker load`
   to transfer the image to the Umbrel Docker daemon. Build for the target
   architecture; a tag alone does not make an image multi-architecture.
3. Use these files only in a separate local/test Umbrel app staging area under
   app ID `kunas-evencomms`, following that Umbrel version's development workflow.
   Do not copy this unpublishable package into the live KNS store.
4. Supply the environment through the staging launcher. Umbrel must supply
   `APP_DATA_DIR` and a nonempty `APP_PASSWORD`. Set `ALLOWED_ORIGINS` explicitly
   for every browser/phone origin, including the external HTTPS origin.
   Configure an existing local `OLLAMA_URL` if wanted; AI is disabled by default
   here. The backend settings in the root README apply to this package too.
5. Verify startup, authentication, pairing, socket reconnect, deletion and a
   real transcription on the intended hardware before calling it deployable.

The compose file is an **Umbrel overlay**, not a standalone compose project:
Umbrel supplies `app_proxy`'s image/network and exposes manifest port `28097`.
Use the root `compose.yml` for standalone deployment. The proxy target follows
Umbrel's `kunas-evencomms_server_1` service naming. Check the generated container name
in your Umbrel version and update the target if its naming differs.

The application process runs as UID/GID `10001:10001`. A short-lived root
`data_init` service creates and fixes ownership of the app's `data` bind mount
before startup, including restored files. It has no network and is not the
application server. Use a dedicated app data directory, never a shared host
directory: its ownership is changed recursively. The database and model cache
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
3. Publish the release image to a registry you control, confirm it is publicly
   pullable, and record its immutable multi-platform digest. The included CI
   deliberately does not publish images.
4. Replace **both** `evencomms:0.1.0` image references with the published
   `registry/owner/image:0.1.0@sha256:<verified-digest>` and remove `pull_policy:
   never`. Do not promote a local-only tag or a made-up GHCR reference.
5. Test a clean pull/install and an upgrade with existing data, then submit the
   completed package to the intended store in a separate reviewed change.

`/health` confirms only HTTP liveness, not models, Ollama, authentication,
storage health, or readiness for a live conversation. A staging manifest and
static YAML checks are not proof of a successful Umbrel installation.
