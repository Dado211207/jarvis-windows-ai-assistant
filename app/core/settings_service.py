"""Persistent user settings — Phase 8.

Safe, local-only preferences stored in the SQLite ``settings`` table so JARVIS
remembers them across restarts. Only an explicit allowlist of keys may be set;
values are length-limited, type/enum-validated, and secret-scanned before being
persisted. Secrets/API keys are NEVER stored here — they live only in ``.env``.

``safety_mode`` is intentionally read-only: it is always reported as ``on`` and
cannot be disabled through settings, so no setting can weaken JARVIS's safety
model.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from app.core.models import PermissionLevel, ToolCategory, ToolDefinition
from app.core.secret_guard import find_secret
from app.logging_config import get_logger

logger = get_logger("settings_service")

MAX_VALUE_LEN = 200


@dataclass(frozen=True)
class SettingSpec:
    key: str
    default: str
    label: str
    kind: str  # "text" | "enum" | "bool" | "int" | "float"
    choices: Tuple[str, ...] = field(default_factory=tuple)
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    locked: bool = False


SETTINGS_SPECS: List[SettingSpec] = [
    # Profile
    SettingSpec("user_display_name", "", "Your name", "text"),
    SettingSpec("assistant_name", "JARVIS", "Assistant name", "text"),
    SettingSpec("preferred_language", "English", "Preferred language", "text"),
    SettingSpec(
        "preferred_response_style", "balanced", "Response style", "enum",
        choices=("short", "balanced", "detailed"),
    ),
    SettingSpec(
        "preferred_tone", "friendly", "Tone", "enum",
        choices=("friendly", "formal", "casual", "neutral", "direct"),
    ),
    # UI
    SettingSpec(
        "theme_mode", "dark", "Theme", "enum",
        choices=("dark", "light", "auto"),
    ),
    SettingSpec("compact_mode", "false", "Compact mode", "bool"),
    SettingSpec(
        "dashboard_default_page", "dashboard", "Default page", "enum",
        choices=("dashboard", "chat", "actions", "voice", "logs", "memory",
                 "settings", "help"),
    ),
    # Voice / TTS
    SettingSpec("tts_enabled", "false", "TTS enabled", "bool"),
    SettingSpec("tts_rate", "175", "TTS rate", "int", minimum=50, maximum=400),
    SettingSpec("tts_volume", "1.0", "TTS volume", "float", minimum=0.0, maximum=1.0),
    SettingSpec("tts_voice", "", "TTS voice", "text"),
    # Commands
    SettingSpec("pinned_commands", "", "Pinned commands", "text"),
    # Startup (installed Windows app only; no-op in dev mode)
    SettingSpec("start_with_windows", "false", "Start with Windows", "bool"),
    # Safety (locked — always on, can never be disabled through settings)
    SettingSpec("safety_mode", "on", "Safety mode", "enum",
                choices=("on",), locked=True),
]

_SPEC_BY_KEY: Dict[str, SettingSpec] = {s.key: s for s in SETTINGS_SPECS}
DEFAULTS: Dict[str, str] = {s.key: s.default for s in SETTINGS_SPECS}
SETTABLE_KEYS = [s.key for s in SETTINGS_SPECS if not s.locked]

_TRUE = {"true", "1", "yes", "on", "enabled"}
_FALSE = {"false", "0", "no", "off", "disabled"}


def _normalise_bool(value: str) -> Optional[str]:
    v = value.strip().lower()
    if v in _TRUE:
        return "true"
    if v in _FALSE:
        return "false"
    return None


def validate(key: str, value: str) -> Tuple[bool, str]:
    """Validate a (key, value) pair without persisting. Returns (ok, message)."""
    spec = _SPEC_BY_KEY.get(key)
    if spec is None:
        return False, f"'{key}' is not a recognised setting."
    if spec.locked:
        return False, f"'{spec.label}' is locked and cannot be changed."

    value = (value or "").strip()
    if len(value) > MAX_VALUE_LEN:
        return False, f"Value too long (max {MAX_VALUE_LEN} characters)."

    secret = find_secret(value)
    if secret is not None:
        return False, (
            f"That looks like {secret}. JARVIS never stores secrets or API keys "
            "in settings — keep credentials in your local .env file only."
        )

    if spec.kind == "enum":
        if value.lower() not in spec.choices:
            return False, f"'{spec.label}' must be one of: {', '.join(spec.choices)}."
    elif spec.kind == "bool":
        if _normalise_bool(value) is None:
            return False, f"'{spec.label}' must be true or false."
    elif spec.kind == "int":
        try:
            n = int(value)
        except ValueError:
            return False, f"'{spec.label}' must be a whole number."
        if spec.minimum is not None and n < spec.minimum:
            return False, f"'{spec.label}' must be >= {int(spec.minimum)}."
        if spec.maximum is not None and n > spec.maximum:
            return False, f"'{spec.label}' must be <= {int(spec.maximum)}."
    elif spec.kind == "float":
        try:
            f = float(value)
        except ValueError:
            return False, f"'{spec.label}' must be a number."
        if spec.minimum is not None and f < spec.minimum:
            return False, f"'{spec.label}' must be >= {spec.minimum}."
        if spec.maximum is not None and f > spec.maximum:
            return False, f"'{spec.label}' must be <= {spec.maximum}."
    return True, "ok"


def _canonical(key: str, value: str) -> str:
    """Return the normalised stored form of a validated value."""
    spec = _SPEC_BY_KEY[key]
    value = value.strip()
    if spec.kind == "enum":
        return value.lower()
    if spec.kind == "bool":
        return _normalise_bool(value) or "false"
    return value


def get_all() -> Dict[str, str]:
    """Return every setting as defaults merged with stored values.

    ``safety_mode`` is always forced to ``on``.
    """
    from db.database import get_db

    merged = dict(DEFAULTS)
    try:
        stored = get_db().get_all_settings()
        for k, v in stored.items():
            if k in merged:
                merged[k] = v
    except Exception as exc:  # never crash the caller on a settings read
        logger.warning("Could not read settings: %s", exc)
    merged["safety_mode"] = "on"
    return merged


def get(key: str, default: Optional[str] = None) -> Optional[str]:
    return get_all().get(key, default)


def update(key: str, value: str) -> Tuple[bool, str]:
    """Validate and persist a single setting. Returns (ok, message)."""
    ok, message = validate(key, value)
    if not ok:
        return False, message
    from db.database import get_db

    stored_value = _canonical(key, value)
    get_db().set_setting(key, stored_value)
    label = _SPEC_BY_KEY[key].label
    logger.info("Setting updated: %s", key)
    return True, f"{label} set to '{stored_value}'."


# --- tools -----------------------------------------------------------------

def _show_settings() -> dict:
    current = get_all()
    lines = ["=== JARVIS Settings ==="]
    for spec in SETTINGS_SPECS:
        val = current.get(spec.key, spec.default) or "(not set)"
        lines.append(f"  {spec.label:<18}: {val}")
    lines.append("")
    lines.append("Change with e.g. 'set assistant name to Jarvis' or the Settings page.")
    return {"success": True, "message": "\n".join(lines), "data": current}


def _update_setting(key: str, value: str) -> dict:
    ok, message = update(key, value)
    return {
        "success": ok,
        "message": message,
        "data": {"key": key} if ok else None,
    }


def register_tools(registry) -> None:
    registry.register(
        ToolDefinition(
            name="show_settings",
            description="Show current JARVIS user settings (names, language, style, voice).",
            permission_level=PermissionLevel.SAFE,
            category=ToolCategory.UTILITY,
        ),
        _show_settings,
    )
    registry.register(
        ToolDefinition(
            name="update_setting",
            description="Update a single safe user setting (never stores secrets).",
            permission_level=PermissionLevel.SAFE,
            category=ToolCategory.UTILITY,
        ),
        _update_setting,
    )
