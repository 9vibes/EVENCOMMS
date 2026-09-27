"""Pinned v2 notification compatibility and private, fixed-label failures."""

import asyncio
import json
import logging
from types import SimpleNamespace

import pytest

from backend.tests.test_codex_bridge import (
    BASE, MODELS, SID, Process, bridge_client, chat, connect, isolated_bridge_token_environment,
)
from codex_bridge.errors import ERROR_HEADER, ERRORS, Failure
from codex_bridge.generation import Generation
from codex_bridge.rpc import ProtocolError, Runtime
from codex_bridge.service import RETRY_WARNING, Session


PRIVATE = "private-provider-context-account-token"
METADATA = [
    ("model/verification", {"verifications": ["trustedAccessForCyber"]}),
    ("turn/moderationMetadata", {"metadata": {"presentation": "inline", "private": PRIVATE}}),
    ("model/safetyBuffering/updated", {
        "model": "gpt-6-luna", "useCases": [PRIVATE], "reasons": [PRIVATE],
        "showBufferingUi": True, "fasterModel": "informational-only-not-selected",
    }),
]


@pytest.mark.parametrize("method,fields", METADATA)
def test_valid_native_metadata_is_discarded_not_a_generation_failure(method, fields, caplog):
    async def run():
        generation = Generation()
        generation.thread_id, generation.turn_id = "thread", "turn"
        scope = {"threadId": "thread", "turnId": "turn"}
        try:
            generation.event(method, {**scope, **fields})
            generation.event("item/completed", {**scope, "item": {
                "type": "agentMessage", "id": "answer", "text": "Answer", "phase": "final_answer",
            }})
            generation.event("rawResponse/completed", scope)
            generation.event("turn/completed", {"threadId": "thread", "turn": {
                "id": "turn", "items": [], "status": "completed", "error": None,
            }})
            assert generation.done.result() == {"text": "Answer", "incomplete": False, "usage": None}
            assert PRIVATE not in repr(vars(generation))
        finally:
            generation.done.cancel()

    asyncio.run(run())
    assert PRIVATE not in caplog.text


def emit_on_start(runtime, callback):
    event = runtime.event

    def notify(method, params):
        event(method, params)
        if method == "turn/started":
            callback(event, {"threadId": runtime.thread, "turnId": runtime.turn})

    runtime.event = notify


def assert_failure(response, code, caplog, *, retry=True):
    assert response.status_code == ERRORS[code][0]
    assert response.headers[ERROR_HEADER] == code
    assert response.json() == {"detail": str(Failure(code)) + (RETRY_WARNING if retry else "")}
    assert response.headers["cache-control"] == "no-store"
    assert PRIVATE not in response.text + repr(dict(response.headers)) + caplog.text


@pytest.mark.parametrize("model", [row["id"] for row in MODELS])
def test_metadata_is_private_through_http_and_does_not_change_the_request(bridge_client, model, caplog):
    caplog.set_level(logging.DEBUG)
    runtime = connect(bridge_client)

    def metadata(event, scope):
        for method, fields in METADATA:
            event(method, {**scope, **fields})

    emit_on_start(runtime, metadata)
    data = chat(model=model)
    response = bridge_client.post(BASE + "/chat", json=data)
    assert response.status_code == 200
    assert response.json() == {"request_id": data["request_id"], "model": model,
                               "text": "Answer", "incomplete": False, "usage": None}
    assert ERROR_HEADER not in response.headers
    assert PRIVATE not in response.text + repr(dict(response.headers)) + caplog.text
    assert [method for method, _ in runtime.calls].count("turn/start") == 1
    assert dict(runtime.calls)["thread/start"]["model"] == model


@pytest.mark.parametrize("method,fields", METADATA)
@pytest.mark.parametrize("fault", ["thread", "turn", "missing_thread", "missing_turn", "no_active_turn", "late", "extra"])
def test_metadata_requires_current_scope_and_only_known_fields(method, fields, fault):
    async def run():
        generation = Generation()
        generation.thread_id, generation.turn_id = "thread", "turn"
        params = {"threadId": "thread", "turnId": "turn", **fields}
        if fault in {"thread", "turn"}:
            params[fault + "Id"] = "foreign"
        elif fault.startswith("missing_"):
            del params[fault.removeprefix("missing_") + "Id"]
        elif fault == "no_active_turn":
            generation.turn_id = None
        elif fault == "late":
            generation.done.set_result({})
        else:
            params["approval"] = {"granted": True}
        try:
            with pytest.raises(Failure) as error:
                generation.event(method, params)
            assert error.value.code == "protocol_mismatch"
        finally:
            generation.done.cancel()

    asyncio.run(run())


@pytest.mark.parametrize("method,fields", [
    ("model/verification", {"verifications": ["authorized"]}),
    ("model/verification", {"verifications": "trustedAccessForCyber"}),
    ("model/verification", {"verifications": ["trustedAccessForCyber"] * 17}),
    ("model/verification", {"verifications": [True]}),
    ("turn/moderationMetadata", {}),
    ("turn/moderationMetadata", {"metadata": {"text": "x" * 16385}}),
    ("turn/moderationMetadata", {"metadata": [[[[[[[[[None]]]]]]]]]}),
    ("turn/moderationMetadata", {"metadata": [None] * 129}),
    ("turn/moderationMetadata", {"metadata": {"invalid": float("nan")}}),
    ("turn/moderationMetadata", {"metadata": {"invalid": "\ud800"}}),
    ("model/safetyBuffering/updated", {**METADATA[2][1], "showBufferingUi": 1}),
    ("model/safetyBuffering/updated", {**METADATA[2][1], "model": None}),
    ("model/safetyBuffering/updated", {**METADATA[2][1], "reasons": [False]}),
    ("model/safetyBuffering/updated", {**METADATA[2][1], "useCases": ["x"] * 33}),
    ("model/safetyBuffering/updated", {**METADATA[2][1], "reasons": ["x" * 513]}),
    ("model/safetyBuffering/updated", {**METADATA[2][1], "fasterModel": {"grant": True}}),
])
def test_metadata_rejects_invalid_or_unbounded_shapes(method, fields):
    async def run():
        generation = Generation()
        generation.thread_id, generation.turn_id = "thread", "turn"
        try:
            with pytest.raises(Failure) as error:
                generation.event(method, {"threadId": "thread", "turnId": "turn", **fields})
            assert error.value.code == "protocol_mismatch"
        finally:
            generation.done.cancel()

    asyncio.run(run())


NATIVE_ERRORS = [
    ("usageLimitExceeded", "rate_limit"),
    ("rateLimitExceeded", "rate_limit"),
    ("unauthorized", "account_auth"),
    ("contextWindowExceeded", "context_limit"),
    ("sessionBudgetExceeded", "context_limit"),
    ("badRequest", "request_rejected"),
    ("cyberPolicy", "policy_rejected"),
    ("misalignmentPolicyViolation", "policy_rejected"),
    ("serverOverloaded", "provider_unavailable"),
    ("internalServerError", "provider_unavailable"),
    ("threadRollbackFailed", "runtime_error"),
    ("sandboxError", "tool_rejected"),
    ("other", "runtime_error"),
    (None, "runtime_error"),
    ({"httpConnectionFailed": {"httpStatusCode": None}}, "network_error"),
    ({"responseStreamConnectionFailed": {}}, "network_error"),
    ({"responseStreamConnectionFailed": {"httpStatusCode": 401}}, "account_auth"),
    ({"httpConnectionFailed": {"httpStatusCode": 403}}, "account_permission"),
    ({"httpConnectionFailed": {"httpStatusCode": 404}}, "request_rejected"),
    ({"httpConnectionFailed": {"httpStatusCode": 429}}, "rate_limit"),
    ({"responseStreamDisconnected": {"httpStatusCode": 200}}, "stream_incomplete"),
    ({"responseTooManyFailedAttempts": {"httpStatusCode": None}}, "stream_incomplete"),
    ({"responseTooManyFailedAttempts": {"httpStatusCode": 503}}, "provider_unavailable"),
    ({"responseTooManyFailedAttempts": {"httpStatusCode": 504}}, "timeout"),
    ({"activeTurnNotSteerable": {"turnKind": "review"}}, "request_rejected"),
    ({"activeTurnNotSteerable": {"turnKind": "compact"}}, "request_rejected"),
]


def turn_error(info):
    return {"message": PRIVATE, "codexErrorInfo": info, "additionalDetails": PRIVATE,
            "misalignment": {"errorType": PRIVATE, "detailedExplanation": PRIVATE, "steer": {"message": PRIVATE}}}


@pytest.mark.parametrize("info,code", NATIVE_ERRORS)
@pytest.mark.parametrize("method", ["error", "turn/completed"])
def test_native_errors_have_safe_http_codes_without_raw_details(bridge_client, info, code, method, caplog):
    runtime = connect(bridge_client)

    def failed(event, scope):
        if method == "error":
            event(method, {**scope, "willRetry": False, "error": turn_error(info)})
        else:
            event(method, {"threadId": scope["threadId"], "turn": {
                "id": scope["turnId"], "status": "failed", "items": [], "error": turn_error(info),
            }})

    emit_on_start(runtime, failed)
    response = bridge_client.post(BASE + "/chat", json=chat())
    assert_failure(response, code, caplog)
    assert runtime.closed
    assert runtime.failure == code
    assert [method for method, _ in runtime.calls].count("turn/start") == 1


@pytest.mark.parametrize("info", [
    "bioPolicy", "unknown", "responseStreamDisconnected", True, [],
    {"usageLimitExceeded": {}}, {"responseStreamDisconnected": None},
    {"responseStreamDisconnected": {"httpStatusCode": "429"}},
    {"responseStreamDisconnected": {"httpStatusCode": True}},
    {"responseStreamDisconnected": {"httpStatusCode": 429.0}},
    {"responseStreamDisconnected": {"httpStatusCode": -1}},
    {"responseStreamDisconnected": {"httpStatusCode": 65536}},
    {"responseStreamDisconnected": {"httpStatusCode": 429, "grant": True}},
    {"responseStreamDisconnected": {}, "httpConnectionFailed": {}},
    {"activeTurnNotSteerable": {"turnKind": "unknown"}},
])
def test_codex_error_info_is_a_valid_pinned_enum_not_provider_json(info):
    async def run():
        generation = Generation()
        generation.thread_id, generation.turn_id = "thread", "turn"
        try:
            with pytest.raises(Failure) as error:
                generation.event("error", {"threadId": "thread", "turnId": "turn", "willRetry": False,
                                           "error": turn_error(info)})
            assert error.value.code == "protocol_mismatch"
        finally:
            generation.done.cancel()

    asyncio.run(run())


@pytest.mark.parametrize("fault", ["thread", "turn", "missing_thread", "missing_turn", "early", "retry", "message", "details"])
@pytest.mark.parametrize("method", ["error", "turn/completed"])
def test_error_scope_and_shape_are_validated_before_classification(method, fault):
    async def run():
        generation = Generation()
        generation.thread_id, generation.turn_id = "thread", "turn"
        params = {"threadId": "thread", "turnId": "turn", "willRetry": False, "error": turn_error("unauthorized")}
        if fault == "thread":
            params["threadId"] = "other"
        elif fault == "turn":
            params["turnId"] = "other"
        elif fault == "early":
            generation.turn_id = None
        elif fault in {"missing_thread", "missing_turn"}:
            params.pop(fault.removeprefix("missing_") + "Id")
        elif fault == "retry":
            params["willRetry"] = "false"
        elif fault == "message":
            params["error"]["message"] = {}
        else:
            params["error"]["additionalDetails"] = []
        if method == "turn/completed":
            params = {"threadId": params.get("threadId"), "turn": {
                "id": params.get("turnId"), "items": [], "status": "failed" if fault != "retry" else "completed",
                "error": params["error"],
            }}
        try:
            with pytest.raises(Failure) as error:
                generation.event(method, params)
            assert error.value.code == "protocol_mismatch"
        finally:
            generation.done.cancel()

    asyncio.run(run())


@pytest.mark.parametrize("method,fields,code", [
    ("unknown/newNotification", {}, "protocol_mismatch"),
    ("warning", {"message": PRIVATE}, "protocol_mismatch"),
    ("model/rerouted", {"fromModel": "requested", "toModel": "substitute", "reason": "highRiskCyberActivity"}, "model_changed"),
    ("item/commandExecution/requestApproval", {}, "tool_rejected"),
    ("item/tool/call", {}, "tool_rejected"),
    ("item/completed", {"item": {"type": "commandExecution", "command": PRIVATE}}, "tool_rejected"),
    ("item/completed", {"item": {"type": "unknownItem", "private": PRIVATE}}, "protocol_mismatch"),
    ("rawResponseItem/completed", {"item": {"type": "function_call", "name": PRIVATE}}, "tool_rejected"),
    ("rawResponseItem/completed", {"item": {"type": "unknownItem", "private": PRIVATE}}, "protocol_mismatch"),
    ("error", {"willRetry": True, "error": turn_error("cyberPolicy")}, "policy_rejected"),
])
def test_metadata_never_authorizes_tools_reroutes_or_policy_bypasses(bridge_client, method, fields, code, caplog):
    runtime = connect(bridge_client)

    def unsafe(event, scope):
        for metadata_method, metadata_fields in METADATA:
            event(metadata_method, {**scope, **metadata_fields})
        event(method, {**scope, **fields})

    emit_on_start(runtime, unsafe)
    assert_failure(bridge_client.post(BASE + "/chat", json=chat()), code, caplog)
    assert [method for method, _ in runtime.calls].count("thread/start") == 1
    assert [method for method, _ in runtime.calls].count("turn/start") == 1
    assert not any(method in {"userVerification/enroll", "turn/steer", "item/tool/call"} for method, _ in runtime.calls)


@pytest.mark.parametrize("code", ["rate_limit", "account_auth", "account_permission", "model_unavailable", "request_rejected",
                                  "context_limit", "policy_rejected", "provider_unavailable", "network_error", "timeout",
                                  "response_encoding", "unsupported_workspace", "stream_incomplete"])
def test_relay_safe_classification_survives_generic_native_failure(bridge_client, code, caplog):
    runtime = connect(bridge_client)

    def failed(event, scope):
        runtime.relay.current.failure = code
        event("error", {**scope, "willRetry": False, "error": turn_error("other")})

    emit_on_start(runtime, failed)
    assert_failure(bridge_client.post(BASE + "/chat", json=chat()), code, caplog)
    assert runtime.closed


def test_unknown_relay_code_and_error_text_cannot_become_diagnostics(bridge_client, caplog):
    runtime = connect(bridge_client)

    def failed(event, scope):
        runtime.relay.current.failure = PRIVATE
        error = turn_error(None)
        error["message"] += " quota model_not_found unauthorized policy_violation"
        event("error", {**scope, "willRetry": False, "error": error})

    emit_on_start(runtime, failed)
    assert_failure(bridge_client.post(BASE + "/chat", json=chat()), "runtime_error", caplog)


@pytest.mark.parametrize("pending_auth_failure", [False, True])
def test_native_retry_notification_does_not_poison_a_recovered_turn(bridge_client, pending_auth_failure):
    runtime = connect(bridge_client)

    def recovered(event, scope):
        if pending_auth_failure:
            runtime.relay.current.failure = "account_auth"
        event("error", {**scope, "willRetry": True, "error": turn_error("unauthorized")})
        runtime.relay.current.failure = None
        runtime.relay.current.attempts = 3

    emit_on_start(runtime, recovered)
    response = bridge_client.post(BASE + "/chat", json=chat())
    assert response.status_code == 200 and ERROR_HEADER not in response.headers
    assert [method for method, _ in runtime.calls].count("turn/start") == 1
    assert not runtime.closed


@pytest.mark.parametrize("retry", [False, True])
def test_native_retry_never_extends_the_application_deadline(bridge_client, monkeypatch, retry, caplog):
    monkeypatch.setattr("codex_bridge.service.CHAT_SECONDS", 0.03)
    runtime = connect(bridge_client)
    runtime.mode = "hold"
    if retry:
        emit_on_start(runtime, lambda event, scope: event("error", {
            **scope, "willRetry": True, "error": turn_error({"responseStreamDisconnected": {"httpStatusCode": None}}),
        }))
    assert_failure(bridge_client.post(BASE + "/chat", json=chat()), "stream_incomplete" if retry else "timeout", caplog)
    assert runtime.closed and [method for method, _ in runtime.calls].count("turn/start") == 1


@pytest.mark.parametrize("cleanup", ["close", "finish", "slow_close"])
def test_cleanup_cannot_erase_a_meaningful_failure_or_release_its_slot(bridge_client, monkeypatch, cleanup, caplog):
    runtime = connect(bridge_client)
    session = bridge_client.app.state.bridge.sessions[SID]
    original_close, original_finish = runtime.close, runtime.relay.finish
    monkeypatch.setattr("codex_bridge.service.CLEANUP_SECONDS", 0.02)

    async def broken(*args, **kwargs):
        if cleanup == "slow_close":
            await asyncio.Event().wait()
        raise OSError(PRIVATE)

    if cleanup == "finish":
        runtime.relay.finish = broken
    else:
        runtime.close = broken
    emit_on_start(runtime, lambda event, scope: event("error", {
        **scope, "willRetry": False, "error": turn_error("usageLimitExceeded"),
    }))
    try:
        assert_failure(bridge_client.post(BASE + "/chat", json=chat()), "rate_limit", caplog)
        assert session.failure == "rate_limit" and session.generation is None
        assert bridge_client.app.state.bridge.sessions[SID] is session
        assert not session.runtime.relay.last.armed
    finally:
        runtime.close, runtime.relay.finish = original_close, original_finish
    assert bridge_client.get(BASE + "/status").json()["state"] == "disconnected"
    assert runtime.closed


@pytest.mark.parametrize("code", [-32600, -32601, -32602, -32603, -32000])
@pytest.mark.parametrize("upstream", [None, "model_unavailable"])
def test_native_rpc_error_responses_are_safe_domain_failures(monkeypatch, tmp_path, code, upstream, caplog):
    async def run():
        process = Process()

        async def spawn(*args, **kwargs):
            return process

        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
        monkeypatch.setattr("codex_bridge.rpc.os.killpg", lambda *args: None)
        runtime = Runtime("/fake/codex", lambda *args: None, temp_parent=str(tmp_path))
        await runtime.start()
        if upstream:
            runtime.relay.arm(MODELS[0]["id"]).failure = upstream
        pending = asyncio.create_task(runtime.call("turn/start"))
        await asyncio.sleep(0)
        process.stdout.feed_data(json.dumps({"id": runtime.sequence, "error": {
            "code": code, "message": PRIVATE, "data": {"rawError": PRIVATE},
        }}).encode() + b"\n")
        expected = upstream or ("protocol_mismatch" if code in {-32600, -32601, -32602} else "runtime_error")
        with pytest.raises(Failure) as error:
            await pending
        assert error.value.code == runtime.failure == expected
        assert str(error.value) == str(Failure(expected))
        assert runtime.failure_reason == "native_rpc_error"
        await runtime.close()
        assert runtime.directory is None and process.returncode == -9

    asyncio.run(run())
    assert PRIVATE not in caplog.text


@pytest.mark.parametrize("approval", [False, True])
@pytest.mark.parametrize("kill_fails", [False, True])
def test_event_consumer_preserves_first_failure_through_rpc_kill_and_cleanup(monkeypatch, tmp_path, approval, kill_fails, caplog):
    async def run():
        process, killed = Process(), []

        async def spawn(*args, **kwargs):
            return process

        def kill(*args):
            killed.append(True)
            if kill_fails and len(killed) == 1:
                raise PermissionError(PRIVATE)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
        monkeypatch.setattr("codex_bridge.rpc.os.killpg", kill)
        manager = SimpleNamespace(clock=lambda: 0, binary="/fake/codex", runtime_factory=Runtime, temp_parent=str(tmp_path))
        session = Session(manager)
        session.state = "connected"
        generation = session.generation = Generation()
        generation.thread_id, generation.turn_id = "thread", "turn"
        await session.runtime.start()
        pending = asyncio.create_task(session.runtime.call("turn/start"))
        await asyncio.sleep(0)
        message = {"method": "error", "params": {
            "threadId": "thread", "turnId": "turn", "willRetry": False, "error": turn_error("usageLimitExceeded"),
        }}
        if approval:
            message = {"id": 99, "method": "unknown/futureApproval", "params": {"private": PRIVATE}}
        process.stdout.feed_data(json.dumps(message).encode() + b"\n")
        code = "tool_rejected" if approval else "rate_limit"
        with pytest.raises(Failure) as error:
            await pending
        assert error.value.code == session.failure == session.runtime.failure == code
        assert killed and session.runtime.failed
        with pytest.raises(Failure) as error:
            generation.done.result()
        assert error.value.code == code
        session.event("bridge/failed", {})
        session.fail("cleanup", ProtocolError())
        session.runtime.abort(ProtocolError())
        assert session.failure == generation.failure == session.runtime.failure == code
        if approval:
            assert {"id": 99, "error": {"code": -32601, "message": "Not permitted"}} in process.sent
        await session.stop()
        assert process.returncode == -9 and session.runtime.directory is None and not session.runtime.pending

    asyncio.run(run())
    assert PRIVATE not in caplog.text


def test_validation_and_bridge_auth_are_not_upstream_account_failures(bridge_client, caplog):
    unauthenticated = bridge_client.post(BASE + "/chat", headers={"Authorization": "Bearer invalid"}, json=chat())
    assert unauthenticated.status_code == 401 and ERROR_HEADER not in unauthenticated.headers
    connect(bridge_client)
    invalid = bridge_client.post(BASE + "/chat", json=chat(model="invalid"))
    assert invalid.status_code == 422 and ERROR_HEADER not in invalid.headers
    bridge_client.app.state.bridge.generation_enabled = False
    assert_failure(bridge_client.post(BASE + "/chat", json=chat()), "generation_disabled", caplog, retry=False)


def test_actual_model_substitution_is_explicitly_classified(bridge_client, caplog):
    runtime = connect(bridge_client)
    runtime.mode = "model_fallback"
    assert_failure(bridge_client.post(BASE + "/chat", json=chat()), "model_changed", caplog)
    assert not any(method == "turn/start" for method, _ in runtime.calls)


@pytest.mark.parametrize("status,error,code", [("failed", None, "runtime_error"), ("interrupted", None, "stream_incomplete")])
def test_failed_or_interrupted_turn_without_details_is_not_malformed_rpc(bridge_client, status, error, code, caplog):
    runtime = connect(bridge_client)
    emit_on_start(runtime, lambda event, scope: event("turn/completed", {"threadId": scope["threadId"], "turn": {
        "id": scope["turnId"], "status": status, "error": error, "items": [],
    }}))
    assert_failure(bridge_client.post(BASE + "/chat", json=chat()), code, caplog)
