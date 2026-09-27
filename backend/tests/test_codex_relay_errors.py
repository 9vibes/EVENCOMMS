"""Strict pinned Lite wire and sanitized relay failures; no live credentials."""

import asyncio
import json
import logging

import httpx
import pytest

from codex_bridge.errors import ERROR_HEADER, Failure
from codex_bridge.policy import MODELS
from codex_bridge.relay import ERROR_BODY_LIMIT, Relay, decode_request
from codex_bridge.rpc import Runtime
from backend.tests.test_codex_bridge import RelayStream, allow_fixture_workspace, inference_payload, jpeg, relay_request


PRIVATE = "private-account-provider-token"


@pytest.mark.parametrize("verifier", ["absent", "false", "none", "error"])
def test_relay_cannot_forward_without_positive_workspace_verification(verifier):
    async def run():
        calls, checks = [], []

        async def check(account_id):
            checks.append(account_id)
            if verifier == "error":
                raise RuntimeError(PRIVATE)
            return False if verifier == "false" else None

        relay = Relay(check_workspace=None if verifier == "absent" else check,
                      transport=httpx.MockTransport(lambda request: calls.append(request) or httpx.Response(500)))
        await relay.start()
        try:
            budget = relay.arm(MODELS[0]["id"])
            relay.bind(budget, "thread-one")
            status, headers, body = await relay_request(relay, budget)
            assert status == 422 and budget.failure == "unsupported_workspace"
            assert not calls and budget.attempts == 0 and budget.upstream_status is None and budget.consumed
            assert checks == ([] if verifier == "absent" else ["fixture-account"])
            assert PRIVATE.encode() not in headers + body
        finally:
            await relay.close()

    asyncio.run(run())


@pytest.mark.parametrize("change,code", [
    ({}, None),
    ({"workspaceRouting": None}, "unsupported_workspace"),
    ({"workspaceRouting": []}, "unsupported_workspace"),
    ({"workspaceRouting": {}}, "unsupported_workspace"),
    ({"workspaceRouting": {"backendOrigin": "https://chatgpt.com"}}, "unsupported_workspace"),
    ({"backendOrigin": "https://private-workspace.example.invalid"}, "unsupported_workspace"),
    ({"backendOrigin": "https://chatgpt.com/"}, "unsupported_workspace"),
    ({"backendOrigin": "https://chatgpt.com:444"}, "unsupported_workspace"),
    ({"backendOrigin": "https://chatgpt.com?private"}, "unsupported_workspace"),
    ({"backendOrigin": "NO_CONSTRAINT"}, "unsupported_workspace"),
    ({"backendOrigin": None}, "unsupported_workspace"),
    ({"backendOrigin": ["https://chatgpt.com"]}, "unsupported_workspace"),
    ({"accountRoutingOverride": "us"}, "unsupported_workspace"),
    ({"accountRoutingOverride": "us_cr"}, "unsupported_workspace"),
    ({"accountRoutingOverride": None}, "unsupported_workspace"),
    ({"accountRoutingOverride": "unknown"}, "unsupported_workspace"),
    ({"chatgptAccountId": PRIVATE}, "account_auth"),
    ({"chatgptAccountId": ""}, "unsupported_workspace"),
    ({"chatgptAccountId": "x" * 257}, "unsupported_workspace"),
    ({"chatgptAccountId": None}, "unsupported_workspace"),
    ({"chatgptAccountId": "private\nheader"}, "unsupported_workspace"),
    ({"account": None}, "account_auth"),
    ({"account": {"type": "apiKey"}}, "account_auth"),
    ({"account": {"type": "chatgptAuthTokens"}}, "account_auth"),
    ({"requiresOpenaiAuth": False}, "account_auth"),
    ({"rpc_error": True}, "unsupported_workspace"),
    ({"rpc_auth_error": True}, "account_auth"),
    ({"missing_routing": True}, "unsupported_workspace"),
])
def test_runtime_checks_discovered_workspace_against_outbound_account(change, code, caplog):
    async def run():
        calls, reads = [], []
        runtime = Runtime("/fake/codex", lambda *args: None)
        runtime.relay.transport = httpx.MockTransport(lambda request: calls.append(request) or httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=RelayStream()))

        async def read(method, params, **kwargs):
            reads.append((method, params))
            if "rpc_error" in change:
                raise RuntimeError(PRIVATE)
            if "rpc_auth_error" in change:
                raise Failure("account_auth")
            result = {"account": {"type": "chatgpt", "email": PRIVATE}, "requiresOpenaiAuth": True,
                      "workspaceRouting": {"chatgptAccountId": "fixture-account", "backendOrigin": "https://chatgpt.com",
                                           "accountRoutingOverride": "NO_CONSTRAINT"}}
            for key, value in change.items():
                if key in {"chatgptAccountId", "backendOrigin", "accountRoutingOverride"}:
                    result["workspaceRouting"][key] = value
                elif key == "missing_routing":
                    del result["workspaceRouting"]
                else:
                    result[key] = value
            return result

        runtime.call = read
        relay = runtime.relay
        await relay.start()
        try:
            budget = relay.arm(MODELS[0]["id"])
            relay.bind(budget, "thread-one")
            status, headers, body = await relay_request(relay, budget)
            assert reads == [("account/read", {"refreshToken": False})]
            assert status == (200 if code is None else Failure(code).status)
            assert budget.failure == code and budget.attempts == len(calls) == int(code is None)
            assert budget.consumed and PRIVATE.encode() not in headers + body
        finally:
            await relay.close()

    asyncio.run(run())
    assert PRIVATE not in caplog.text


def test_workspace_check_is_repeated_after_every_401_and_blocks_changed_policy():
    async def run():
        reads, calls = [], []
        runtime = Runtime("/fake/codex", lambda *args: None)

        async def read(method, params, **kwargs):
            reads.append((method, params))
            return {"account": {"type": "chatgpt"}, "requiresOpenaiAuth": True,
                    "workspaceRouting": {"chatgptAccountId": "fixture-account", "backendOrigin": "https://chatgpt.com",
                                         "accountRoutingOverride": "us" if len(reads) == 3 else "NO_CONSTRAINT"}}

        runtime.call = read
        relay = runtime.relay
        relay.transport = httpx.MockTransport(lambda request: calls.append(request) or httpx.Response(401))
        await relay.start()
        try:
            budget = relay.arm(MODELS[0]["id"])
            relay.bind(budget, "thread-one")
            for status in (401, 401, 422):
                assert (await relay_request(relay, budget))[0] == status
            assert len(reads) == 3 and len(calls) == budget.attempts == 2 and budget.non_401 == 0
            assert budget.failure == "unsupported_workspace" and budget.upstream_status == 401 and budget.consumed
            assert (await relay_request(relay, budget))[0] == 409 and len(reads) == 3
        finally:
            await relay.close()

    asyncio.run(run())


def test_workspace_discovery_wait_is_within_overall_deadline():
    async def run():
        calls = []

        async def check(account_id):
            await asyncio.Event().wait()

        relay = Relay(check_workspace=check,
                      transport=httpx.MockTransport(lambda request: calls.append(request) or httpx.Response(500)))
        await relay.start()
        try:
            budget = relay.arm(MODELS[0]["id"])
            relay.bind(budget, "thread-one")
            budget.deadline = asyncio.get_running_loop().time() + 0.03
            assert (await relay_request(relay, budget))[0] == 502
            assert budget.failure == "timeout" and budget.consumed and not calls and budget.attempts == 0
        finally:
            await relay.close()

    asyncio.run(run())


@pytest.mark.parametrize("path,value", [
    (("tools",), []), (("instructions",), ""), (("tool_choice",), "none"),
    (("tool_choice",), {"type": "function", "name": "exec_command"}),
    (("parallel_tool_calls",), True), (("reasoning", "summary"), "auto"),
    (("reasoning", "context"), "current_turn"), (("previous_response_id",), "hidden-history"),
    (("input", 0, "tools"), [{"type": "function", "name": "exec_command"}]),
    (("input", 0, "tools"), [{"type": "namespace", "name": "functions", "tools": []}]),
    (("input", 0, "tools"), None), (("input", 0, "role"), "user"),
    (("input", 0, "id"), None), (("input", 0, "type"), "message"), (("input", 0, "other"), {"tools": []}),
    (("input", 1, "tools"), []),
    (("input", 1, "content", 0, "text"), "Replaced base instructions"),
    (("input", 2, "content", 0, "tools"), []),
    (("input", 2, "content", 0, "type"), "function_call_output"),
    (("input", 2, "internal_chat_message_metadata_passthrough"), {"tools": []}),
    (("input", 2, "role"), "tool"), (("input", 2, "role"), "system"),
])
def test_lite_rejects_tool_declarations_overrides_and_nested_tools(path, value):
    request = inference_payload()
    target = request
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ValueError):
        decode_request(json.dumps(request).encode())


@pytest.mark.parametrize("kind", ["additional_tools", "function_call", "function_call_output", "custom_tool_call",
                                  "custom_tool_call_output", "local_shell_call", "reasoning", "item_reference"])
def test_lite_rejects_any_extra_non_message_history(kind):
    request = inference_payload()
    request["input"].insert(2, {"type": kind, "id": "at_rogue", "role": "developer", "tools": []})
    with pytest.raises(ValueError):
        decode_request(json.dumps(request).encode())


@pytest.mark.parametrize("model", [row["id"] for row in MODELS])
def test_lite_preserves_all_displayed_roles_text_and_image_bytes(model):
    async def run():
        captured = []
        relay = Relay(check_workspace=allow_fixture_workspace, transport=httpx.MockTransport(lambda request: captured.append(request.content) or httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=RelayStream())))
        await relay.start()
        try:
            budget = relay.arm(model)
            relay.bind(budget, "thread-one")
            payload = inference_payload(model)
            payload["input"].extend([
                {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Displayed answer\n"}]},
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": " Still frame "},
                                                             {"type": "input_image", "image_url": jpeg(), "detail": "auto"}]},
            ])
            raw = json.dumps(payload).encode()
            assert (await relay_request(relay, budget, encoded=raw))[0] == 200
            assert captured == [raw] and budget.completed and budget.failure is None
        finally:
            await relay.close()

    asyncio.run(run())


@pytest.mark.parametrize("header", ["version", "x-openai-internal-codex-responses-lite"])
@pytest.mark.parametrize("value", [None, "", "false", "0.4.1", "True"])
def test_missing_or_wrong_native_headers_fail_before_forwarding(header, value):
    async def run():
        calls = []
        relay = Relay(check_workspace=allow_fixture_workspace,
                      transport=httpx.MockTransport(lambda request: calls.append(request) or httpx.Response(500)))
        await relay.start()
        try:
            budget = relay.arm(MODELS[0]["id"])
            relay.bind(budget, "thread-one")
            assert (await relay_request(relay, budget, headers={header: value}))[0] == 400
            assert budget.failure == "protocol_mismatch" and budget.upstream_status is None
            assert budget.consumed and not calls
        finally:
            await relay.close()

    asyncio.run(run())


@pytest.mark.parametrize("header,value", [("x-openai-fedramp", "true"),
    ("x-openai-internal-codex-residency", "us"), ("x-openai-account-routing-override", "us"),
    ("x-openai-account-routing-override", "us_cr"), ("x-openai-account-routing-override", "unknown")])
def test_regulated_routing_never_reaches_fixed_public_endpoint(header, value):
    async def run():
        calls = []
        relay = Relay(check_workspace=allow_fixture_workspace,
                      transport=httpx.MockTransport(lambda request: calls.append(request) or httpx.Response(500)))
        await relay.start()
        try:
            budget = relay.arm(MODELS[0]["id"])
            relay.bind(budget, "thread-one")
            status, _, body = await relay_request(relay, budget, headers={header: value})
            assert status == 422 and budget.failure == "unsupported_workspace"
            assert header.encode() not in body and not calls and budget.attempts == 0
        finally:
            await relay.close()

    asyncio.run(run())


@pytest.mark.parametrize("status,expected", [(400, "request_rejected"), (401, "account_auth"),
    (403, "account_permission"), (404, "request_rejected"), (422, "request_rejected"), (429, "rate_limit"),
    (500, "provider_unavailable"), (503, "provider_unavailable"), (307, "unsupported_workspace")])
def test_non_200_status_precedes_encoding_and_never_reads_or_forwards_body(status, expected, caplog):
    async def run():
        class Unreadable(RelayStream):
            async def __aiter__(self):
                pytest.fail("Encoded errors must not be decoded or read")
                yield b""

        relay = Relay(check_workspace=allow_fixture_workspace, transport=httpx.MockTransport(lambda request: httpx.Response(status, headers={
            "content-encoding": "gzip", "content-type": "application/json", "location": "https://example.invalid/" + PRIVATE,
            "set-cookie": PRIVATE, ERROR_HEADER: "model_unavailable",
        }, stream=Unreadable())))
        await relay.start()
        try:
            budget = relay.arm(MODELS[0]["id"])
            relay.bind(budget, "thread-one")
            code, headers, body = await relay_request(relay, budget)
            assert code == (status if status >= 400 else 502)
            assert budget.failure == expected and budget.upstream_status == status and budget.attempts == 1
            assert budget.non_401 == int(status != 401) and budget.consumed == (status != 401)
            assert PRIVATE.encode() not in headers + body and ERROR_HEADER.lower().encode() not in headers
            assert b"content-encoding" not in headers and not relay.client.cookies
        finally:
            await relay.close()

    caplog.set_level(logging.DEBUG)
    asyncio.run(run())
    assert PRIVATE not in caplog.text and "fixture-private-access" not in caplog.text


@pytest.mark.parametrize("status,error,expected", [
    (404, {"code": "model_not_found"}, "model_unavailable"),
    (400, {"code": "unsupported_model"}, "model_unavailable"),
    (400, {"type": "context_length_exceeded"}, "context_limit"),
    (400, {"code": "unknown", "type": "context_window_exceeded"}, "context_limit"),
    (403, {"type": "content_policy_violation"}, "policy_rejected"),
    (400, {"code": "policy_violation"}, "policy_rejected"),
    (403, {"code": "model_not_found"}, "account_permission"),
    (400, {"code": "unknown", "message": "model_not_found"}, "request_rejected"),
    (400, {"code": {"code": "model_not_found"}}, "request_rejected"),
    (400, {"type": ["context_length_exceeded"]}, "request_rejected"),
    (429, {"code": "model_not_found"}, "rate_limit"),
    (401, {"code": "content_policy_violation"}, "account_auth"),
    (503, {"code": "context_length_exceeded"}, "provider_unavailable"),
])
def test_only_allowlisted_error_code_or_type_affects_failure(status, error, expected, caplog):
    async def run():
        relay = Relay(check_workspace=allow_fixture_workspace, transport=httpx.MockTransport(lambda request: httpx.Response(status, json={
            "error": {"message": PRIVATE, **error}, "account": PRIVATE,
        })))
        await relay.start()
        try:
            budget = relay.arm(MODELS[0]["id"])
            relay.bind(budget, "thread-one")
            code, headers, body = await relay_request(relay, budget)
            assert code == status and budget.failure == expected and budget.upstream_status == status
            assert PRIVATE.encode() not in headers + body and expected.encode() not in body
            if status != 401:
                payload = inference_payload()
                payload["input"].append({"type": "function_call_output", "call_id": "rogue", "output": PRIVATE})
                assert (await relay_request(relay, budget, payload=payload))[0] == 409
                assert budget.failure == expected and budget.attempts == 1
        finally:
            await relay.close()

    asyncio.run(run())
    assert PRIVATE not in caplog.text


@pytest.mark.parametrize("body", [b"not JSON", b"[]", b"null", b'{"error":[]}',
    json.dumps({"error": {"code": "model_not_found", "message": "x" * ERROR_BODY_LIMIT}}).encode()])
def test_malformed_or_oversized_error_json_keeps_status_category(body):
    async def run():
        relay = Relay(check_workspace=allow_fixture_workspace, transport=httpx.MockTransport(lambda request: httpx.Response(
            400, headers={"content-type": "application/json"}, stream=RelayStream((body,)))))
        await relay.start()
        try:
            budget = relay.arm(MODELS[0]["id"])
            relay.bind(budget, "thread-one")
            assert (await relay_request(relay, budget))[0] == 400
            assert budget.failure == "request_rejected" and budget.consumed
        finally:
            await relay.close()

    asyncio.run(run())


@pytest.mark.parametrize("slow", [False, True])
def test_unreadable_error_json_preserves_confirmed_status(slow):
    async def run():
        class Broken(RelayStream):
            async def __aiter__(self):
                yield b'{"error":'
                if slow:
                    await asyncio.Event().wait()
                raise httpx.ReadError(PRIVATE)

        relay = Relay(check_workspace=allow_fixture_workspace, transport=httpx.MockTransport(lambda request: httpx.Response(
            400, headers={"content-type": "application/json"}, stream=Broken())))
        await relay.start()
        try:
            budget = relay.arm(MODELS[0]["id"])
            relay.bind(budget, "thread-one")
            async with asyncio.timeout(2):
                status, _, body = await relay_request(relay, budget)
            assert status == 400 and PRIVATE.encode() not in body
            assert budget.failure == "request_rejected" and budget.upstream_status == 400
        finally:
            await relay.close()

    asyncio.run(run())


@pytest.mark.parametrize("last,expected", [(200, None), (401, "account_auth"), (429, "rate_limit")])
def test_encoded_401_recovery_clears_auth_failure_only_on_recovery(last, expected):
    async def run():
        calls = []

        def upstream(request):
            status = [401, 401, last][len(calls)]
            calls.append(request)
            return httpx.Response(status, headers={"content-type": "text/event-stream",
                                                  "content-encoding": "identity" if status == 200 else "gzip"},
                                  stream=RelayStream())

        relay = Relay(check_workspace=allow_fixture_workspace, transport=httpx.MockTransport(upstream))
        await relay.start()
        try:
            budget = relay.arm(MODELS[0]["id"])
            relay.bind(budget, "thread-one")
            for status in (401, 401, last):
                assert (await relay_request(relay, budget))[0] == status
            assert budget.failure == expected and budget.upstream_status == last
            assert (await relay_request(relay, budget))[0] == 409 and budget.failure == expected
            assert budget.attempts == 3 and budget.non_401 == int(last != 401)
        finally:
            await relay.close()

    asyncio.run(run())


@pytest.mark.parametrize("exception,expected", [(httpx.ConnectError, "network_error"),
    (httpx.ReadError, "network_error"), (httpx.ReadTimeout, "timeout"), (httpx.ConnectTimeout, "timeout")])
def test_network_failures_and_remaining_deadline_read_timeout(exception, expected):
    async def run():
        def upstream(request):
            bounds = request.extensions["timeout"]
            assert 39 < bounds["read"] <= 40 and bounds["connect"] == 5 and bounds["write"] == 10
            raise exception(PRIVATE)

        relay = Relay(check_workspace=allow_fixture_workspace, transport=httpx.MockTransport(upstream))
        await relay.start()
        try:
            budget = relay.arm(MODELS[0]["id"])
            relay.bind(budget, "thread-one")
            budget.deadline = asyncio.get_running_loop().time() + 40
            status, _, body = await relay_request(relay, budget)
            assert status == 502 and PRIVATE.encode() not in body
            assert budget.failure == expected and budget.upstream_status is None
            assert budget.consumed and budget.attempts == 1
        finally:
            await relay.close()

    asyncio.run(run())
