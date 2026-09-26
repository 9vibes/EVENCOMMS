# Container and Release Checks

`container_smoke.py` requires Python 3.11+ and a running Docker daemon. It uses
only the Python standard library on the host; WebSocket checks run with the
image's installed dependencies. It never pulls or rebuilds the supplied image.

```sh
python scripts/container_smoke.py --image evencomms:0.2.0
python scripts/container_smoke.py --image evencomms:0.2.0 --speech-pcm /tmp/speech.pcm
```

Add `--upgrade-from <local-prior-image>` to seed the data/model cache with an
already-pulled prior image before starting the candidate image on the same
volumes. The 0.2.0 release workflow uses the immutable 0.1.0 image for this check.
The candidate's initializer is tested on both fresh and existing volumes,
including literal managed-config installation and replacement of stale configs.
Existing SQLite permissions are deliberately tightened to `0600`/UID 10001;
message contents and model-cache contents/metadata must survive unchanged.

Without `--speech-pcm`, inference is disabled for quick local checks. With it,
the fixture must be raw, mono, 16 kHz, signed 16-bit little-endian PCM, at most
15 seconds, saying "north entrance". The release workflow generates this with
espeak and ffmpeg. Transcription must recognize at least one of those words,
ignoring punctuation and case; it is never mocked.

The script creates an empty, dedicated Docker volume, prepares it with
`python -m backend.init_data --config-dir /config` as root from the same image,
using a separate managed-config volume, and boots the default
UID/GID 10001 application with a read-only root filesystem and dropped
capabilities. It checks static pages, bearer authentication, pairing, messages,
authenticated WebSocket readiness/ping with same-host and explicitly allowed
rewritten-Host origins, and persistence after removing and recreating the
container. This simulates proxy headers; it is not an end-to-end proxy test.
It cleans up its own containers and volume even on failure, and does not print
credentials or container logs.

The inference mode checks the default `base.en` model with a cold download,
then checks that model files survive unchanged and a second transcription
succeeds in the replacement container. It requires internet access for the
download and does **not** claim offline operation. Each HTTP request has a
150-second timeout; the inference service has a 140-second timeout in this
test. Model hosting/network failures fail the release rather than bypass STT.

## Real RTMP/HLS Smoke

`stream_smoke.py` tests an **already running, isolated** current-source stack:
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
deadline. The HLS test checks fMP4 bytes, content types and lengths. A Range probe
validates partial bytes/headers when supported; a consistent full-file 200 from
an upstream without advertised range support is accepted and reported. Logout
must revoke both the operator token and its playback cookie without stopping the
publisher. Cleanup terminates only FFmpeg processes created by this invocation,
confirms offline with a separate operator session, and attempts to revoke all
test logins even on failure. FFmpeg has a 120-second media limit and a separate
125-second wall-clock kill guard. Python uses two logins; `--browser` adds one,
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
16:9 layout without overflow at 1440/390/320 px, removes the real playback cookie
and requires real HTTP authorization recovery, then verifies unmount/request
cleanup and playing video after returning. It never mocks HTTP or reveals encoder
settings. It does not pair a wearer or inspect/change an operator draft; draft
retention is covered separately by the existing isolated application E2E test.
Its context/browser close and logout is attempted on failure too. Native Safari
HLS, physical devices, audible output and scheduled five-minute cookie renewal
remain manual checks; this test covers immediate missing-cookie recovery and API
cookie rotation, not passage of the full expiration interval.

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

CI also starts the Umbrel development overlay with a separate data/config bind
mount. It runs the real initializer, then boots the server, nginx and MediaMTX
using only the generated config directory, and repeats real RTMP/browser checks.
`deploy/umbrel/ci.override.yml` exposes web on loopback for this test. The actual
Umbrel-supplied app proxy is not started or tested by that substitute.

## Publication

`release.yml` runs on `v*` tags and manual dispatch. It first calls the full CI
workflow, including its existing Compose boot test. The optional manual
version defaults to the source tag or project
version. Versions must match `pyproject.toml`, `frontend/package.json`, and
`frontend/app.json`; source tags must be exactly `v<version>`. Stable SemVer
and SemVer prereleases are supported, but build metadata is rejected because
Docker tags cannot contain `+`. No source tags are created or changed.

The release builds **linux/amd64 only** on Ubuntu 24.04, labels it with source,
revision and version, and smoke-tests its local image ID with real CPU STT.
Only that exact image is tagged and pushed to
`ghcr.io/9vibes/evencomms:<version>` using `GITHUB_TOKEN` with `packages: write`.
There are no `latest`, moving minor-version, or ARM tags. The first version is
`0.1.0`.

Publication is serialized. An authenticated registry check rejects existing
tags and fails closed on authentication, network, and unexpected registry
errors. It permits only a confirmed missing manifest/repository. This prevents
replacement by this workflow, not by external writers: GHCR's tag API does not
provide an atomic create-only push. Restrict other package writers accordingly.
To retry a successfully published version, do not delete/reassign its tag;
publish a new version instead. A manual run publishes its selected source ref,
so select the intended release tag (or reviewed commit) deliberately.

The published repository digest is written to `image-reference.txt`, uploaded
as the `image-reference` artifact, and recorded in the job summary. Use this
digest for the Umbrel store integration. A first GHCR package can be private:
a maintainer must set its visibility to **Public** if needed and separately
verify an anonymous pull by digest. The workflow does not wait for visibility
changes. The token also needs permission to publish into the `9vibes` namespace;
organization/package policy can deny that even with `packages: write`.
