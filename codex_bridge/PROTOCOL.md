# Pinned Runtime Policy

This prototype uses official Codex `0.157.1`, source tag `rust-v0.157.1`, commit
`36650394c5b38c2990ccf2a3457165ca3e9d9726`. The npm lock pins the official assets;
startup additionally checks the Linux binary SHA-256 and generated schema.

## Retry Disclosure

There is **no paid API fallback** and **no application-level automatic
resubmission after generation or failure**. Cached results for an identical
request UUID do not start another turn. Codex itself can retry during bounded
OAuth credential recovery. Do not describe this integration as "no retries".

The configured provider request and stream retry limits are zero. In the pinned
source, `codex-rs/core/src/client.rs::stream_responses_api` and
`handle_unauthorized` separately use
`codex-rs/login/src/auth/manager.rs::UnauthorizedRecovery`:

1. The initial Responses request uses the current ChatGPT credential.
2. On 401, `Reload` reloads the same account's credential, including from the
   ephemeral store. Even an unchanged credential permits a second attempt.
3. On another 401, `RefreshToken` makes one OAuth refresh request. A successful
   refresh permits a third Responses attempt with the refreshed credential.
4. `Done` disallows further recovery for that request. Failed refresh or another
   401 after the third attempt ends the turn. The bridge does not resubmit it.

The offline startup probe requires these exact phase sequences and counts, not
merely a request count below some generous ceiling:

| Synthetic Scenario | Responses Attempts | OAuth Refreshes | Required Outcome |
| --- | ---: | ---: | --- |
| Persistent 401, refresh denied | 2 | 1 | Failure |
| 401, success after reload | 2 | 0 | Success |
| Two 401s, success after refresh | 3 | 1 | Success, account remains connected |
| Three 401s, successful refresh still rejected | 3 | 1 | Failure, recovery exhausted |
| HTTP 500 | 1 | 0 | Failure |

The mock upstream rejects the first unexpected phase or extra forwarded request. The gate also
checks the recorded counts independently. `auth_recovery_bounded: true` is a
required proof; `no_retries: false` is informational and remains false on a
successful probe. These are observations of a pinned offline fixture, not a
claim of live-account entitlement or billing behavior.

The synthetic JWT includes stable user and workspace identities. This matters:
`login/src/auth/change_state.rs::same_owner` requires both. An incomplete
synthetic identity makes refreshed credentials look like an ownership change,
which correctly invalidates the runtime's network policy instead of proving
ordinary same-account credential refresh.

## Alias And Safety

The documented custom-provider configuration uses the fixed alias `evencomms`,
because this Codex version disallows overriding the built-in `openai` provider.
It retains `requires_openai_auth=true` and `forced_login_method='chatgpt'`.
Codex's generation URL is always the private loopback relay, never a direct
production inference destination. The relay alone owns the fixed upstream
`https://chatgpt.com/backend-api/codex/responses`. No API key, environment-key
selector, credential command, or alternate paid endpoint exists in the bridge
policy. Every thread explicitly selects that alias and the bridge rejects a
runtime response that changes provider or model.

All probe generation cases now use synthetic device login with
`requires_openai_auth=true`. The probe checks `model/list` against the complete
sanitized startup catalog, regardless of runtime priority ordering, and runs a
successful one-shot for **each exposed selector**. It inspects every authenticated Responses
request, including recovery attempts, for `tools: []`, exact role-labelled
history, and exact image bytes. The history fixture contains 20 messages, six
JPEGs, maximum-length message fields, and instruction-shaped user text. Catalog
selectors do not establish subscription entitlement or actual availability.
The probe replaces the OAuth issuer and the relay's upstream origin with
loopback fixtures through internal constructors. The real binary still uses the
real relay, not a bypass path. This does not verify live account access or
tenant-specific production workspace routing; the relay does not follow a
redirect or select another backend for such accounts.

The exposed selectors, in bridge order, are `gpt-6-luna` and `gpt-6-astra`, both
with text/image input in the same pinned upstream catalog. Upstream describes
Luna as "Fast and affordable model for easier tasks." Its `medium` default
reasoning level and priority `3` are preserved, as are Astra's `low` default and
priority `1`. Only allowlisted metadata is retained; upstream prompt strings are
not imported. Both entries have identical mandatory no-tool restrictions.
These descriptors are not a guarantee of account allowance or price.

Luna is the bridge startup default and the recovery-fixture selector. Chat still
requires an explicitly supplied model, and the UI's explicit selection behavior
is unchanged. `model_proofs` must contain a successful tools/history/images
one-shot for every exposed ID; missing or failed model coverage disables
generation. The per-model smoke tests add only one extra sequential runtime to
the existing recovery suite; the generation proof's 75-second timeout remains.
The service independently verifies the binary off the event loop with a
10-second deadline first. A generation-probe timeout cannot erase that binary
verification or block device login; generation still needs the complete proof.

The gate also requires binary/schema verification, ephemeral credentials,
unexpected tool-call rejection, HTTP-500 behavior, revocation handling, fixture
integrity, and cleanup. Failed or incomplete proof leaves generation disabled
without disabling supported login. Unknown tool, approval, or protocol events
still kill the process. None of the retry changes relax those restrictions.

An `account/updated` notification with any non-`chatgpt` auth mode (including
null) fails an active generation. Mocked service tests cover this explicitly.
During actual-runtime synthetic logout, invalidation of the active turn's
network policy can emit an `error` before `account/updated`; that also fails
generation immediately. The probe reports which event arrived first rather than
waiting for a later account notification after an already-fatal event.

Pinned `core/src/session/mod.rs::next_internal_sub_id` assigns
`auto-compact-0` to the first internal recording context. `thread/inject_items`
uses that context for its history echoes and initial skills-context message;
this name does not mean that an inference or compaction ran. App-server forwards
it in `rawResponseItem/completed.turnId`. The `turn/start` RPC reply can arrive
before those notifications finish, so a replay echo's ID need not equal the new
generation's turn UUID. The shared service/probe guard accepts only exact
submitted replay messages, in order, and one bounded, text-only
`host_skills.instructions` prelude on that same fresh thread. They are never
returned as generated output. Other turn IDs, changed/extra echoes, tools,
approvals and unknown events remain fatal; no delay or retry hides a rejection.

### Verified Request Barrier

Loaded probes also found a separate unsupported-tool continuation race in the
pinned binary. `core/src/stream_events_utils.rs::handle_output_item_done` sets
`needs_follow_up` for tool calls (including unsupported calls), and
`core/src/session/turn.rs` can start another sampling request before app-server's
queued stdio tool notification reaches the bridge. The empty executable registry
prevents execution, but notification-driven interruption alone cannot prevent
an extra inference request. A loopback-only HTTP relay now enforces that boundary
before any upstream forwarding, independently of notification timing.

- Each Runtime binds one relay on `127.0.0.1` at an ephemeral, unpublished port.
  Its startup URL is unarmed. Only trusted `Generation.run` arms a budget for an
  explicit send; no HTTP request or Codex command can arm/reset one.
- Every send receives a fresh opaque capability path. The pinned binary honors
  `thread/start.config["model_providers.evencomms.base_url"]`, which installs that
  path only in the new ephemeral thread. The relay also checks the bound
  `thread-id` and `x-client-request-id` headers. Old paths cannot use new budgets.
- Reservation is atomic and precedes network I/O. Only a confirmed HTTP 401 can
  reopen it, for at most three total forwarded attempts. At most one non-401
  attempt is forwarded; HTTP 500, redirect, cancellation, timeout and network
  uncertainty consume the budget. Overlapping calls and follow-ups are rejected
  locally. Authentication recovery cannot change the model, input or account.
- The relay validates `tools: []`, the selected catalog model, full message
  input, streaming, and non-persistence. Origin/cookie/unknown/hop-by-hop request
  headers, compressed or ambiguous framing, redirects, and arbitrary destinations
  are rejected. Request headers use an explicit pinned-runtime allowlist; Host
  is checked locally but never used for upstream routing. HTTPX has no environment
  proxy, transport retry, or cookie acceptance. Response cookies and raw error
  bodies are never forwarded.
- Bounds are 32 KiB headers, 10 MiB request body, 16 MiB streamed response, four
  admitted connection tasks per Runtime, and one upstream connection. Streaming
  uses bounded chunks and backpressure with write/idle deadlines, within the
  generation's 85-second lifetime. Closing a Runtime closes its listener, owned
  tasks, client and any in-flight stream.

The actual-binary probe deliberately delays the stdio notification consumer in
the tool case. Codex can make multiple **local** requests, but the gate requires
exactly one **upstream** non-401 request and an observed blocked follow-up. This
is not acceptance of another billable turn. Ordinary tool rejection then kills
the runtime as before. The probe also sends two explicit generations through
different paths in the same process and checks that a stale path is rejected
while the second budget is armed. `tool_continuation_guarded`, `relay_used` and
`relay_paths_isolated` are mandatory proofs, not configuration bypass flags.

### Credential Custody

Codex still owns official device login, polling and token refresh; these use its
supported authentication client directly. The inference relay now necessarily
handles the OAuth Authorization and account headers **in bridge RAM** while
forwarding to the fixed trusted upstream. Thus credentials are not exclusively
in the native runtime anymore. They are never exposed to the app API/browser,
placed in paths, persisted, or logged. Capability paths contain independent
random non-credential values and are not logged. Native stderr is discarded,
HTTP transport header tracing is disabled, and core dumps are disabled for the
bridge and its children. The relay is not a browser endpoint or generic proxy.

## Private Contract

- `CODEX_BRIDGE_TOKEN`: 32 through 256 ASCII hexadecimal characters, inclusive;
  uppercase and lowercase are accepted and compared without normalization.
- Device URL: exactly `https://auth.openai.com/codex/device`, with no query,
  fragment, trailing slash, or alternate origin. Pinned
  `login/src/device_code_auth.rs::request_device_code` constructs the response
  URL as `{issuer}/codex/device`; `login/src/server.rs::DEFAULT_ISSUER` is
  `https://auth.openai.com`. The probe verifies that literal path in the actual
  binary's response using a loopback-only issuer, not a live account.
- User code: `[A-Za-z0-9][A-Za-z0-9-]{0,31}`, exactly matching the backend's
  bounded ASCII pattern. Codes are opaque and case-preserving, 1 through 32
  characters; neither uppercasing nor lowercasing is performed.
- `POST /lease` returns HTTP 200 with `{"sessions": ["renewed-id", "..."]}`:
  only requested, existing, healthy pending/connected sessions actually renewed
  for 120 seconds. Failed/expired sessions are cleaned up before acknowledgement;
  missing IDs are never recreated. The backend drops mappings omitted from the
  acknowledgement, even while the UI is hidden.
- Successful failed-session cleanup removes the session and frees its process
  reservation. Only an actual cleanup failure retains a reservation; it is not
  acknowledged or renewed and cleanup is retried by the independent sweeper.
- Maximum eight generated ephemeral threads per login. Result eight is returned,
  then the process is logged out/terminated and cached results are cleared. The
  next status is `disconnected` after successful cleanup, and further chat
  requires explicit sign-in again. No cold resumption.

The Docker image uses `python:3.12-slim-bookworm` like the main application, but
installs only standalone bridge dependencies and has no application DB imports
or volumes. Container isolation, memory/PID limits, private networking, and
read-only/tmpfs mounts remain required deployment controls.
