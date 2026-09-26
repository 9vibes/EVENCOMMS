# Real-server browser tests

Run from `frontend` after installing the frontend and backend dependencies and
building the frontend (`npm run build`). The tests serve the existing `dist`
through the production FastAPI `create_app`, not Vite or a mock HTTP server.

```sh
npx playwright install chromium
EVENCOMMS_PYTHON="$(pwd)/../.venv/bin/python" \
npm run test:e2e
```

The example uses a virtual environment at the repository root with the backend
dependencies installed. `EVENCOMMS_PYTHON` defaults to `python3`; omit the override
if that interpreter already has the dependencies. To use an existing Chromium,
set `CHROMIUM_PATH` to its executable. For CI, install Playwright's pinned browser
and system dependencies with `npx playwright install --with-deps chromium`.

The test-only `backend/e2e_server.py` creates a temporary SQLite database and
model directory, disables STT, and removes the temporary directory on orderly
shutdown. The operator password is test-only. Port `127.0.0.1:8765` must be free:
the runner deliberately refuses to reuse an existing server. No existing
database or operator credentials are used. Ollama points to an unusable local
port; only the suggestion response is intercepted with `page.route`. All other
HTTP traffic and wearer WebSockets use the real backend.

There are four tests, run with one worker and no retries, staying below the real
login/pairing rate limits. Rerunning the command starts a fresh server/database.
The main flow covers private correction, quick-click safety, continuous hold
and release, exactly-once submission, operator polling, suggestion review,
paginated replies, reload recovery, unauthorized deletion rejection, and
authenticated deletion/token/socket revocation. Three viewport tests check both
pages at 320, 390, and 1440 pixels before pairing and with a populated conversation.

Screenshots are saved for populated conversations at every viewport. Failure
screenshots and traces are retained. Artifacts default to the system temporary
directory under `evencomms-playwright`, or `EVENCOMMS_E2E_OUTPUT` when set.
Playwright clears that output directory at the start of a run.

Browser simulation exercises the real wearer runtime without a physical Even
bridge. These tests do not claim to validate native SDK gestures, microphone
capture, STT, or model inference. Holds explicitly bring the wearer to the front
because the production UI cancels holds on blur/visibility changes.
