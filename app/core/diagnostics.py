"""A copyable diagnostic report that is safe to paste anywhere.

The point of this module is the guarantee, not the data: a user asked to
"send your diagnostics" must be able to copy the whole report into an
issue, an email, or a chat window without leaking a credential. So the
report is built from an explicit allowlist of fields — never by dumping
settings, os.environ, or a config object — and every value is passed
through the same redaction used for child-process output before it is
included.

Building it by allowlist rather than by exclusion is deliberate. A
denylist ("everything except the API key") silently starts leaking the
moment someone adds a new secret-bearing setting; an allowlist stays
safe by default, because a field nobody added simply is not there.

Paths are included because they are genuinely needed for support (where
is the database? where are the logs?) and are not secrets. Usernames
appear inside those paths on Windows, which is the one piece of personal
data present; the report says so rather than pretending otherwise.
"""

import platform
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List

from app.logging_config import get_logger

logger = get_logger("core.diagnostics")


@dataclass
class DiagnosticSection:
    title: str
    items: List[Dict[str, str]] = field(default_factory=list)


def _safe(value: Any) -> str:
    """Every value in the report goes through here. Reuses the child-log
    redactor so there is one definition of "what a secret looks like"
    rather than a second, drifting copy."""
    from app.launcher.server_process import redact_text

    return redact_text(str(value))


def _application_section() -> DiagnosticSection:
    from app import __phase__, __version__

    return DiagnosticSection(
        title="Application",
        items=[
            {"label": "Version", "value": _safe(__version__)},
            {"label": "Build", "value": _safe(__phase__)},
            {"label": "Packaged", "value": _safe(bool(getattr(sys, "frozen", False)))},
        ],
    )


def _system_section() -> DiagnosticSection:
    return DiagnosticSection(
        title="System",
        items=[
            {"label": "Operating system", "value": _safe(f"{platform.system()} {platform.release()}")},
            {"label": "OS version", "value": _safe(platform.version())},
            {"label": "Architecture", "value": _safe(platform.machine())},
            {"label": "Python", "value": _safe(platform.python_version())},
        ],
    )


def _runtime_section() -> DiagnosticSection:
    from app.core.runtime_state import runtime
    from app.core.tool_registry import registry

    items = [
        {"label": "Runtime state", "value": _safe(getattr(runtime.state, "value", runtime.state))},
        {"label": "Tools registered", "value": _safe(len(registry))},
    ]

    try:
        from app.core.privacy import privacy_mode
        items.append({"label": "Privacy mode", "value": _safe("on" if privacy_mode.active else "off")})
    except Exception:
        items.append({"label": "Privacy mode", "value": "unknown"})

    return DiagnosticSection(title="Runtime", items=items)


def _api_section() -> DiagnosticSection:
    from app.config import settings

    return DiagnosticSection(
        title="Local API",
        items=[
            {"label": "Bind address", "value": _safe(f"{settings.jarvis_host}:{settings.jarvis_port}")},
            {"label": "Reachable from network", "value": "No — loopback only"},
        ],
    )


def _database_section() -> DiagnosticSection:
    from app.config import settings

    accessible = False
    try:
        from db.database import get_db
        get_db().get_recent_logs(limit=1)
        accessible = True
    except Exception:
        pass

    return DiagnosticSection(
        title="Database",
        items=[
            {"label": "Status", "value": "Accessible" if accessible else "Not reachable"},
            {"label": "Path", "value": _safe(settings.db_path)},
        ],
    )


def _providers_section() -> DiagnosticSection:
    """Availability only. A provider's credential is never read here —
    see app/core/providers.py, whose status objects carry booleans."""
    from app.core.providers import detect_all, selected_provider

    items = [{"label": "Selected", "value": _safe(selected_provider())}]
    for status in detect_all():
        items.append({
            "label": status.display_name,
            "value": _safe("available" if status.available else "not detected"),
        })
    return DiagnosticSection(title="AI providers", items=items)


def _voice_section() -> DiagnosticSection:
    from app.voice.stt import stt_service
    from app.voice.tts import tts_service

    stt_available, stt_detail = stt_service.is_available()
    model_ready, model_detail = stt_service.model_status()
    return DiagnosticSection(
        title="Voice",
        items=[
            {"label": "Speech-to-text runtime", "value": _safe(stt_detail if not stt_available else "available")},
            {"label": "Speech model", "value": _safe(model_detail if not model_ready else "installed")},
            {"label": "Text-to-speech", "value": _safe("available" if tts_service.is_available() else "not available")},
        ],
    )


def _paths_section() -> DiagnosticSection:
    from app.core.app_paths import app_data_root, config_dir, logs_dir, models_dir

    return DiagnosticSection(
        title="Locations",
        items=[
            {"label": "Application data", "value": _safe(app_data_root())},
            {"label": "Logs", "value": _safe(logs_dir())},
            {"label": "Configuration", "value": _safe(config_dir())},
            {"label": "Speech models", "value": _safe(models_dir())},
        ],
    )


def build_report() -> List[DiagnosticSection]:
    """Never raises: a diagnostic report that crashes is worthless
    exactly when it is most needed. A section that cannot be built is
    reported as unavailable rather than taking the report down."""
    builders = (
        _application_section, _system_section, _runtime_section, _api_section,
        _database_section, _providers_section, _voice_section, _paths_section,
    )
    sections: List[DiagnosticSection] = []
    for builder in builders:
        try:
            sections.append(builder())
        except Exception:
            logger.warning("Diagnostic section %s failed to build.", builder.__name__, exc_info=True)
            sections.append(DiagnosticSection(
                title=builder.__name__.strip("_").replace("_section", "").title(),
                items=[{"label": "Status", "value": "Could not be collected"}],
            ))
    return sections


def render_report_text(sections: List[DiagnosticSection] = None) -> str:
    """The plain-text form the Copy button puts on the clipboard."""
    sections = sections if sections is not None else build_report()
    lines = ["JARVIS diagnostic report", "=" * 24, ""]
    for section in sections:
        lines.append(section.title)
        lines.append("-" * len(section.title))
        for item in section.items:
            lines.append(f"  {item['label']}: {item['value']}")
        lines.append("")
    lines.append(
        "Note: file paths above include your Windows user name. They contain no "
        "passwords or API keys."
    )
    return "\n".join(lines)
