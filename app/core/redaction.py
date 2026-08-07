"""Redaction helpers for values that may end up in persisted audit records,
logs, or WebSocket events. Best-effort and key-name based: it masks
common sensitive-looking keys and caps string length so no tool input can
leak a secret or flood a client/log line. Not a substitute for tools
themselves declining to accept secrets as plain arguments.
"""

from typing import Any, Dict

_SENSITIVE_KEY_MARKERS = (
    "password",
    "passwd",
    "token",
    "secret",
    "api_key",
    "apikey",
    "key",
    "credential",
    "clipboard",
)
_MAX_VALUE_LENGTH = 200
_REDACTED = "***redacted***"


def redact_value(key: str, value: Any) -> Any:
    """Mask *value* if its *key* name looks sensitive, otherwise truncate
    long strings. Non-string values are returned unchanged (aside from the
    key-name mask)."""
    if any(marker in key.lower() for marker in _SENSITIVE_KEY_MARKERS):
        return _REDACTED
    if isinstance(value, str) and len(value) > _MAX_VALUE_LENGTH:
        return value[:_MAX_VALUE_LENGTH] + "…(truncated)"
    return value


def redact_params(params: Dict[str, Any]) -> Dict[str, Any]:
    """Return a shallow copy of *params* safe to persist, log, or send to a
    client — sensitive-looking keys are masked, long strings are truncated."""
    return {key: redact_value(key, value) for key, value in params.items()}
