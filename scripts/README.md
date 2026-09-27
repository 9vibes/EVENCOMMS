# Container and Release Checks

`container_smoke.py` requires Python 3.11+ and a running Docker daemon. It uses
only the Python standard library on the host; WebSocket checks run with the
image's installed dependencies. It never pulls or rebuilds the supplied image.

```sh
python scripts/container_smoke.py --image evencomms:0.4.2
python scripts/container_smoke.py --image evencomms:0.4.2 --speech-pcm /tmp/speech.pcm
```

Add `--upgrade-from <local-prior-image>` to seed the data/model cache with an
already-pulled prior image before starting the candidate image on the same
volumes. The 0.3.0 release used separate digest-pinned 0.1.0 and 0.2.1 upgrade
checks as well as a fresh candidate. The 0.4.1 workflow retained those gates and the
0.3.0 upgrade, and added the published 0.4.0 app pinned to
`ghcr.io/9vibes/evencomms:0.4.0@sha256:0b22e2b2d2967e55f244985ebc16cdac3426c852527f83398dce7b639b5e6d15`.
The 0.4.2 workflow retains all of those gates and adds the published 0.4.1 app:
`ghcr.io/9vibes/evencomms:0.4.1@sha256:bb72aa606eaa2dd217dfd80076c121449b5d05bb897996aa540d4cd346f941ab`.
Both the 0.4.0 and 0.4.1 gates use `--upgrade-private-auth`: each first runs that image's
private-auth initializer on the same volumes, then the candidate's `INIT_CHECK`
hashes the existing token internally and requires unchanged bytes and correct
permissions across repeated initialization. The old app boots after candidate
initialization, then the new app boots with its saved data. No prior bridge is
started; this is old-to-new initializer/token preservation plus app/data upgrade,
not a live-account migration or a full old-stack Umbrel installation test.
Do not use that flag with 0.1.0/0.2.1/0.3.0: their legacy upgrade paths are unchanged
and do not require a private-auth-capable initializer.
The candidate's initializer is tested on both fresh and existing volumes,
including literal managed-config installation and replacement of stale configs.
Existing SQLite permissions are deliberately tightened to `0600`/UID 10001;
message contents and model-cache contents/metadata must survive unchanged.

Without `--speech-pcm`, inference is disabled for quick local checks. With it,
the fixture must be raw, mono, 16 kHz, signed 16-bit little-endian PCM, at most
15 seconds, saying "north entrance". The release workflow generates this with
espeak and ffmpeg. Transcription must recognize at least one of those words,
ignoring punctuation and case; it is never mocked.

The script creates dedicated data, managed-config and private-auth Docker volumes.
It runs `python -m backend.init_data --config-dir /config --codex-auth-dir /codex-auth`
as offline root from the same image, checks token preservation and permissions
across repeated initialization, and confirms UID 101 cannot read the token.
It then boots the default
UID/GID 10001 application with a read-only root filesystem and dropped
capabilities. It checks static pages, bearer authentication, pairing, messages,
authenticated WebSocket readiness/ping with same-host and explicitly allowed
rewritten-Host origins, and persistence after removing and recreating the
container. This simulates proxy headers; it is not an end-to-end proxy test.
It cleans up its own containers and volumes even on failure, and does not print
credentials or container logs.

The inference mode checks the default `base.en` model with a cold download,
then checks that model files survive unchanged and a second transcription
succeeds in the replacement container. It requires internet access for the
download and does **not** claim offline operation. Each HTTP request has a
150-second timeout; the inference service has a 140-second timeout in this
test. Model hosting/network failures fail the release rather than bypass STT.

## Real RTMP/HLS Smoke

`stream_smoke.py` tests an **already running, isolated** 0.4.2 stack:
backend + public nginx ingress + MediaMTX. It never starts/rebuilds containers,
changes settings, connects to a default preview URL, or stops an existing encoder.
See [streaming deployment and API contracts](../docs/streaming.md). Use nginx's
public HTTP origin, not the backend or private MediaMTX API/HLS ports.

Requirements: Python 3.11+, FFmpeg on PATH with lavfi, libx264 and AAC, and
`ADMIN_PASSWORD` already exported securely for that isolated stack. There is no
password CLI flag or built-in password. `--base-url` is mandatory; RTMP defaults
to host `127.0.0.1`, port `21936`. Override both destinations for a separate fixture:

```sh
python scripts/stream_smoke.py --base-url http://127.0.0.1:28097 --rtmp-host 127.0.0.1 --rtmp-port 21936
python scripts/stream_smoke.py --base-url http://127.0.0.1:28197 --rtmp-host 127.0.0.1 --rtmp-port 21937 --browser
python scripts/stream_smoke.py --base-url http://127.0.0.1:28197 --rtmp-port 21937 --browser --research
```

The runner refuses if publisher status is already online, streaming is disabled,
or the media service is unavailable. The target must retain MediaMTX's
`overridePublisher: no` to protect against an encoder connecting after preflight.
Use `STREAM_ENABLED=true` (the Compose default), and match `COOKIE_SECURE` to the
HTTP/HTTPS origin. The encoder destination comes from CLI flags plus the **actual
API stream key**, not the advertised `PUBLIC_HOST` setting.

Checks include anonymous/invalid-Bearer/cookie-only control denial, bearer-only
HLS denial, all-method public `/internal` denial, cookie scope/lifetime/rotation,
cross-origin cookie-only authorization denial and disallowed CORS preflight,
wrong-key RTMP failure within ten seconds, and real H.264/AAC ingest. Synthetic
video is a 640x360, 25 fps test pattern, with a 440 Hz/48 kHz sine wave, baseline
H.264 and one-second keyframes. No camera, microphone, speech model, wearer,
conversation, media recording or on-disk HLS fixture is used.

Publisher readiness and master/child/init/media HLS fetches each have a 30-second
deadline. The HLS test requires a low-latency playlist, fetches authenticated MP4
parts, exercises `_HLS_msn`/`_HLS_part` blocking reloads, and checks that both reject
missing playback cookies. It also checks fMP4 bytes, content types and lengths. A Range probe
validates partial bytes/headers when supported; a consistent full-file 200 from
an upstream without advertised range support is accepted and reported. Logout
must revoke both the operator token and its playback cookie without stopping the
publisher. Cleanup terminates only FFmpeg processes created by this invocation,
confirms offline with a separate operator session, and attempts to revoke all
test logins even on failure. FFmpeg has a 120-second media limit and a separate
125-second wall-clock kill guard (240/245 seconds with `--research`). Python uses
two logins; `--browser` adds one and `--research` adds one more, totaling four,
below the five-per-minute limit on a fresh isolated stack. Repeated runs can hit
that limit; wait a minute rather than weakening authentication.

For `--browser`, install the existing frontend dependencies and Playwright
Chromium (`npm ci` and `npx playwright install --with-deps chromium` from
`frontend`). To use an installed browser instead, export
`CHROMIUM_PATH=/usr/bin/chromium`; otherwise Playwright uses its managed browser,
including in CI. The Python runner passes `STREAM_TEST_URL` and `ADMIN_PASSWORD`
through the child environment. For a feed you have already started on your own
isolated fixture, the browser-only command is:

```sh
STREAM_TEST_URL=http://127.0.0.1:28197 CHROMIUM_PATH=/usr/bin/chromium node frontend/scripts/check-stream.mjs
```

The browser logs in through the UI, opens STREAM, requires decoded video and
three seconds of advancing playback plus the playing caption, checks full-width
16:9 layout without overflow at 1440/390/320 px, verifies manual pause/resume,
removes the real playback cookie
and requires real HTTP authorization recovery, then verifies unmount/request
cleanup and playing video after returning. It never mocks HTTP or reveals encoder
settings. It does not pair a wearer or inspect/change an operator draft; draft
retention is covered separately by the existing isolated application E2E test.
Its context/browser close and logout is attempted on failure too. Native Safari
HLS, physical devices, audible output and scheduled five-minute cookie renewal
remain manual checks; this test covers immediate missing-cookie recovery and API
cookie rotation, not passage of the full expiration interval.
Deployed browser access requires TLS; the localhost HTTP fixture is not evidence
of native Safari LL-HLS working through an external HTTPS proxy. The recorded
[latency comparison](../docs/streaming.md#synthetic-comparison) measured median
server-timestamp age of 2.048 s for the historical fMP4 profile and 1.226 s for the
selected LL-HLS profile, not glass-to-glass delay or a release-wide guarantee.

The real browser check also exercises zoom limits/reset, keyboard/mouse/touch
panning, resize clamping, zoomed playback/audio controls and supported container
fullscreen. It verifies that zoom preserves the same video element and media
source. Touch input is emulated in Chromium; native Safari/fullscreen still needs
device validation. The pure zoom-boundary tests cover portrait and ultrawide feeds.

No artifacts are written by default. Optionally set `STREAM_TEST_SCREENSHOT` to
a PNG path whose parent directory **already exists** (prepared by your runner).
Only the STREAM panel is captured, with encoder credentials still hidden. Treat
even synthetic test artifacts as private. Scripts suppress raw subprocess/HTTP/
Playwright failures and retain only a bounded private FFmpeg error buffer; they
never print keys, tokens, passwords or the publisher command. The RTMP key is
necessarily in FFmpeg's process arguments and travels over plaintext RTMP: use a
trusted machine/network, avoid process listings/debug traces and disable/redact
external service access logs. CI runs this test after the existing Compose health
checks, using its already-installed browser and a separately installed FFmpeg.

Umbrel candidate checks must start the development overlay with separate
data/config and private `codex-auth` bind mounts. The real initializer installs
the configs and provisions the service token once; only backend and bridge
receive the token directory read-only. Checks must cover unchanged token reuse,
permissions and exclusion from nginx/MediaMTX, then repeat real RTMP/browser checks.
`deploy/umbrel/ci.override.yml` exposes web on loopback for this test. The actual
Umbrel-supplied app proxy is not started or tested by that substitute.

### Research Real-Frame Browser Smoke

`--research` requires `--browser` and runs `frontend/scripts/check-research.mjs`
**after** the existing stream browser check, while the same owned FFmpeg publisher
is active. Research has an 80-second internal deadline and a 90-second child wait
timeout; the ordinary stream-only deadlines remain unchanged. CI enables both
checks on standalone and managed-config stacks with separate databases/login
budgets. No production-code modifications, provider credentials or provider
network calls are needed. Backend provider tests remain separate, using HTTPX
MockTransport.

For an already publishing private 640x360 fixture, with `ADMIN_PASSWORD` securely
exported, run directly:

```sh
STREAM_TEST_URL=http://127.0.0.1:28197 CHROMIUM_PATH=/usr/bin/chromium node frontend/scripts/check-research.mjs
```

Both environment variables `STREAM_TEST_URL` and `ADMIN_PASSWORD` are required;
there is no default origin/password. Omit `CHROMIUM_PATH` to use Playwright's
installed Chromium. Never point this test at a public preview or a live deployment.
Only `/api/research/**` responses are stubbed inside a fresh private Playwright
context: initially disconnected status, a clearly fake sign-in key, two explicitly
synthetic model IDs, and deterministic plaintext replies. Unknown Research routes
and unexpected external browser HTTP/WebSocket traffic are denied, never forwarded
to a provider. Authentication, stream status, playback sessions and HLS remain
real. Other API mutations are denied except own login/logout/playback-session;
the test does not pair a wearer, send wearer messages or change server settings.

Checks cover decoded/advancing 640x360 video, native canvas JPEGs (at most 1280px
per side and 1 MiB decoded bytes), full-frame then 2x cropped captures, draft
thumbnails/timestamps, the three-pending limit and discard preserving other frames.
Capture before connecting a key must stay local. Captures must not change the
video element/source or interrupt playback. Prompt/attachments survive
RESEARCH -> OPERATOR -> RESEARCH, allowing a new player source after remount.
The draft layout is checked at 390px and 320px. Explicit model selection and Send
are required; wire images must be JPEG strings without frame metadata or API keys.
Follow-ups retain prior images/replies within the six-image and 20-message budgets;
the UI must refuse an over-budget turn rather than silently shorten history.
HTML-like synthetic replies render as text. A one-shot delayed **native** `toBlob`
callback tests New chat during capture without replacing pixels or JPEG encoding.
The late old frame must not reappear, and a new capture must still work.
Send must remain disabled while asynchronous capture prepares its thumbnail.
Capture uses native video pixels at the current zoom/pan, excluding UI/native
controls; the player also permits a decoded paused frame (`readyState >= 2`),
which should be checked on target devices. Unit tests cover capture geometry;
the browser smoke exercises delayed encoding and New chat generation guards.

The fake key and JPEGs are checked against local/session storage; the input must
clear after connection. Own logout/token revocation is verified and cleanup is
attempted even on failure. No recording, HAR, trace or screenshot is written by
default. Set `RESEARCH_TEST_SCREENSHOT` to a private PNG path only if its parent
already exists. Only the Research panel is captured, after the key field is blank;
compact preview has no encoder configuration and must never fetch stream settings.
Artifacts contain synthetic video/chat only. Failure output is a safe stage label,
never raw exceptions, request bodies, keys or images. This smoke does not validate
real OpenAI model compatibility, billing, provider retention, native Safari or
physical mobile devices; provider calls are deliberately never made.

### Codex Runtime Probe

The [experimental Codex connection](../docs/codex.md) uses the official pinned
Codex runtime, separately from the API Research smoke above. Its tests must not
use a developer's login cache or real OpenAI credentials. Install the bridge's
locked npm assets and run the real-binary probe from the repository root:

```sh
npm ci --prefix codex_bridge --ignore-scripts --no-audit --no-fund
python scripts/check_codex_runtime.py --binary codex_bridge/node_modules/@openai/codex-linux-x64/vendor/x86_64-unknown-linux-musl/bin/codex --temp-parent /tmp
PATH="$PWD/codex_bridge/node_modules/@openai/codex-linux-x64/vendor/x86_64-unknown-linux-musl/bin:$PATH" python -m pytest backend/tests/test_codex_bridge.py -k offline_pinned_binary_device_login_uses_production_session
PATH="$PWD/codex_bridge/node_modules/@openai/codex-linux-x64/vendor/x86_64-unknown-linux-musl/bin:$PATH" python -m pytest backend/tests/test_codex_native_chat.py
```

The first pytest command exercises the production `Session` device-login path with the
real native binary and an offline nine-digit code fixture, not a mocked runtime
or real OpenAI account. The second runs all 15 full native-chat pipeline cases
through the real backend, bridge, production Session/Generation, pinned binary
and real relay. It covers both models, JPEG history, scoped metadata/reasoning/text
deltas, safe quota/model/stream/tool diagnostics, and native workspace discovery
before forwarding, including routing changes after credential refresh. Native
runtime version metadata and the upstream `version` header remain `0.157.1`,
not the application's `0.4.2` client metadata version.
These tests require the pinned binary on `PATH`; otherwise the normal backend
suite skips 16 cases (one login plus 15 native chat), not just the login test.
The dedicated Codex CI job installs the locked assets and explicitly sets `PATH`
before running both commands, so all 16 must pass rather than skip. Account access
and the cause of a historical generic failure on a user's host are not established.

The probe uses loopback OAuth and Responses fixtures. It checks digest/schema,
ephemeral synthetic login, both pinned model selectors, zero tool registration,
exact role-labelled text/image replay, unexpected tool-call rejection, bounded
401 recovery, an enforced one-response inference budget, no API fallback and
process cleanup. The tool-case fixture deliberately delays notification delivery
and verifies that a second local request is blocked before reaching the upstream
fixture. `no_retries: false` is expected:
the runtime may retry while recovering OAuth authentication. Exit zero requires
all positive safety proofs, not a claim that real-account access is verified.
The isolated service repeats this probe before enabling generation at startup.
Never bypass a failed proof by changing the protocol policy or enabling tools.

In the Umbrel source package, the isolated service starts idle by default;
standalone still requires the explicit `compose.codex.yml` overlay. Startup
launches synthetic loopback probe processes, not a real account login/inference.
No account use is permitted before Research provider choice, device sign-in,
explicit model choice and Send. API mode remains the UI default. Neither normal
server startup nor wearer/stream/API features depend on bridge health.

Container verification must check the **running service's** authenticated private
`GET /ready`, not just `/health` or an earlier probe subprocess result. Assert
verified binary, enabled generation and zero active account sessions after the
startup probe. Readiness must not expose credentials or initiate account use.
The separate 10-second binary verification and 75-second generation-proof
deadlines fit within the 90-second health start period. Generation-proof failure
or timeout preserves independently verified device-login readiness, but Send
stays disabled without the complete proof.
Verify UID/GID `10002:10002`, read-only root, 256 MiB noexec/nosuid/nodev tmpfs,
1 GiB memory and total memory-plus-swap, one CPU, 128 PIDs, no capabilities or
privilege escalation, disabled core dumps, rotated logs, no published ports and
the private-link/separate-egress networks. A failed gate must disable Codex
generation, not trigger a provider/model/API fallback or prevent the ordinary app
from starting.

Umbrel's initializer uses `--codex-auth-dir /codex-auth` alongside
`--config-dir /config`. Check a persistent random 64-hex `token`, directory/file ownership
`10001:10002`, modes `0750`/`0440`, and read-only `/run/codex-auth` mounts with
`CODEX_BRIDGE_TOKEN_FILE` in backend and bridge. Neither bridge nor init receives
`APP_PASSWORD`. Do not expose the token through shared `/config`, app data, the
UI or logs. Standalone's private `.env` token remains a separate opt-in path;
conflicting `CODEX_BRIDGE_TOKEN` and `CODEX_BRIDGE_TOKEN_FILE` sources must fail.
Never print expanded Compose configuration while checking these contracts.

### Paired-Image Smoke

`codex_container_smoke.py` requires a Docker daemon and already-built Linux AMD64
app/bridge images. It checks their exact image IDs, users and OCI source/revision/
version labels before starting a private test stack. Build with the labels in
`release.yml`; ordinary unlabelled local builds are not sufficient for this check.
With `APP_IMAGE_ID`, `CODEX_IMAGE_ID` and `REVISION` set to those tested build
identities and their source revision:

```sh
python scripts/codex_container_smoke.py \
  --image "$APP_IMAGE_ID" --codex-image "$CODEX_IMAGE_ID" \
  --version 0.4.2 --revision "$REVISION" \
  --source https://github.com/9vibes/EVENCOMMS --browser
```

The runner never rebuilds or pulls the app/bridge images. It runs the pinned
production probe in a networkless container, tests managed auth and token reuse,
starts the backend without the bridge, then checks the running bridge's actual
`/ready` gate, idle account state and runtime controls. It also checks operator
isolation, UID 101 token denial, and exclusion of the secret from media/proxy
mounts. The runner pulls the pinned nginx/MediaMTX images. `--browser` adds the
real RTMP/HLS and synthetic Research browser smoke, requiring FFmpeg and the
frontend Chromium dependencies described above. It cleans up only its own stack
and prints bounded results, not tokens, expanded Compose or raw container logs.
These are synthetic checks, not live-account login, inference or entitlement tests.

## Publication

The source version is **0.4.2**, a [reply-compatibility and diagnostics hotfix](../docs/codex.md#042-hotfix)
for Codex Research introduced in 0.4.0. It retains the
[0.4.1 device-login fixes](../docs/codex.md#041-hotfix); Codex remains **experimental**.
The [release page](https://github.com/9vibes/EVENCOMMS/releases),
[CI runs](https://github.com/9vibes/EVENCOMMS/actions) and canonical
[KNS-Umbrel package](https://github.com/9vibes/KNS-Umbrel/tree/master/kunas-evencomms)
record actual publication and verification status. Local `evencomms:0.4.2` and
`evencomms-codex:0.4.2` are not registry references. After each release run, record
its actual outcome and both verified digests rather than inferring success from
this checklist. Live OpenAI and physical-device acceptance are not established
by these scripts.

`release.yml` runs on `v*` tags and manual dispatch. It first calls the full CI
workflow, including its existing Compose boot test. The optional manual
version defaults to the source tag or project
version. Versions must match `pyproject.toml`, frontend package/lock/app metadata,
bridge package/lock metadata and client version, and the source Umbrel manifest;
source tags must be exactly `v<version>`. The Codex dependency remains `0.157.1`,
not the application's SemVer. Stable SemVer
and SemVer prereleases are supported, but build metadata is rejected because
Docker tags cannot contain `+`. No source tags are created or changed.

The 0.4.2 release workflow builds **linux/amd64 only** on Ubuntu 24.04, labels both
images with source, revision and version, smoke-tests the app's exact local image
ID with real CPU STT, and verifies the isolated bridge's actual generation gate.
Only the exact tested images may be published using `GITHUB_TOKEN` with
`packages: write`. Both tags belong to the **same existing public GHCR package**:

- App/init: `ghcr.io/9vibes/evencomms:0.4.2` (`:<version>`).
- Bridge: `ghcr.io/9vibes/evencomms:0.4.2-codex` (`:<version>-codex`).

Do not create a separate registry package for the bridge. There are no `latest`,
moving minor-version or ARM tags. Official Codex `0.157.1` dependencies and binary
hashes are unchanged by this app version bump.
Do not retag or replace any prior release, including either published 0.4.0 or 0.4.1 image.

Publication is serialized. An authenticated registry check rejects existing
tags for both images and fails closed on authentication, network, and unexpected registry
errors. It permits only a confirmed missing manifest/repository. This prevents
replacement by this workflow, not by external writers: GHCR's tag API does not
provide an atomic create-only push. Restrict other package writers accordingly.
To retry a successfully published version, do not delete/reassign its tag;
publish a new version instead. A manual run publishes its selected source ref,
so select the intended release tag (or reviewed commit) deliberately.

Release artifacts and the job summary must record **both immutable repository
digests** and the actual verification results. The `image-reference` artifact
contains `image-reference.txt` (app), `codex-image-reference.txt` (bridge) and
`release-images.json` with version, revision, platform, image IDs and digests.
Check that all three files are present and the run succeeded; partial publication
is not a successful paired release. Verify anonymous pulls of both
digests from the existing public package before changing the canonical KNS-Umbrel
package. Pin app/init to the verified app digest and the bridge to its own digest;
preserve app ID, password, ports, settings and data paths. Keep the source staging
templates on their local build tags. Never invent a digest or infer successful
publication from a tag name. The token needs permission to publish into the
`9vibes` namespace; organization/package policy can deny that even with
`packages: write`. Source/docs preparation alone does not publish or update a store.
