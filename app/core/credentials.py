"""Secure storage for JARVIS API credentials via the OS credential store
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
from dataclasses import dataclass
import threading
from typing import Any, Callable, Optional, Tuple

from app.logging_config import get_logger

logger = get_logger("core.credentials")

SERVICE_NAME = "JARVIS"
USERNAME = "anthropic_api_key"
TIMEOUT_SECONDS = 5.0

# Separately named entries — never extra fields in one shared secret.
#
# Credentials for different services belong in different Credential
# Manager entries: they are granted, replaced and revoked independently.
# The ownership registry below makes that rule executable at uninstall.
ELEVENLABS_USERNAME = "elevenlabs_api_key"
OPENAI_USERNAME = "openai_api_key"

# Keyring calls that time out keep running in Python; a Future timeout does
# not cancel a call already inside WinCred. Serialize the backend and track
# the latest desired value so a late set is reconciled to a newer delete (or
# removed after its own timeout) instead of becoming an orphan credential.
_backend_lock = threading.Lock()
_mutation_state_lock = threading.Lock()
_mutation_generation = 0
_desired_values = {}


@dataclass(frozen=True)
class OwnedCredential:
    """One independently granted secret owned by JARVIS.

    This registry is the single source for full-uninstall discovery,
    cleanup and reporting. A new provider adds one entry here; ownership
    and its parametrized regression tests consume it automatically.
    """

    key: str
    username: str
    what: str


OWNED_CREDENTIALS = (
    OwnedCredential(
        "credential",
        USERNAME,
        "Your Anthropic API key in Windows Credential Manager",
    ),
    OwnedCredential(
        "elevenlabs_credential",
        ELEVENLABS_USERNAME,
        "Your ElevenLabs API key in Windows Credential Manager",
    ),
    OwnedCredential(
        "openai_credential",
        OPENAI_USERNAME,
        "Your OpenAI voice API key in Windows Credential Manager",
    ),
)


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
        # A backend exception may quote the value it was asked to store.
        # Log only its class: neither str(exc) nor a traceback belongs in a
        # file whose contract says credential values never reach logs.
        logger.warning("OS credential store call failed: %s", type(exc).__name__)
        return False, None
    finally:
        executor.shutdown(wait=False)


def _read(username: str) -> Tuple[bool, str]:
    try:
        import keyring
    except ImportError:
        return False, ""

    def _read_locked():
        # Serialized: a Future timeout does not cancel a call already
        # inside WinCred, so two overlapping reads must not enter the
        # backend together.
        with _backend_lock:
            return keyring.get_password(SERVICE_NAME, username)

    ok, value = _run_isolated(_read_locked)
    # The (reached, value) pair is load-bearing for uninstall: collapsing
    # "could not reach the store" into "no such credential" would erase the
    # data folder and report a successful purge while a secret may still
    # exist. Ordinary callers use _get() and see only the value.
    return ok, (value or "") if ok else ""


def _get(username: str) -> str:
    _ok, value = _read(username)
    return value


def _record_desired(username: str, value: Optional[str]) -> int:
    global _mutation_generation
    with _mutation_state_lock:
        _mutation_generation += 1
        _desired_values[username] = (_mutation_generation, value)
        return _mutation_generation


def _apply_value(keyring, username: str, value: Optional[str]) -> None:
    if value is not None:
        keyring.set_password(SERVICE_NAME, username, value)
        return
    try:
        keyring.delete_password(SERVICE_NAME, username)
    except keyring.errors.PasswordDeleteError:
        pass


def _mutation_worker(keyring, username: str, generation: int,
                     value: Optional[str]) -> None:
    """Apply this mutation, then reconcile anything requested behind it."""
    with _backend_lock:
        applied_generation = generation
        _apply_value(keyring, username, value)
        while True:
            with _mutation_state_lock:
                latest_generation, latest_value = _desired_values.get(
                    username, (applied_generation, value),
                )
            if latest_generation == applied_generation:
                return
            _apply_value(keyring, username, latest_value)
            applied_generation = latest_generation


def _mutate(username: str, value: Optional[str]) -> bool:
    try:
        import keyring
        import keyring.errors
    except ImportError:
        return False

    generation = _record_desired(username, value)
    executor = concurrent.futures.ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="jarvis-keyring-mutation",
    )
    future = executor.submit(_mutation_worker, keyring, username, generation, value)
    try:
        future.result(timeout=TIMEOUT_SECONDS)
        return True
    except BaseException as exc:
        # Class name only, and deliberately no traceback. _run_isolated
        # above already refuses to log str(exc) because a backend
        # exception may quote the value it was asked to store; exc_info
        # renders exactly that string, so it was the same leak by another
        # route. What is lost is a stack through keyring internals; what
        # is kept is the contract that a credential value never reaches a
        # log file.
        logger.warning("OS credential mutation failed: %s", type(exc).__name__)
        if value is not None:
            # A failed/timed-out save is not owned by JARVIS. Make absence
            # the newest desired state and enqueue a cleanup behind the late
            # operation. If the first worker is still alive it also observes
            # this generation and performs the same idempotent cleanup.
            cleanup_generation = _record_desired(username, None)
            executor.submit(
                _mutation_worker, keyring, username, cleanup_generation, None,
            )
        return False
    finally:
        executor.shutdown(wait=False)


def _set(username: str, value: str) -> bool:
    return _mutate(username, value)


def _clear(username: str) -> bool:
    return _mutate(username, None)


def owned_credential_status(credential: OwnedCredential) -> Tuple[bool, bool]:
    """Return ``(store_reached, credential_present)`` for uninstall.

    Ordinary callers deliberately receive only an empty string when the
    store is unavailable. Full uninstall cannot collapse "unreachable"
    into "absent": doing so would erase the data folder and report a
    successful purge while a secret may still exist.
    """
    reached, value = _read(credential.username)
    return reached, bool(value)


def clear_owned_credential(credential: OwnedCredential) -> bool:
    """Remove one registry entry through the same bounded keyring path."""
    return _clear(credential.username)


def get_stored_api_key() -> str:
    """Returns the key from the OS credential store, or "" if none is
    stored, keyring isn't installed, or the store can't be reached."""
    return _get(USERNAME)


def set_stored_api_key(value: str) -> bool:
    """Stores *value*. Returns False (never raises) if keyring isn't
    installed or the store can't be written to."""
    return _set(USERNAME, value)


def clear_stored_api_key() -> bool:
    """Removes any stored key. Returns True if the key is now absent
    either way (already-absent counts as success), False only if the
    store genuinely could not be reached."""
    return _clear(USERNAME)


def stored_api_key_snapshot() -> Tuple[bool, str]:
    """``(store_reached, value)`` for the Anthropic key.

    For **rollback only** — see app/core/ai/credential_pair.py. Saving a
    key writes two stores (this one and the preferences file), and the
    second write can fail, so the first has to be undoable. Undoing it
    needs the previous value, and it needs to know whether that value was
    actually observed.

    That second half is the point of returning a pair rather than a
    string. get_stored_api_key() answers "" both for "there is no key" and
    for "the store could not be reached", and a rollback that treats the
    second as the first would *delete* a key it simply could not read.

    The value is held in memory for the length of one request and never
    logged, echoed, returned by an endpoint or written anywhere but back
    into this same store.
    """
    return _read(USERNAME)


# ---------------------------------------------------------------------------
# ElevenLabs. Same store, same isolation, its own entry.
#
# Deliberately has no environment-variable fallback, unlike the Anthropic
# key above. That fallback exists because development and CI have to be
# able to run chat without touching a credential store; nothing in
# development or CI may ever reach the real ElevenLabs API, so an env var
# would only be a way for a key to end up somewhere it should not be.
# ---------------------------------------------------------------------------

def get_elevenlabs_key() -> str:
    return _get(ELEVENLABS_USERNAME)


def set_elevenlabs_key(value: str) -> bool:
    return _set(ELEVENLABS_USERNAME, value)


def clear_elevenlabs_key() -> bool:
    return _clear(ELEVENLABS_USERNAME)


def has_elevenlabs_key() -> bool:
    """Whether a key is configured — the only thing about it that is ever
    reported outside this module. The value itself never leaves."""
    return bool(get_elevenlabs_key())


# OpenAI Speech has its own credential entry. It is intentionally not the
# Anthropic/general chat credential and has no environment-variable fallback.
def get_openai_key() -> str:
    return _get(OPENAI_USERNAME)


def set_openai_key(value: str) -> bool:
    return _set(OPENAI_USERNAME, value)


def clear_openai_key() -> bool:
    return _clear(OPENAI_USERNAME)
