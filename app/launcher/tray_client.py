"""Minimal HTTP client the tray uses to talk to JARVIS's own loopback
API — the same REST endpoints the dashboard's own JS calls (GET /health,
GET /privacy/status, POST /command), not a private back-door API.

Session-token handling mirrors app.js's getSessionCookie() /
X-JARVIS-Session-Token pattern exactly: app.api.session.SessionCookieMiddleware
sets a jarvis_session cookie on every response; a mutating request must
echo that same value back as a header (double-submit). httpx.Client's
cookie jar collects the cookie automatically; only the header needs
building here.
"""

from typing import Optional

import httpx

from app.api.session import COOKIE_NAME, HEADER_NAME
from app.logging_config import get_logger

logger = get_logger("launcher.tray_client")


class TrayApiClient:
    def __init__(self, host: str, port: int, timeout: float = 3.0) -> None:
        self._client = httpx.Client(base_url=f"http://{host}:{port}", timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "TrayApiClient":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def _session_token(self) -> str:
        token = self._client.cookies.get(COOKIE_NAME)
        if not token:
            # Prime the cookie jar — any response sets a fresh token.
            try:
                self._client.get("/health")
            except Exception:
                return ""
            token = self._client.cookies.get(COOKIE_NAME)
        return token or ""

    def is_healthy(self) -> bool:
        try:
            response = self._client.get("/health")
            return response.status_code == 200 and response.json().get("healthy") is True
        except Exception:
            return False

    def privacy_active(self) -> Optional[bool]:
        """None means "couldn't determine" (server unreachable) — distinct
        from a real False, so the tray can show an honest "unknown"
        state instead of falsely claiming privacy mode is off."""
        try:
            response = self._client.get("/privacy/status")
            if response.status_code == 200:
                return bool(response.json().get("active"))
        except Exception:
            pass
        return None

    def clap_label(self) -> str:
        """One line about the double-clap listener, or "" if unreachable.

        The server composes it (app/voice/clap.py::tray_label) rather than
        the tray, so there is one place that decides when "On" is honest —
        and that place refuses to say it unless a page has recently
        reported a live microphone. "" leaves the row out of the menu
        entirely; a guess would be worse than a gap.
        """
        try:
            response = self._client.get("/voice/clap")
            if response.status_code == 200:
                return str(response.json().get("tray_label") or "")
        except Exception:
            pass
        return ""

    def set_privacy_mode(self, enable: bool) -> bool:
        token = self._session_token()
        if not token:
            logger.warning("Tray could not obtain a session token — privacy toggle aborted.")
            return False
        try:
            response = self._client.post(
                "/command",
                json={"command": "privacy mode on" if enable else "privacy mode off"},
                headers={HEADER_NAME: token},
            )
            return response.status_code == 200 and bool(response.json().get("success"))
        except Exception:
            logger.warning("Tray failed to toggle privacy mode.", exc_info=True)
            return False
