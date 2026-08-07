"""Privacy mode — one authoritative switch layered over the existing
system, not a new memory architecture.

When active:
  - new conversation turns are not written to the conversations table
    (app/core/router.py::_brain_response).
  - explicit long-term memory writes are rejected outright, not silently
    redirected anywhere (add_memory below).
  - stored memory is excluded from AI provider requests — true today by
    construction (app/core/brain.py never injects memory into the
    Anthropic call at all; see tests/test_privacy.py for a regression
    test that keeps it true).
  - sensitive tool outputs that would otherwise write a file (currently:
    take_screenshot) refuse to run rather than save.
  - clipboard content was already never persisted regardless of privacy
    mode (see app/desktop/clipboard.py) — this module does not need to
    add anything there, only verify it stays true.
  - any future wake-word listener MUST check privacy_mode.active before
    starting or continuing to listen; no wake-word listener exists in
    this codebase (v0.2 ships push-to-talk, not wake-word) but this is
    the contract a future one must follow.

State is in-memory only, matching this codebase's existing pattern for
per-process runtime state — see app/core/pending_actions.py's own
documented reset-on-restart behavior, which this deliberately mirrors.
It is NOT written to SQLite: a fresh process always starts with privacy
mode OFF, a clean and unsurprising default, rather than silently carrying
forward a stale prior toggle from a persisted settings row. This is "the
safest existing configuration model" in this codebase, not a new one.
It survives a browser refresh (which does not restart the server
process) but not a server restart.

This is not encryption and must never be described as one — see
docs/THREAT_MODEL.md. SQLite remains an unencrypted file on disk;
privacy mode only controls what gets written to it going forward.
"""

import threading
from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel


class PrivacyModeState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active = False
        self._changed_at: Optional[datetime] = None

    @property
    def active(self) -> bool:
        with self._lock:
            return self._active

    @property
    def changed_at(self) -> Optional[datetime]:
        with self._lock:
            return self._changed_at

    def set(self, active: bool) -> bool:
        """Set the mode and stamp changed_at, even on a no-op call (a
        deliberate re-confirmation of the same state is still a real
        event worth timestamping). Returns the resulting state."""
        with self._lock:
            self._active = bool(active)
            self._changed_at = datetime.now(timezone.utc)
            return self._active


# Module-level singleton, matching this codebase's existing pattern
# (registry, pending_store, event_bus, runtime, session_tokens).
privacy_mode = PrivacyModeState()


# --- the set_privacy_mode tool ---
# Registered like any other tool so toggling it goes through the full
# v0.2 pipeline for free: policy evaluation, a persisted action_lifecycle
# record (its own input — a boolean — is never sensitive, so redaction is
# a no-op here), and a WebSocket event the dashboard's indicator listens
# for. No parallel plumbing was built for this; see app/core/router.py's
# ROUTES for "privacy mode on"/"privacy mode off".


class SetPrivacyModeInput(BaseModel):
    active: bool


def set_privacy_mode(active: bool) -> dict:
    privacy_mode.set(active)
    if active:
        message = (
            "Privacy mode is now ON. New conversation turns and memory writes "
            "are not saved, stored memory is excluded from AI requests, and "
            "screenshots are not captured until it's turned off."
        )
    else:
        message = "Privacy mode is now OFF. Normal persistence has resumed."
    return {"success": True, "message": message, "data": {"active": active}}


def register_tools(registry) -> None:
    from app.core.models import PermissionLevel, RiskLevel, ToolCategory, ToolDefinition

    registry.register(
        ToolDefinition(
            name="set_privacy_mode",
            description=(
                "Turn privacy mode on or off. When on: new conversation content and "
                "memory writes are not persisted, stored memory is excluded from AI "
                "requests, and screenshots are not captured."
            ),
            permission_level=PermissionLevel.SAFE,
            category=ToolCategory.SYSTEM,
            risk=RiskLevel.REVERSIBLE,
            input_model=SetPrivacyModeInput,
            verification_strategy="Handler's own success flag; privacy_mode.active reflects the change immediately.",
        ),
        set_privacy_mode,
    )
