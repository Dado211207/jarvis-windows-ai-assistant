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
    PROVIDER_RATE_LIMIT = "provider_rate_limit"
    PROVIDER_TIMEOUT = "provider_timeout"
    PROVIDER_ERROR = "provider_error"
    TOOL_ERROR = "tool_error"
    TOOL_TIMEOUT = "tool_timeout"
    INTERNAL_ERROR = "internal_error"


_SAFE_MESSAGES = {
    ErrorCategory.PROVIDER_UNAVAILABLE: "The AI provider is temporarily unavailable. Local commands still work normally.",
    ErrorCategory.PROVIDER_AUTH: "The AI provider rejected the configured credentials. Open Settings to check or replace your API key.",
    ErrorCategory.PROVIDER_RATE_LIMIT: "The AI provider is rate-limiting requests right now. Please try again shortly.",
    ErrorCategory.PROVIDER_TIMEOUT: "The AI provider did not respond in time. Please try again.",
    ErrorCategory.PROVIDER_ERROR: "The AI provider returned an error. Local commands still work normally.",
    ErrorCategory.TOOL_ERROR: "This tool ran into an unexpected error and could not complete.",
    ErrorCategory.TOOL_TIMEOUT: "This tool did not finish in time and was stopped.",
    ErrorCategory.INTERNAL_ERROR: "Something went wrong processing that request.",
}


class SafeError(BaseModel):
    """Everything that's safe to send to a client. No raw exception text,
    ever — see the module docstring."""
    category: ErrorCategory
    message: str
    correlation_id: str


def classify_anthropic_exception(exc: Exception) -> ErrorCategory:
    """Best-effort classification from the exception's TYPE NAME only —
    never its message, which may hold request/response detail."""
    name = type(exc).__name__
    if "Authentication" in name or "PermissionDenied" in name:
        return ErrorCategory.PROVIDER_AUTH
    if "RateLimit" in name:
        return ErrorCategory.PROVIDER_RATE_LIMIT
    if "Timeout" in name or "TimedOut" in name:
        return ErrorCategory.PROVIDER_TIMEOUT
    if "Connection" in name:
        return ErrorCategory.PROVIDER_UNAVAILABLE
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
