"""Local-only request guard.

Binding to 127.0.0.1 stops other devices on the network, but not an
unrelated website open in the same browser from directing it to send
requests to this API — the "localhost CSRF" problem. Three checks, applied
to every request:

1. **Host header allowlist** (all requests). A browser cannot forge its own
   Host header — it always reflects the URL actually navigated to — so
   this catches DNS-rebinding-style attempts where a public hostname
   resolves to 127.0.0.1.
2. **Origin allowlist** (state-changing requests only: POST/PUT/PATCH/
   DELETE). If an Origin header is present it must be
   `http://127.0.0.1[:port]` or `http://localhost[:port]`; a foreign or
   `null` Origin is rejected. A *missing* Origin is not itself rejected —
   plenty of legitimate non-browser local callers (curl, this project's
   own test suite, a developer scripting against `--api`) never send one,
   and real browsers do send Origin on state-changing fetches even for
   same-origin requests, so its absence is not a reliable signal either
   way. See docs/SECURITY.md for why this alone is not the real defense.
3. **Session token** (state-changing requests only). The actual gate — see
   app/core/session_token.py for why a custom header, checked in constant
   time, is what actually stops the attack Origin/CORS checks alone can't:
   a foreign page cannot read or guess this value, and setting any custom
   header on a cross-origin request forces a CORS preflight that our
   Origin allowlist then fails.

`testserver` is allowlisted alongside 127.0.0.1/localhost purely because
Starlette's TestClient sends `Host: testserver` by default and is not a
real, routable hostname — it can never appear in a request that actually
reached this process over a real socket.
"""

from urllib.parse import urlparse

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.core import session_token
from app.logging_config import get_logger

logger = get_logger("api.local_guard")

ALLOWED_HOSTNAMES = frozenset({"127.0.0.1", "localhost", "testserver"})
ALLOWED_ORIGIN_HOSTNAMES = frozenset({"127.0.0.1", "localhost"})
STATE_CHANGING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
TOKEN_HEADER = "x-jarvis-token"


def _hostname_from_host_header(host_header: str) -> str:
    if not host_header:
        return ""
    # "hostname" or "hostname:port" — we never bind to an IPv6 address, so
    # there is no "[::1]:port" case to special-case here.
    return host_header.rsplit(":", 1)[0]


def is_allowed_host(host_header: str) -> bool:
    return _hostname_from_host_header(host_header) in ALLOWED_HOSTNAMES


def is_allowed_origin(origin: str) -> bool:
    try:
        parsed = urlparse(origin)
    except ValueError:
        return False
    return parsed.scheme == "http" and parsed.hostname in ALLOWED_ORIGIN_HOSTNAMES


class LocalOnlyGuardMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        host_header = request.headers.get("host", "")
        if not is_allowed_host(host_header):
            logger.warning("Rejected request with unexpected Host header: %r", host_header)
            return JSONResponse({"detail": "Forbidden: unexpected Host header."}, status_code=400)

        if request.method in STATE_CHANGING_METHODS:
            origin = request.headers.get("origin")
            if origin is not None and not is_allowed_origin(origin):
                logger.warning("Rejected state-changing request with foreign Origin: %r", origin)
                return JSONResponse({"detail": "Forbidden: unexpected Origin."}, status_code=403)

            token = request.headers.get(TOKEN_HEADER, "")
            if not session_token.is_valid(token):
                logger.warning("Rejected state-changing request: missing or invalid session token.")
                return JSONResponse({"detail": "Missing or invalid session token."}, status_code=401)

        return await call_next(request)
