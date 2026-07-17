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
Protects the secret *at rest* — the encrypted bytes sitting in
``secret.bin`` on disk while nothing is actively reading them. Protects
against:
  * Casual inspection of the JARVIS install/data directory (e.g. by another
    application, a file backup tool, or someone browsing the filesystem).
  * Other ordinary Windows user accounts on the same machine — DPAPI ties
    decryption to the encrypting user's login credentials, so a second
    account (even an administrator's, without that user's own logged-in
    session) cannot decrypt this blob.
  * The key ending up in plaintext in ``.env``, browser localStorage/
    sessionStorage, the SQLite database, application logs, or diagnostics
    exports (all of those paths are explicitly disallowed; see
    docs/SECURITY.md).

Does NOT protect against:
  * Malware or another process already running as the same Windows user —
    DPAPI decrypts transparently in that context, same as Credential
    Manager would; this is a property of what DPAPI *is* (a per-user
    encryption primitive), not a bug in how JARVIS calls it.
  * An attacker with an unlocked, logged-in session as the user.
  * The key *after* JARVIS has legitimately decrypted it into process
    memory to make an Anthropic API call — at that point it is plaintext
    in RAM like any in-use secret in any process, for as long as that call
    takes. DPAPI protects the file on disk; it says nothing about, and was
    never meant to protect, the moment the application actually uses the
    thing it decrypted.
  * Revocation: DPAPI can keep a stolen *file* useless to anyone without
    the matching Windows account, but it cannot un-leak a key that already
    reached Anthropic in a request, or was captured from memory by
    something already running as the user. If a key is ever suspected
    compromised, only revoking it at
    https://console.anthropic.com/settings/keys actually neutralizes it —
    DPAPI is not a substitute for that.

This is the same trust boundary every desktop app that stores a local API
key operates under; it is documented here rather than left implicit. None
of the above should be read as a claim of absolute protection — DPAPI
raises the bar for casual/cross-account access to the file at rest; it is
not a general-purpose defense against a compromised machine.
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
    """Encrypt *api_key* with DPAPI and atomically replace the per-user
    config file with it.

    Writes to a sibling ``.tmp`` file first, then ``os.replace()`` — atomic
    on both Windows and POSIX — swaps it into place. A crash or power loss
    between those two steps leaves either the untouched previous file or a
    stray ``.tmp`` file, never a half-written, corrupted ``secret.bin``:
    replacing an existing key can never leave the store in a state where
    ``load_api_key()`` reads back a truncated blob.

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
        tmp_path = path.with_name(path.name + ".tmp")
        tmp_path.write_bytes(encrypted)
        _restrict_permissions(tmp_path)
        os.replace(tmp_path, path)
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
