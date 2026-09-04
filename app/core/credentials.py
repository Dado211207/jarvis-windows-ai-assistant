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
# the latest desired value so a late set is reconciled to whatever was most
# recently asked for, rather than becoming an orphan credential.
#
# **What "most recently asked for" has to be after a failure**, because the
# first version got this wrong in a way that could destroy a working key.
# It reconciled *every* failed non-None write to absence:
#
#     if value is not None:
#         cleanup_generation = _record_desired(username, None)   # delete
#
# That is right for a first-time save — a late `set_password` would leave a
# credential the user was told had not been saved. It is destructive for a
# **replacement**, because the old key and the new key are the same
# Credential Manager entry. Replacing key A with key B and having that write
# fail or time out therefore deleted A, while the caller reported "Nothing
# was changed."
#
# The reconciliation target after a failed write is now the value that was
# *proven* to be there beforehand. Establishing it is a precondition of
# attempting the write at all: a store JARVIS cannot read is a store it will
# not write to, because a failure there could not be undone. An unreadable
# entry is never treated as an empty one — it may hold the only working key
# on the machine.
_backend_lock = threading.Lock()
_mutation_state_lock = threading.Lock()
#: Signalled whenever a mutation worker finishes, so shutdown and tests can
#: wait for the store to stop changing under them.
_mutation_idle = threading.Condition(_mutation_state_lock)
_mutation_generation = 0
_desired_values = {}
_inflight_mutations = {}

#: The value is in the store: the backend confirmed it.
MUTATION_APPLIED = "applied"
#: Provably nothing changed — either nothing was sent to the backend, or the
#: backend raised before applying anything and nothing is still in flight.
MUTATION_UNCHANGED = "unchanged"
#: Sent to the backend and never confirmed. It may still complete. This is
#: the state that must never be described to a user as "nothing was changed".
MUTATION_UNCERTAIN = "uncertain"


@dataclass(frozen=True)
class MutationResult:
    """What one attempted write to the credential store established.

    Three outcomes, not two, for the same reason `ProviderStatus` has five
    states rather than one boolean: "it worked", "it definitely did not
    happen" and "the call never came back and may yet land" call for three
    different sentences to the person holding the machine, and collapsing
    the last two lets JARVIS promise a postcondition it never observed.
    """

    outcome: str
    #: Why, when it is not APPLIED. One of "store_unavailable" (the keyring
    #: package is not installed), "store_unreadable" (the entry could not be
    #: read, so a failed write could not be undone), "backend_refused" or
    #: "timed_out". Never contains a credential value.
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.outcome == MUTATION_APPLIED

    @property
    def provably_unchanged(self) -> bool:
        """Whether the store is *known* to hold what it held before.

        The only basis on which a caller may tell a user that nothing was
        changed.
        """
        return self.outcome == MUTATION_UNCHANGED


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


def _compound_target(username: str) -> str:
    """The second target name the pinned Windows backend can create.

    `keyring==25.7.0`'s `WinVaultKeyring.set_password()` copies any existing
    credential to ``{existing_username}@{service}`` *before* writing the
    replacement — unconditionally, not only on the username collision its
    own docstring describes:

        existing_pw = self._read_credential(service)
        if existing_pw:
            target = self._compound_name(existing_pw['UserName'], service)
            self._set_password(target, existing_pw['UserName'], existing_pw.value)
        self._set_password(service, username, str(password))

    So replacing one JARVIS key leaves two real Credential Manager entries —
    `JARVIS` and `anthropic_api_key@JARVIS` — the second holding the key
    that was just replaced. Both were observed on a real machine with
    `cmdkey /list`. Reads never reveal it: `_resolve_credential()` returns
    the plain target first, so the stale secret is invisible to the
    application while remaining on disk indefinitely.

    JARVIS therefore owns *two* names per credential and keeps the invariant
    that only the first of them exists after any settled mutation. The name
    is composed from two JARVIS constants, so nothing else writes it — but
    ownership is still proven from the stored username before anything is
    deleted (see `_discard_superseded`).
    """
    return f"{username}@{SERVICE_NAME}"


def _read_target(target: str, username: str) -> Tuple[bool, str]:
    """``(store_reached, value)`` for one exact target name.

    Passing the target *as the service* is how an exact name is addressed
    through keyring's public API. A non-None result also proves the stored
    username matches: `_resolve_credential` falls through to a
    doubly-compound name when it does not, and nothing ever writes that.
    """
    try:
        import keyring
    except ImportError:
        return False, ""

    def _read_locked():
        # Serialized: a Future timeout does not cancel a call already
        # inside WinCred, so two overlapping reads must not enter the
        # backend together.
        with _backend_lock:
            return keyring.get_password(target, username)

    ok, value = _run_isolated(_read_locked)
    # The (reached, value) pair is load-bearing for uninstall: collapsing
    # "could not reach the store" into "no such credential" would erase the
    # data folder and report a successful purge while a secret may still
    # exist. Ordinary callers use _get() and see only the value.
    return ok, (value or "") if ok else ""


def _read(username: str) -> Tuple[bool, str]:
    """What a `get` would return: the plain target, else the compound one.

    Deliberately the backend's own resolution order rather than the plain
    target alone, because that is what every other caller of this module
    would observe. It means "reads as empty" proves *both* targets are gone,
    which is what a removal has to establish.
    """
    return _read_target(SERVICE_NAME, username)


def _get(username: str) -> str:
    _ok, value = _read(username)
    return value


def _record_desired(username: str, value: Optional[str]) -> int:
    global _mutation_generation
    with _mutation_state_lock:
        _mutation_generation += 1
        _desired_values[username] = (_mutation_generation, value)
        return _mutation_generation


def _record_desired_if_latest(username: str, generation: int,
                              value: Optional[str]) -> Optional[int]:
    """Record *value* as the newest desired state, unless something newer
    has already been asked for.

    A failed write must not undo a *later* request. If a second Save landed
    while the first was stuck inside the backend, the second one's value is
    the newest intent and putting the first one's previous value back over
    it would be the same class of defect this whole mechanism exists to
    prevent. Returns the new generation, or None when there was nothing to
    do because a newer intent already stands.
    """
    global _mutation_generation
    with _mutation_state_lock:
        current = _desired_values.get(username)
        if current is None or current[0] != generation:
            return None
        _mutation_generation += 1
        _desired_values[username] = (_mutation_generation, value)
        return _mutation_generation


def _enter_mutation(username: str) -> None:
    with _mutation_state_lock:
        _inflight_mutations[username] = _inflight_mutations.get(username, 0) + 1


def _leave_mutation(username: str) -> None:
    with _mutation_idle:
        remaining = _inflight_mutations.get(username, 1) - 1
        if remaining > 0:
            _inflight_mutations[username] = remaining
        else:
            _inflight_mutations.pop(username, None)
        _mutation_idle.notify_all()


def _pending_mutations(username: str) -> int:
    """How many workers for *username* have not finished.

    Read on the failure path: a backend that refused this write changed
    nothing *by itself*, but an earlier worker still inside the store will
    converge to the newest desired value when it comes out. While one
    exists, "nothing changed" is a prediction rather than an observation.
    """
    with _mutation_state_lock:
        return _inflight_mutations.get(username, 0)


def wait_for_pending_mutations(timeout: float = TIMEOUT_SECONDS * 4) -> bool:
    """Block until no credential mutation worker is running. Never raises.

    Not called on any request path — a route must never wait on a backend
    that has already blown its timeout. It exists so shutdown and tests can
    make an assertion about what the store ends up holding, which is not a
    meaningful question while a late write is still in flight.
    """
    import time

    deadline = time.monotonic() + max(0.0, timeout)
    with _mutation_idle:
        while any(_inflight_mutations.values()):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            _mutation_idle.wait(remaining)
    return True


def _apply_value(keyring, username: str, value: Optional[str]) -> None:
    if value is not None:
        keyring.set_password(SERVICE_NAME, username, value)
        return
    try:
        keyring.delete_password(SERVICE_NAME, username)
    except keyring.errors.PasswordDeleteError:
        pass


def _discard_superseded(keyring, username: str, survivor: Optional[str]) -> bool:
    """Remove the copy of the previous secret the backend leaves behind.

    Returns whether the store is *proven* to be free of a superseded copy.
    False means "could not establish it" — never "there wasn't one".

    **Nothing is deleted until three things are proven**, because a target
    JARVIS did not write is not JARVIS's to remove:

      1. *The intended survivor is in place.* `_read()` resolves the plain
         target first, so it returning exactly what was meant to be stored
         proves the plain target holds it — and therefore that deleting the
         other copy cannot leave the user with no credential. For a removal
         (`survivor is None`) the proof is that the store reads empty.
      2. *The compound target actually exists*, read by its exact name.
      3. *It carries our own username.* A non-None read through
         `_read_target` establishes this: keyring falls through to a
         doubly-compound name when the username does not match, and that
         name is never written by anything.

    Called with `_backend_lock` already held.
    """
    compound = _compound_target(username)

    if survivor is not None:
        # Deleting the copy must never be what leaves the user with no
        # credential, so the intended survivor has to be in place first.
        ok, current = _read_target(SERVICE_NAME, username)
        if not ok or current != survivor:
            return False
    # With no survivor the caller wants the credential gone entirely, so
    # there is nothing to protect: any copy under the compound name is
    # residue by definition, and removing it is the goal rather than a risk.

    reached, residue = _read_target(compound, username)
    if not reached:
        return False
    if not residue:
        return True

    def _delete_locked():
        with _backend_lock:
            try:
                keyring.delete_password(compound, username)
            except keyring.errors.PasswordDeleteError:
                pass

    deleted, _ = _run_isolated(_delete_locked)
    if not deleted:
        return False

    confirmed, still_there = _read_target(compound, username)
    return bool(confirmed and not still_there)


def _discard_worker(keyring, username: str, survivor: Optional[str]) -> None:
    """`_discard_superseded` as a queued task, for the paths where the write
    it tidies up after has not finished yet.

    Submitted to the same single-worker executor as the mutation, so it runs
    strictly after it. Deliberately **not** called from inside
    `_mutation_worker`: that holds `_backend_lock`, and every read below
    acquires it on a different thread, which would deadlock rather than
    fail.
    """
    try:
        _discard_superseded(keyring, username, survivor)
    finally:
        _leave_mutation(username)


def _queue_discard(executor, keyring, username: str, survivor: Optional[str]) -> None:
    """Clear the superseded copy once everything queued ahead has settled."""
    _enter_mutation(username)
    try:
        executor.submit(_discard_worker, keyring, username, survivor)
    except BaseException:  # noqa: BLE001 — a shut-down executor is not a failure path
        _leave_mutation(username)


def _queue_reconciliation(executor, keyring, username: str,
                          survivor: Optional[str]) -> None:
    """Drive the store back to *survivor*, then clear any superseded copy.

    Used where the outcome could not be established. It is deliberately a
    real submitted worker: updating `_desired_values` alone only tells a
    *future* write what to converge to, and when nothing else is in flight
    that future write never happens.
    """
    _enter_mutation(username)
    try:
        executor.submit(
            _mutation_worker, keyring, username,
            _record_desired(username, survivor), survivor,
        )
    except BaseException:  # noqa: BLE001
        _leave_mutation(username)
        return
    _queue_discard(executor, keyring, username, survivor)


def _mutation_worker(keyring, username: str, generation: int,
                     value: Optional[str]) -> None:
    """Apply this mutation, then reconcile anything requested behind it.

    The primary apply is deliberately left to propagate. That is what lets
    `_mutate_detailed` tell "the backend refused, so the store still holds
    what it held" from "the call never came back, so it may yet land" — two
    facts that call for two different sentences to the user. Everything
    *after* it is best effort and must not be reported as a failure of a
    write that already happened.
    """
    try:
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
                try:
                    _apply_value(keyring, username, latest_value)
                except BaseException as exc:
                    logger.warning(
                        "Reconciling the OS credential store failed: %s", type(exc).__name__,
                    )
                    return
                applied_generation = latest_generation
    finally:
        _leave_mutation(username)


def _mutate_detailed(username: str, value: Optional[str]) -> MutationResult:
    """Apply one change to the credential store and report what it settled.

    A *set* reads the entry first. That read is not a convenience: it is the
    only way to know what a failed write has to be undone back to. If it
    cannot be done, the write is not attempted at all — starting one that
    could not be reversed is how a replacement destroys the key it was
    replacing, and an unreadable entry is never assumed to be an empty one.
    """
    try:
        import keyring
        import keyring.errors
    except ImportError:
        return MutationResult(MUTATION_UNCHANGED, "store_unavailable")

    # Read first, always. For a *set* this is the value a failed write has
    # to be undone back to, and not having it is a refusal. For a *delete*
    # it is the baseline that makes "nothing changed" checkable afterwards —
    # a delete cannot destroy something the caller wanted to keep, so a
    # failed read there is not a refusal, only a loss of that proof.
    baseline_known, baseline = _read(username)
    restore_to: Optional[str] = None
    if value is not None:
        if not baseline_known:
            logger.warning(
                "Not writing to the OS credential store: its current contents could not "
                "be read, so a write that failed could not be undone.",
            )
            return MutationResult(MUTATION_UNCHANGED, "store_unreadable")
        # "" means the store answered and the entry is empty, so absence is
        # the proven previous state and deleting on failure is correct.
        restore_to = baseline or None

    generation = _record_desired(username, value)
    executor = concurrent.futures.ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="jarvis-keyring-mutation",
    )
    _enter_mutation(username)
    try:
        future = executor.submit(_mutation_worker, keyring, username, generation, value)
    except BaseException:
        _leave_mutation(username)
        executor.shutdown(wait=False)
        return MutationResult(MUTATION_UNCHANGED, "store_unavailable")

    try:
        future.result(timeout=TIMEOUT_SECONDS)
        # The write landed. The pinned Windows backend may have copied the
        # secret it replaced to a second target on the way; the operation is
        # not finished until that copy is gone and proven gone.
        if _discard_superseded(keyring, username, value):
            return MutationResult(MUTATION_APPLIED)
        logger.warning(
            "The credential was written but a superseded copy of the previous one "
            "could not be confirmed removed.",
        )
        return MutationResult(MUTATION_UNCERTAIN, "superseded_copy")
    except concurrent.futures.TimeoutError:
        # Still inside the backend. It may complete after this returns, so
        # the newest desired state has to be the one that should survive:
        # the proven previous value for a set, and absence for a delete,
        # which is what was asked for anyway.
        logger.warning("OS credential mutation did not answer within its timeout.")
        survivor = restore_to if value is not None else None
        if value is not None:
            reconcile = _record_desired_if_latest(username, generation, restore_to)
            if reconcile is not None:
                _enter_mutation(username)
                try:
                    executor.submit(
                        _mutation_worker, keyring, username, reconcile, restore_to,
                    )
                except BaseException:
                    _leave_mutation(username)
        # Queued behind whatever is still running on this single-worker
        # executor, so the superseded copy is cleared once the late write
        # and its reconciliation have settled.
        _queue_discard(executor, keyring, username, survivor)
        return MutationResult(MUTATION_UNCERTAIN, "timed_out")
    except BaseException as exc:
        # Class name only, and deliberately no traceback. _run_isolated
        # above already refuses to log str(exc) because a backend
        # exception may quote the value it was asked to store; exc_info
        # renders exactly that string, so it was the same leak by another
        # route. What is lost is a stack through keyring internals; what
        # is kept is the contract that a credential value never reaches a
        # log file.
        logger.warning("OS credential mutation failed: %s", type(exc).__name__)
        # An exception is not a postcondition. The pinned backend's
        # `set_password` performs two writes and its `delete_password` up to
        # two deletes, so either can mutate the store and *then* raise — the
        # earlier code assumed the opposite, called that "nothing was
        # changed", and skipped the reconciliation, which left a
        # half-applied replacement standing.
        #
        # So: put the intended state back on the desired list, then go and
        # look. Only what the store actually reads back may be reported.
        survivor = restore_to if value is not None else None
        _record_desired_if_latest(username, generation, survivor)

        reached, current = _read(username)
        settled = _discard_superseded(keyring, username, survivor) if reached else False
        # Compared against what was observed *before* the attempt, not
        # against the value it was meant to converge to: for a removal those
        # are different, and only the first can prove nothing happened.
        unchanged = (
            reached
            and settled
            and baseline_known
            and current == baseline
            and not _pending_mutations(username)
        )
        if unchanged:
            return MutationResult(MUTATION_UNCHANGED, "backend_refused")

        # The store may have moved. Recording a desired value is not
        # restoring one — a worker has to apply it — so enqueue that now
        # rather than hoping something else picks it up.
        _queue_reconciliation(executor, keyring, username, survivor)
        return MutationResult(
            MUTATION_UNCERTAIN, "partially_applied" if reached else "unverifiable",
        )
    finally:
        executor.shutdown(wait=False)


def _mutate(username: str, value: Optional[str]) -> bool:
    return _mutate_detailed(username, value).ok


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


def set_stored_api_key_detailed(value: str) -> MutationResult:
    """`set_stored_api_key` with the reason attached.

    For app/core/ai/credential_pair.py, which has to tell the user whether
    the previous key is still there. "Could not save it, and nothing was
    changed" and "could not confirm the save, and the previous key is being
    put back" are different situations with different next steps.
    """
    return _mutate_detailed(USERNAME, value)


def clear_stored_api_key_detailed() -> MutationResult:
    """`clear_stored_api_key` with the reason attached — see above.

    A removal that timed out is the case that matters here: the delete may
    complete after the response is written, so the removal must not be
    reported as having changed nothing.
    """
    return _mutate_detailed(USERNAME, None)


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
