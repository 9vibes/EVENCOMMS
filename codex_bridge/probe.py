"""Offline protocol proof, shared by the command-line probe and startup gate.

Only loopback Responses and synthetic OAuth fixtures are used. This never
contacts a real login issuer, imports credentials, or sends live inference.
"""

import asyncio
import base64
import contextlib
import hashlib
import io
import json
from pathlib import Path
import re
import tempfile
from uuid import uuid4
from urllib.parse import parse_qs
from urllib.parse import urlsplit

from PIL import Image

from .generation import Generation
from .policy import (
    AUTH_RECOVERY_PHASES, MODEL_PROOF_FIELDS, MODELS, REQUIRED_PROOFS, USER_CODE_PATTERN, VERSION,
    generation_allowed, history_items,
)
from .rpc import Runtime, ProtocolError, WIRE_LIMIT, child_environment
from .validation import Chat


BINARY_SHA256 = {
    "3e2584f3f3829a43a0495011a1cecb2facbe64a2403e2b682351fd9c2983f970",
    "9cbc3cdcc18ca336523ffa7d64207a1ae1f5991f823081d0a37bcb3a748de093",
}


def verify_binary(binary):
    with open(binary, "rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if digest not in BINARY_SHA256:
        raise ProtocolError()
    return digest


async def check_schema(binary, parent):
    with tempfile.TemporaryDirectory(prefix="evencomms-schema-", dir=parent) as directory:
        home = Path(directory)
        (home / "codex").mkdir(mode=0o700)
        process = await asyncio.create_subprocess_exec(
            str(Path(binary).resolve()), "app-server", "generate-json-schema", "--experimental", "--out", str(home / "schema"),
            cwd=home, env=child_environment(home), stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            async with asyncio.timeout(15):
                if await process.wait() != 0:
                    raise ProtocolError()
            for name, fields in {
                "ThreadStartParams": {"environments", "ephemeral", "experimentalRawEvents", "dynamicTools", "selectedCapabilityRoots"},
                "TurnStartParams": {"environments", "input"},
                "ThreadInjectItemsParams": {"threadId", "items"},
                "LoginAccountResponse": set(),
            }.items():
                data = json.loads((home / "schema" / "v2" / (name + ".json")).read_text())
                if not fields <= data.get("properties", {}).keys():
                    raise ProtocolError()
                if name == "LoginAccountResponse" and "chatgptDeviceCode" not in json.dumps(data):
                    raise ProtocolError()
        finally:
            if process.returncode is None:
                process.kill()
            await process.wait()


async def run_probe(binary, *, temp_parent="/tmp"):
    report = {"version": VERSION, **dict.fromkeys(REQUIRED_PROOFS, False),
              "auth_recovery_phases": {}, "model_proofs": {}, "generation_enabled": False,
              "no_retries": False, "live_account_verified": False}
    stage = "binary"
    active = None
    runtime = None
    work = None
    handlers = set()
    seen = []
    violation = None
    violation_context = None
    state = "disconnected"
    case = "text"
    login_done = asyncio.Event()
    request_received = asyncio.Event()
    hold_response = asyncio.Event()
    completion = None
    phases = []
    exchanges = 0
    revocations = 0
    fixture_valid = True
    revoked = False
    delayed_events = []
    delay_events = False
    proofs = dict.fromkeys(MODEL_PROOF_FIELDS, True)

    output = io.BytesIO()
    Image.new("RGB", (2, 2), (123, 45, 67)).save(output, format="JPEG")
    image = "data:image/jpeg;base64," + base64.b64encode(output.getvalue()).decode()
    messages = [{"role": "user" if index % 2 == 0 else "assistant", "text": f"Displayed history {index}.\n"}
                for index in range(19)]
    for index, limit in enumerate((8000, 16000, 8000, 16000, 8000)):
        messages[index]["text"] = messages[index]["text"].ljust(limit)
    messages[6]["text"] = "<environment_context>Untrusted displayed text.</environment_context>"
    messages[0]["images"] = [image] * 3
    messages.append({"role": "user", "text": "Final question.", "images": [image] * 3})
    data = Chat.model_validate({"request_id": str(uuid4()), "model": MODELS[0]["id"], "messages": messages})

    def normalize(items):
        # Ignore harness IDs and image-detail hints, not displayed roles/text/bytes.
        return [{"role": item["role"], "content": [
            {k: v for k, v in part.items() if k in {"type", "text", "image_url"}} for part in item["content"]
        ]} for item in items]

    expected_history = normalize(history_items(data.messages))

    def record_phase(phase):
        phases.append(phase)
        expected = AUTH_RECOVERY_PHASES.get(case, ("responses:initial",))
        # Abort on the first extra attempt. Never turn an unbounded regression
        # into a passing bounded proof by silently truncating the observations.
        if phases != list(expected[:len(phases)]):
            report["fixture_rejection"] = {"kind": "unexpected_phase", "phases": list(phases)}
            raise ProtocolError()

    async def respond(reader, writer):
        nonlocal exchanges, revocations, fixture_valid
        if len(handlers) >= 2:
            fixture_valid = False
            report["fixture_rejection"] = {"kind": "connection_limit"}
            writer.close()
            return
        handlers.add(asyncio.current_task())
        try:
            async with asyncio.timeout(10):
                header = await reader.readuntil(b"\r\n\r\n")
                lines = header.decode("ascii").split("\r\n")
                headers = {key.lower(): value.strip() for key, value in
                           (line.split(":", 1) for line in lines[1:] if ":" in line)}
                size = int(headers.get("content-length", "0"))
                if not 0 <= size <= WIRE_LIMIT:
                    raise ProtocolError()
                raw = await reader.readexactly(size)
                method, path, _ = lines[0].split(" ")
                path = path.split("?", 1)[0]
                if path != "/responses":
                    status = b"200 OK"
                    if method == "POST" and path == "/api/accounts/deviceauth/usercode":
                        value = {"device_auth_id": "offline-device", "user_code": "cOdE-12345", "interval": "0"}
                    elif method == "POST" and path == "/api/accounts/deviceauth/token":
                        value = {"authorization_code": "offline-code", "code_challenge": "offline", "code_verifier": "offline"}
                    elif method == "POST" and path == "/oauth/token":
                        grant = (json.loads(raw) if headers.get("content-type", "").startswith("application/json")
                                 else {key: values[0] for key, values in parse_qs(raw.decode()).items()})
                        if grant.get("grant_type") == "authorization_code":
                            exchanges += 1
                            if exchanges != 1 or phases:
                                raise ProtocolError()
                            claims = {"email": "offline@example.invalid", "https://api.openai.com/auth": {
                                "chatgpt_user_id": "offline-user", "chatgpt_account_id": "offline-account", "chatgpt_plan_type": "pro"}}
                            token = "e30." + base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=") + ".offline"
                            value = {"id_token": token, "access_token": "offline-access", "refresh_token": "offline-refresh"}
                        elif grant.get("grant_type") == "refresh_token" and grant.get("refresh_token") == "offline-refresh":
                            record_phase("oauth:refresh")
                            if case == "authenticated_401":
                                status, value = b"400 Bad Request", {"error": "invalid_grant"}
                            elif case in {"auth_refresh_success", "auth_refresh_exhausted"}:
                                value = {"access_token": "offline-refreshed-access", "refresh_token": "offline-refreshed-refresh"}
                            else:
                                raise ProtocolError()
                        else:
                            raise ProtocolError()
                    elif method == "GET" and path.endswith("/accounts/check"):
                        value = {"accounts": [{"id": "offline-account", "workspace_backend_origin": origin.replace("http:", "https:"),
                                              "account_routing_override": "NO_CONSTRAINT"}]}
                    elif method == "POST" and path == "/oauth/revoke":
                        revocations += 1
                        if case != "revoke" or revocations > 2:
                            raise ProtocolError()
                        value = {}
                    elif method == "GET" and path.endswith(("/config/bundle", "/settings/user")):
                        value = {}
                    else:
                        raise ProtocolError()
                    body = json.dumps(value).encode()
                    writer.write(b"HTTP/1.1 " + status + b"\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: "
                                 + str(len(body)).encode() + b"\r\n\r\n" + body)
                    await writer.drain()
                    return
                if method != "POST" or path != "/responses" or not size:
                    raise ProtocolError()
                auth = headers.get("authorization")
                if auth not in {"Bearer offline-access", "Bearer offline-refreshed-access"}:
                    raise ProtocolError()
                record_phase("responses:initial" if auth == "Bearer offline-access" else "responses:refreshed")
                request = json.loads(raw)
                if request.get("model") != data.model or state != "connected":
                    raise ProtocolError()
                items = [item for item in request.get("input", []) if item.get("role") in {"user", "assistant"}]
                proofs["tools_empty"] &= request.get("tools") == []
                proofs["history_roles_exact"] &= normalize(items) == expected_history
                proofs["images_verified"] &= sum(part.get("type") == "input_image" and part.get("image_url") == image
                                                for item in items for part in item["content"]) == 6
                report.update(proofs)
                if not all(proofs.values()):
                    raise ProtocolError()
                request_received.set()
                attempts = sum(phase.startswith("responses:") for phase in phases)
                if (case in {"authenticated_401", "auth_refresh_exhausted"}
                        or case == "auth_reload_success" and attempts == 1
                        or case == "auth_refresh_success" and attempts <= 2):
                    writer.write(b"HTTP/1.1 401 Unauthorized\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                elif case == "http_error":
                    writer.write(b"HTTP/1.1 500 Internal Server Error\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                else:
                    if case == "revoke":
                        await hold_response.wait()
                    item = ({"type": "message", "id": "msg_probe", "role": "assistant", "phase": "final_answer",
                             "content": [{"type": "output_text", "text": "Offline probe answer."}]}
                            if case != "tool" else
                            {"type": "function_call", "id": "fc_probe", "call_id": "call_probe",
                             "name": "exec_command", "arguments": '{"cmd":"false"}'})
                    events = [
                        {"type": "response.created", "response": {"id": "resp_probe"}},
                        {"type": "response.output_item.added", "output_index": 0, "item": item},
                        {"type": "response.output_item.done", "output_index": 0, "item": item},
                        {"type": "response.completed", "response": {"id": "resp_probe", "status": "completed",
                         "output": [item], "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}}},
                    ]
                    body = "".join("data: " + json.dumps(event) + "\n\n" for event in events).encode()
                    writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\nContent-Length: "
                                 + str(len(body)).encode() + b"\r\n\r\n" + body)
                await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError, asyncio.CancelledError):
            pass
        except Exception:
            fixture_valid = False
            if active:
                active.fail()
            if runtime:
                runtime.abort()
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            handlers.discard(asyncio.current_task())

    def event(method, params):
        nonlocal violation, violation_context, completion, state, revoked
        if delay_events and method != "bridge/failed":
            if len(delayed_events) >= 64:
                raise ProtocolError()
            delayed_events.append((method, params))
            return
        if method not in seen and len(seen) < 64:
            seen.append(method)
        if method == "bridge/failed":
            state = "failed"
            login_done.set()
            if active:
                active.fail()
            return
        if method == "account/login/completed":
            if completion is not None or state != "pending":
                raise ProtocolError()
            completion = (params.get("loginId"), params.get("success"))
            login_done.set()
            return
        if method == "account/updated":
            # Match the production session guard, rather than ignoring account
            # events during successful refresh or explicit revocation.
            if state == "connected" and params.get("authMode") != "chatgpt":
                violation, state, revoked = method, "failed", True
                if active:
                    active.fail()
                raise ProtocolError()
            return
        if active is not None:
            try:
                active.event(method, params)
            except Exception:
                violation = method
                # IDs, message contents, image data and provider errors never
                # enter diagnostics. These labels distinguish ordering failures.
                item = params.get("item", {})
                if not isinstance(item, dict):
                    item = {}
                violation_context = {
                    "thread_matches": params.get("threadId", active.thread_id) == active.thread_id,
                    "turn_scope": ("replay" if params.get("turnId") == "auto-compact-0" else
                                   "current" if params.get("turnId") == active.turn_id else "unexpected"),
                    "item_type": item.get("type") if item.get("type") in ("message", "reasoning", "function_call") else "other",
                    "role": item.get("role") if item.get("role") in ("user", "assistant", "developer", "system") else "other",
                    "replay_remaining": len(active.replay or ()),
                }
                active.fail()
                raise

    async def check_stale_path(url):
        # This deliberately has no credential. Path rejection must happen before
        # body/auth validation and must leave the new send's budget untouched.
        parsed = urlsplit(url)
        reader, writer = await asyncio.open_connection("127.0.0.1", parsed.port)
        try:
            writer.write((f"POST {parsed.path}/responses HTTP/1.1\r\nHost: {parsed.netloc}\r\n"
                          "Content-Length: 2\r\n\r\n{}").encode())
            await writer.drain()
            async with asyncio.timeout(3):
                head = await reader.readuntil(b"\r\n\r\n")
            if not head.startswith(b"HTTP/1.1 403 "):
                raise ProtocolError()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(respond, "127.0.0.1", 0, limit=65536)
    port = server.sockets[0].getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    local_config = {
        "chatgpt_base_url": f"http://127.0.0.1:{port}",
        "openai_base_url": f"http://127.0.0.1:{port}",
    }
    try:
        await asyncio.to_thread(verify_binary, binary)
        report["binary_verified"] = True
        stage = "schema"
        await check_schema(binary, temp_parent)
        report["schema_verified"] = True
        cases = [("text", row["id"]) for row in MODELS]
        cases.extend((case, MODELS[0]["id"]) for case in (*AUTH_RECOVERY_PHASES, "http_error", "tool", "revoke"))
        for case, model in cases:
            data = data.model_copy(update={"model": model, "request_id": uuid4()})
            stage = case + "/" + model + "/login"
            state, violation, violation_context, completion, revoked = "pending", None, None, None, False
            phases, exchanges, revocations = [], 0, 0
            login_done.clear()
            request_received.clear()
            runtime = Runtime(binary, event, temp_parent=temp_parent, probe_origin=origin,
                              probe_upstream=origin, probe_config=local_config)
            await runtime.start()
            directory = Path(runtime.directory.name)
            account = await runtime.call("account/read", {"refreshToken": False})
            if account.get("account") is not None or account.get("requiresOpenaiAuth") is not True:
                raise ProtocolError()
            report["account_disconnected"] = report["authenticated_provider"] = True
            login = await runtime.call("account/login/start", {"type": "chatgptDeviceCode"})
            if (login.get("type") != "chatgptDeviceCode" or login.get("verificationUrl") != origin + "/codex/device"
                    or login.get("userCode") != "cOdE-12345" or not re.fullmatch(USER_CODE_PATTERN, login["userCode"])):
                raise ProtocolError()
            async with asyncio.timeout(15):
                await login_done.wait()
            account = await runtime.call("account/read", {"refreshToken": False})
            if (completion != (login.get("loginId"), True) or account.get("account", {}).get("type") != "chatgpt"
                    or account.get("requiresOpenaiAuth") is not True or phases or exchanges != 1):
                raise ProtocolError()
            state = "connected"
            report["synthetic_device_login"] = True
            if list(directory.rglob("auth.json")):
                raise ProtocolError()
            stage = case + "/" + model + "/catalog"
            catalog = await runtime.call("model/list", {"includeHidden": True, "limit": 100})
            selectors = [{"id": row["id"], "image": "image" in row["inputModalities"]} for row in catalog["data"]]
            # Codex sorts by upstream priority, independently of bridge order.
            if (sorted(selectors, key=lambda row: row["id"]) != sorted(MODELS, key=lambda row: row["id"])
                    or catalog.get("nextCursor") is not None or phases):
                raise ProtocolError()
            report["catalog_for_alias"] = True
            stage = case + "/" + model + "/turn"
            active = Generation()
            delay_events = case == "tool"
            delayed_events.clear()
            work = asyncio.create_task(active.run(runtime, data))
            result = None
            async with asyncio.timeout(20):
                if case == "tool":
                    # Adversarial transport scheduling, not production behavior:
                    # let the native runtime attempt its follow-up while the
                    # stdio notification consumer is delayed. Only the relay may
                    # stop that request before it reaches this mock upstream.
                    async with asyncio.timeout(5):
                        while runtime.relay.blocked_requests == 0:
                            if work.done():
                                raise ProtocolError()
                            await asyncio.sleep(0.01)
                    await asyncio.sleep(0.1)
                    delay_events = False
                    for method, params in delayed_events:
                        try:
                            event(method, params)
                        except ProtocolError:
                            runtime.abort()
                            event("bridge/failed", {})
                            break
                    delayed_events.clear()
                if case == "revoke":
                    await request_received.wait()
                    with contextlib.suppress(ProtocolError):
                        await runtime.call("account/logout", timeout=3)
                try:
                    result = await work
                except ProtocolError:
                    pass
            success = case in {"text", "auth_reload_success", "auth_refresh_success"}
            if success:
                if result is None or result["text"] != "Offline probe answer." or state != "connected":
                    raise ProtocolError()
                account = await runtime.call("account/read", {"refreshToken": False})
                if account.get("account", {}).get("type") != "chatgpt" or state != "connected":
                    raise ProtocolError()
                if case != "text":
                    report[case] = True
                if case == "auth_refresh_success":
                    report["auth_refresh_preserves_login"] = True
            elif result is not None or violation not in {
                "tool": {"rawResponseItem/completed"}, "revoke": {"account/updated", "error"},
            }.get(case, {"error", "account/updated"}):
                raise ProtocolError()
            # Let already-enqueued events/HTTP attempts surface before disposal.
            await asyncio.sleep(0.05)
            expected = AUTH_RECOVERY_PHASES.get(case, ("responses:initial",))
            if phases != list(expected) or not fixture_valid or list(directory.rglob("auth.json")):
                raise ProtocolError()
            count = sum(p.startswith("responses:") for p in phases)
            if (active.budget is None or active.budget.attempts != count or runtime.relay.forwarded_requests != count
                    or active.budget.non_401 > 1 or active.budget.armed or runtime.relay.current is not None):
                raise ProtocolError()
            report["relay_used"] = True
            report["credentials_ephemeral"] = True
            if case == "text":
                report["model_proofs"][model] = {**proofs, "requests": 1}
            if case in AUTH_RECOVERY_PHASES:
                report["auth_recovery_phases"][case] = list(phases)
                report[case + "_requests"] = sum(p.startswith("responses:") for p in phases)
                report[case + "_refreshes"] = phases.count("oauth:refresh")
            if case == "auth_refresh_exhausted":
                report[case] = True
            elif case == "http_error":
                report["transport_500_no_retry"] = True
            elif case == "tool":
                report["tool_call_rejected"] = runtime.failed
                report["tool_upstream_requests"] = runtime.relay.forwarded_requests
                report["tool_upstream_non_401"] = runtime.relay.forwarded_non_401
                report["tool_downstream_requests"] = runtime.relay.downstream_requests
                report["tool_blocked_requests"] = runtime.relay.blocked_requests
                report["tool_continuation_guarded"] = (runtime.relay.forwarded_requests == 1
                    and runtime.relay.forwarded_non_401 == 1 and runtime.relay.downstream_requests >= 2
                    and runtime.relay.blocked_requests >= 1)
            elif case == "revoke":
                # Logout can invalidate the active turn's network policy before
                # account/updated arrives. Both events must stop generation.
                report["account_revocation_rejected"] = 1 <= revocations <= 2 and runtime.failed and state == "failed"
                report["revocation_event"] = violation
                report["non_chatgpt_event_observed"] = revoked
            if case == "text" and model == MODELS[0]["id"]:
                previous_url = active.budget.base_url
                original_call = runtime.call

                async def isolated_call(method, params=None, **kwargs):
                    if method == "turn/start":
                        current = runtime.relay.current
                        if current is None or current.base_url == previous_url:
                            raise ProtocolError()
                        await check_stale_path(previous_url)
                        if current.attempts or current.consumed:
                            raise ProtocolError()
                    return await original_call(method, params, **kwargs)

                runtime.call = isolated_call
                phases = []
                active = Generation()
                try:
                    async with asyncio.timeout(20):
                        second = await active.run(runtime, data.model_copy(update={"request_id": uuid4()}))
                    if (second["text"] != "Offline probe answer." or phases != ["responses:initial"]
                            or active.budget.attempts != 1 or active.budget.non_401 != 1
                            or runtime.relay.forwarded_requests != 2):
                        raise ProtocolError()
                    report["relay_paths_isolated"] = True
                finally:
                    runtime.call = original_call
            active, state = None, "closing"
            await runtime.close(logout=False)
            for task in tuple(handlers):
                task.cancel()
            await asyncio.gather(*handlers, return_exceptions=True)
            if directory.exists() or runtime.pending or runtime.process.returncode is None:
                raise ProtocolError()
        report.update(proofs)
        report["no_paid_api_fallback"] = report["fixture_valid"] = fixture_valid
        report["auth_recovery_bounded"] = report["cleanup_verified"] = True
    except Exception:
        report["failed_stage"] = stage
        if violation:
            report["rejected_event"] = violation
            if violation_context:
                report["rejected_event_context"] = violation_context
        if runtime and runtime.failure_reason:
            report["rpc_failure"] = runtime.failure_reason
    finally:
        active = None
        report["fixture_valid"] = fixture_valid
        if case in AUTH_RECOVERY_PHASES:
            report["auth_recovery_phases"][case] = list(phases)
            report[case + "_requests"] = sum(p.startswith("responses:") for p in phases)
            report[case + "_refreshes"] = phases.count("oauth:refresh")
        if work and not work.done():
            work.cancel()
        if work:
            await asyncio.gather(work, return_exceptions=True)
        if runtime:
            await runtime.close(logout=False)
        server.close()
        await server.wait_closed()
        for task in tuple(handlers):
            task.cancel()
        await asyncio.gather(*handlers, return_exceptions=True)
    report["generation_enabled"] = generation_allowed(report)
    report["observed_events"] = seen
    return report
