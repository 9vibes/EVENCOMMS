# EVENCOMMS 0.4.4

Fixes Codex Research failing immediately after a successful device-code login.
A direct diagnostic on Umbrel reproduced `codex:protocol_mismatch`: the upstream
returned HTTP 200 without a Content-Type header, which the relay rejected before
Codex could parse the reply. The public Cloudflare route masked this with a generic
origin-response error.

The relay now permits an omitted Content-Type, matching the pinned native client's
SSE parsing behavior. Explicitly incompatible or empty media types remain rejected.
The existing native event validation, completed-turn requirement, tool rejection,
model checks, response bounds and request budget remain in force. No automatic
resubmission or paid API fallback is added.

An isolated live-account check on Umbrel with GPT-6 Luna returned a complete
reply after this change. The installed service is updated through the release
images, not through a permanent container patch.

Regression coverage includes a headerless SSE response through the real pinned
Codex runtime and private backend/bridge path. Synthetic tests do not establish
account entitlement or availability of other models.

Update the app and bridge images together in place. Preserve the app password,
database, model cache, settings and private service token. No new ports, volumes,
services or permissions are required. Codex remains pinned to 0.157.1. Restarting
the bridge clears the temporary ChatGPT login; reconnect after updating.
