"""One coherent look at the Anthropic credential, for everything that reads
it.

**Why this exists.** Round 6 made the *writers* serialise. Readers were
still assembling the pair out of separate store reads:

    api_key=settings.effective_api_key if settings.has_anthropic_key else "",
    anthropic_workspace_id=get_preference(WORKSPACE_PREFERENCE) or "",

Three reads, none of them inside the credential transaction — and
`has_anthropic_key` calls `effective_api_key` again, so the credential store
was read twice. A save paused between its two stores put a real chat request
in that window and produced:

    observed_key        NEW-KEY
    observed_workspace  wrkspc_OLD

which is a request sent to Anthropic with one key's credential and another
key's workspace. The writer lock does not protect readers, and the Settings
page's button state protects neither Chat nor a direct API call.

**What a reader gets instead.** One immutable `CredentialPair`, built while
holding the coordinator's gate so the two stores cannot move underneath it,
carrying a non-secret `revision` that identifies *which* pair it is.

**The revision is what makes a delayed failure attributable.** A request
carries the revision it used; when Anthropic finally rejects it,
`providers.note_runtime_failure()` will only downgrade that revision. It is
an integer from a counter — never a hash of a key, never a fingerprint,
never anything derived from a secret, so it is safe in a log line or a
diagnostic in a way the key itself never is.

**A read may be stale; it may never be mixed.** If the gate is held by a
writer for longer than a reader is willing to wait, the reader is handed the
last coherent snapshot rather than a fresh incoherent one. A pair that was
true a moment ago is a pair that really existed; NEW-key-with-OLD-workspace
never was.

**No network work happens under the gate.** A snapshot is acquired, the gate
is released, and only then does anything talk to Anthropic — otherwise every
reader and writer would queue behind somebody else's HTTP request.

Nothing here writes to either store, and nothing here logs a value: the
dataclass keeps the key and the workspace out of its own repr, because a
`ProviderConfig` or a snapshot in a traceback is the ordinary way a
credential reaches a log file.
"""

from dataclasses import dataclass, field, replace
import threading
from typing import Optional

from app.logging_config import get_logger

logger = get_logger("core.ai.credential_view")

#: How long a reader waits for a writer that is mid-transaction, when it
#: already holds a coherent snapshot it can fall back on. Short, because a
#: chat request should not queue behind a credential store that has stopped
#: answering; the fallback is coherent, merely older.
READ_WAIT_SECONDS = 8.0


@dataclass(frozen=True)
class CredentialPair:
    """The credential and everything that describes it, at one instant.

    Frozen because it is held for the length of a request and passed into
    provider configuration: a value that could be edited afterwards would
    reintroduce exactly the drift this replaces.
    """

    #: Non-secret, monotonic. Identifies which pair this is, so a failure
    #: can be attributed to the credential that caused it.
    revision: int
    #: Never logged, never returned by an endpoint, never in a repr.
    api_key: str = field(default="", repr=False)
    workspace_id: str = field(default="", repr=False)
    #: The recorded verification state, before any process-local downgrade.
    state: str = ""
    configured: bool = False
    #: False when the credential store could not be read coherently at all.
    #: Distinct from "there is no key": one is a fact about the credential,
    #: the other is a fact about this machine.
    readable: bool = True

    @property
    def workspace_configured(self) -> bool:
        return bool(self.workspace_id.strip())


def _unreadable() -> CredentialPair:
    return CredentialPair(revision=-1, readable=False)


_cache_lock = threading.Lock()
_cached: Optional[CredentialPair] = None


def _publish(pair: CredentialPair) -> None:
    global _cached
    with _cache_lock:
        _cached = pair


def _published() -> Optional[CredentialPair]:
    with _cache_lock:
        return _cached


def invalidate() -> None:
    """Forget the published snapshot.

    Needed because the stores can also change without a transaction — a
    test that seeds them directly, an environment variable that differs
    from the credential store — and the revision alone cannot see that.
    """
    global _cached
    with _cache_lock:
        _cached = None


def _build(revision: int) -> CredentialPair:
    """Read both stores. Only ever called while the gate is held."""
    from app.config import settings
    from app.core.ai.workspace import PREFERENCE_KEY as WORKSPACE_PREFERENCE
    from app.core.preferences import get as get_preference
    from app.core.providers import VERIFICATION_PREFERENCE

    key = settings.effective_api_key or ""
    return CredentialPair(
        revision=revision,
        api_key=key,
        workspace_id=(get_preference(WORKSPACE_PREFERENCE) or "").strip(),
        state=(get_preference(VERIFICATION_PREFERENCE) or "").strip(),
        configured=bool(key.strip()),
        readable=True,
    )


def current() -> CredentialPair:
    """The credential pair, coherently. Never raises.

    Cheap when nothing has changed: the published snapshot is reused while
    its revision still matches, so an ordinary request touches no store at
    all.
    """
    from app.core.ai import credential_transaction as coordinator

    try:
        cached = _published()
        # Deliberately **not** a cache lookup keyed on the revision. Such a
        # cache would be stale for any change that did not come through the
        # coordinator, and "which writes go through the coordinator" is an
        # invariant enforced by a test rather than by the type system — a
        # bad thing for a *reader* to depend on. Building every time also
        # costs less than the code this replaces, which read the credential
        # store twice per request (`has_anthropic_key` calls
        # `effective_api_key`, and then the caller called it again).
        #
        # The published snapshot exists only as the fallback below.
        wait = READ_WAIT_SECONDS if cached is not None and cached.readable else None
        try:
            with coordinator.read_gate(wait):
                built = _build(coordinator.pair_revision())
        except coordinator.TransactionBusy:
            if cached is not None and cached.readable:
                logger.warning(
                    "A credential change is still running; using the previous coherent "
                    "credential snapshot for this request.",
                )
                return cached
            logger.warning("Could not read the credential store coherently.")
            return _unreadable()
        _publish(built)
        return built
    except Exception as exc:  # noqa: BLE001 — a read must never break a request
        from app.core.safe_traceback import describe

        logger.warning("Could not take a credential snapshot. %s", describe(exc))
        return _unreadable()


def current_revision() -> int:
    """The revision alone, without touching either store.

    For `providers.note_runtime_failure()`, which runs on a request's
    failure path and must be cheap and incapable of blocking.
    """
    from app.core.ai import credential_transaction as coordinator

    try:
        return coordinator.pair_revision()
    except Exception:  # noqa: BLE001
        return -1


def without_secret(pair: CredentialPair) -> CredentialPair:
    """The same snapshot with the key removed — for anything that only needs
    to describe the credential rather than use it."""
    return replace(pair, api_key="")
