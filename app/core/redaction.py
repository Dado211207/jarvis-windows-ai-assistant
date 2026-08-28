"""Redaction for values that may end up in persisted audit records, logs
or WebSocket events.

Two rules, because one was not enough:

* **By key name** — a parameter called `password` or `api_key` is masked
  whatever it contains.
* **By value shape** — a parameter called anything at all is masked if
  what it holds looks like a credential.

The second rule exists because the first one silently failed in exactly
the case the product had just been hardened against. `memory add my key
is sk-ant-…` reaches the router as `{"content": "my key is sk-ant-…"}`.
`app/core/secret_guard.py` correctly refuses to store that memory — but
`app/core/action_lifecycle.py::propose()` runs *before* the tool
executes, and `"content"` is not a sensitive-looking key name, so the
secret was written verbatim into `action_lifecycle.input_summary`,
broadcast over `/ws/events`, and served by `GET /actions/history`.
Reproduced, not theorised:

    router refused: True
    memories rows: 0
    input_summary: {"content": "my key is sk-ant-api03-…"}

The memory was refused and the secret was persisted anyway, one table
across. Key-name redaction cannot fix that, because there is nothing
wrong with the key name.

So this module now asks `secret_guard` what a credential *looks* like.
The two are deliberately layered rather than merged: `secret_guard`
decides whether a value may be **stored as user data** and refuses the
write; this decides whether a value may be **written to a log, an audit
row or an event** and masks it. Both run; neither replaces the other.

**A mask never quotes what it caught.** A redactor that wrote
`***redacted: sk-ant-…***` would put the secret in the very record it
was protecting. The replacement names the kind only.
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

# How deep to walk a nested structure. Tool inputs are shallow by
# construction (a pydantic input_model with scalar fields), so this is a
# guard against a pathological value rather than an expected shape — and
# a bound is cheaper than trusting that it stays that way.
_MAX_DEPTH = 4


def _redact_text(value: str) -> str:
    """Mask a string that looks like a credential, whatever it is called.

    Returns the original string when it is clean. The replacement names
    the kind of secret and never the secret.
    """
    try:
        from app.core.secret_guard import find_secret
    except Exception:  # noqa: BLE001 — redaction must never break the caller
        return value

    label = find_secret(value)
    if label:
        return f"***redacted: {label}***"
    return value


def redact_value(key: str, value: Any, _depth: int = 0) -> Any:
    """Mask *value* if its key name looks sensitive **or** its content
    looks like a credential; otherwise truncate long strings.

    Recurses into dicts and lists so a secret one level down is not
    missed. Truncation happens after the shape check, so a long string
    is never cut in a way that hides the very pattern being looked for.
    """
    if any(marker in key.lower() for marker in _SENSITIVE_KEY_MARKERS):
        return _REDACTED

    if isinstance(value, str):
        cleaned = _redact_text(value)
        if cleaned != value:
            return cleaned
        if len(value) > _MAX_VALUE_LENGTH:
            return value[:_MAX_VALUE_LENGTH] + "…(truncated)"
        return value

    if _depth >= _MAX_DEPTH:
        # Returning an unvisited container here would preserve any secret
        # nested just beyond the recursion limit. Collapse it instead:
        # bounded work must also mean bounded leakage.
        if isinstance(value, (dict, list, tuple)):
            return _REDACTED
        return value

    if isinstance(value, dict):
        return {
            _redact_text(str(inner_key)): redact_value(
                str(inner_key), inner_value, _depth + 1,
            )
            for inner_key, inner_value in value.items()
        }
    if isinstance(value, (list, tuple)):
        redacted = [redact_value(key, item, _depth + 1) for item in value]
        return type(value)(redacted) if isinstance(value, tuple) else redacted

    return value


def redact_params(params: Dict[str, Any]) -> Dict[str, Any]:
    """Return a copy of *params* safe to persist, log, or send to a
    client — sensitive keys masked, credential-shaped values masked, long
    strings truncated."""
    return {
        _redact_text(str(key)): redact_value(str(key), value)
        for key, value in params.items()
    }


def redact_message(text: str) -> str:
    """Mask a free-text string on its way to a log line, an audit row or
    an event payload.

    For the places that carry a sentence rather than a parameter — a
    tool's result message, a command as typed. Truncation is the caller's
    business; this only removes what must not be written down.
    """
    if not isinstance(text, str) or not text:
        return text
    return _redact_text(text)
