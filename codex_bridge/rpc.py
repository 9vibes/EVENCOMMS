"""Bounded private stdio RPC. No browser transport, inherited secrets or stderr."""

import asyncio
import contextlib
import json
import os
from pathlib import Path
import re
import signal
import tempfile

from .errors import ERRORS, Failure
from .policy import VERSION, config_args, configuration
from .relay import Relay


WIRE_LIMIT = 10 * 1024 * 1024
RPC_TIMEOUT = 15


class ProtocolError(Failure):
    def __init__(self, code="protocol_mismatch"):
        super().__init__(code)


def child_environment(home):
    # Construct from scratch. In particular, never copy PATH, proxy variables,
    # OPENAI_API_KEY, CODEX_BRIDGE_TOKEN, telemetry or application credentials.
    return {
        "PATH": "/usr/bin:/bin", "HOME": str(home), "CODEX_HOME": str(home / "codex"),
        "TMPDIR": str(home), "XDG_CONFIG_HOME": str(home / "config"),
        "XDG_CACHE_HOME": str(home / "cache"), "XDG_DATA_HOME": str(home / "data"),
        "RUST_LOG": "off", "RUST_BACKTRACE": "0", "LANG": "C.UTF-8", "TZ": "UTC",
        "TOKIO_WORKER_THREADS": "2", "RAYON_NUM_THREADS": "2",
    }


class Runtime:
    def __init__(self, binary, on_event, *, temp_parent="/tmp", probe_config=None, probe_origin=None, probe_upstream=None):
        self.binary = str(Path(binary).resolve())
        self.on_event = on_event
        self.temp_parent = temp_parent
        self.probe_config = probe_config
        self.probe_origin = probe_origin
        self.probe_upstream = probe_upstream
        self.relay = Relay(check_workspace=self.check_workspace, probe_origin=probe_upstream)
        self.directory = None
        self.process = None
        self.reader = None
        self.pending = {}
        self.sequence = 0
        self.workspace_request = None
        self.failed = False
        self.closing = False
        self.write_lock = asyncio.Lock()
        self.close_lock = asyncio.Lock()
        self.event_count = 0
        self.failure_reason = None
        self.failure = None

    async def start(self):
        # Container has no system/project config. Reject any accidental host
        # configuration instead of trying to override an inherited MCP table.
        parent = Path(self.temp_parent).resolve()
        if Path("/etc/codex").exists() or any((p / ".codex").exists() for p in (parent, *parent.parents)):
            raise ProtocolError()
        if self.probe_origin is not None and not re.fullmatch(r"http://127\.0\.0\.1:[0-9]{1,5}", self.probe_origin):
            raise ProtocolError()
        if self.probe_origin is not None and self.probe_upstream != self.probe_origin:
            raise ProtocolError()
        self.directory = tempfile.TemporaryDirectory(prefix="evencomms-codex-", dir=parent)
        home = Path(self.directory.name)
        (home / "codex").mkdir(mode=0o700)
        (home / "work").mkdir(mode=0o700)
        env = child_environment(home)
        if self.probe_origin is not None:
            # Test-only synthetic OAuth endpoints. Never taken from process
            # environment or from the private HTTP contract.
            env.update({
                "CODEX_APP_SERVER_LOGIN_ISSUER": self.probe_origin,
                "CODEX_REFRESH_TOKEN_URL_OVERRIDE": self.probe_origin + "/oauth/token",
                "CODEX_REVOKE_TOKEN_URL_OVERRIDE": self.probe_origin + "/oauth/revoke",
            })
        try:
            await self.relay.start()
            settings = configuration(self.relay.base_url + "/unarmed")
            if self.probe_config is not None:
                if set(self.probe_config) - {"chatgpt_base_url", "openai_base_url"}:
                    raise ProtocolError()
                settings.update(self.probe_config)
            spawn = asyncio.create_task(asyncio.create_subprocess_exec(
                self.binary, "app-server", "--stdio", "--strict-config", *config_args(settings),
                cwd=home / "work", env=env,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, limit=WIRE_LIMIT + 1, start_new_session=True,
            ))
            try:
                self.process = await asyncio.shield(spawn)
            except asyncio.CancelledError:
                # Cancellation while fork/exec is in flight must still acquire
                # the child handle so close() can kill and reap it.
                self.process = await spawn
                raise
            self.reader = asyncio.create_task(self.read_loop())
            result = await self.call("initialize", {
                "clientInfo": {"name": "evencomms_codex_bridge", "title": "EVENCOMMS Research Prototype", "version": "0.4.2"},
                "capabilities": {"experimentalApi": True},
            })
            if not isinstance(result, dict) or VERSION not in result.get("userAgent", ""):
                raise ProtocolError()
            await self.send({"method": "initialized", "params": {}})
            return result
        except BaseException:
            with contextlib.suppress(Exception):
                async with asyncio.timeout(5):
                    await self.close(logout=False)
            raise

    async def send(self, message):
        if self.failed or not self.process or self.process.returncode is not None:
            raise ProtocolError(self.failure or "runtime_error")
        data = json.dumps(message, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode() + b"\n"
        if len(data) > WIRE_LIMIT:
            self.abort(ProtocolError())
            raise ProtocolError()
        try:
            async with asyncio.timeout(3), self.write_lock:
                self.process.stdin.write(data)
                await self.process.stdin.drain()
        except (OSError, TimeoutError) as error:
            self.abort(ProtocolError("timeout" if isinstance(error, TimeoutError) else "runtime_error"))
            raise ProtocolError(self.failure) from None

    async def check_workspace(self, account_id):
        try:
            result = await self.call("account/read", {"refreshToken": False}, _workspace_check=True)
        except Exception as error:
            raise Failure("account_auth" if isinstance(error, Failure) and error.code == "account_auth"
                          else "unsupported_workspace") from None
        if not isinstance(result, dict):
            raise Failure("unsupported_workspace")
        account = result.get("account")
        if (not isinstance(account, dict) or account.get("type") != "chatgpt"
                or result.get("requiresOpenaiAuth") is not True):
            raise Failure("account_auth")
        routing = result.get("workspaceRouting")
        if (not isinstance(routing, dict)
                or set(routing) != {"chatgptAccountId", "backendOrigin", "accountRoutingOverride"}
                or not isinstance(routing.get("chatgptAccountId"), str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", routing["chatgptAccountId"])):
            raise Failure("unsupported_workspace")
        if routing["chatgptAccountId"] != account_id:
            raise Failure("account_auth")
        # Never derive production destinations from config, discovery or headers.
        # The only extra origin is the already-validated internal loopback fixture.
        origin = routing["backendOrigin"]
        if (routing["accountRoutingOverride"] != "NO_CONSTRAINT"
                or origin != "https://chatgpt.com" and (
                    self.probe_origin is None or origin != self.probe_origin.replace("http:", "https:", 1))):
            raise Failure("unsupported_workspace")
        return True

    async def call(self, method, params=None, *, timeout=RPC_TIMEOUT, _workspace_check=False):
        if self.failed:
            raise ProtocolError(self.failure or "runtime_error")
        if len(self.pending) >= 4:
            raise ProtocolError()
        self.sequence += 1
        identity = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[identity] = future
        if _workspace_check:
            self.workspace_request = identity
        try:
            async with asyncio.timeout(timeout):
                await self.send({"id": identity, "method": method, "params": params or {}})
                return await future
        except TimeoutError:
            self.abort(ProtocolError("unsupported_workspace" if _workspace_check else "timeout"))
            raise ProtocolError(self.failure) from None
        finally:
            if self.workspace_request == identity:
                self.workspace_request = None
            self.pending.pop(identity, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()

    def abort(self, error=None):
        if self.failure is None:
            upstream = getattr(self.relay.current, "failure", None)
            self.failure = (upstream if isinstance(upstream, str) and upstream in ERRORS
                            else error.code if isinstance(error, Failure) else "runtime_error")
        self.failed = True
        self.relay.disarm()
        if self.process and self.process.returncode is None:
            # close() still has to reap before releasing the reservation. A kill
            # error must not prevent pending callers receiving the original cause.
            with contextlib.suppress(OSError):
                os.killpg(self.process.pid, signal.SIGKILL)
        for future in self.pending.values():
            if not future.done():
                future.set_exception(ProtocolError(self.failure))

    async def read_loop(self):
        try:
            while True:
                line = await self.process.stdout.readline()
                if not line or len(line) > WIRE_LIMIT or not line.endswith(b"\n"):
                    self.failure_reason = "invalid_frame_or_eof"
                    raise ProtocolError("protocol_mismatch" if line else "runtime_error")
                message = json.loads(line)
                if not isinstance(message, dict):
                    raise ProtocolError()
                if "method" in message:
                    if (not isinstance(message["method"], str) or len(message["method"]) > 128
                            or not isinstance(message.get("params", {}), dict)):
                        raise ProtocolError()
                    if "id" in message:
                        if not (type(message["id"]) is int or isinstance(message["id"], str) and len(message["id"]) <= 128):
                            raise ProtocolError()
                        # Deny every server request, including future approval types.
                        self.failure_reason = "server_request_denied"
                        self.failure = self.failure or "tool_rejected"
                        await self.send({"id": message["id"], "error": {"code": -32601, "message": "Not permitted"}})
                        raise ProtocolError("tool_rejected")
                    self.event_count += 1
                    if self.event_count > 32768:
                        raise ProtocolError()
                    self.on_event(message["method"], message.get("params", {}))
                else:
                    identity = message.get("id")
                    if type(identity) is not int or identity not in self.pending:
                        self.failure_reason = "unknown_response_id"
                        raise ProtocolError()
                    future = self.pending[identity]
                    if future.done() or ("error" in message) == ("result" in message):
                        self.failure_reason = "invalid_rpc_response"
                        raise ProtocolError()
                    if "error" in message:
                        error = message["error"]
                        if (not isinstance(error, dict) or type(error.get("code")) is not int
                                or not isinstance(error.get("message"), str)):
                            self.failure_reason = "invalid_rpc_response"
                            raise ProtocolError()
                        if identity == self.workspace_request:
                            # Native discovery errors contain private routing text.
                            # Let the relay fail this claimed send with a fixed label.
                            future.set_exception(Failure("unsupported_workspace"))
                            continue
                        self.failure_reason = "native_rpc_error"
                        # JSON-RPC codes describe the request contract, not account
                        # authorization. Do not inspect provider text or error.data.
                        raise ProtocolError("protocol_mismatch" if error["code"] in {-32600, -32601, -32602}
                                            else "runtime_error")
                    future.set_result(message["result"])
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.abort(error if isinstance(error, Failure) else ProtocolError())
            if not self.closing:
                with contextlib.suppress(Exception):
                    self.on_event("bridge/failed", {})

    async def close(self, *, logout=True):
        async with self.close_lock:
            self.closing = True
            if logout and self.process and not self.failed and self.process.returncode is None:
                with contextlib.suppress(Exception):
                    await self.call("account/logout", timeout=1)
            self.abort()
            if self.reader and self.reader is not asyncio.current_task():
                self.reader.cancel()
                await asyncio.gather(self.reader, return_exceptions=True)
            if self.process:
                self.process.stdin.close()
                # A paused full stdout pipe can keep Process.wait() blocked even
                # after SIGKILL. Drain after stopping the sole reader, in chunks.
                while await self.process.stdout.read(65536):
                    pass
                await self.process.wait()
            await self.relay.close()
            if self.directory:
                self.directory.cleanup()
                self.directory = None
