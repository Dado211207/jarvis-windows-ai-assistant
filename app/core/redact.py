"""Text redaction for anything JARVIS surfaces that ultimately traces back
to an OS/SDK exception message or a filesystem path — a tool error shown in
chat, a Diagnostics report, an AI-provider call failure. These are strings
JARVIS itself did not compose, so unlike the rest of the app's user-facing
text they cannot be trusted by construction to be free of a Windows
username, an accidentally-embedded key, or similar.

Not a replacement for app.core.secret_guard: secret_guard *rejects* user
INPUT (settings/preferences) that contains a secret before it's ever
stored. This module *redacts* OUTPUT — text JARVIS is about to show the
user or hand back over the API — that it does not fully control the shape
of, so it can still be shown/copied instead of suppressed outright.
"""

import re
from typing import Optional

# \\+ (one-or-more backslashes), not a single \\ — json.dumps() escapes
# each backslash in a path as \\\\ in the resulting text, so a
# JSON-serialized path has doubled separators; matching "one or more"
# handles both the raw path form and the JSON-escaped form with the same
# pattern, rather than needing a second one just for JSON.
_PATH_USERNAME_RE = re.compile(r'((?:\\+|/)(?:Users|home)(?:\\+|/))([^\\/"]+)', re.IGNORECASE)
_ANTHROPIC_KEY_RE = re.compile(r"sk-ant-[A-Za-z0-9_-]{10,}", re.IGNORECASE)
_GENERIC_SK_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{10,}", re.IGNORECASE)
_BEARER_RE = re.compile(r"Bearer\s+[A-Za-z0-9._-]{10,}", re.IGNORECASE)
# Matches both a plain header ("Authorization: Bearer xyz") and a
# dict/JSON-repr style key ("'Authorization': 'Basic xyz'") — the key may
# be quoted, and so may the value; only the value gets redacted.
_AUTH_HEADER_RE = re.compile(r"(Authorization['\"]?\s*:\s*['\"]?)([^,\"'\n}]+)", re.IGNORECASE)
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def redact_text(text: Optional[str]) -> Optional[str]:
    """Redact anything that looks like a Windows/Unix home-directory
    username, an Anthropic-style API key, a bearer token, an Authorization
    header, or an email address. Order matters only in that key/bearer
    patterns run before the path pattern would otherwise be able to touch
    them (they don't overlap in practice, but keeping key/token patterns
    first is the safer order). Works across multiline text (exception
    messages, tracebacks) since none of these patterns anchor to a single
    line."""
    if not isinstance(text, str) or not text:
        return text
    redacted = text
    redacted = _ANTHROPIC_KEY_RE.sub("<redacted-api-key>", redacted)
    redacted = _GENERIC_SK_KEY_RE.sub("<redacted-api-key>", redacted)
    redacted = _BEARER_RE.sub("Bearer <redacted>", redacted)
    redacted = _AUTH_HEADER_RE.sub(r"\1<redacted>", redacted)
    redacted = _EMAIL_RE.sub("<redacted-email>", redacted)
    redacted = _PATH_USERNAME_RE.sub(r"\1<user>", redacted)
    return redacted
