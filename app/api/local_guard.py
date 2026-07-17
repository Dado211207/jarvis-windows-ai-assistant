"""Local-only request guard — the single policy point for every request.

Binding to 127.0.0.1 stops other devices on the network, but not an
unrelated website open in the same browser from directing it to send
requests to this API — the "localhost CSRF" problem — and it says nothing
about which *paths* are safe to expose without authentication at all. This
module is both:

1. **Endpoint classification.** Default-private: every path requires the
   session token unless it is explicitly listed in ``PUBLIC_PATHS``/
   ``PUBLIC_PATH_PREFIXES`` below. A new route that forgets to be added
   anywhere is *protected by default* — the failure mode of forgetting is
   "the new page/endpoint 401s until someone notices," not "silently leaks
   private data." This directly replaces the earlier per-route-method
   scheme (state-changing methods only) with the exact scope this branch's
   own audit called for: private *reads* need the same protection as
   writes.

2. **Transport policy.** Host allowlist (all requests — DNS-rebinding
   defense), Origin allowlist anchored to the *exact active port* JARVIS
   bound to this launch (not "any port" — see ``_expected_port()``), and
   CORS handled natively here (not Starlette's ``CORSMiddleware``, which
   has no way to make its allowed-origin decision at request time against
   a port only known after the server starts).

See docs/SECURITY.md for the full threat model and exact boundaries.
"""

from urllib.parse import urlparse

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.core import session_token
from app.logging_config import get_logger

logger = get_logger("api.local_guard")

ALLOWED_HOSTNAMES = frozenset({"127.0.0.1", "localhost", "testserver"})
ALLOWED_ORIGIN_HOSTNAMES = frozenset({"127.0.0.1", "localhost"})
STATE_CHANGING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
TOKEN_HEADER = "x-jarvis-token"

# Every request whose path is NOT covered here requires a valid session
# token, regardless of HTTP method. Keep this list small and deliberate.
_PUBLIC_PATH_PREFIXES = ("/ui/static/",)
_PUBLIC_PATHS = frozenset({
    "/health",       # minimal status only — see app/api/routes.py's trimmed HealthResponse
    "/docs", "/redoc", "/openapi.json",  # API schema/documentation, not application data
})


def _is_ui_page_shell(path: str) -> bool:
    """The HTML page routes (/ui/, /ui/chat, /ui/onboarding, ...) — these
    carry no private data themselves, just page structure plus the
    server-rendered session token. They ARE the trusted bootstrap: a
    browser's very first request can't have the token yet, and this is how
    it gets one. Excludes /ui/static/* (handled separately, cacheable)."""
    if not path.startswith("/ui/") and path != "/ui":
        return False
    return not path.startswith("/ui/static/")


def is_public_path(path: str) -> bool:
    if path in _PUBLIC_PATHS:
        return True
    if any(path.startswith(p) for p in _PUBLIC_PATH_PREFIXES):
        return True
    if _is_ui_page_shell(path):
        return True
    return False


def _hostname_from_host_header(host_header: str) -> str:
    if not host_header:
        return ""
    # "hostname" or "hostname:port" — never "[::1]:port": JARVIS only binds
    # IPv4, and IPv6 loopback is intentionally not in ALLOWED_HOSTNAMES.
    return host_header.rsplit(":", 1)[0]


def _port_from_host_header(host_header: str) -> "int | None":
    if not host_header or ":" not in host_header:
        return None
    try:
        return int(host_header.rsplit(":", 1)[1])
    except ValueError:
        return None


def _expected_port() -> "int | None":
    """The exact port JARVIS actually bound to this launch, or None when
    that isn't known yet (dev/test — TestClient never calls run_api()/
    run_production(), so no real port was ever bound). Both real entry
    points (app.api.server.run_api and app.core.launcher.run_production)
    always call runtime_state.set_actual_port() before the server can
    receive its first real request, so this is only ever None outside a
    genuine running instance."""
    from app.core import runtime_state
    return runtime_state.get_actual_port()


def is_allowed_host(host_header: str) -> bool:
    hostname = _hostname_from_host_header(host_header)
    if hostname not in ALLOWED_HOSTNAMES:
        return False
    if hostname == "testserver":
        return True  # Starlette TestClient's fixed synthetic host — never a real network name
    expected_port = _expected_port()
    if expected_port is None:
        return True  # dev/test: no bound port to pin against
    return _port_from_host_header(host_header) == expected_port


def is_allowed_origin(origin: str) -> bool:
    try:
        parsed = urlparse(origin)
        # .port is lazily parsed and raises ValueError on a malformed value
        # (e.g. "http://127.0.0.1:5555.attacker.example" — .hostname alone
        # would happily return "127.0.0.1" for that, since it only splits on
        # the first colon; accessing .port here, inside the same try, is
        # what actually catches it instead of leaking an unhandled exception
        # out of the middleware).
        port = parsed.port
    except ValueError:
        return False
    if parsed.scheme != "http" or parsed.hostname not in ALLOWED_ORIGIN_HOSTNAMES:
        return False
    expected_port = _expected_port()
    if expected_port is None:
        return True  # dev/test: no bound port to pin against
    return port == expected_port


def _reject(status_code: int, detail: str) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=status_code)


def _cors_preflight_response(origin: str) -> Response:
    resp = Response(status_code=204)
    resp.headers["Access-Control-Allow-Origin"] = origin
    resp.headers["Vary"] = "Origin"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, PATCH, DELETE"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Jarvis-Token"
    resp.headers["Access-Control-Max-Age"] = "600"
    # Never Access-Control-Allow-Credentials — JARVIS uses no cookies, and
    # never pair credentials with a reflected origin.
    return resp


class LocalOnlyGuardMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        host_header = request.headers.get("host", "")
        if not is_allowed_host(host_header):
            logger.warning("Rejected request with unexpected Host header: %r", host_header)
            return _reject(400, "Forbidden: unexpected Host header.")

        origin = request.headers.get("origin")
        origin_ok = origin is not None and is_allowed_origin(origin)

        # CORS preflight — handled natively so the allowed-origin decision
        # is made at request time against the exact active port, which
        # Starlette's CORSMiddleware cannot do (its config is fixed at
        # app-startup, before the port is known).
        if request.method == "OPTIONS" and "access-control-request-method" in request.headers:
            if origin_ok:
                return _cors_preflight_response(origin)
            logger.warning("Rejected CORS preflight with foreign Origin: %r", origin)
            return _reject(403, "Forbidden: unexpected Origin.")

        path = request.url.path
        is_state_changing = request.method in STATE_CHANGING_METHODS
        requires_token = is_state_changing or not is_public_path(path)

        if requires_token:
            if origin is not None and not origin_ok:
                logger.warning("Rejected request with foreign Origin: %r", origin)
                return _reject(403, "Forbidden: unexpected Origin.")
            token = request.headers.get(TOKEN_HEADER, "")
            if not session_token.is_valid(token):
                logger.warning(
                    "Rejected %s %s: missing or invalid session token.", request.method, path
                )
                return _reject(401, "Missing or invalid session token.")

        response = await call_next(request)

        if origin_ok:
            response.headers["Access-Control-Allow-Origin"] = origin
            existing_vary = response.headers.get("vary")
            response.headers["Vary"] = f"{existing_vary}, Origin" if existing_vary else "Origin"

        # Token-bearing responses (the UI HTML shells, which embed the live
        # session token, and every protected endpoint) must never be cached
        # or stored — by a browser disk cache, an intermediary, or reused
        # across launches.
        if requires_token or _is_ui_page_shell(path):
            response.headers["Cache-Control"] = "no-store, private"
            response.headers["Pragma"] = "no-cache"

        return response
