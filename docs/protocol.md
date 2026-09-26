# EVENCOMMS v0.1 protocol

All API requests use same-origin URLs by default. JSON responses unless noted.
Bearer authentication uses `Authorization: Bearer <token>`, never URL parameters.
Operator tokens expire after 8 hours. Pairing codes expire after 5 minutes and
are single use. Wearer tokens identify exactly one persistent conversation.

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
