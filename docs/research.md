# Research Chat (0.3.0)

RESEARCH is an operator-only OpenAI chat with optional still-frame attachments.
It is separate from wearer conversations and never automatically replies to the
glasses. No continuous video, audio, stream key, or unsent wearer draft is sent to
OpenAI. Capturing a frame only adds a local draft attachment; Send is explicit.

## Connection And Privacy

- `OPENAI_API_KEY` optionally configures a shared server-managed key. Never use a
  `VITE_` variable, browser storage or a committed `.env` for this credential.
- An operator may connect a key for their current sign-in. It is retained only in
  server memory, scoped to that operator token, and cleared on logout/expiry,
  explicit Research disconnect or server restart. Closing a tab or losing the
  network is not an explicit disconnect. The API never returns the key.
- Research history and JPEG captures stay in browser memory, survive navigation
  between app tabs, and clear on reload/sign-out/New chat. They are not saved to
  the app database. A bounded server retry cache may hold results briefly.
- Sending submits the displayed conversation and its attached frames to OpenAI.
  OpenAI API billing and data-retention rules apply. Requests set `store: false`,
  which is not a guarantee of zero provider retention. No web-search or other
  external tools are enabled by this feature.
- Use HTTPS or localhost before entering a key in the browser. A ChatGPT
  subscription is not an OpenAI API key. Use a private deployment for sensitive
  footage; do not place real keys or footage in public demo screenshots/logs.

Connect your own OpenAI API key using **Connect for this sign-in**, or have the
deployment administrator provide `OPENAI_API_KEY` through a private server
environment. The latter is shared by authenticated operators; removing a sign-in
key falls back to it, rather than disabling or revoking it. Do not expose a key
as a plain Umbrel metadata field. Neither keys nor Research history belong in
`localStorage` or `sessionStorage`. Server restarts lose sign-in keys and operator
sessions by design; signing in again does not restore browser-only chat history.

The selector lists model IDs returned directly by OpenAI for the connected
account, not a hard-coded set of suggested or synthetic choices. Listing an ID
does not guarantee Responses API or vision support. Choose a compatible model
explicitly; the app never silently substitutes one.

## Backend Contract

All routes require the existing operator Bearer token; wearer tokens and playback
cookies cannot access Research. Provider calls use only
`https://api.openai.com/v1`, with no redirects or user-supplied provider URLs.

- `GET /api/research/status` -> `{configured: boolean, key_source: "server" |
  "session" | null}`. Does not call OpenAI.
- `POST /api/research/connection` `{api_key: string}` -> status above. Validate
  the new key by listing models before replacing a working session connection.
- `DELETE /api/research/connection` -> status above. Clears the session override;
  if an environment key exists, status falls back to `key_source: "server"`.
- `GET /api/research/models` -> `{models: [{id: string}]}` from OpenAI's `/models`,
  cached briefly per credential. IDs indicate availability, not verified support
  for Responses or images. The UI must explain model compatibility may vary.
- `POST /api/research/chat` -> `{request_id, model, text, incomplete, usage}`.
  `usage` is null or `{input_tokens, output_tokens, total_tokens}`.

Chat body:

```json
{
  "request_id": "client-generated UUID",
  "model": "an ID from the available-model list",
  "messages": [
    {"role": "user", "text": "What is on this screen?", "images": ["data:image/jpeg;base64,..."]}
  ]
}
```

Limits: 20 messages, user text up to 8000 characters, assistant text up to 16000,
64000 aggregate text characters, up to 3 JPEG images per user message and 6 in the
whole chat. Each decoded JPEG is at most 1 MiB and at most 1280 pixels per side.
Assistant images, external image URLs, other roles and unknown fields are rejected.
The last message must be a user message with text and/or an image. Requests are
bounded at 9 MiB; ordinary API and audio limits remain unchanged.

Chat admission is bounded before body receipt: one validation per operator and
two globally, with no waiting queue (excess requests return 429). Aggregate message
and image budgets are checked before any JPEG decoding; full validation runs off
the event loop. Cancelled HTTP waiters retain admission until their worker finishes,
and shutdown waits for workers. Validation slots are separate from the existing
one-per-operator/two-global provider limits; Research state is capped at 100 operator
sessions. Logout, expiry or connection changes discard late validation results.

The server builds Responses input with `input_text` and `input_image` parts and
`detail: auto`, preserves prior assistant text, and supplies its own instructions
to treat screenshot contents as untrusted data and not claim live-feed access.
`OPENAI_TIMEOUT` defaults to 90 seconds; `OPENAI_MAX_OUTPUT_TOKENS` defaults to 2048.
Replies longer than 16000 characters are truncated and marked incomplete so they
remain usable in follow-up history. The wire response is separately size-bounded.
No automatic billable retry. Use bounded pending/success-result deduplication
per operator + request UUID. Reusing an ID with different content returns 409.
Provider authentication/compatibility errors must NOT become operator-session
401s; return a sanitized 4xx/5xx error without upstream bodies, keys or prompts.
Only an invalid local operator token returns 401.

## Browser Contract

The Research component stays mounted across OPERATOR / STREAM / RESEARCH tab
switches to retain its history/draft. Its live player and polling run only while
RESEARCH is active. A request already sent may complete while another tab is
selected. New chat/sign-out cancels browser waiting and clears local images;
stopping a request does not guarantee OpenAI stops processing or billing it.

`StreamTab` accepts optional `compact`, `onCapture`, and `captureDisabled` props.
`LivePlayer` accepts optional `onCapture` and `captureDisabled`. Both components
also accept `onCaptureStateChange` so Send waits for JPEG encoding to finish.
A capture callback
receives `{id, dataUrl, width, height, capturedAt}`. Capture reflects the current
zoom/pan viewport, excluding page UI/native controls, and produces a bounded JPEG.
Capture draws native video pixels to a canvas, not a screenshot of the page.
No capture is allowed without a decoded frame (`readyState >= 2`); a paused
decoded frame is allowed. Send stays disabled during asynchronous JPEG encoding
until the attachment thumbnail is available for review. Thumbnails are removable
and show capture timestamps. New chat generation guards discard late captures
and replies, so an old request cannot repopulate a cleared chat.

Models are selected explicitly; never substitute a different model silently.
Rendering assistant replies as plain text avoids interpreting provider HTML.
Research requests and keys must never enter browser persistence, app logs,
shared screenshots, or wearer message history. Wearer voice capture, draft ordering
and human-approved replies are unchanged.

## Verification Scope

Backend provider tests use HTTPX MockTransport; the real-frame browser smoke
stubs only the Research API while using real authenticated HLS and native canvas
JPEG capture. Synthetic keys/model IDs are test fixtures, not production choices.
These checks do not verify a live OpenAI account, model compatibility, billing or
retention. Native Safari and physical glasses/phone acceptance remain unverified.
See [the smoke guide](../scripts/README.md#research-real-frame-browser-smoke).
The GitHub release page records 0.3.0 publication results; source features are not proof
of a published image or Umbrel installation.
