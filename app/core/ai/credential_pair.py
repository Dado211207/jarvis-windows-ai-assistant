"""The Anthropic credential and the metadata that describes it, applied
together — or not applied at all.

**Why this exists.** The key belongs in Windows Credential Manager and the
workspace ID does not: one is a secret, the other is account metadata that
identifies a workspace and authenticates nothing. That is the right split,
and it means saving a key writes **two different stores**. The first
version wrote them in order and threw the second result away:

    set_stored_api_key(key)            # Credential Manager
    store_preferences({...})           # preferences.json — result discarded
    return "API key saved and verified."

Replacing a working key on a machine where the preferences write failed
therefore produced: the *new* key in Credential Manager, the *previous*
key's workspace ID and the *previous* key's verdict in preferences, and a
success message. Every later request would then be sent with one key's
credential and another key's workspace, and the Settings page would report
a verification that belonged to a credential that no longer existed.

**What this module guarantees.** Not "atomicity" — there is no transaction
spanning Credential Manager and a JSON file, and claiming one would be the
same species of overclaim as the message above. What it guarantees is that
*every* failure ordering ends in a state that is either correct or
described:

    credential write fails      nothing was written; the previous pair
                                stands, untouched, and is reported as such
    metadata write fails        the credential is put back, so the previous
                                pair stands again
    the put-back also fails     the stale metadata is cleared, so the new
                                key reads as configured-but-unchecked
                                rather than inheriting a stranger's
                                workspace and verdict
    that clear fails too        reported precisely, as an inconsistent
                                state, and never as success

**A failed replacement never destroys a working pair.** The rollback needs
the previous key, and `credentials.stored_api_key_snapshot()` reports
whether the store was actually *reached* rather than collapsing
"unreachable" into "there was no key". A rollback that cleared the
credential on the strength of an unread snapshot would delete the only
working key on the machine because a *different* write failed.

**The previous metadata needs no snapshot, and that is a property of the
preferences store rather than an oversight.** `preferences.store_many()`
serialises the whole file to a temporary path and `replace()`s it, so a
failed write leaves the previous contents byte-for-byte intact — there is
nothing to restore. Restoring the credential therefore restores the whole
pair. The one place this module *does* change metadata on a failure path
is the orphan branch below, where the old values are cleared precisely
because putting them back would leave them describing a key that is gone.

**Nothing here is logged.** Not the previous key, not the proposed key,
not either workspace ID. The log lines carry booleans and an outcome name.
"""

from dataclasses import dataclass
from typing import Optional

from app.logging_config import get_logger

logger = get_logger("core.ai.credential_pair")

#: Outcome names. Each is a distinct thing that happened, because the
#: whole point of this module is that they are not the same thing.
APPLIED = "applied"
CREDENTIAL_STORE_FAILED = "credential_store_failed"
ROLLED_BACK = "rolled_back"
METADATA_ORPHANED = "metadata_orphaned"
INCONSISTENT = "inconsistent"


@dataclass(frozen=True)
class PairOutcome:
    """What happened, in terms a route can turn into an honest response.

    `ok` is "the intended final state is in place". `consistent` is the
    weaker and more important question: do the two stores describe the
    same credential? A caller that only checks `ok` still cannot report
    success by accident, because `ok` is False for every failure ordering.
    """

    outcome: str
    message: str
    stored: bool
    consistent: bool
    rolled_back: bool = False
    category: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.outcome == APPLIED


def _credentials():
    from app.core import credentials
    return credentials


def _preferences():
    from app.core import preferences
    return preferences


def _metadata_keys():
    from app.core.ai.workspace import PREFERENCE_KEY as WORKSPACE_KEY
    from app.core.providers import VERIFICATION_PREFERENCE
    return WORKSPACE_KEY, VERIFICATION_PREFERENCE


def _write_metadata(workspace: str, state: str) -> bool:
    workspace_key, state_key = _metadata_keys()
    return bool(_preferences().store_many({workspace_key: workspace, state_key: state}))


def save(api_key: str, workspace: str, state: str) -> PairOutcome:
    """Store *api_key* with the *workspace* it acts in and the *state* the
    verification observed, or leave the installation as it was.

    *state* is one of app/core/providers.py's CREDENTIAL_* names and comes
    from `state_for_verification()`; this module never decides what a
    verification meant, only that the verdict and the credential it
    describes are written together.
    """
    credentials = _credentials()

    # Read before writing. Held in memory for the length of this call and
    # never logged, echoed or returned — see the module docstring.
    reachable, previous_key = credentials.stored_api_key_snapshot()

    if not credentials.set_stored_api_key(api_key):
        return PairOutcome(
            outcome=CREDENTIAL_STORE_FAILED,
            message="Could not save the key to the Windows credential store. Nothing was changed.",
            stored=False,
            consistent=True,          # nothing was written, so nothing can disagree
            category="credential_store",
        )

    if _write_metadata(workspace, state):
        logger.info(
            "Anthropic credential saved. workspace_configured=%s state=%s",
            bool(workspace), state,
        )
        return PairOutcome(outcome=APPLIED, message="API key saved.", stored=True, consistent=True)

    # The credential moved and its description did not. Put the credential
    # back, so the pair that was there before is the pair that is there
    # now — the previous metadata was never touched, so restoring the key
    # restores the whole pair.
    logger.warning("Could not save the Anthropic credential's metadata; rolling the credential back.")
    if reachable:
        restored = (
            credentials.set_stored_api_key(previous_key) if previous_key
            else credentials.clear_stored_api_key()
        )
    else:
        # The snapshot never reached the store, so there is no previous
        # value to restore and no way to tell "there was no key" from
        # "could not read one". Clearing here would destroy a credential
        # that may be the only working one on the machine.
        restored = False
        logger.warning("The previous credential could not be read, so it is not being rolled back.")

    if restored:
        return PairOutcome(
            outcome=ROLLED_BACK,
            message=(
                "Could not save the Workspace ID and verification state, so the key was not "
                "changed either. Your previous settings are still in place — please try again."
            ),
            stored=False,
            consistent=True,
            rolled_back=True,
            category="preferences",
        )

    # Rollback failed. The stored key is now the new one while the metadata
    # still describes the old one, which is the exact state that must never
    # be reported as success. Clearing the metadata is the only remaining
    # move: it leaves the new key reading as configured-but-unchecked
    # instead of inheriting a workspace and a verdict that are not its own.
    if _write_metadata("", ""):
        return PairOutcome(
            outcome=METADATA_ORPHANED,
            message=(
                "The key was saved, but its Workspace ID and verification state could not be "
                "written and the previous key could not be restored. The Workspace ID has been "
                "cleared so nothing describes the wrong key — enter it again and save."
            ),
            stored=True,
            consistent=False,
            category="preferences",
        )

    return PairOutcome(
        outcome=INCONSISTENT,
        message=(
            "The key was saved, but neither its Workspace ID nor the previous key could be "
            "written back. JARVIS cannot tell which workspace the stored key acts in. Check "
            "that this PC can write to its settings folder, then save the key and Workspace ID "
            "again."
        ),
        stored=True,
        consistent=False,
        category="inconsistent_state",
    )


def clear() -> PairOutcome:
    """Remove the credential and the metadata that only described it.

    The workspace ID and the verification state are properties *of the
    credential*, not standing preferences: leaving them behind means the
    next key entered inherits the workspace of the one before it, and the
    status page goes on reporting a verification that belonged to a
    credential that no longer exists.

    The credential goes first, deliberately: a metadata write that failed
    beforehand would otherwise leave a key with no workspace, which is a
    worse state than a workspace with no key. If the metadata write fails
    afterwards, that is reported — never folded into "API key removed."
    """
    credentials = _credentials()

    if not credentials.clear_stored_api_key():
        return PairOutcome(
            outcome=CREDENTIAL_STORE_FAILED,
            message="Could not remove the key from the OS credential store. Nothing was changed.",
            stored=True,
            consistent=True,
            category="credential_store",
        )

    if _write_metadata("", ""):
        logger.info("Anthropic API key removed; workspace metadata cleared.")
        return PairOutcome(
            outcome=APPLIED, message="API key removed.", stored=False, consistent=True,
        )

    logger.warning("The Anthropic API key was removed but its metadata could not be cleared.")
    return PairOutcome(
        outcome=METADATA_ORPHANED,
        message=(
            "The API key was removed, but its Workspace ID could not be cleared from this PC's "
            "settings. Clear the Workspace ID field and save before entering a different key, "
            "so the new one does not inherit it."
        ),
        stored=False,
        consistent=False,
        category="preferences",
    )
