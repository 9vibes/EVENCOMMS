# EVENCOMMS Store Assets

Source assets for EVENCOMMS **0.2.0** integration into KNS-Umbrel. Captured from
the release's already-built `frontend/dist`; the operator footer reads `v0.2`.
No store manifests or application files are changed here.

| File | Format / Size | Purpose |
| --- | --- | --- |
| `icon.svg` | SVG, 512 x 512 viewBox | Hand-drawn geometric glasses and chat mark, warm amber `#eeb653` and dark charcoal `#101210`, rounded tile. No fonts or external resources. |
| `screenshot-operator.png` | PNG, 1440 x 1161 | Full-page desktop operator console captured at a 1440 x 1000 viewport, Alex's submitted question, human reply, and text preview. |
| `screenshot-wearer.png` | PNG, 390 x 1575 | Full-page wearer browser simulation captured at a 390 x 844 phone viewport, including the reply and empty private draft. |
| `screenshot-stream.png` | PNG, 1440 x 1376 | Full-page desktop STREAM view at a 1440 x 1000 viewport, including CONTROL ROOM / OPERATOR / STREAM, heading, decoded synthetic video, playing status, collapsed encoder configuration, and footer. |

The screenshots use the current built app and its SteamLab-style KUNAS theme,
without injected styling, compositing, mocked API responses, or altered UI copy.
The wearer is **Alex (demo)**. The operator/wearer images contain only this
synthetic conversation:

- Wearer: "Which entrance should I use for the meeting?"
- Operator: "Use the north entrance. I will meet you at reception."

The question is typed and submitted with the real hold-to-send control. The reply
is sent from the operator UI. The wearer image explicitly shows **BROWSER
SIMULATION**, not a physical glasses capture. STT is disabled; AI is configured
only to the fixture's unreachable `127.0.0.1:1` endpoint. No audio, transcription,
AI suggestion, or live model is used. "Configured" in the real UI is not a model
health claim; the operator preview is not a device delivery receipt.

## Reproduce

Prerequisites: Node 24, the existing Playwright installation resolvable from
`frontend/node_modules`, Chromium, the backend's installed Python dependencies,
and an already-built `frontend/dist`. This script does not install, rebuild, or
change application/package configuration.

Run from the repository root:

```sh
node frontend/scripts/screenshots.mjs
```

Portable defaults are `python3` and Playwright's installed Chromium. Override
executable paths through the process environment when needed, for example with
a virtual environment at the repository root:

```sh
EVENCOMMS_PYTHON="$(pwd)/.venv/bin/python" \
CHROMIUM_PATH=/usr/bin/chromium \
node frontend/scripts/screenshots.mjs
```

The script refuses an occupied port 8765, starts its own
`python -B -m backend.e2e_server` on `127.0.0.1:8765`, and shuts that child down
afterward. The fixture creates a fresh temporary database and model directory;
normal shutdown removes them. It never reuses a running server or accesses a
live preview, public tunnel, production database, or production credentials.
The origin is deliberately not configurable.

A random operator password is generated per run. The consumed pairing code,
operator token (sessionStorage), and wearer token (localStorage) exist only in
the isolated process/browser contexts. Browser storage, traces, and backend
logs are not saved. Only generated PNG paths are printed on success; failures
report a credential-free stage description. All browser HTTP and WebSocket
traffic is restricted to the fixture; transcription and suggestion requests
are blocked and fail the capture.

Before capture, checks verify the exact two-message backend history, connected
UI, reply previews, empty drafts, no active pairing code, no visible credentials,
no loading/error state, theme colors, no horizontal document/body overflow,
and unclipped message/preview content. Both captures intentionally include
vertical scrolling so the real controls, service status, and latest reply are
not cropped. Locale is `en-US`, timezone is UTC, and pixel
ratio is 1; real fixture timestamps vary between runs.

## Real Stream Capture

The stream image uses a separate, fresh native stack with **no paired wearers or
conversation records**. FFmpeg generates `testsrc2` video at 1280 x 720 / 30 fps
and silent stereo AAC. `SYNTHETIC DEMO` is burned into the encoded frames with
`drawtext`, not composited into the screenshot. The actual path is FFmpeg RTMP
-> MediaMTX 1.12.3 -> backend-authenticated fMP4 HLS -> Chromium's video decoder.
Media buffers are memory-only; recording, STT, and AI are disabled. No camera,
microphone, user media, or user conversations are used.

The release capture verified `Playing live preview`, 1280 x 720 decoded video,
73 decoded frames, `readyState = 4`, unpaused playback, and `currentTime`
advancing from 0.436 to 2.321 seconds before capture. Source bitrate and publisher
time come from real backend polling, not mocked responses. Frame content,
timestamps, and bitrate naturally vary. Encoder configuration remains collapsed;
no operator password, token, stream key, or pairing code is visible.

### Native Fixture Setup

The capture used a temporary local launcher, not a repository script. The
following setup and commands describe its reproducible method without requiring
that launcher or its machine-specific paths. In addition to the prerequisites
above, install MediaMTX **1.12.3**, nginx, and FFmpeg with `lavfi`, `libx264`, AAC,
and `drawtext` support. Put the backend virtual environment and these binaries
on `PATH`. Run from the repository root, without shell tracing or debug logging.

1. Refuse any occupied fixture port before starting anything. Do not attach to
   an existing listener or kill it. The dedicated loopback ports are nginx
   **28197**, backend **28198**, MediaMTX API **19997**, HLS **18888**, and RTMP
   **21937**. The operator/wearer script independently reserves **8765**.
2. Create a private temporary directory, a new database path, and a random test
   operator password. Never reuse an existing database, `.env`, password, or
   publisher key. Start children with a sanitized environment, not inherited
   production/model/proxy settings.
3. Make temporary copies of `infra/nginx.conf` and `infra/mediamtx.yml`. In nginx,
   use one worker, change `listen 8080;` to `listen 127.0.0.1:28197;`, replace
   `server:8000` with `127.0.0.1:28198`, and relocate every `/tmp/` path into the
   private directory. In MediaMTX, change the auth URL host to
   `127.0.0.1:28198` and bind `apiAddress`, `hlsAddress`, and `rtmpAddress` to
   `127.0.0.1:19997`, `127.0.0.1:18888`, and `127.0.0.1:21937` respectively.
   Preserve HTTP media authentication, `overridePublisher: no`, `record: no`,
   `hlsDirectory: ''`, and disabled unused protocols. Do not edit repo configs.

For example, prepare the directory/password and check ports with:

```sh
export CAPTURE_DIR="$(mktemp -d)"
export ADMIN_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
python3 - <<'PY'
import socket
probes = []
try:
    for port in (28197, 28198, 19997, 18888, 21937):
        probe = socket.socket(socket.AF_INET6)
        probes.append(probe)
        probe.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        probe.bind(('::', port))
        probe.listen(1)
finally:
    for probe in probes:
        probe.close()
PY
```

After preparing the two temporary configs as above, run these commands in the
same shell. Track only the PIDs started here. On any startup or readiness failure,
stop these children and abort; never fall back to another origin.

```sh
env -i PATH="$PATH" HOME="$HOME" PYTHONDONTWRITEBYTECODE=1 \
  ADMIN_PASSWORD="$ADMIN_PASSWORD" DATABASE_PATH="$CAPTURE_DIR/data.sqlite3" \
  MODEL_CACHE="$CAPTURE_DIR/models" FRONTEND_DIST="$PWD/frontend/dist" \
  STT_ENABLED=false OLLAMA_URL='' OLLAMA_MODEL='' ALLOWED_ORIGINS='' \
  STREAM_ENABLED=true COOKIE_SECURE=false PUBLIC_HOST=localhost RTMP_PORT=21937 \
  MEDIA_API_URL=http://127.0.0.1:19997 MEDIA_HLS_URL=http://127.0.0.1:18888 \
  python3 -B -m uvicorn backend.main:app --host 127.0.0.1 --port 28198 \
  --no-access-log --no-proxy-headers --ws-max-size 8192 \
  >"$CAPTURE_DIR/backend.log" 2>&1 &
backend_pid=$!
mediamtx "$CAPTURE_DIR/mediamtx.yml" >"$CAPTURE_DIR/media.log" 2>&1 &
media_pid=$!
nginx -c "$CAPTURE_DIR/nginx.conf" -g 'daemon off;' >"$CAPTURE_DIR/nginx.log" 2>&1 &
nginx_pid=$!
```

Wait for these owned children and `http://127.0.0.1:28197/health` to be healthy.
Log in through `/api/login` with the random password, assert `/api/sessions` is
empty and `/api/stream/status` reports enabled, media available, and offline.
Retrieve **this fixture's** key from authenticated `/api/stream/settings` without
printing it. This command performs that preflight and replaces its Python process
with the sole owned encoder, so `$encoder_pid` remains the process to stop:

```sh
python3 - <<'PY' >/dev/null 2>&1 &
import json, os, urllib.request
base = 'http://127.0.0.1:28197'
def api(path, token=None, body=None):
    request = urllib.request.Request(base + path,
        data=None if body is None else json.dumps(body).encode(),
        headers={'Content-Type': 'application/json',
                 **({'Authorization': 'Bearer ' + token} if token else {})})
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.load(response)
token = api('/api/login', body={'password': os.environ['ADMIN_PASSWORD']})['token']
assert api('/api/sessions', token) == []
status = api('/api/stream/status', token)
assert status['enabled'] and status['media_available'] and not status['online']
settings = api('/api/stream/settings', token)
assert settings['server_url'] == 'rtmp://localhost:21937/live'
os.execvp('ffmpeg', ['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error',
    '-re', '-f', 'lavfi', '-i', 'testsrc2=size=1280x720:rate=30',
    '-f', 'lavfi', '-i', 'anullsrc=channel_layout=stereo:sample_rate=48000',
    '-vf', 'drawtext=text=SYNTHETIC DEMO:fontcolor=white:fontsize=56:box=1:boxcolor=black@0.8:boxborderw=18:x=(w-tw)/2:y=(h-th)/2',
    '-c:v', 'libx264', '-threads', '2', '-preset', 'ultrafast', '-tune', 'zerolatency',
    '-pix_fmt', 'yuv420p', '-g', '30', '-keyint_min', '30', '-sc_threshold', '0',
    '-b:v', '2500k', '-c:a', 'aac', '-b:a', '128k', '-f', 'flv',
    'rtmp://127.0.0.1:21937/live/' + settings['stream_key']])
PY
encoder_pid=$!
```

RTMP is **plaintext**, even when a console uses HTTPS. This fixture confines it
to loopback; real deployments need a trusted LAN/VPN and restricted ingest, as
described in [streaming.md](../docs/streaming.md). An HTTP tunnel does not carry
RTMP. Never target live preview ports **28097/28098**, any `*.trycloudflare` URL,
or any other existing service for gallery captures.

### Full-Page Browser Capture

Use a fresh Chromium context, 1440 x 1000 viewport, device scale 1, `en-US`, UTC,
dark color scheme, reduced motion, and blocked service workers. Restrict all HTTP
traffic to `http://127.0.0.1:28197`; block unexpected WebSockets. Log in with the
fixture password and select the real STREAM tab. Do not reveal encoder settings,
intercept responses, change UI copy/styles, freeze polling, or substitute media.

The key Playwright checks and capture operation are below (`page` is the fresh,
authenticated page; `expect` is from `@playwright/test`):

```js
await page.getByRole('tab', { name: 'STREAM', exact: true }).click();
await expect(page.getByText('Playing live preview', { exact: true }))
  .toBeVisible({ timeout: 40000 });
const video = page.locator('video');
await expect.poll(() => video.evaluate(v => v.videoWidth)).toBe(1280);
expect(await video.evaluate(v => v.videoHeight)).toBe(720);
const start = await video.evaluate(v => v.currentTime);
await expect.poll(() => video.evaluate(v => v.currentTime)).toBeGreaterThan(start + 1);
expect(await video.evaluate(v => !v.paused && v.readyState >= 2 &&
  v.getVideoPlaybackQuality().totalVideoFrames > 0)).toBe(true);
await expect(page.getByText('CONTROL ROOM /', { exact: true })).toBeVisible();
await expect(page.getByRole('tab', { name: 'OPERATOR', exact: true })).toBeVisible();
await expect(page.getByRole('tab', { name: 'STREAM', exact: true }))
  .toHaveAttribute('aria-selected', 'true');
await expect(page.getByRole('heading', { name: 'Super Secret Comms Platform' })).toBeVisible();
await expect(page.getByRole('button', { name: /Encoder configuration/ }))
  .toHaveAttribute('aria-expanded', 'false');
await expect(page.locator('#encoder-config')).toHaveCount(0);
await expect(page.getByRole('alert')).toHaveCount(0);
await page.evaluate(() => document.fonts.ready);
await page.mouse.move(0, 0);
await page.evaluate(() => { document.activeElement?.blur(); window.scrollTo(0, 0); });
await page.screenshot({ path: 'assets/screenshot-stream.png', fullPage: true,
  animations: 'disabled' });
```

Also check for no visible credentials, horizontal overflow, runtime errors, or
unexpected network requests, and confirm the charcoal/amber theme and `v0.2`
footer. Inspect the resulting PNG for the burned-in label and complete page.
Use `page.screenshot({ fullPage: true })`, not the panel-only screenshot option
in `frontend/scripts/check-stream.mjs`, which omits the heading and tabs.

Always close the owned browser in `finally`. Stop and wait for only the saved
encoder, nginx, media, and backend PIDs, including on failure:

```sh
kill "$encoder_pid" "$nginx_pid" "$media_pid" "$backend_pid"
wait "$encoder_pid" "$nginx_pid" "$media_pid" "$backend_pid" || true
unset ADMIN_PASSWORD
```

Verify the five ports are released. A recent shutdown can leave TCP `TIME_WAIT`
sockets that make a strict bind probe refuse a quick retry; wait rather than
reusing or terminating another process. Discard only the owned temporary
directory after shutdown. Do not publish its database, logs, browser storage,
traces, or credentials. Only the three synthetic PNGs belong in the gallery.
