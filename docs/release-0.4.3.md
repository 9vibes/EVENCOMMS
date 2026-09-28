# EVENCOMMS 0.4.3

The installed Even companion was observed at `http://127.0.0.1:63233`.
The server rejected its health preflight with `Disallowed CORS origin` because
that origin was absent from the exact allowlist.

`ALLOW_EVEN_LOCALHOST=true` permits `http://127.0.0.1:<port>` with a valid
explicit port (1–65535) for both HTTP CORS and wearer WebSockets. It defaults
to false in source deployments. It supports changed ports without assuming
that the phone actually changes them; verify on the target device.
Other hosts, HTTPS loopback, null origins, paths and malformed ports remain
rejected unless separately allowed by the existing exact-origin configuration.
This option trusts any app served at that loopback address, not just Even.
Pairing codes, bearer authentication and WebSocket query rejection remain required.

Keep public/operator origins in `ALLOWED_ORIGINS`. The companion's network
whitelist must separately permit the server. An existing server can instead
allow the exact observed origin without upgrading, but it must be updated if
the port changes. No wildcard CORS is needed.

## Upgrade

Update the app and bridge images together in place. Preserve the database,
model cache, settings, app password and private service token. Do not uninstall.
No new volumes, published ports or services are added. Codex stays pinned to
0.157.1; the 0.4.2 reply-compatibility behavior is retained.

The release workflow checks both exact candidate images before publishing,
including upgrades from 0.4.2 and earlier supported versions. The separate
KNS-Umbrel store must pin the resulting image digests before offering the update.
After installing, enable `ALLOW_EVEN_LOCALHOST` if the package has not enabled it,
and check health, pairing and message delivery in the installed Even app.
Physical G2 behavior and actual phone port changes require device verification.
