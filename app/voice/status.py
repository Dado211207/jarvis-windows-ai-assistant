"""One description of the voice system, rendered two ways.

The Voice page, the diagnostics panel, `/voice/status`, and the `speak
status` command all have to agree about what is installed and what is
speaking. They agree by being the same function: this one builds the
facts once, and the API serves them while the CLI formats them.

Two flags is how the desktop app ended up never speaking at all. Two
descriptions of the same state is the same mistake one level up.
"""

from typing import Dict, List

from app.voice import engines
from app.voice.kokoro import assets, install


def snapshot() -> Dict:
    """Everything the UI needs about speech output, computed now."""
    from app.voice.tts import tts_service

    voice_key = engines.selected_voice_key()
    statuses = engines.statuses(voice_key)
    active = next((status for status in statuses if status.active), None)

    return {
        "speaks_replies": tts_service.output_enabled,
        "available": active is not None,
        "active_engine": active.key if active else engines.NONE,
        "active_engine_name": active.display_name if active else engines.DISPLAY_NAMES[engines.NONE],
        "engines": [
            {
                "key": status.key,
                "name": status.display_name,
                "available": status.available,
                "detail": status.detail,
                "tier": status.tier,
                "active": status.active,
            }
            for status in statuses
        ],
        "voice_key": voice_key,
        "voice_name": assets.resolve_voice(voice_key).display_name,
        "voices": engines.installed_voices(),
        "speed": engines.selected_speed(),
        "model_installed": install.is_installed(voice_key),
        "download_bytes_required": install.bytes_required(voice_key),
        "install_dir": str(install.install_dir()),
    }


def status_text() -> str:
    """The same facts as lines of text, for the command line."""
    data = snapshot()
    lines: List[str] = [
        "Voice output status:",
        f"  Speaks replies : {data['speaks_replies']}",
        f"  Speaking with  : {data['active_engine_name']}",
        f"  Voice          : {data['voice_name']}",
        f"  Speed          : {data['speed']:.2f}x",
        "",
        "Engines, best first:",
    ]
    for item in data["engines"]:
        mark = "*" if item["active"] else ("+" if item["available"] else "-")
        lines.append(f"  {mark} {item['name']}: {item['detail']}")
    if not data["available"]:
        lines += ["", engines.unavailable_message(data["voice_key"])]
    return "\n".join(lines)
