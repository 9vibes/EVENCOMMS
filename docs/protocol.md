# EVENCOMMS 0.3.0 Protocol

All API requests use same-origin URLs by default. JSON responses unless noted.
Bearer authentication uses `Authorization: Bearer <token>`, never URL parameters.
Operator tokens expire after 8 hours. Pairing codes expire after 5 minutes and
are single use. Wearer tokens identify exactly one persistent conversation.
The original v0.1 wearer conversation, voice transcription and draft-ordering
contracts below are unchanged. Streaming and Research are separate operator
features; neither automatically sends a reply to the wearer.

## HTTP

- `GET /health`: public liveness only.
- `POST /api/login` `{password}` -> `{token}` (operator).
- `GET /api/status` (operator) -> `{stt_enabled, stt_model, ai_configured, ollama_model}`.
- `POST /api/pairings` (operator) -> `{code, expires_in}`.
- `POST /api/pair` `{code, name}` -> `{token, session_id}` (wearer).
- `GET /api/sessions` (operator) -> `[{id, name, connected, created_at}]`.
- `GET /api/sessions/{id}/messages` (operator) -> `[Message]`.
- `POST /api/sessions/{id}/reply` (operator) `{text, client_id}` -> `Message`.
- `POST /api/sessions/{id}/suggest` (operator) -> `{text}`. No automatic sending.
- `DELETE /api/sessions/{id}` (operator) -> 204. Revoke token, delete conversation.
- `GET /api/me` (wearer) -> `{session_id, messages: Message[]}`.
- `POST /api/messages` (wearer) `{text, client_id}` -> `Message`.
- `POST /api/transcribe` (wearer) raw `application/octet-stream` PCM s16le,
  16 kHz mono, max 15 seconds (480000 bytes) -> `{text}`. English local ASR.
  Audio is processed in memory and never retained. Not added to conversation.

`Message` = `{id, session_id, role: "wearer" | "operator", text, created_at, client_id}`.
Timestamps are ISO UTC. `client_id` is a client-generated UUID; retries return
the existing message. Reusing an ID with different text returns 409.
Text length is 1..4000 after trimming. Errors use `{detail: string}`.

## Wearer WebSocket

`/api/wearer`: connect, then send `{type: "auth", token}` within 5 seconds.
Never send credentials in the WebSocket URL. Server responds with
`{type: "ready", session_id, messages: Message[]}` including persisted messages.
New messages are sent as `{type: "message", message: Message}`. The socket is
notification-only: sends and transcription use authenticated HTTP. Client may
send `{type: "ping"}` and receive `{type: "pong"}`. On reconnect, deduplicate by
message ID; restore latest reply without resubmitting acknowledged questions.
Deleting the conversation closes its active socket. Only one socket per wearer
session is allowed; a replacement closes the previous connection with 4009.

## Draft ordering

The wearer owns the unsent draft. Audio segmentation, transcription responses,
word deletion, and submission are serialized. A tap finalizes the preceding
audio segment before removing its last word. A hold freezes audio capture;
release finalizes preceding speech and submits once. If transcription fails,
keep the audio for an explicit retry/discard; do not silently send partial text.
No unsent draft is exposed to the operator or Ollama.

## Stream And Logout

See [the streaming API](streaming.md#api) for status, explicit credential reveal,
playback sessions, authenticated HLS and the internal media authentication route.
Status/settings require operator Bearer authentication; HLS uses a short-lived
HttpOnly playback cookie, not bearer tokens in URLs. `POST /api/logout` revokes
the operator token, linked playback sessions and its in-memory Research state.
The internal media route must never be exposed by a public proxy.

Version 0.3.0 uses LL-HLS with 200 ms parts and a one-second browser live-sync
target, not a guaranteed glass-to-glass delay. The 0.1.0 image has no streaming;
0.2.1 uses ordinary fMP4 HLS. Upgrades require the full stack and matching configs.
Digital zoom/pan is browser-only, not a camera-control API.

## Research

See [the Research contract](research.md#backend-contract) for the complete schema,
error handling and retry semantics. Every Research route requires an operator
Bearer token; wearer tokens and playback cookies do not grant access.

- `GET /api/research/status` reports configuration without calling OpenAI.
- `POST /api/research/connection` validates and connects an operator's sign-in key;
  `DELETE` on the same route removes it, falling back to any shared server key.
- `GET /api/research/models` lists IDs returned by OpenAI for that credential,
  without promising Responses API or image compatibility.
- `POST /api/research/chat` accepts an explicit model, request UUID and displayed
  history with selected JPEG stills. It returns text, usage and incomplete status;
  it does not stream video/audio to OpenAI, enable web-search tools or send to glasses.

Chat limits are 20 messages, 6 images total and 3 per user turn, at most 1 MiB per
JPEG and 1280 pixels per side, with a 9 MiB request-body ceiling. Validation runs
off the event loop with two global slots and one per operator; provider calls
have a separate two-global/one-per-operator limit. Excess work returns `429`,
not an unbounded CPU queue. Existing ordinary JSON/audio limits are unchanged.

Sign-in keys remain in server RAM until logout, expiry, explicit Research
disconnect or restart. `OPENAI_API_KEY` is an optional shared backend credential,
never a frontend `VITE_` variable or browser-persisted value. Chat history/captures
are browser-RAM-only; a bounded server result cache supports recent retries.
Explicit Send includes the displayed history and frames. OpenAI calls are billable
and use `store: false`, which is not a zero-retention guarantee. Provider tests use
MockTransport/browser stubs, not live OpenAI verification.
