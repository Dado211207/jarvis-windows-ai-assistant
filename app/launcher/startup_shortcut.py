""""Start JARVIS when I sign in" — a real Windows Startup shortcut.

Uses the per-user Startup folder (%APPDATA%\\Microsoft\\Windows\\Start
Menu\\Programs\\Startup), which needs no admin rights and no registry
write. Deliberately not a HKCU\\...\\Run registry value: a shortcut is
visible to the user in a folder they can open, is removed by deleting a
file, and matches what the Inno Setup installer's own optional
"startupicon" task already creates — one mechanism, not two competing
ones.

Only ever touches JARVIS's own shortcut, by exact filename. It never
enumerates or removes anything else in the Startup folder.

All Windows-specific work (the COM call that writes a .lnk) is isolated
behind a single injectable seam so the enable/disable/state logic is
testable on this repo's Linux CI, matching how the tray and the window
child already separate decidable logic from native calls.
"""

import os
import sys
from pathlib import Path
from typing import Optional

from app.logging_config import get_logger

logger = get_logger("launcher.startup_shortcut")

SHORTCUT_NAME = "JARVIS.lnk"


def is_supported() -> bool:
    return sys.platform == "win32"


def startup_dir(env: Optional[dict] = None) -> Optional[Path]:
    """The per-user Startup folder, or None when it cannot be resolved
    (non-Windows, or APPDATA unset). Never raises."""
    env = env if env is not None else os.environ
    appdata = env.get("APPDATA")
    if not appdata:
        return None
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def shortcut_path(env: Optional[dict] = None) -> Optional[Path]:
    directory = startup_dir(env)
    return (directory / SHORTCUT_NAME) if directory is not None else None


def is_enabled(env: Optional[dict] = None) -> bool:
    """True only when JARVIS's own shortcut actually exists on disk —
    the state is read from reality, never cached in a settings file that
    could drift out of sync with it."""
    path = shortcut_path(env)
    return bool(path is not None and path.exists())


def _target_executable() -> str:
    """The packaged JARVIS.exe when frozen. In dev there is no single
    executable to point a shortcut at, which is why enable() refuses to
    create one outside a frozen build rather than writing a shortcut that
    would not work."""
    return sys.executable


def _write_shortcut(target: Path, executable: str, icon: Optional[str]) -> None:
    """The one place that touches Windows COM. Imported locally so this
    module stays importable everywhere."""
    import pythoncom  # noqa: F401  (required to initialise COM)
    from win32com.client import Dispatch

    shell = Dispatch("WScript.Shell")
    link = shell.CreateShortCut(str(target))
    link.TargetPath = executable
    link.WorkingDirectory = str(Path(executable).parent)
    if icon:
        link.IconLocation = icon
    link.Description = "JARVIS — local AI assistant"
    link.save()


def enable(env: Optional[dict] = None, writer=None) -> bool:
    """Creates the Startup shortcut. Returns False (never raises) when it
    could not be created, so a settings toggle can report an honest
    failure instead of silently claiming success."""
    if not is_supported() and writer is None:
        logger.info("Start-with-Windows is a Windows-only feature; ignoring.")
        return False

    path = shortcut_path(env)
    if path is None:
        logger.warning("Could not resolve the Startup folder; not creating a shortcut.")
        return False

    if not getattr(sys, "frozen", False) and writer is None:
        # A dev checkout has no single executable to launch; writing a
        # shortcut to the bare interpreter would produce something that
        # does not start JARVIS.
        logger.info("Not creating a Startup shortcut outside a packaged build.")
        return False

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        write = writer if writer is not None else _write_shortcut
        write(path, _target_executable(), _icon_path())
        logger.info("Start-with-Windows enabled.")
        return True
    except Exception:
        logger.warning("Could not create the Startup shortcut.", exc_info=True)
        return False


def disable(env: Optional[dict] = None) -> bool:
    """Removes only JARVIS's own shortcut, by exact filename. Returns
    True when the shortcut is gone afterwards — including when it was
    already absent, since the requested end state holds either way."""
    path = shortcut_path(env)
    if path is None:
        return False
    try:
        path.unlink(missing_ok=True)
        logger.info("Start-with-Windows disabled.")
        return True
    except OSError:
        logger.warning("Could not remove the Startup shortcut.", exc_info=True)
        return False


def _icon_path() -> Optional[str]:
    icon = Path(__file__).resolve().parent.parent / "ui" / "static" / "icon.ico"
    return str(icon) if icon.exists() else None
