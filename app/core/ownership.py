"""Everything JARVIS owns, and everything it must never touch.

"Uninstall completely" is only a meaningful promise if there is a list.
Without one it degrades into whichever paths somebody happened to
remember, which is how an uninstalled application leaves a Startup
shortcut pointing at a deleted executable and an API key in Windows
Credential Manager.

**The list is here, in code, and the installer is checked against it.**
`packaging/jarvis.iss` removes the files it installed; this module
removes what the *application* created while it ran, which the installer
never knew about. A test asserts neither list has grown a member the
other has never heard of.

**What is deliberately not ours**, and is never removed:

  * **WebView2 and the Visual C++ runtime.** Shared Windows components.
    Other applications depend on them, and an uninstaller that removes a
    shared component because one of its users left is a bug in that
    uninstaller, not a courtesy.
  * **Ollama and its models.** Separate software with its own publisher
    and its own uninstaller. Even when JARVIS installed it — recorded as
    `ollama_installed_by_jarvis` — removing it silently would be
    deciding, on somebody's behalf, that they no longer want local AI
    because they no longer want JARVIS. It is reported, so a person can
    remove it themselves, and never removed here.
  * **Anything in the notes folder.** `~/Documents/JARVIS_Notes` holds
    documents a person wrote. Uninstalling a program is not consent to
    delete what was written with it.

Nothing here deletes anything unless it is called, and the caller is the
uninstaller.
"""

import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from app.logging_config import get_logger

logger = get_logger("core.ownership")


@dataclass(frozen=True)
class Owned:
    """One thing JARVIS created, and when it may be removed."""

    key: str
    what: str
    where: str
    # True when it is removed by an ordinary uninstall; False when it
    # only goes on an explicit "remove my data too".
    always: bool


@dataclass(frozen=True)
class NotOurs:
    """Something JARVIS uses but did not create, or did create and still
    has no business deleting."""

    key: str
    what: str
    why: str


# What the *installer* put on the machine. Removed by Inno Setup itself;
# listed here so the two halves can be checked against each other.
INSTALLED_BY_SETUP = (
    Owned("install_dir", "The application itself",
          r"%LOCALAPPDATA%\Programs\JARVIS", always=True),
    Owned("start_menu", "Start menu shortcuts",
          r"%APPDATA%\Microsoft\Windows\Start Menu\Programs\JARVIS", always=True),
    Owned("desktop_icon", "The desktop shortcut, if you asked for one",
          r"%USERPROFILE%\Desktop\JARVIS.lnk", always=True),
    Owned("setup_startup_icon", "The sign-in shortcut, if you asked for one at install time",
          r"%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\JARVIS.lnk", always=True),
    Owned("uninstall_entry", "The Apps & features entry",
          r"HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall\JARVIS", always=True),
)

# What the *application* created while it ran. The installer has never
# heard of any of these, which is exactly why they need their own list.
CREATED_AT_RUNTIME = (
    Owned("runtime_startup_shortcut", "The sign-in shortcut, if you switched it on in Settings",
          r"%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\JARVIS.lnk", always=True),
    Owned("credential", "Your Anthropic API key, in Windows Credential Manager",
          "Windows Credential Manager (JARVIS)", always=False),
    # Its own entry, because it is its own credential. Two keys for two
    # services are granted and revoked independently, and a manifest that
    # named only one would leave the other behind while reporting
    # success — which is the exact failure this module exists to prevent.
    Owned("elevenlabs_credential", "Your ElevenLabs API key, in Windows Credential Manager",
          "Windows Credential Manager (JARVIS)", always=False),
    Owned("data_dir", "Settings, chat history, logs, and any voice or speech model you downloaded",
          r"%LOCALAPPDATA%\JARVIS", always=False),
)

NEVER_REMOVED = (
    NotOurs("webview2", "Microsoft Edge WebView2 Runtime",
            "A shared Windows component other applications use. Removing it because "
            "JARVIS left would break them."),
    NotOurs("vcredist", "Microsoft Visual C++ Runtime",
            "The same: shared, and depended on by other software."),
    NotOurs("ollama", "Ollama and any models you downloaded",
            "Separate software with its own publisher and its own uninstaller. Even "
            "when JARVIS installed it for you, removing it would be deciding you no "
            "longer want local AI because you no longer want JARVIS."),
    NotOurs("notes", "Notes you wrote, in Documents\\JARVIS_Notes",
            "Documents you created. Uninstalling a program is not consent to delete "
            "what was written with it."),
)


@dataclass
class RemovalReport:
    """What actually happened, item by item. Never a summary — an
    uninstaller that says "done" while a step failed silently is how a
    key stays in Credential Manager."""

    removed: List[str] = field(default_factory=list)
    not_present: List[str] = field(default_factory=list)
    failed: List[str] = field(default_factory=list)
    kept: List[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "removed": list(self.removed),
            "not_present": list(self.not_present),
            "failed": list(self.failed),
            "kept": list(self.kept),
        }


def data_dir() -> Path:
    """%LOCALAPPDATA%\\JARVIS, matching app/core/app_paths.py."""
    from app.core.app_paths import app_data_root

    return app_data_root()


def startup_shortcut_path() -> Optional[Path]:
    from app.launcher.startup_shortcut import shortcut_path

    return shortcut_path()


def remove(purge_data: bool = False) -> RemovalReport:
    """Remove what the application created. Never raises.

    *purge_data* is the difference between "uninstall" and "uninstall and
    forget me": without it, settings, chat history and downloaded models
    stay, which is what somebody reinstalling next week wants. It is
    never the default and is never inferred.
    """
    report = RemovalReport()

    _remove_startup_shortcut(report)
    if purge_data:
        _remove_credential(report)
        _remove_data_dir(report)
    else:
        report.kept.append("Your settings, history and downloaded models")
        report.kept.append("Your API key in Windows Credential Manager")

    for item in NEVER_REMOVED:
        report.kept.append(item.what)
    return report


def _remove_startup_shortcut(report: RemovalReport) -> None:
    """Always removed, even on a data-preserving uninstall.

    Not a preference: it points at an executable that is about to stop
    existing, so leaving it means Windows tries to launch a deleted file
    at every sign-in.
    """
    try:
        path = startup_shortcut_path()
    except Exception:  # noqa: BLE001
        report.failed.append("The sign-in shortcut could not be located.")
        return
    if path is None:
        report.not_present.append("The sign-in shortcut")
        return
    try:
        if path.exists():
            path.unlink()
            report.removed.append("The sign-in shortcut")
        else:
            report.not_present.append("The sign-in shortcut")
    except OSError as exc:
        logger.warning("Could not remove the startup shortcut: %s", exc)
        report.failed.append(f"The sign-in shortcut ({exc})")


def _remove_credential(report: RemovalReport) -> None:
    """Delete the API key through the app's own credential module.

    Done here rather than from the installer on purpose: only this code
    knows how the key was stored, and an installer guessing at a
    Credential Manager target name is how an uninstall leaves a secret
    behind while reporting success.
    """
    from app.core import credentials

    for label, read, clear in (
        ("Your Anthropic API key in Windows Credential Manager",
         credentials.get_stored_api_key, credentials.clear_stored_api_key),
        ("Your ElevenLabs API key in Windows Credential Manager",
         credentials.get_elevenlabs_key, credentials.clear_elevenlabs_key),
        ("Your OpenAI voice API key in Windows Credential Manager",
         credentials.get_openai_key, credentials.clear_openai_key),
    ):
        try:
            if not read():
                report.not_present.append(label)
                continue
            if clear():
                report.removed.append(label)
            else:
                report.failed.append(label)
        except Exception as exc:  # noqa: BLE001
            # The exception class, not str(exc): this line goes into a
            # log, and a credential-store error can quote what it was
            # looking for.
            logger.warning("Could not remove a stored API key: %s", exc.__class__.__name__)
            report.failed.append(label)


def _remove_data_dir(report: RemovalReport) -> None:
    try:
        target = data_dir()
    except Exception:  # noqa: BLE001
        report.failed.append("The JARVIS data folder could not be located.")
        return
    if not target.exists():
        report.not_present.append("The JARVIS data folder")
        return

    # A guard, not a formality: this deletes a tree, and a data_dir()
    # that resolved to a drive root or a home directory because an
    # environment variable was empty must stop here rather than proceed.
    resolved = target.resolve()
    if resolved.name.lower() != "jarvis" or resolved == resolved.anchor:
        report.failed.append(f"Refused to delete {resolved} — that is not the JARVIS data folder.")
        return

    try:
        shutil.rmtree(resolved)
        report.removed.append(f"The JARVIS data folder ({resolved})")
    except OSError as exc:
        logger.warning("Could not remove the data folder: %s", exc)
        report.failed.append(f"The JARVIS data folder ({exc})")


def describe_for_user() -> str:
    """The manifest, in words, for the uninstall prompt and the docs."""
    lines = ["Removed by uninstalling JARVIS:"]
    for item in INSTALLED_BY_SETUP:
        lines.append(f"  - {item.what} — {item.where}")
    for item in CREATED_AT_RUNTIME:
        if item.always:
            lines.append(f"  - {item.what} — {item.where}")
    lines.append("")
    lines.append("Removed only if you choose to remove your data as well:")
    for item in CREATED_AT_RUNTIME:
        if not item.always:
            lines.append(f"  - {item.what} — {item.where}")
    lines.append("")
    lines.append("Never removed:")
    for item in NEVER_REMOVED:
        lines.append(f"  - {item.what} — {item.why}")
    return "\n".join(lines)


def _windows_only_note() -> str:
    return "" if sys.platform == "win32" else " (this machine is not Windows)"


def environment_summary() -> str:
    """Where the two trees actually are on this machine, for diagnostics."""
    return (
        f"install: {os.environ.get('LOCALAPPDATA', '?')}\\Programs\\JARVIS; "
        f"data: {data_dir()}{_windows_only_note()}"
    )
