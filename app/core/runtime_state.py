"""In-memory runtime facts that aren't known until after startup — currently
just the actual port the API bound to (which can differ from the configured
default if it was already taken; see app.core.launcher.find_free_port).

Not persisted anywhere; exists only for the lifetime of this process, purely
so Diagnostics can report the real port instead of the configured default.
"""

from typing import Optional

_actual_port: Optional[int] = None


def set_actual_port(port: int) -> None:
    global _actual_port
    _actual_port = port


def get_actual_port() -> Optional[int]:
    return _actual_port
