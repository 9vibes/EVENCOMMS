# Experimental Codex Research (0.4.1)

EVENCOMMS `0.4.0` introduced **ChatGPT account (Experimental Codex)** alongside the
existing **OpenAI API key** connection in Research. Version `0.4.1` fixes device
login; the feature remains **experimental**. Check the
[release page](https://github.com/9vibes/EVENCOMMS/releases) and canonical
[KNS-Umbrel package](https://github.com/9vibes/KNS-Umbrel/tree/master/kunas-evencomms)
for actual CI results, published digests and installation instructions. Source
metadata is not evidence that publication or a live-account test succeeded.

The Umbrel source package installs an isolated bridge running idle by default.
Startup **does run synthetic loopback probe processes**, but never signs in to a
real account or makes real inference requests. Account use requires the operator
to choose Codex in Research, complete device login, select a model and Send.
Standalone `compose.yml` still has no Codex service without `compose.codex.yml`.
API mode remains the UI default, and existing optional API-key settings are
preserved. Wearer conversations, transcription, Ollama and streaming keep their
existing paths and do not depend on bridge health.

OpenAI documents account authentication and custom clients through
[Codex app-server](https://developers.openai.com/codex/app-server). This integration
uses the official pinned `0.157.1` runtime over private stdio, with EVENCOMMS's own
client identity. You sign in on OpenAI's site using a device code; EVENCOMMS never
asks you to paste an account password, browser cookie or OAuth token. It is not
a general-purpose ChatGPT web API, a cookie scraper or unlimited API access.

## 0.4.1 Hotfix

- Fixes the disabled device-login button on Umbrel's default HTTP console.
  **Get Codex login code** requires explicit trusted-network confirmation on
  non-loopback HTTP; this exception applies only to Codex. API-key entry still
  requires HTTPS or exact localhost/loopback. Console traffic remains unencrypted;
  OpenAI account sign-in must always use its HTTPS device page.
- Displays the actual OpenAI-issued code unchanged, including nine-digit numeric
  codes and other supported formats. EVENCOMMS never generates a substitute code
  or imposes a nine-digit-only rule.
- Independently verifies the pinned binary off the event loop within 10 seconds,
  then bounds the generation proof at 75 seconds. A generation-proof timeout or
  failure preserves verified login readiness, never bypasses generation safety.
  Heartbeat acknowledgements overlapping login no longer cancel an unacknowledged
  session; uncertain creation keeps its cleanup obligation rather than orphaning it.

This hotfix adds no directories, networks, ports or resource-limit changes.
The existing Umbrel bridge stays installed and idle until explicit account use;
provider choice, device login, model selection and Send remain opt-in. Official
Codex `0.157.1` and its binary hashes are unchanged. Update both app and bridge
images in place, preserving the existing private `codex-auth/token` and app data.

## Cost And Availability

- Requires a ChatGPT plan/workspace eligible for Codex device login and the chosen
  model. ChatGPT and OpenAI API billing remain separate.
- Requests consume the account's Codex allowance. Plan limits, purchased credits
  and workspace policies apply; this is not necessarily free or unlimited.
- The pinned selectors are `gpt-6-luna` and `gpt-6-astra`, both with image inputs.
  Luna is described by the upstream catalog as fast and affordable for easier
  tasks; actual allowance consumption depends on the account and request.
- These are restricted runtime selectors, **not a live account entitlement
  list**. An unavailable model fails rather than being substituted. The user must
  explicitly select a model; the app never chooses a more expensive one.
- Codex mode never uses `OPENAI_API_KEY`, even if a server key is configured. It
  never switches to paid API access after a failure, logout or quota limit.
- EVENCOMMS does not automatically resubmit failed generation. Codex itself can
  recover a rejected OAuth credential, making up to three Responses attempts
  during the pinned 401 recovery sequence. That behavior is tested and disclosed,
  not described as zero retries. Manual retries can consume additional allowance.

A private loopback inference relay enforces the request budget independently of
Codex's agent loop. Each explicit Send receives a fresh, thread-bound path; only
a confirmed 401 can reopen its budget. At most one non-401 request is forwarded,
including when the runtime tries to continue after a tool error. Old paths,
overlapping requests, network uncertainty and extra continuations cannot create
another upstream turn. There is no arbitrary-URL or public proxy endpoint.

Consult OpenAI's [authentication guide](https://developers.openai.com/codex/auth),
[plan and usage information](https://developers.openai.com/codex/pricing), and
[usage dashboard](https://chatgpt.com/codex/settings/usage). Live model access,
tenant-specific routing, actual billing and retention have not been verified.

## Umbrel Deployment

Use the canonical store package for a normal install/update after verified image
promotion. For source staging, follow [the Umbrel guide](../deploy/umbrel/README.md).
Do not apply the standalone overlay to an installed Umbrel app. Update in place,
not by uninstalling; preserve app ID `kunas-evencomms`, web port `28097`, RTMP port
`21936`, password, data, model cache, pairings and stream secrets.

No manual bridge credential setup is needed. The networkless initializer runs
`--config-dir /config --codex-auth-dir /codex-auth` and creates a random
64-character hexadecimal `${APP_DATA_DIR}/codex-auth/token` once. That directory
has ownership `10001:10002` and mode `0750`; the file has the same ownership and
mode `0440`. Init alone has a writable `/codex-auth` mount. Backend and bridge
mount it read-only at `/run/codex-auth` and both use
`CODEX_BRIDGE_TOKEN_FILE=/run/codex-auth/token`. The bridge never receives the
app/operator password; neither does init. The token is a local service credential,
not an OpenAI credential, and never belongs in shared `/config`, `/data`, nginx,
MediaMTX or manifest/UI metadata. Back up this private directory alongside app
data and preserve its ownership/modes on restore. Do not persist account tokens.

Both the ordinary server and bridge depend only on successful `data_init`, not
on bridge health. There is no new enable flag or public port. Prefer manually
configured TLS for private use, with exact `ALLOWED_ORIGINS` and
`COOKIE_SECURE=true`; installing Codex does not set up TLS. Device-code login
can also start from the HTTP Umbrel console after a trusted-network confirmation.
That does not encrypt the console's operator session, code or chat. Do not use
plaintext access on an untrusted/public network. OpenAI account sign-in itself
always takes place on OpenAI's HTTPS site. The bridge's presence is not an account
connection or permission to send data.

## Standalone Opt-In

Use a private Linux AMD64 deployment with Docker Engine and Compose v2. Back up
existing persistent data first. The normal standalone stack does not start Codex;
only this explicit overlay adds it.

1. Configure the ordinary standalone `.env` described in the main README. Generate
   a new private bridge secret, for example with `openssl rand -hex 32`, and set
   `CODEX_BRIDGE_TOKEN` in that ignored `.env`. This is a local service credential,
   not your OpenAI password, API key or operator password. Keep `.env` private;
   do not publish Compose's expanded config.
2. Prefer HTTPS for remote operator access, with explicit `ALLOWED_ORIGINS` and
   `COOKIE_SECURE=true`. On a trusted LAN/VPN, HTTP device-code login is available
   after a warning and confirmation. API-key entry remains disabled there; the
   exception for exact localhost/loopback development is unchanged. Do not set
   `COOKIE_SECURE=true` on a plain HTTP deployment: its playback cookie would not
   be sent. TLS is still recommended to protect all console traffic.
3. From the repository root, build and start the explicitly opted-in stack:

```sh
docker compose -f compose.yml -f compose.codex.yml config --quiet
docker compose -f compose.yml -f compose.codex.yml up -d --build --wait --wait-timeout 150
```

The local source tags are `evencomms:0.4.1` and `evencomms-codex:0.4.1`, not registry
references. The optional override supplies `CODEX_BRIDGE_URL=http://codex-bridge:8001`
only to the application server and shares the bridge token with those two services.
The backend requires a URL and one token source together, and validates an
HTTP(S) origin with no path, user information or query. Tokens must be 32-256
hexadecimal characters. Both services also accept `CODEX_BRIDGE_TOKEN_FILE`
instead of `CODEX_BRIDGE_TOKEN`; setting both sources is rejected. The supplied
standalone overlay deliberately uses the private `.env` token, while Umbrel
uses the automatically provisioned file. Do not provide these settings through
a browser form or a `VITE_` variable. Server startup is not gated on bridge health;
`--wait` can still report an unhealthy bridge without disabling the other services.

## Isolation And Readiness

The bridge runs as UID/GID `10002:10002` with init/reaping, a read-only root
filesystem, 256 MiB `/tmp` tmpfs (`noexec,nosuid,nodev`), 1 GiB memory limit with no
extra swap allowance, one CPU and 128 PID limit. It mounts **no application data,
shared config, host home, account cache or Docker socket** and publishes no ports.
A private internal network links it to the backend; a separate
network permits outgoing authentication/inference traffic. The latter is not an
OpenAI-only network firewall. Umbrel's only bridge bind mount is the read-only
private service-secret directory. Capabilities are all dropped, privilege
escalation and core dumps are disabled, and JSON logs are rotated at 10 MiB with
three files. Do not expose port `8001` or raw app-server RPC.

The image checks the official binary's digest. At every startup a credential-free,
loopback-only probe verifies the schema, synthetic login/refresh, ephemeral
credentials, both model selectors, exact text/image history and an empty tool
registry. Unexpected tool calls and approval requests are rejected. If the proof
fails, generation stays disabled; the UI never falls back to paid API mode.
Pinned-binary verification runs independently with a 10-second deadline, followed
by the generation proof's 75-second deadline. If that longer proof times out or
fails, an independently verified binary can still issue login codes; generation
remains disabled until its complete proof passes. Compose allows a 90-second health
start period. `/health` establishes only liveness. The private `GET /ready`
requires the bridge's service Bearer token and returns `{binary_verified,
generation_enabled, active_sessions}`, never credentials, codes, account details,
prompts or responses. Verification must assert `binary_verified: true`,
`generation_enabled: true` and `active_sessions: 0` after startup, not merely
accept HTTP liveness.
Readiness does not initiate login or inference and does not prove real account
entitlement, live routing or model availability. Never publish expanded Compose
or credentials while checking it.

The bridge is intentionally not embedded in the normal server image. Running it
on a host with application files or mounting a personal Codex configuration would
break the isolation assumptions. Never enable shell, MCP, plugins, browsing,
agents or provider/model fallback to work around a failed probe. Upgrading Codex
requires updating the pin and revalidating the protocol and no-tool policy.

To remove the standalone opt-in service, recreate the server using only the
normal Compose file, then stop/remove the optional service. Do not delete the
application data volume:

```sh
docker compose -f compose.yml up -d --force-recreate server
docker compose -f compose.yml -f compose.codex.yml stop codex-bridge
docker compose -f compose.yml -f compose.codex.yml rm -f codex-bridge
```

## Operator Steps

1. Open Research and select **ChatGPT account (Experimental Codex)**. Confirm clearing
   any current local conversation. If the server has not enabled the bridge, the
   UI reports that and leaves sending disabled.
2. Choose **Get Codex login code**. On HTTP, confirm only if you trust the LAN/VPN.
   Open the displayed `https://auth.openai.com/codex/device` link and enter the
   code exactly as shown. This is the same flow as `codex login --device-auth`.
   OpenAI issues the code; nine-digit numeric codes and other supported code
   formats are preserved, never fabricated or reformatted by EVENCOMMS. A code
   from a separate CLI session authorizes that session, not this app. Enable
   device-code authentication in ChatGPT security settings or ask the workspace
   administrator if OpenAI requires it. The code expires after 15 minutes.
3. Wait for **ChatGPT connected**, then explicitly choose a Codex model. Runtime
   verification failure is shown separately and keeps Send disabled.
4. Write a question and optionally capture/review still frames. Only **Send via
   Codex** transmits the displayed conversation and attached images. Connecting,
   polling and selecting a model never submit a question or frame.
5. Use **Cancel sign-in** or **Disconnect ChatGPT** when finished. Disconnect keeps
   Codex selected with Send disabled, rather than enabling the API connection.

The experimental service permits two signed-in operators at once and at most
eight fresh Research requests per login. After the eighth result it destroys that login's
runtime, bounding retained in-memory threads. Sign in again to continue. A login
also expires after eight hours or the operator's earlier session expiry; pending
device sign-in has a 15-minute deadline. These are application resource limits,
not OpenAI plan limits. Reply waiting is bounded at 85 seconds in the bridge;
the backend's `OPENAI_TIMEOUT` setting also applies. `OPENAI_MAX_OUTPUT_TOKENS`
applies only to API mode, not Codex. Codex output is limited to 16,000 characters;
an oversized/unsafe result fails rather than silently triggering another turn.

## Privacy And Lifetime

ChatGPT account/workspace data controls apply, **not the API `store:false`
contract**. Review those controls for sensitive captures. Ephemeral local state
does not establish zero provider retention or prevent administrator access.

The isolated runtime handles and refreshes its own OAuth credentials in RAM.
Its inference relay also handles authorization/account headers in bridge RAM
while forwarding to the fixed OpenAI Codex endpoint. The main application API and
browser receive only state and a bounded one-time device code, never access/refresh
tokens or account email. The runtime inherits no application password, API key,
bridge token, proxy variables or personal configuration. Credentials and chats
are not stored in the application's SQLite database or application logs.

Local browser history/drafts clear on reload, New chat, sign-out or confirmed
provider change. **Submitted** history and frames also remain in ephemeral bridge
threads until the runtime is destroyed; clearing the browser view does not erase
those threads. Each Send creates a fresh thread from exactly the displayed
history, never hidden prior turns. Bounded RAM caches retain successful results
for retries. The eight-request cap, disconnect and expiry destroy the runtime.

Logout/disconnect immediately discard the backend mapping and late replies, and
request best-effort runtime logout/termination. The backend renews a 120-second
bridge lease every 30 seconds; if it crashes or cleanup cannot reach the bridge,
the bridge expires the lease independently. Scheduler/cleanup time can add a small
delay. Failed cleanup can temporarily reserve one of the two session slots.
Lease acknowledgements list only existing healthy sessions; a failed login's slot
is reclaimed even when its browser is no longer polling. A heartbeat that overlaps
an unacknowledged login cannot cancel it merely because its remote session has
not appeared yet; uncertain creation retains its cleanup obligation. Chat admission is capped
before body receipt at two waiters per operator/four globally, including duplicate
request IDs. Codex and API image validators each allow two workers, so the optional
mode can permit four concurrent validation workers in the main backend.
Process termination loses local credentials but does not guarantee successful
remote token revocation or sign out the user's ChatGPT browser session.

Changing Research provider or closing a tab does not itself revoke the login.
No continuous stream, microphone audio, stream secret or unsent wearer draft is
submitted. Research results never automatically go to the glasses.

## Verification

Run the normal backend, frontend and browser suites. New backend tests cover the
private bridge and operator isolation; browser tests use synthetic Codex responses
with real local operator authentication. The actual pinned runtime probe uses a
loopback synthetic OAuth/provider fixture with no live OpenAI requests:

```sh
npm ci --prefix codex_bridge --ignore-scripts --no-audit --no-fund
python scripts/check_codex_runtime.py --binary codex_bridge/node_modules/@openai/codex-linux-x64/vendor/x86_64-unknown-linux-musl/bin/codex --temp-parent /tmp
PATH="$PWD/codex_bridge/node_modules/@openai/codex-linux-x64/vendor/x86_64-unknown-linux-musl/bin:$PATH" python -m pytest backend/tests/test_codex_bridge.py -k offline_pinned_binary_device_login_uses_production_session
```

Use a Python environment with the project test dependencies. The script prints
only bounded proof results, never real credentials. The production `Session`
test uses the actual pinned native binary and a synthetic nine-digit device-code
fixture, without an account request. It skips in the ordinary backend suite when
the binary is missing from `PATH`; the dedicated Codex CI job installs the locked
binary and puts it on `PATH` so this test runs, not skips.
CI/release verification must build and exercise the bridge under its container
controls without an account,
including authenticated `/ready` assertions on its binary and generation gates
and zero idle sessions. `/health` or an exited probe script alone is insufficient
to establish that the deployed bridge enabled generation.
See [the pinned protocol evidence](../codex_bridge/PROTOCOL.md) for exact recovery
sequences and [the script guide](../scripts/README.md#codex-runtime-probe).

Planned publication uses **the same existing public package** for
`ghcr.io/9vibes/evencomms:0.4.1` (app/init) and
`ghcr.io/9vibes/evencomms:0.4.1-codex` (bridge). Promote the canonical Umbrel package
only after the exact tested images are published, both digests are anonymously
pullable, and the actual release-run results are recorded. No digest or CI
success is implied by these source tags. See [release checks](../scripts/README.md#publication).

Direct binary and synthetic tests do not establish container enforcement or a
successful Umbrel installation. ARM64 assets are pinned but native ARM64
execution is not verified and does not extend the application's AMD64 support.
Physical devices, a real ChatGPT account, live image answers, entitlement/usage
accounting, retention and production TLS/routing still require acceptance testing.
