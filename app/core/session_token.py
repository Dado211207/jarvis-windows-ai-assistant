"""Per-launch local API session token.

JARVIS's API binds to 127.0.0.1 only, but that alone does not stop an
unrelated website open in the same browser from directing the user's
browser to send requests to it — the classic "localhost CSRF" problem. A
JSON `Content-Type` forces a CORS preflight (blocked by our Origin
allowlist), but a `text/plain`/form-encoded body does not, and FastAPI does
not itself reject a mismatched Content-Type before parsing — so Origin/CORS
checks alone are not sufficient for state-changing requests.

This token is the actual gate: it is generated fresh in memory every time
the app starts (never persisted, never reused across restarts), delivered
to the JARVIS UI only by being rendered server-side into the HTML JARVIS
itself serves (see app/ui/routes.py — never via a URL query string, so it
never appears in the address bar or browser history), and required as the
`X-Jarvis-Token` header on every state-changing request. A custom header
is not part of the CORS-safelisted set, so any cross-origin attempt to set
it forces a preflight — which our Origin allowlist then rejects outright,
meaning a foreign page's browser never even gets to send the real request.

A page from a different origin has no way to read this value: Same-Origin
Policy prevents it from reading JARVIS's own rendered HTML or JS globals,
and the token is never written anywhere a foreign origin could reach it
(no cookie, no localStorage, no URL).
"""

import hmac
import secrets

_token: str = ""


def generate_token() -> str:
    """Generate and store a new random token. Called once per app startup
    (see the lifespan handler in app/api/server.py) — every restart gets a
    brand new value; nothing is ever written to disk."""
    global _token
    _token = secrets.token_urlsafe(32)
    return _token


def get_token() -> str:
    return _token


def is_valid(candidate) -> bool:
    """Constant-time comparison. False for anything but an exact match,
    including when no token has been generated yet."""
    if not _token or not candidate or not isinstance(candidate, str):
        return False
    return hmac.compare_digest(_token, candidate)
