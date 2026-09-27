# EVENCOMMS 0.4.0 Protocol

All API requests use same-origin URLs by default. JSON responses unless noted.
Bearer authentication uses `Authorization: Bearer <token>`, never URL parameters.
Operator tokens expire after 8 hours. Pairing codes expire after 5 minutes and
are single use. Wearer tokens identify exactly one persistent conversation.
The original v0.1 wearer conversation, voice transcription and draft-ordering
contracts below are unchanged. Streaming and Research are separate operator
features; neither automatically sends a reply to the wearer.
The 0.4.0 source candidate adds experimental Codex Research without changing the
default API provider or existing optional OpenAI-key settings. This protocol
does not establish successful CI, publication or live-account acceptance.

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

Since version 0.3.0, streaming uses LL-HLS with 200 ms parts and a one-second
browser live-sync target, not a guaranteed glass-to-glass delay. The 0.1.0 image has no streaming;
0.2.1 uses ordinary fMP4 HLS. Upgrades require the full stack and matching configs.
Digital zoom/pan is browser-only, not a camera-control API.

## Research API Mode

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

## Experimental Codex Research

See [Codex deployment, allowance and privacy](codex.md). These separate routes
require the same operator Bearer authentication, never a wearer token or playback
cookie. Codex does not consume `OPENAI_API_KEY`, substitute a model, or fall back
to another provider/API billing. The UI starts in API mode; switching providers
requires confirmation and clears local history/drafts without replaying them.

- `GET /api/research/codex/status` -> `{enabled, state, verification_url,
  user_code, generation_enabled}`. `state` is `disconnected`, `pending`,
  `connected` or `failed`. Reading status never starts a login or inference.
- `POST /api/research/codex/login` `{}` -> status above. Explicitly starts an
  isolated device-code login for that operator. Remote browser initiation requires
  HTTPS; exact loopback HTTP is only for isolated development. The UI accepts only
  `https://auth.openai.com/codex/device` as the verification URL.
- `DELETE /api/research/codex/connection` -> disconnected status. Clears local
  mapping/results immediately and requests best-effort runtime cleanup; it never
  switches the UI to API mode. Logout/expiry also discard late replies.
- `GET /api/research/codex/models` -> `{models: [{id, image}]}`. Lists pinned runtime
  selectors for a connected account, not live entitlement guarantees. Explicit
  selection is required; unsupported selections fail rather than being replaced.
- `POST /api/research/codex/chat` uses the API mode's bounded request schema:
  explicit model, request UUID and displayed user/assistant history with selected
  JPEG stills. Returns `{request_id, model, text, incomplete, usage}`. Only Send
  submits the question/images; login, polling and model selection do not.

The connected state alone is insufficient: Send also requires the runtime's
`generation_enabled` safety gate. The official Codex 0.157.1 dependency and binary
hashes are pinned. Unexpected tool calls/approval requests fail closed; no shell,
web search, MCP or other external tools are enabled. A private loopback relay
enforces one non-401 upstream Responses request per explicit Send; the pinned
credential-recovery path permits up to three attempts after confirmed 401s.
There is no automatic application resubmission. Manual retries can consume more
allowance, even with the same UUID after a failure, cache expiry or restart.

Message/image/body limits match API mode. Codex has its own two-worker validation
pool; admission before body receipt permits at most two waiters per operator and
four globally, including duplicate IDs. There are at most two account sessions,
one active generation per session, and eight fresh sends per login to bound
ephemeral threads. Pending sign-in expires after 15 minutes; connected login
expires after at most eight hours or the operator's earlier expiry. A 120-second
bridge lease renewed every 30 seconds bounds abandoned sessions after backend
failure. The bridge request deadline is 85 seconds; backend `OPENAI_TIMEOUT` also
applies. `OPENAI_MAX_OUTPUT_TOKENS` is API-only. Codex text is bounded at 16,000
characters; unsafe/oversized runtime output fails without another turn.

ChatGPT eligible-plan allowance, purchased credits and account/workspace data
controls apply, not the API `store: false` contract. The browser and main backend
never receive OAuth tokens or account email. Credentials and submitted threads
remain in isolated bridge RAM until cleanup; New chat clears the browser view,
not submitted threads. No continuous video/audio, wearer drafts or stream secrets
are submitted. Cleanup is not a guarantee of remote revocation or zero retention.

## Private Bridge

Umbrel starts the isolated service by default and provisions a private service
token once. Backend and bridge read `CODEX_BRIDGE_TOKEN_FILE=/run/codex-auth/token`
from read-only mounts of `${APP_DATA_DIR}/codex-auth`; init alone mounts it
writable at `/codex-auth`. The directory/file use ownership `10001:10002`, modes
`0750`/`0440`. No token is placed in shared config or app-password metadata.
Standalone `compose.yml` does not start Codex; its explicit `compose.codex.yml`
overlay instead shares a manually generated `CODEX_BRIDGE_TOKEN` from private
`.env`. Both services reject conflicting token sources. The service secret is
not a user/account token and is never sent to the Codex child runtime.

The bridge publishes no ports and joins only an internal backend link and a
separate outbound network. It has no app-data/config, host-home or Docker-socket
mount. Ordinary server startup is not gated on bridge health. Startup runs
bounded synthetic loopback probe processes, not a real account login/inference.
The service stays idle with no account sessions until explicit device login.

`GET /health` is liveness only. Private `GET /ready` requires the service Bearer
token and returns `{binary_verified: boolean, generation_enabled: boolean,
active_sessions: number}`. It does not create a session, contact a real provider
or return credentials/account details. Release verification must require both
booleans to be `true` and `active_sessions: 0` after startup, not just HTTP
liveness. These checks do not prove entitlement or a successful installation.
Do not expose bridge port `8001` or raw app-server RPC through public ingress.
