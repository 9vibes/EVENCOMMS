"""Public failure labels, never upstream bodies, account details or credentials."""

ERROR_HEADER = "X-Evencomms-Codex-Error"
ERRORS = {
    "rate_limit": (429, "Codex allowance or rate limit reached. Check your ChatGPT usage limits before retrying."),
    "account_auth": (409, "ChatGPT authentication could not be recovered. Disconnect and sign in again."),
    "account_permission": (403, "OpenAI denied access to this request. Check your account and workspace permissions."),
    "model_unavailable": (422, "The selected Codex model is unavailable to this account. Check your plan's model access."),
    "request_rejected": (422, "OpenAI rejected the Codex request. Model or input compatibility may differ."),
    "context_limit": (422, "Codex rejected the conversation size or session budget. Start a new chat with less context."),
    "policy_rejected": (403, "OpenAI declined this request under its policy. No alternate model or provider was tried."),
    "provider_unavailable": (503, "The OpenAI Codex service could not complete the request."),
    "network_error": (503, "The bridge could not reach the OpenAI Codex service."),
    "timeout": (504, "The Codex request exceeded its response deadline."),
    "stream_incomplete": (502, "The Codex response stream ended before a complete reply was received."),
    "response_encoding": (502, "OpenAI returned an unsupported response encoding. The bridge did not forward it."),
    "protocol_mismatch": (502, "The Codex runtime and bridge request/response formats did not match."),
    "tool_rejected": (502, "Codex requested a tool or approval that Research does not permit. No tool was approved."),
    "model_changed": (422, "OpenAI selected a different model. Research discarded the response instead of switching models."),
    "unsupported_workspace": (422, "This account requires routing that the isolated Codex bridge does not support."),
    "generation_disabled": (503, "The Codex generation safety check did not pass. Sending remains disabled."),
    "runtime_error": (502, "The Codex runtime stopped before returning a complete reply."),
}


class Failure(Exception):
    def __init__(self, code):
        self.code = code if isinstance(code, str) and code in ERRORS else "runtime_error"
        self.status, message = ERRORS[self.code]
        super().__init__(message)


def provider_failure(status, code=None):
    code = code if isinstance(code, str) else None
    if status == 401:
        return "account_auth"
    if status == 429:
        return "rate_limit"
    if code in {"content_policy_violation", "policy_violation", "cyber_policy", "bio_policy", "misalignment_policy_violation"}:
        return "policy_rejected"
    if status == 403:
        return "account_permission"
    if code in {"model_not_found", "model_not_available", "unsupported_model"}:
        return "model_unavailable"
    if code in {"context_length_exceeded", "context_window_exceeded"}:
        return "context_limit"
    if status in {400, 404, 422}:
        return "request_rejected"
    if type(status) is int and 300 <= status < 400:
        return "unsupported_workspace"
    return "provider_unavailable"
