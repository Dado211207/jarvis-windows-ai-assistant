"""Local dashboard session and canonical-Host boundary.

JARVIS is single-user and local-only; this is not a user-account identity
system. It proves that protected reads and mutations came through the
local dashboard, and rejects a non-canonical Host before routing or
issuing a token so DNS rebinding cannot turn an attacker origin into a
same-origin request to loopback.

Origin validation (app/api/origin.py) already blocks a foreign browser
page's WebSocket handshake, and CORS blocks a foreign page's JSON fetch()
from succeeding in a standards-compliant browser. This adds a second,
independent layer that does not rely on either of those: a real,
unpredictable, server-generated token that must be presented on every
mutating request, via the classic double-submit-cookie pattern — the
token is delivered as a *non*-HttpOnly cookie specifically so the
dashboard's own JS can read it and echo it back as a request header.
A foreign origin's page cannot read this cookie itself (the browser's
same-origin policy blocks cross-origin `document.cookie` access
unconditionally), so it cannot construct the matching header even if it
could somehow get some cross-origin request to fire at all.

One token is valid at a time, for the lifetime of this process (or until
TOKEN_TTL_SECONDS elapses, whichever comes first) — there is no
multi-user session store here, matching the rest of this codebase's
single-process, single-user architecture. A server restart invalidates
any previously-issued token, same as pending actions already reset on
restart.
"""

import secrets
import threading
import time
from typing import Optional

from fastapi import Cookie, Header, HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import PlainTextResponse

from app.config import settings

COOKIE_NAME = "jarvis_session"
HEADER_NAME = "X-JARVIS-Session-Token"
TOKEN_TTL_SECONDS = 24 * 60 * 60  # 24 hours


def allowed_hosts() -> set[str]:
    """Exact Host headers the local dashboard is allowed to use.

    Host is part of the browser origin but CORS does not reject a
    same-origin DNS-rebinding request: JavaScript loaded from
    attacker.example:5555 can keep that origin while DNS changes the
    address to 127.0.0.1. Rejecting the non-canonical Host before routing
    or issuing a session cookie closes that gap.
    """
    port = settings.jarvis_port
    return {
        f"127.0.0.1:{port}",
        f"localhost:{port}",
        f"[::1]:{port}",
        f"[0:0:0:0:0:0:0:1]:{port}",
    }


def is_allowed_host(host: Optional[str], client_host: Optional[str] = None) -> bool:
    if not host:
        return False
    candidate = host.strip().lower()
    if candidate in {item.lower() for item in allowed_hosts()}:
        return True

    # Starlette's in-process TestClient uses a synthetic Host/client pair.
    # It cannot arrive over a real socket, so accepting that exact pair
    # preserves unit tests without widening the production allowlist.
    return candidate == "testserver" and client_host == "testclient"


class SessionTokenStore:
    """Thread-safe. One token at a time; auto-rotates on expiry."""

    def __init__(self, ttl_seconds: float = TOKEN_TTL_SECONDS) -> None:
        self._lock = threading.Lock()
        self._token: Optional[str] = None
        self._issued_at: float = 0.0
        self._ttl_seconds = ttl_seconds

    def current(self) -> str:
        """Return the currently valid token, minting a fresh one first if
        there is none yet or the existing one has expired."""
        with self._lock:
            now = time.monotonic()
            if self._token is None or (now - self._issued_at) > self._ttl_seconds:
                self._token = secrets.token_urlsafe(32)
                self._issued_at = now
            return self._token

    def is_valid(self, candidate: Optional[str]) -> bool:
        """Constant-time comparison against the current, unexpired token.
        A missing, wrong, or expired candidate is always invalid — never
        raises."""
        if not candidate:
            return False
        with self._lock:
            if self._token is None:
                return False
            if (time.monotonic() - self._issued_at) > self._ttl_seconds:
                return False
            return secrets.compare_digest(candidate, self._token)

    def rotate(self) -> str:
        """Force a fresh token immediately, invalidating the previous
        one. Not currently wired to any route — available for a future
        explicit 'log out' / 'rotate session' control."""
        with self._lock:
            self._token = secrets.token_urlsafe(32)
            self._issued_at = time.monotonic()
            return self._token


# Module-level singleton, matching this codebase's existing pattern
# (registry, pending_store, event_bus, runtime).
session_tokens = SessionTokenStore()


class SessionCookieMiddleware(BaseHTTPMiddleware):
    """Ensures every response carries a fresh, valid session cookie.
    A no-op once the browser already holds a matching, unexpired one;
    otherwise (first visit, or the previous token expired/rotated) sets a
    new cookie so the dashboard's own JS can read and start using it.

    The cookie is deliberately *not* HttpOnly — see the module docstring
    for why that is correct for this specific double-submit pattern, not
    an oversight. SameSite=Strict is the primary defense; the header
    double-submit is the second, independent layer.
    """

    async def dispatch(self, request: Request, call_next):
        client_host = request.client.host if request.client is not None else None
        if not is_allowed_host(request.headers.get("host"), client_host):
            # Deliberately return before call_next() and before current():
            # a DNS-rebinding probe must receive neither application data
            # nor a freshly minted token.
            return PlainTextResponse("Invalid Host header.", status_code=400)

        response = await call_next(request)
        current = session_tokens.current()
        if request.cookies.get(COOKIE_NAME) != current:
            response.set_cookie(
                key=COOKIE_NAME,
                value=current,
                httponly=False,
                samesite="strict",
                secure=False,  # http://127.0.0.1 — no TLS to require Secure for
                path="/",
            )
        return response


def require_session_token(
    x_jarvis_session_token: Optional[str] = Header(None, alias=HEADER_NAME),
    jarvis_session: Optional[str] = Cookie(None, alias=COOKIE_NAME),
) -> None:
    """FastAPI dependency for protected reads and every state-changing route.

    Requires a valid, unexpired token presented as *both* the header and
    the cookie (double-submit) — matching values from a client that could
    not have read our cookie itself is not enough; failing either check
    rejects the request with the same generic message, so a caller can't
    distinguish "expired" from "wrong" from "absent" by response text.
    Never logs the token value.
    """
    if not session_tokens.is_valid(x_jarvis_session_token):
        raise HTTPException(status_code=403, detail="Missing or invalid session token.")
    if not secrets.compare_digest(x_jarvis_session_token, jarvis_session or ""):
        raise HTTPException(status_code=403, detail="Missing or invalid session token.")
