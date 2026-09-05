"""AI provider discovery — what is *actually* usable right now.

The onboarding wizard and the Settings page both need to answer one
question honestly: which providers can this machine really use? A
provider is only ever reported available when something real was
checked — a key present in the OS credential store, or a live local
Ollama instance that answered — never because the code supports it in
principle. CLAUDE.md's onboarding rule is explicit that a capability
must not be claimed unless detected, and this module is where that rule
is enforced for providers.

Two providers exist today:

  anthropic  cloud. Available when a key is configured (env var or the
             Windows credential store — see app/config.py's
             effective_api_key). Never echoes the key back.
  ollama     local. Available only when an Ollama server answers on its
             loopback API. Model choices come from what that instance
             actually reports; this module never invents a model list
             and never triggers a download — pulling a multi-gigabyte
             model is a decision for the user, made in Ollama itself.

Detection never raises and never blocks for long: an unreachable local
server is the normal case for most users, so it must be an ordinary
"not detected" result returned quickly, not an error or a stall.
"""

from dataclasses import dataclass, field
import threading
from typing import List, Optional

from app.core.safe_traceback import describe
from app.logging_config import get_logger

logger = get_logger("core.providers")

PROVIDER_ANTHROPIC = "anthropic"
PROVIDER_OLLAMA = "ollama"
KNOWN_PROVIDERS = (PROVIDER_ANTHROPIC, PROVIDER_OLLAMA)

# Ollama's documented default loopback endpoint. Loopback only, matching
# the rest of this application's network posture.
OLLAMA_DEFAULT_HOST = "127.0.0.1"
OLLAMA_DEFAULT_PORT = 11434
OLLAMA_PROBE_TIMEOUT_SECONDS = 1.5


@dataclass
class ProviderStatus:
    """What the UI renders. *detail* is always safe to display: it
    describes availability, never credentials."""

    name: str
    display_name: str
    kind: str  # "cloud" | "local"
    available: bool
    detail: str
    models: List[str] = field(default_factory=list)
    requires_credentials: bool = False


#: What is actually known about the stored Anthropic credential. Five
#: states, not two, because "a key exists", "a key works" and "a key was
#: never successfully checked" are different facts, and reporting the
#: first as the second is what told the owner "natural-language chat is
#: available" while every request was being rejected with HTTP 400.
#:
#: The fifth, ACCOUNT_UNFUNDED, exists because collapsing it into either
#: neighbour states something false: it is not unchecked (Anthropic
#: answered) and it is not a rejected key (Anthropic accepted the key and
#: declined to bill it).
CREDENTIAL_NOT_CONFIGURED = "not_configured"
CREDENTIAL_UNVERIFIED = "configured_unverified"
CREDENTIAL_VERIFIED = "verified"
CREDENTIAL_FAILED = "verification_failed"
CREDENTIAL_UNFUNDED = "account_unfunded"

#: The preference recording what the key-save path observed. Not a
#: credential and not a secret — a one-word state name.
VERIFICATION_PREFERENCE = "anthropic_key_state"

_CREDENTIAL_DETAIL = {
    CREDENTIAL_NOT_CONFIGURED: (
        "No API key configured yet. JARVIS still works without one; "
        "deterministic commands do not need a provider."
    ),
    CREDENTIAL_UNVERIFIED: (
        "An API key is saved but has not been confirmed with Anthropic on this "
        "installation — either it was never checked, or the check could not "
        "complete. It may well work. Open Settings and save it again to check."
    ),
    CREDENTIAL_VERIFIED: (
        "API key answered successfully the last time Anthropic was asked. A key "
        "can still expire or be revoked; JARVIS reports that the next time it "
        "is used."
    ),
    CREDENTIAL_FAILED: (
        "Anthropic rejected the saved API key the last time it was used. Open "
        "Settings to correct it — an identity-linked key also needs its "
        "Workspace ID."
    ),
    CREDENTIAL_UNFUNDED: (
        "Anthropic accepted the API key but the account has no credit "
        "available. Add credit or a payment method to your Anthropic account; "
        "the key itself is fine."
    ),
}

#: The states that may be recorded against a stored credential. Anything
#: else read back from the preferences file is treated as "unverified",
#: which is the honest reading of a value this code did not write.
_RECORDED_STATES = (CREDENTIAL_VERIFIED, CREDENTIAL_FAILED, CREDENTIAL_UNFUNDED)

#: The only states a *live rejection* can put a credential into. The
#: process-local note below is restricted to these by construction, which is
#: what makes "it can never upgrade a credential" a property of the code
#: rather than a promise about how it is called.
_NEGATIVE_STATES = (CREDENTIAL_FAILED, CREDENTIAL_UNFUNDED)

# ---------------------------------------------------------------------------
# The downgrade that could not be written down.
#
# `note_runtime_failure()` records a live rejection by writing a preference,
# and it used to discard `store_many()`'s result: on a machine that could
# not write its settings file the log said "downgraded", the preference
# still said "verified", and the dashboard went on offering Claude as
# available for the rest of the session — which is the exact failure the
# downgrade exists to prevent, with a log line claiming otherwise.
#
# Persisting is still attempted first and is still what survives a restart.
# When it fails, the observation is kept in this process instead, because
# "Anthropic rejected this key thirty seconds ago" is knowledge JARVIS
# genuinely has and must not act against.
#
# **The note is bound to a credential revision, and that correction matters.**
# An earlier version of this comment said the note is "cleared only when the
# credential it describes stops being the stored one", and concluded that it
# "cannot outlive the key it describes". That was disproved: `save()` does
# clear it, but a request made with the *old* key can still be in flight, and
# when it finally comes back rejected it recreated the note afterwards —
# marking the new, working credential as rejected. Clearing on save orders
# nothing, because the failure arrives later than the save.
#
# So every rejection now carries the revision of the pair that made the
# request, and is discarded unless that revision is still the current one.
# The lifecycle, restated:
#
#   set     only by note_runtime_failure(), only from an explicit live
#           rejection of the *current* revision, and only ever to a value in
#           _NEGATIVE_STATES
#   read    only by credential_state_for(), and only for the revision it was
#           recorded against
#   cleared when the credential changes (credential_pair calls
#           clear_runtime_downgrade()) and, independently, ignored the moment
#           the revision moves on — so a stale note cannot describe a newer
#           credential even if the clear never happened
#   lost    on restart, which is correct: it was never persisted, and
#           claiming otherwise is what this replaces
#
# There is no path that sets it to a positive state, so it cannot report a
# rejected credential as working, and it cannot report a working one as
# rejected without a provider having said so about that exact pair.
# ---------------------------------------------------------------------------
#: How long the failure path will wait for the coordinator before giving up
#: and recording nothing. Short on purpose: this runs after a request has
#: already failed, and a rejection that cannot be attributed safely is better
#: dropped than guessed at — a real one recurs on the next request.
DOWNGRADE_WAIT_SECONDS = 5.0

_runtime_downgrade_lock = threading.Lock()
_runtime_downgrade: Optional[str] = None
#: Which credential revision `_runtime_downgrade` describes. Never a key,
#: never derived from one — see app/core/ai/credential_view.py.
_runtime_downgrade_revision: int = -1


def _remember_runtime_downgrade(state: str, revision: int) -> None:
    """Note a live rejection for the rest of this process. Negative only,
    and only ever about the revision that was actually rejected."""
    global _runtime_downgrade, _runtime_downgrade_revision
    if state not in _NEGATIVE_STATES:
        return
    with _runtime_downgrade_lock:
        _runtime_downgrade = state
        _runtime_downgrade_revision = revision


def clear_runtime_downgrade() -> None:
    """Forget the note, because the credential it described is gone."""
    global _runtime_downgrade, _runtime_downgrade_revision
    with _runtime_downgrade_lock:
        _runtime_downgrade = None
        _runtime_downgrade_revision = -1


def runtime_downgrade(revision: Optional[int] = None) -> Optional[str]:
    """The note for *revision*, or None.

    Answering only for the revision it was recorded against is what stops a
    rejection of a replaced key describing the key that replaced it. Called
    without a revision it answers for the note's own, which is what a
    diagnostic wants: "is there a note at all".
    """
    with _runtime_downgrade_lock:
        if _runtime_downgrade is None:
            return None
        if revision is not None and revision != _runtime_downgrade_revision:
            return None
        return _runtime_downgrade


def state_for_verification(ok: bool, category=None) -> str:
    """The state to record for one verification attempt.

    The distinction this makes is the reason it exists. The previous
    version wrote `CREDENTIAL_VERIFIED if ok else CREDENTIAL_FAILED`, so a
    machine that was merely offline during setup ended up with a Settings
    page reading "The saved API key was rejected by Anthropic" — a false
    diagnosis that sends someone to replace a key that was never the
    problem.

      * the provider answered            -> verified
      * the provider was never reached,
        timed out, or rate-limited       -> configured_unverified
      * the provider answered about
        credit                           -> account_unfunded

    An outright rejection (auth, workspace-required) never reaches here:
    app/core/ai/key_check.py refuses to store that pair at all, so there
    is no state to record for it.
    """
    from app.core.errors import ErrorCategory

    if ok:
        return CREDENTIAL_VERIFIED
    if category == ErrorCategory.PROVIDER_BILLING:
        return CREDENTIAL_UNFUNDED
    return CREDENTIAL_UNVERIFIED


def note_runtime_failure(provider: str, category, credential_revision: int = -1) -> None:
    """Downgrade a recorded verification when a *live* request is rejected.

    **`credential_revision` is the pair that made the failed request**, from
    the snapshot the request was built with. A rejection is discarded unless
    that revision is still the current one: a request carrying the previous
    key can come back long after the user replaced it, and attributing its
    rejection to the new key marks a working credential as failed. A lock
    would not help — the delayed failure could simply wait for the save and
    then be just as wrong. Only identity can settle it.

    The default of -1 never matches a real revision, so a caller that
    forgets to pass one downgrades nothing rather than downgrading blindly.

    "Verified" has to mean "answered successfully the last time Anthropic
    was asked", not a permanent promise. A key that is revoked, expires, or
    loses access to its workspace an hour after it was saved would
    otherwise go on being reported as working until somebody re-saved it.

    Only an explicit rejection downgrades. A timeout, a rate limit and an
    unreachable provider say nothing about the credential, and treating
    them as a rejection would recreate the defect this replaces in the
    opposite direction.

    Never raises, never records success — a live *failure* cannot be
    evidence that a credential works — and never touches anything for a
    provider other than Anthropic. It can move between two negative states
    (a key that was rejected yesterday and is merely unfunded today is
    described by the newer observation, not the older one).
    """
    from app.core.errors import ErrorCategory

    try:
        if normalise_provider(provider) != PROVIDER_ANTHROPIC:
            return
        if category == ErrorCategory.PROVIDER_AUTH or category == ErrorCategory.PROVIDER_WORKSPACE_REQUIRED:
            downgraded = CREDENTIAL_FAILED
        elif category == ErrorCategory.PROVIDER_BILLING:
            downgraded = CREDENTIAL_UNFUNDED
        else:
            return

        from app.core.ai import credential_transaction as coordinator
        from app.core.ai import credential_view
        from app.core.preferences import get as get_preference
        from app.core.preferences import store_many as store_preferences

        # A cheap early-out, and *only* that. It can end in a skip and never
        # in a write, so an answer that goes stale between here and the gate
        # costs nothing. Its job is to keep an obviously-delayed failure from
        # queueing behind a credential change it is going to be discarded by
        # anyway.
        if credential_revision != credential_view.current_revision():
            logger.info(
                "Ignoring a provider rejection for a credential that is no longer stored.",
            )
            return

        try:
            with coordinator.pair_state_gate(DOWNGRADE_WAIT_SECONDS):
                # **The check that authorises the write, made while holding
                # the gate that keeps it true.** Checking before the gate and
                # writing after it is what this replaces: a save could commit
                # in between, and the rejection of the key it replaced was
                # then written over the key that replaced it —
                #
                #     revision_after_new_save        1
                #     state_before_old_failure       verified
                #     persisted_state_after_failure  verification_failed
                #
                # The process-local note stayed correctly revision-scoped;
                # the persisted preference did not, and the next snapshot
                # read it back and handed it to the new revision.
                if credential_revision != credential_view.current_revision():
                    logger.info(
                        "Ignoring a provider rejection for a credential that was "
                        "replaced while the rejection was being recorded.",
                    )
                    return

                # Held in this process first, so the observation applies even
                # if nothing below can be written.
                _remember_runtime_downgrade(downgraded, credential_revision)

                if (get_preference(VERIFICATION_PREFERENCE) or "").strip() == downgraded:
                    return  # already says this; a rewrite would only churn the file
                if not store_preferences({VERIFICATION_PREFERENCE: downgraded}):
                    logger.warning(
                        "Could not write the Anthropic credential downgrade to this PC's "
                        "settings. It applies for this session and will not survive a restart.",
                    )
                    return
                logger.info(
                    "Anthropic credential state downgraded to %s after a live rejection.",
                    downgraded,
                )
        except coordinator.TransactionBusy:
            # A credential change is running, so what is stored is being
            # decided right now and this rejection may be about either side
            # of it. Recording nothing is the only truthful option; a real
            # rejection of whatever ends up stored recurs on the next
            # request, which is how this path has always been expected to
            # converge.
            logger.info(
                "A credential change is in progress; a provider rejection was not "
                "recorded against it.",
            )
            return
    except Exception as exc:  # noqa: BLE001 — bookkeeping must never break a request
        logger.warning("Could not record the provider's runtime rejection. %s", describe(exc))


def anthropic_credential_state() -> str:
    """Which of the five states the stored Anthropic credential is in.

    Verification is recorded by the key-save path and by
    `note_runtime_failure()` — the only two places that have ever seen the
    provider answer. A key that predates the record — an upgrade from a
    build that stored keys without one — reads as `configured_unverified`,
    which is the honest answer: it may well work, and nothing here has
    watched it do so.
    """
    from app.core.ai import credential_view

    return credential_state_for(credential_view.current())


def credential_state_for(pair) -> str:
    """The state of one already-taken credential snapshot.

    Separated from the function above so a caller that has a snapshot — a
    request, a status endpoint — describes *that* pair rather than taking a
    second, possibly different look at the store.
    """
    if not pair.configured:
        return CREDENTIAL_NOT_CONFIGURED
    # A live rejection this process saw but could not write down still
    # happened. It is only ever a negative state, and it is answered only
    # for the revision it was recorded against — so it can neither claim a
    # rejected key works nor describe a credential that replaced the one it
    # was about.
    live = runtime_downgrade(pair.revision)
    if live:
        return live
    recorded = (pair.state or "").strip()
    return recorded if recorded in _RECORDED_STATES else CREDENTIAL_UNVERIFIED


def anthropic_status() -> ProviderStatus:
    """Reports what is known, never more.

    `available` means the provider answered for this credential — not that
    a credential exists. Neither the key nor the workspace ID is read into
    the result; only the state name and a fixed sentence for it.
    """
    state = anthropic_credential_state()
    return ProviderStatus(
        name=PROVIDER_ANTHROPIC,
        display_name="Anthropic (Claude)",
        kind="cloud",
        available=state == CREDENTIAL_VERIFIED,
        detail=_CREDENTIAL_DETAIL.get(state, _CREDENTIAL_DETAIL[CREDENTIAL_UNVERIFIED]),
        requires_credentials=True,
    )


def _ollama_base_url() -> str:
    return f"http://{OLLAMA_DEFAULT_HOST}:{OLLAMA_DEFAULT_PORT}"


def ollama_status(http_client=None) -> ProviderStatus:
    """Probes a real local Ollama instance. *http_client* is an injection
    seam for tests; production passes none and uses httpx directly.

    An unreachable server is the common case, not a failure: it means
    Ollama simply is not installed or running, which is reported plainly
    rather than logged as an error."""
    unavailable = ProviderStatus(
        name=PROVIDER_OLLAMA,
        display_name="Ollama (local models)",
        kind="local",
        available=False,
        detail=(
            "No local Ollama server detected. Set up local AI from Settings and "
            "JARVIS will install it for you, after showing you what it downloads."
        ),
    )

    try:
        if http_client is None:
            import httpx
            response = httpx.get(f"{_ollama_base_url()}/api/tags", timeout=OLLAMA_PROBE_TIMEOUT_SECONDS)
        else:
            response = http_client.get(f"{_ollama_base_url()}/api/tags", timeout=OLLAMA_PROBE_TIMEOUT_SECONDS)
    except Exception:
        # Connection refused/DNS/timeout — Ollama is not running. Normal.
        return unavailable

    if getattr(response, "status_code", None) != 200:
        return unavailable

    try:
        payload = response.json()
    except Exception:
        return unavailable

    models = _model_names(payload)
    if not models:
        return ProviderStatus(
            name=PROVIDER_OLLAMA,
            display_name="Ollama (local models)",
            kind="local",
            available=False,
            detail=(
                "Ollama is running but has no models installed. Download the "
                "recommended one from Settings — nothing starts until you press it."
            ),
        )

    return ProviderStatus(
        name=PROVIDER_OLLAMA,
        display_name="Ollama (local models)",
        kind="local",
        available=True,
        detail=f"Ollama is running with {len(models)} model(s) installed.",
        models=models,
    )


def _model_names(payload) -> List[str]:
    """Ollama's /api/tags returns {"models": [{"name": "llama3:latest", ...}]}.
    Anything that does not match that shape yields no models rather than
    a guess — a fabricated model list would be exactly the kind of false
    capability claim this module exists to prevent."""
    if not isinstance(payload, dict):
        return []
    entries = payload.get("models")
    if not isinstance(entries, list):
        return []
    names = []
    for entry in entries:
        if isinstance(entry, dict) and isinstance(entry.get("name"), str) and entry["name"]:
            names.append(entry["name"])
    return names


def detect_all(http_client=None) -> List[ProviderStatus]:
    return [anthropic_status(), ollama_status(http_client=http_client)]


def is_valid_provider(name: str) -> bool:
    return name in KNOWN_PROVIDERS


def normalise_provider(value) -> str:
    """Map any configured value onto a known provider name. Pure and
    total: an unrecognised value (including None or a non-string, which
    a mocked settings object can produce) falls back to anthropic — the
    historical default — rather than leaving the app in an unknown
    state or raising on a config typo."""
    try:
        name = str(value or "").strip().lower()
    except Exception:  # noqa: BLE001 — a __str__ that raises is still just "unrecognised"
        return PROVIDER_ANTHROPIC
    return name if is_valid_provider(name) else PROVIDER_ANTHROPIC


def selected_provider() -> str:
    """The provider actually in effect, normalised.

    A choice saved in Settings wins over the configured default — see
    app/core/preferences.py for why that precedence and not the other
    one. This is the single answer to "which provider?"; nothing should
    read settings.jarvis_ai_provider directly and reach a different one.
    """
    from app.config import settings
    from app.core.preferences import get as get_preference

    return normalise_provider(get_preference("ai_provider") or settings.jarvis_ai_provider)


def selected_ollama_model() -> str:
    """The chosen local model: the saved preference, else the configured
    default, else "" meaning "whatever the local instance reports first"."""
    from app.config import settings
    from app.core.preferences import get as get_preference

    return get_preference("ollama_model") or (getattr(settings, "jarvis_ollama_model", "") or "")
