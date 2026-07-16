"""First-run onboarding state machine — installed Windows app only.

Dev mode never triggers onboarding: ``is_required()`` is only true for a
frozen build that hasn't finished the wizard yet (``paths.is_frozen()`` is
always False under ``python -m app.main`` / pytest). Completion is recorded
in ``paths.onboarding_flag_path()`` and is only ever written after the
wizard reaches a coherent end state — a validated key, an explicit skip, or
an already-configured key found on a previous run. A crash, closed window,
or failed validation never marks onboarding complete, so JARVIS returns to
the wizard on the next launch instead of running unconfigured.

State is kept in memory (module-level), matching the existing in-memory
``PendingActionStore`` pattern for Phase 5 approvals — there is exactly one
desktop app instance and the wizard only ever needs to survive a single run.
"""

import json
import time
from dataclasses import dataclass
from typing import Optional, Tuple

from app.core import paths
from app.logging_config import get_logger

logger = get_logger("onboarding")

STEPS = ("welcome", "privacy", "api_key", "voice", "startup_pref", "finish")


@dataclass
class _OnboardingState:
    step: str = "welcome"
    api_key_status: str = "not_set"  # not_set | validated | skipped | invalid
    api_key_error: Optional[str] = None
    tts_enabled: bool = False
    start_with_windows: bool = False


_state = _OnboardingState()


def is_complete() -> bool:
    return paths.onboarding_flag_path().exists()


def is_required() -> bool:
    """True only when a frozen build hasn't completed onboarding yet."""
    return paths.is_frozen() and not is_complete()


def get_state() -> dict:
    from app.core import secret_store

    return {
        "step": _state.step,
        "steps": list(STEPS),
        "api_key_status": _state.api_key_status,
        "api_key_error": _state.api_key_error,
        "api_key_masked": secret_store.mask_api_key(secret_store.load_api_key() or ""),
        "secure_storage_available": secret_store.is_available(),
        "tts_enabled": _state.tts_enabled,
        "start_with_windows": _state.start_with_windows,
        "complete": is_complete(),
    }


def set_step(step: str) -> dict:
    if step not in STEPS:
        return {"success": False, "error": f"Unknown onboarding step '{step}'."}
    _state.step = step
    return {"success": True}


def submit_api_key(api_key: str) -> dict:
    """Shape-check, live-validate, and securely store *api_key*.

    Never logs or echoes the raw key back — only a masked form and a
    human-readable error classification.
    """
    from app.core import secret_store

    api_key = (api_key or "").strip()
    if not secret_store.looks_like_anthropic_key(api_key):
        _state.api_key_status = "invalid"
        _state.api_key_error = "That doesn't look like a valid Anthropic API key."
        return {"success": False, "error": _state.api_key_error}

    ok, error = _validate_with_anthropic(api_key)
    if not ok:
        _state.api_key_status = "invalid"
        _state.api_key_error = error
        logger.warning("Onboarding API key validation failed: %s", error)
        return {"success": False, "error": error}

    try:
        secret_store.save_api_key(api_key)
    except secret_store.SecretStoreError as exc:
        _state.api_key_status = "invalid"
        _state.api_key_error = str(exc)
        logger.warning("Onboarding API key save failed: %s", exc)
        return {"success": False, "error": str(exc)}

    _state.api_key_status = "validated"
    _state.api_key_error = None
    logger.info("Onboarding: API key validated and stored securely.")
    return {"success": True, "api_key_masked": secret_store.mask_api_key(api_key)}


def skip_api_key() -> dict:
    _state.api_key_status = "skipped"
    _state.api_key_error = None
    return {"success": True}


def remove_api_key() -> dict:
    from app.core import secret_store

    secret_store.delete_api_key()
    _state.api_key_status = "not_set"
    _state.api_key_error = None
    return {"success": True}


def _validate_with_anthropic(api_key: str) -> Tuple[bool, Optional[str]]:
    try:
        import anthropic

        client = anthropic.Anthropic(api_key=api_key, timeout=15.0)
        client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1,
            messages=[{"role": "user", "content": "hi"}],
        )
        return True, None
    except Exception as exc:  # classify without ever logging api_key
        return False, _classify_error(exc)


def _classify_error(exc: Exception) -> str:
    import anthropic

    if isinstance(exc, anthropic.AuthenticationError):
        return "That API key was rejected by Anthropic. Double-check it and try again."
    if isinstance(exc, anthropic.RateLimitError):
        return "Anthropic reported a rate limit while validating the key. Try again shortly, or skip for now."
    if isinstance(exc, anthropic.APIConnectionError):
        return "Couldn't reach Anthropic — check your internet connection, or skip for now and add a key later."
    if isinstance(exc, anthropic.APIStatusError):
        return "Anthropic's API is currently unavailable right now. Try again shortly, or skip for now."
    return "Could not validate the API key right now. Try again, or skip for now."


def set_voice_preference(enabled: bool) -> dict:
    from app.core import settings_service

    settings_service.update("tts_enabled", "true" if enabled else "false")
    _state.tts_enabled = bool(enabled)
    return {"success": True}


def set_startup_preference(enabled: bool) -> dict:
    from app.core import settings_service

    settings_service.update("start_with_windows", "true" if enabled else "false")
    _state.start_with_windows = bool(enabled)
    return {"success": True}


def complete() -> dict:
    """Mark onboarding complete. Refuses while the API key step is unresolved."""
    if _state.api_key_status not in ("validated", "skipped"):
        return {
            "success": False,
            "error": "Finish the API key step (add a key or skip it) before continuing.",
        }
    flag_path = paths.onboarding_flag_path()
    payload = {
        "complete": True,
        "completed_at": time.time(),
        "api_key_status": _state.api_key_status,
    }
    flag_path.write_text(json.dumps(payload), encoding="utf-8")
    logger.info("Onboarding complete. api_key_status=%s", _state.api_key_status)
    return {"success": True}


def reset_state_for_tests() -> None:
    """Test-only: reset in-memory wizard state. Does not touch the flag file."""
    global _state
    _state = _OnboardingState()
