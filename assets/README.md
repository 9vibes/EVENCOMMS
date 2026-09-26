# EVENCOMMS Store Assets

Source assets for integration into KNS-Umbrel. No store manifests or application
files are changed here.

| File | Format / Size | Purpose |
| --- | --- | --- |
| `icon.svg` | SVG, 512 x 512 viewBox | Hand-drawn geometric glasses and chat mark, warm amber `#eeb653` and dark charcoal `#101210`, rounded tile. No fonts or external resources. |
| `screenshot-operator.png` | PNG, 1440 x 1143 | Full-page desktop operator console captured at a 1440 x 1000 viewport, Alex's submitted question, human reply, and text preview. |
| `screenshot-wearer.png` | PNG, 390 x 1575 | Full-page wearer browser simulation captured at a 390 x 844 phone viewport, including the reply and empty private draft. |

The screenshots use the current built app and its SteamLab-style KUNAS theme,
without injected styling, compositing, mocked API responses, or altered UI copy.
The wearer is **Alex (demo)**. The only conversation is synthetic:

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
