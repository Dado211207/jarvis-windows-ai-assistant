"""Secure storage for the Anthropic API key in production (frozen) builds.

Dev mode is unaffected: developers keep using a local, gitignored ``.env``
file (already outside JARVIS's threat boundary — the developer's own
machine, not shipped to anyone). This module exists only for the installed
Windows app, where there is no ``.env`` to edit and the key must never be
written to disk as plaintext (not in a config file, not in the SQLite
settings table, not in a log, not in a crash report).

Storage decision: Windows DPAPI (``CryptProtectData`` / ``CryptUnprotectData``)
rather than the Credential Manager vault API. DPAPI is the lower-level
primitive Credential Manager itself is built on; encrypting a small blob and
storing it as ``%LOCALAPPDATA%\\JARVIS\\config\\secret.bin`` gives the same
"only this Windows user, on this machine, can decrypt it" guarantee without
adding a Credential-Manager-vault dependency (``keyring`` / entry-count and
naming quirks). The only extra dependency is ``pywin32`` (``win32crypt``),
already ubiquitous in Windows Python builds.

Threat model
------------
Protects against:
  * Casual inspection of the JARVIS install/data directory (e.g. by another
    application, a file backup tool, or someone browsing the filesystem).
  * Other Windows user accounts on the same machine — DPAPI ties decryption
    to the encrypting user's login credentials.
  * The key ending up in plaintext in ``.env``, browser localStorage/
    sessionStorage, the SQLite database, application logs, or diagnostics
    exports (all of those paths are explicitly disallowed; see
    docs/SECURITY.md).

Does NOT protect against:
  * Malware or another process already running as the same Windows user —
    DPAPI decrypts transparently in that context, same as Credential Manager
    would.
  * An attacker with an unlocked, logged-in session as the user.
  * A compromised JARVIS process itself reading its own decrypted key.

This is the same trust boundary every desktop app that stores a local API
key operates under; it is documented here rather than left implicit.
"""

import os
import re
import sys
from pathlib import Path
from typing import Optional

from app.core import paths

_SECRET_FILENAME = "secret.bin"
_ENTROPY = b"JARVIS-anthropic-api-key-v1"

_KEY_SHAPE = re.compile(r"^sk-ant-[A-Za-z0-9_\-]{10,}$")


class SecretStoreError(Exception):
    """Raised when the secure secret store cannot save or load a key."""


def _is_windows() -> bool:
    return sys.platform == "win32"


def is_available() -> bool:
    """True when DPAPI-backed secure storage can actually be used.

    False on any non-Windows platform (dev/CI), and False on Windows if
    ``pywin32`` isn't installed — callers must handle both cases (onboarding
    shows a clear "secure storage unavailable" state rather than silently
    falling back to plaintext).
    """
    if not _is_windows():
        return False
    try:
        import win32crypt  # noqa: F401
    except ImportError:
        return False
    return True


def _secret_file_path() -> Path:
    return paths.config_dir() / _SECRET_FILENAME


def _dpapi_protect(plaintext: bytes) -> bytes:
    import win32crypt
    return win32crypt.CryptProtectData(plaintext, "JARVIS Anthropic API key", _ENTROPY, None, None, 0)


def _dpapi_unprotect(ciphertext: bytes) -> bytes:
    import win32crypt
    return win32crypt.CryptUnprotectData(ciphertext, _ENTROPY, None, None, 0)[1]


def looks_like_anthropic_key(api_key: str) -> bool:
    """Cheap shape check only — never a substitute for a real validation call."""
    return bool(api_key) and bool(_KEY_SHAPE.match(api_key.strip()))


def mask_api_key(api_key: str) -> str:
    """Human-safe representation for onboarding/diagnostics UI and logs.

    Never returns enough of the key to reconstruct it.
    """
    if not api_key:
        return ""
    key = api_key.strip()
    if len(key) <= 10:
        return "*" * len(key)
    return f"{key[:6]}...{key[-4:]}"


def save_api_key(api_key: str) -> None:
    """Encrypt *api_key* with DPAPI and write it to the per-user config dir.

    Raises SecretStoreError (never leaks the key in the message) if secure
    storage isn't available on this platform or the write fails.
    """
    if not api_key or not api_key.strip():
        raise SecretStoreError("Refusing to store an empty API key.")
    if not is_available():
        raise SecretStoreError("Secure secret storage (Windows DPAPI) is not available on this platform.")
    try:
        encrypted = _dpapi_protect(api_key.strip().encode("utf-8"))
        path = _secret_file_path()
        path.write_bytes(encrypted)
        _restrict_permissions(path)
    except SecretStoreError:
        raise
    except Exception as exc:
        raise SecretStoreError(f"Failed to save API key securely: {type(exc).__name__}") from exc


def load_api_key() -> Optional[str]:
    """Return the decrypted key, or None if nothing is stored / it can't be
    decrypted (e.g. secure storage unavailable, blob corrupted, or the blob
    was encrypted by a different Windows user profile)."""
    path = _secret_file_path()
    if not path.exists() or not is_available():
        return None
    try:
        encrypted = path.read_bytes()
        return _dpapi_unprotect(encrypted).decode("utf-8")
    except Exception:
        return None


def delete_api_key() -> None:
    """Remove the stored key, if any. Idempotent."""
    path = _secret_file_path()
    if path.exists():
        path.unlink()


def has_api_key() -> bool:
    return _secret_file_path().exists()


def backend_name() -> str:
    """For Diagnostics display only — never reveals key material."""
    if not _is_windows():
        return "unavailable (non-Windows)"
    return "windows-dpapi" if is_available() else "unavailable (pywin32 missing)"


def _restrict_permissions(path: Path) -> None:
    """Defence in depth on top of DPAPI: best-effort owner-only file mode."""
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
