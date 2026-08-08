"""Secure storage for the Anthropic API key via the OS credential store
(Windows Credential Manager, through the `keyring` package) instead of a
plaintext .env file or SQLite.

Every keyring call runs on a short-lived worker thread with a bounded
timeout, read back via future.result() — the same "an external call
must never be allowed to hang or crash the caller" pattern already used
for tool execution (app/core/tool_registry.py) and STT transcription
(app/voice/stt.py::FasterWhisperAdapter.transcribe()). This isn't
defensive-for-its-own-sake: verified directly in this project's own
Linux development sandbox, a mismatched cryptography/cffi native
extension makes keyring's Linux SecretService backend fail with a
Rust-level panic (pyo3_runtime.PanicException) that a plain same-thread
`except Exception` around the same call does NOT catch — the process
just dies. Reading the result through a ThreadPoolExecutor future does
catch it reliably (confirmed empirically, not assumed). The real target
platform (Windows, via keyring's pywin32-ctypes-backed WinVaultKeyring)
never goes near that code path at all, but the isolation was already
the established pattern in this codebase for "don't fully trust an
external call," and it costs nothing to apply here too.

keyring itself is a Windows-packaging-only dependency (see
requirements-windows.txt) — not installed in this project's base
dev/CI environment — so "the package isn't even present" is handled
exactly like "the store is unreachable": a safe empty/False result,
never an exception.

Falls back to the ANTHROPIC_API_KEY environment variable
unconditionally — see app/config.py::Settings.effective_api_key, which
is the only intended caller of this module. Development and CI set the
env var directly and must never be asked to touch a credential store
that may not even exist in that context.
"""

import concurrent.futures
from typing import Any, Callable, Tuple

from app.logging_config import get_logger

logger = get_logger("core.credentials")

SERVICE_NAME = "JARVIS"
USERNAME = "anthropic_api_key"
TIMEOUT_SECONDS = 5.0


def _run_isolated(func: Callable, *args: Any) -> Tuple[bool, Any]:
    """Runs *func(*args)* on its own thread with a bounded timeout.
    Returns (True, result) on success, (False, None) on any failure —
    timeout, ordinary exception, or an exception type that would
    otherwise escape a same-thread try/except (see module docstring).
    Never raises."""
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="jarvis-keyring")
    try:
        future = executor.submit(func, *args)
        return True, future.result(timeout=TIMEOUT_SECONDS)
    except BaseException as exc:
        logger.warning("OS credential store call failed: %s", type(exc).__name__, exc_info=True)
        return False, None
    finally:
        executor.shutdown(wait=False)


def get_stored_api_key() -> str:
    """Returns the key from the OS credential store, or "" if none is
    stored, keyring isn't installed, or the store can't be reached."""
    try:
        import keyring
    except ImportError:
        return ""
    ok, value = _run_isolated(keyring.get_password, SERVICE_NAME, USERNAME)
    return (value or "") if ok else ""


def set_stored_api_key(value: str) -> bool:
    """Stores *value*. Returns False (never raises) if keyring isn't
    installed or the store can't be written to."""
    try:
        import keyring
    except ImportError:
        return False
    ok, _ = _run_isolated(keyring.set_password, SERVICE_NAME, USERNAME, value)
    return ok


def clear_stored_api_key() -> bool:
    """Removes any stored key. Returns True if the key is now absent
    either way (already-absent counts as success), False only if the
    store genuinely could not be reached."""
    try:
        import keyring
        import keyring.errors
    except ImportError:
        return False

    def _delete() -> None:
        try:
            keyring.delete_password(SERVICE_NAME, USERNAME)
        except keyring.errors.PasswordDeleteError:
            pass  # already absent

    ok, _ = _run_isolated(_delete)
    return ok
