"""Safe error envelope — the boundary between exceptions from any
external system (an AI provider's SDK, a tool handler, a future
integration) and anything the browser sees.

Raw exception text can contain implementation detail that must never
reach a REST response, a WebSocket event, or a rendered page: stack
frames, local file paths, SDK internals, occasionally a still-identifiable
fragment of a request or response. Never put str(exc) directly into
anything the browser receives — call to_safe_error() and use the
returned SafeError instead. The original exception is always still
logged in full, server-side only (data/logs/jarvis.log), tagged with the
same correlation_id the client sees, so a developer can find the real
cause from a user's bug report without the user, or the response body,
ever holding the raw detail.
"""

import uuid
from enum import Enum
from typing import Optional

from pydantic import BaseModel

from app.logging_config import get_logger

logger = get_logger("errors")


class ErrorCategory(str, Enum):
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    PROVIDER_AUTH = "provider_auth"
    PROVIDER_BILLING = "provider_billing"
    PROVIDER_RATE_LIMIT = "provider_rate_limit"
    PROVIDER_TIMEOUT = "provider_timeout"
    PROVIDER_ERROR = "provider_error"
    TOOL_ERROR = "tool_error"
    TOOL_TIMEOUT = "tool_timeout"
    INTERNAL_ERROR = "internal_error"


_SAFE_MESSAGES = {
    ErrorCategory.PROVIDER_UNAVAILABLE: "The AI provider could not be reached. Check your internet connection — local commands still work normally.",
    ErrorCategory.PROVIDER_AUTH: "The AI provider rejected that API key. Check it was copied in full, or create a new one, and enter it again in Settings.",
    ErrorCategory.PROVIDER_BILLING: "That API key is valid, but the account has no credit available. Add credit or a payment method to your Anthropic account and try again.",
    ErrorCategory.PROVIDER_RATE_LIMIT: "The AI provider is rate-limiting requests right now. Please try again shortly.",
    ErrorCategory.PROVIDER_TIMEOUT: "The AI provider did not respond in time. Please try again.",
    ErrorCategory.PROVIDER_ERROR: "The AI provider returned an error. Local commands still work normally.",
    ErrorCategory.TOOL_ERROR: "This tool ran into an unexpected error and could not complete.",
    ErrorCategory.TOOL_TIMEOUT: "This tool did not finish in time and was stopped.",
    ErrorCategory.INTERNAL_ERROR: "Something went wrong processing that request.",
}


def safe_message(category: ErrorCategory) -> str:
    """The one sentence a user is allowed to read for *category*.

    Exists so the key-verification path and the generation path cannot
    drift into describing the same failure two different ways — the point
    of having categories at all.
    """
    return _SAFE_MESSAGES.get(category, _SAFE_MESSAGES[ErrorCategory.INTERNAL_ERROR])


# The one marker in a provider's response body this module is allowed to
# look at, and why.
#
# An account with no credit and a malformed request both come back from
# Anthropic as the same exception type with the same structured
# `invalid_request_error` code; only the human-readable message separates
# them. Telling someone their key was rejected when it was accepted and
# merely unfunded sends them to generate a new key that will fail exactly
# the same way — CLAUDE.md's "the failure a user reads must be the
# failure that happened" rule, breached in the way that wastes the most
# of the user's time.
#
# So: a *containment test* against a fixed marker we wrote here, whose
# only output is one of our own fixed sentences above. Nothing from the
# response is stored, echoed, logged as user-visible text, or returned.
# Matching is not echoing — but it is the single exception to the
# type-name-only rule, and it stays that way.
_BILLING_MARKERS = ("credit balance", "billing", "insufficient funds", "quota")


class SafeError(BaseModel):
    """Everything that's safe to send to a client. No raw exception text,
    ever — see the module docstring."""
    category: ErrorCategory
    message: str
    correlation_id: str


def _looks_like_a_billing_problem(exc: Exception) -> bool:
    """Whether *exc* is an unfunded account rather than a bad request.

    Reads the message, and only ever answers True/False — see
    _BILLING_MARKERS above for why this one exception exists and what it
    is bounded to.
    """
    try:
        text = str(exc).lower()
    except Exception:  # noqa: BLE001 — an exception whose __str__ raises
        return False
    return any(marker in text for marker in _BILLING_MARKERS)


def classify_anthropic_exception(exc: Exception) -> ErrorCategory:
    """Best-effort classification from the exception's TYPE NAME — never
    its message, which may hold request/response detail. The single,
    documented exception is the billing check below."""
    name = type(exc).__name__
    if "Authentication" in name:
        return ErrorCategory.PROVIDER_AUTH
    if "PermissionDenied" in name:
        # A permission failure on an otherwise-valid key is usually an
        # unfunded or restricted account, not a bad key.
        return ErrorCategory.PROVIDER_BILLING if _looks_like_a_billing_problem(exc) else ErrorCategory.PROVIDER_AUTH
    if "RateLimit" in name:
        return ErrorCategory.PROVIDER_RATE_LIMIT
    if "Timeout" in name or "TimedOut" in name:
        return ErrorCategory.PROVIDER_TIMEOUT
    if "Connection" in name or "APIConnection" in name:
        return ErrorCategory.PROVIDER_UNAVAILABLE
    if "BadRequest" in name and _looks_like_a_billing_problem(exc):
        return ErrorCategory.PROVIDER_BILLING
    return ErrorCategory.PROVIDER_ERROR


def to_safe_error(
    exc: Exception,
    category: ErrorCategory = ErrorCategory.INTERNAL_ERROR,
    context: Optional[str] = None,
) -> SafeError:
    """Log *exc* in full, server-side only, tagged with a fresh
    correlation_id, and return a SafeError carrying only the category, a
    fixed safe message, and that same id — safe to place in any REST
    response, WebSocket event, or rendered page.
    """
    correlation_id = str(uuid.uuid4())
    logger.error(
        "Unhandled error [correlation_id=%s] category=%s type=%s%s",
        correlation_id,
        category.value,
        type(exc).__name__,
        f" context={context}" if context else "",
        exc_info=exc,  # the exception object itself, not exc_info=True — correct
        # whether or not we're still inside the original except block (True
        # relies on ambient sys.exc_info(), which can be empty by the time
        # this runs, e.g. if a caller stores the exception and calls this
        # later; passing exc directly is unambiguous either way).
    )
    return SafeError(
        category=category,
        message=_SAFE_MESSAGES.get(category, _SAFE_MESSAGES[ErrorCategory.INTERNAL_ERROR]),
        correlation_id=correlation_id,
    )
