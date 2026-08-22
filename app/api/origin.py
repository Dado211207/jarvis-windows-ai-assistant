"""Local-origin allowlist shared by CORS middleware and the WebSocket
handshake.

JARVIS is loopback-only (see app/config.py's jarvis_host default). The
only origins that should ever be allowed to talk to it are its own
dashboard, served from 127.0.0.1/localhost on its configured port — not
an arbitrary web page that happens to be open in the same browser, which
could otherwise open a cross-origin WebSocket to a locally running JARVIS
and observe or (on future authenticated-mutation endpoints) trigger
actions on the user's behalf. Browsers do not apply CORS preflight to
WebSocket handshakes, so this check has to happen in application code.
"""

from typing import List, Optional

from app.config import settings


def allowed_origins() -> List[str]:
    port = settings.jarvis_port
    return [
        f"http://127.0.0.1:{port}",
        f"http://localhost:{port}",
    ]


def is_allowed_origin(origin: Optional[str]) -> bool:
    """True if *origin* is safe to serve.

    No Origin header at all (typical for non-browser tools connecting
    directly — curl, a local script, a test client) is allowed, since a
    browser cannot forge a WebSocket handshake without sending a real
    Origin header. Only a *present but foreign* Origin indicates a
    cross-origin browser page and is rejected.
    """
    if origin is None:
        return True
    return origin in allowed_origins()
