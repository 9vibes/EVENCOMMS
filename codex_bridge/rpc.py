"""Bounded private stdio RPC. No browser transport, inherited secrets or stderr."""

import asyncio
import contextlib
import json
import os
from pathlib import Path
import re
import signal
import tempfile

from .policy import VERSION, config_args, configuration
from .relay import Relay


WIRE_LIMIT = 10 * 1024 * 1024
RPC_TIMEOUT = 15


class ProtocolError(Exception):
    def __init__(self):
        super().__init__("Codex runtime protocol failure")


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
        self.relay = Relay(probe_origin=probe_upstream)
        self.directory = None
        self.process = None
        self.reader = None
        self.pending = {}
        self.sequence = 0
        self.failed = False
        self.closing = False
        self.write_lock = asyncio.Lock()
        self.close_lock = asyncio.Lock()
        self.event_count = 0
        self.failure_reason = None

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
                "clientInfo": {"name": "evencomms_codex_bridge", "title": "EVENCOMMS Research Prototype", "version": "0.4.1"},
                "capabilities": {"experimentalApi": True},
            })
            if not isinstance(result, dict) or VERSION not in result.get("userAgent", ""):
                raise ProtocolError()
            await self.send({"method": "initialized", "params": {}})
            return result
        except BaseException:
            await self.close(logout=False)
            raise

    async def send(self, message):
        if self.failed or not self.process or self.process.returncode is not None:
            raise ProtocolError()
        data = json.dumps(message, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode() + b"\n"
        if len(data) > WIRE_LIMIT:
            self.abort()
            raise ProtocolError()
        try:
            async with asyncio.timeout(3), self.write_lock:
                self.process.stdin.write(data)
                await self.process.stdin.drain()
        except (OSError, TimeoutError):
            self.abort()
            raise ProtocolError() from None

    async def call(self, method, params=None, *, timeout=RPC_TIMEOUT):
        if len(self.pending) >= 4 or self.failed:
            raise ProtocolError()
        self.sequence += 1
        identity = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[identity] = future
        try:
            async with asyncio.timeout(timeout):
                await self.send({"id": identity, "method": method, "params": params or {}})
                return await future
        except TimeoutError:
            self.abort()
            raise ProtocolError() from None
        finally:
            self.pending.pop(identity, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()

    def abort(self):
        self.failed = True
        self.relay.disarm()
        if self.process and self.process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGKILL)
        for future in self.pending.values():
            if not future.done():
                future.set_exception(ProtocolError())

    async def read_loop(self):
        try:
            while True:
                line = await self.process.stdout.readline()
                if not line or len(line) > WIRE_LIMIT or not line.endswith(b"\n"):
                    self.failure_reason = "invalid_frame_or_eof"
                    raise ProtocolError()
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
                        await self.send({"id": message["id"], "error": {"code": -32601, "message": "Not permitted"}})
                        raise ProtocolError()
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
                    if future.done() or "error" in message or "result" not in message:
                        self.failure_reason = "invalid_rpc_response"
                        raise ProtocolError()
                    future.set_result(message["result"])
        except asyncio.CancelledError:
            raise
        except Exception:
            self.abort()
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
