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

**One operation at a time, and that is a separate guarantee from the one
above.** The failure orderings are about what happens when a step *fails*.
They said nothing about what happens when two operations *succeed* and
interleave between the steps — and two overlapping saves, each entirely
successful, left the newer request's key beside the older request's
Workspace ID. Every part had behaved as designed: the credential layer owes
`MUTATION_APPLIED` to a write that really landed, and nothing had ever said
that "my write landed" is a different permission from "I may still commit
my own description of the credential". `save()` and `clear()` therefore run
their whole body — snapshot, both stores, rollback, the runtime-downgrade
note and the outcome — inside `app/core/ai/credential_transaction.py`,
which serves one credential change at a time and refuses an overlapping one
rather than queueing it forever. Still not atomicity across two stores;
what it is, is one writer.

**A failed replacement never destroys a working pair**, and that claim
needed two corrections before it was true. The first version read the
previous key for its own rollback but *began the replacement anyway* when
the snapshot had not reached the store — leaving the one case it could not
undo as the one case it entered blind. Worse, the layer underneath it
reconciled every failed write to *absence*, so a replacement that failed or
timed out deleted the key it was replacing while this module answered
"Nothing was changed" (see `app/core/credentials.py`).

So: a store that cannot be read is a store this module will not write to,
and it says so instead of trying. `credentials.stored_api_key_snapshot()`
reports whether the store was actually *reached* rather than collapsing
"unreachable" into "there was no key", and an unreachable snapshot is now a
refusal rather than a rollback JARVIS is unable to perform.

**"Nothing was changed" is a postcondition, not a consolation.** A write
that timed out may still complete inside the backend, so it earns a
different sentence from one the backend refused outright. `MutationResult`
carries that distinction and every message below is chosen from it.

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
CREDENTIAL_STORE_UNREADABLE = "credential_store_unreadable"
CREDENTIAL_WRITE_UNCONFIRMED = "credential_write_unconfirmed"
CREDENTIAL_SUPERSEDED = "credential_superseded"
CREDENTIAL_BUSY = "credential_busy"
REMOVAL_UNCONFIRMED = "removal_unconfirmed"
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
    #: Whether JARVIS *established* that the new key is in the store. Not
    #: "the key is definitely absent": after a write that never came back
    #: nobody knows, and this reads False because nothing was established.
    #: The message says which of the two happened; no message claims a
    #: postcondition that was only predicted.
    #:
    #: `None` is a third answer, distinct from False: **not established at
    #: all**. It is what a request that never started must say, because
    #: "the key is not stored" is a claim about the installation and such a
    #: request looked at neither store.
    stored: Optional[bool]
    consistent: Optional[bool]
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


def _forget_runtime_downgrade() -> None:
    """Drop the process-local "this credential was rejected" note.

    That note describes *the credential that was in the store*. Once a
    different one is there — or none at all — it describes nothing, and
    leaving it would report a new key as rejected before it had ever been
    used. Called on every path where the stored credential actually
    changed, and on no other.
    """
    from app.core.providers import clear_runtime_downgrade
    clear_runtime_downgrade()


def _metadata_keys():
    from app.core.ai.workspace import PREFERENCE_KEY as WORKSPACE_KEY
    from app.core.providers import VERIFICATION_PREFERENCE
    return WORKSPACE_KEY, VERIFICATION_PREFERENCE


def _write_metadata(workspace: str, state: str) -> bool:
    workspace_key, state_key = _metadata_keys()
    return bool(_preferences().store_many({workspace_key: workspace, state_key: state}))


def _transaction():
    from app.core.ai import credential_transaction
    return credential_transaction


def _superseded_request(what: str) -> PairOutcome:
    """A later request already claimed the credential, so this one stops.

    The rule is **the request admitted later wins** — admitted, not
    finished. `POST /settings/api-key` asks Anthropic to check the key
    before storing it, and that check cannot happen under the coordinator's
    lock, so two requests can reach this module in the opposite order to the
    one the person made them in. Ordering by admission means the outcome is
    the order they acted in rather than the order Anthropic answered in.

    Nothing has been written when this is returned: the claim is tested
    before the first store is touched.
    """
    return PairOutcome(
        outcome=CREDENTIAL_SUPERSEDED,
        message=(
            f"A newer change to your API key was accepted while this one was being "
            f"checked, so JARVIS did not {what} — the newer change is what is in place. "
            f"Open Settings to see what is stored."
        ),
        stored=False,
        consistent=True,
        category="credential_store",
    )


def _busy(what: str) -> PairOutcome:
    """This request did not start — and that is *all* it may claim.

    An earlier version answered `stored=False` for a busy save,
    `stored=True` for a busy removal and `consistent=True` for both. Those
    are statements about the installation, not about this request, and this
    request observed neither store. Worse, the moment they are least likely
    to be true is exactly the moment this outcome occurs: the transaction in
    front may be part-way between the credential and its metadata, which is
    the one window in which the pair really is inconsistent.

    So both are `None` — not established — and the sentence says what did
    happen rather than describing a state nobody looked at.
    """
    return PairOutcome(
        outcome=CREDENTIAL_BUSY,
        message=(
            f"Another change to your API key is already running, so JARVIS did not "
            f"{what} and cannot yet say what that other change has done. "
            f"Wait for it to finish, check Settings, then try again."
        ),
        stored=None,
        consistent=None,
        category="credential_store",
    )


def save(api_key: str, workspace: str, state: str, intent=None) -> PairOutcome:
    """Store *api_key* with the *workspace* it acts in and the *state* the
    verification observed, or leave the installation as it was.

    *state* is one of app/core/providers.py's CREDENTIAL_* names and comes
    from `state_for_verification()`; this module never decides what a
    verification meant, only that the verdict and the credential it
    describes are written together.

    **The whole operation runs alone.** Snapshot, credential write, metadata
    write, rollback, the runtime-downgrade note and the outcome that
    describes them are one transaction — see
    `app/core/ai/credential_transaction.py` for the interleaving that made
    two *successful* saves leave a key and a Workspace ID belonging to
    different requests.
    """
    transaction = _transaction()
    ticket = transaction.admit("save") if intent is None else intent
    try:
        with transaction.begin("save") as active:
            if not transaction.claim(ticket):
                return _superseded_request("save this key")
            return _save_alone(api_key, workspace, state, active)
    except transaction.TransactionBusy:
        return _busy("save this key")


def _save_alone(api_key: str, workspace: str, state: str, active) -> PairOutcome:
    """`save()`'s body, with the coordinator's transaction already held."""
    credentials = _credentials()

    # Read before writing. Held in memory for the length of this call and
    # never logged, echoed or returned — see the module docstring.
    reachable, previous_key = credentials.stored_api_key_snapshot()
    if not reachable:
        # The one situation this module cannot recover from is the one it
        # must therefore not enter. Without a proven previous value there is
        # no rollback target, and "the entry read as empty" cannot be told
        # from "the entry could not be read" — so a failure here would be
        # indistinguishable from a machine that never had a key, and the
        # recovery would delete one that did.
        return PairOutcome(
            outcome=CREDENTIAL_STORE_UNREADABLE,
            message=(
                "JARVIS could not read this PC's credential store, so it will not write to "
                "it either — a failed write could not then be undone, and that could lose "
                "the key you already have. Nothing was changed. Sign in to Windows normally "
                "and try again; if it keeps happening, Credential Manager may be unavailable "
                "on this account."
            ),
            stored=False,
            consistent=True,          # nothing was written, so nothing can disagree
            category="credential_store",
        )

    write = credentials.set_stored_api_key_detailed(api_key)
    if not write.ok:
        if write.provably_unchanged:
            return PairOutcome(
                outcome=CREDENTIAL_STORE_FAILED,
                message=(
                    "Could not save the key to the Windows credential store. "
                    "Nothing was changed."
                ),
                stored=False,
                consistent=True,
                category="credential_store",
            )
        if write.superseded:
            # A different change to the same key was made while this one was
            # failing, and JARVIS deliberately did *not* roll back over it.
            # The sentence below must therefore not be the one that promises
            # a rollback: that promise would be describing an action nobody
            # took, about a key the user may have just deliberately changed.
            return PairOutcome(
                outcome=CREDENTIAL_SUPERSEDED,
                message=(
                    "Windows did not confirm this key was saved, and another change to the "
                    "key was made while this one was still running. That newer change is "
                    "what is in place — JARVIS did not undo it. Open Settings to see which "
                    "key is stored, and save again if it is not the one you wanted."
                ),
                stored=False,
                # This request never reached the metadata, so it is not the
                # reason the two stores could disagree.
                consistent=True,
                category="credential_store",
            )
        # The call never came back. It may still complete, so nothing here
        # may claim the store is as it was; what JARVIS *has* done is ask
        # for the previous key to be put back behind it.
        return PairOutcome(
            outcome=CREDENTIAL_WRITE_UNCONFIRMED,
            message=(
                "Windows did not confirm the new key was saved, so JARVIS cannot tell you "
                "whether it was. It has asked for your previous key to be put back and has "
                "changed nothing else. Check Settings, then try saving again."
            ),
            stored=False,
            consistent=True,          # the metadata was never touched
            category="credential_store",
        )

    # The credential is in place. Committing *this* request's description of
    # it is a separate permission, and the tripwire below is where they are
    # kept separate: a write that landed never authorises an operation that
    # has been overtaken to write its own Workspace ID. Holding the
    # transaction makes this unreachable — which is the point of asserting
    # it rather than assuming it.
    if not active.is_newest:
        logger.warning(
            "A credential save was overtaken before its metadata could be written; "
            "leaving the newer request's Workspace ID in place.",
        )
        return PairOutcome(
            outcome=CREDENTIAL_SUPERSEDED,
            message=(
                "Another change to the key was made while this one was still running, and "
                "that newer change is what is in place — JARVIS did not overwrite it with "
                "this key's Workspace ID. Open Settings to see what is stored, and save "
                "again if it is not what you wanted."
            ),
            stored=False,
            consistent=True,
            category="credential_store",
        )

    if _write_metadata(workspace, state):
        _forget_runtime_downgrade()
        logger.info(
            "Anthropic credential saved. workspace_configured=%s state=%s",
            bool(workspace), state,
        )
        return PairOutcome(outcome=APPLIED, message="API key saved.", stored=True, consistent=True)

    # The credential moved and its description did not. Put the credential
    # back, so the pair that was there before is the pair that is there
    # now — the previous metadata was never touched, so restoring the key
    # restores the whole pair. The snapshot was proven above, so there is
    # a real target for this and no guessing involved.
    logger.warning("Could not save the Anthropic credential's metadata; rolling the credential back.")
    restore = (
        credentials.set_stored_api_key_detailed(previous_key) if previous_key
        else credentials.clear_stored_api_key_detailed()
    )

    if restore.ok:
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
        _forget_runtime_downgrade()
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

    _forget_runtime_downgrade()
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


def clear(intent=None) -> PairOutcome:
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

    **Repeating this is safe, and repeating it is the recovery.** Deleting a
    credential that is already absent succeeds, so a second Remove goes
    straight on to the metadata clear that failed the first time. That
    matters because the advice this used to give — clear the Workspace ID
    field and save — cannot be carried out: `SetApiKeyRequest` refuses a
    blank API key, so there is no such request to make. Every partial
    outcome below therefore names Remove, which is a button that exists.

    **The whole operation runs alone**, for the reason `save()` does: a
    metadata clear that lands after a newer save committed leaves a stored
    key describing nothing. See `app/core/ai/credential_transaction.py`.
    """
    transaction = _transaction()
    ticket = transaction.admit("clear") if intent is None else intent
    try:
        with transaction.begin("clear") as active:
            if not transaction.claim(ticket):
                return _superseded_request("remove this key")
            return _clear_alone(active)
    except transaction.TransactionBusy:
        return _busy("remove this key")


def _clear_alone(active) -> PairOutcome:
    """`clear()`'s body, with the coordinator's transaction already held."""
    credentials = _credentials()

    removal = credentials.clear_stored_api_key_detailed()
    if not removal.ok:
        if removal.provably_unchanged:
            return PairOutcome(
                outcome=CREDENTIAL_STORE_FAILED,
                message=(
                    "Could not remove the key from the OS credential store. "
                    "Nothing was changed."
                ),
                stored=True,
                consistent=True,
                category="credential_store",
            )
        if removal.superseded:
            # "Press Remove again" is the right advice for an ordinary
            # unconfirmed removal and the wrong advice here: a newer request
            # may have just stored a key on purpose, and repeating a removal
            # blindly would delete it. Say what happened instead.
            logger.warning("The Anthropic API key removal was superseded by a newer request.")
            return PairOutcome(
                outcome=CREDENTIAL_SUPERSEDED,
                message=(
                    "Windows did not confirm the key was removed, and another change to the "
                    "key was made while this removal was still running. That newer change is "
                    "what is in place — JARVIS did not undo it. The Workspace ID has been "
                    "left as it was. Open Settings to see what is stored before removing again."
                ),
                stored=False,
                consistent=False,
                category="credential_store",
            )
        # The delete never came back and may still complete, so the key may
        # be gone by the time this is read. The metadata is deliberately
        # left alone: clearing it while the key may still be there produces
        # a key with no workspace, which is the worse of the two states.
        logger.warning("The Anthropic API key removal was not confirmed by the credential store.")
        return PairOutcome(
            outcome=REMOVAL_UNCONFIRMED,
            message=(
                "Windows did not confirm the key was removed, so JARVIS cannot tell you "
                "whether it is gone. The Workspace ID has been left as it was. Press Remove "
                "again — repeating it is safe, and it finishes the job either way."
            ),
            stored=False,
            consistent=False,
            category="credential_store",
        )

    # Same separation as in `save()`: the credential is gone, but clearing
    # the description of it is a second permission that an overtaken
    # operation does not have. A stale clear here is what would leave a key
    # a newer save had just stored describing nothing at all.
    if not active.is_newest:
        logger.warning(
            "A credential removal was overtaken before its metadata could be cleared; "
            "leaving the newer request's Workspace ID in place.",
        )
        return PairOutcome(
            outcome=CREDENTIAL_SUPERSEDED,
            message=(
                "Another change to the key was made while this removal was still running, "
                "and that newer change is what is in place — JARVIS did not clear its "
                "Workspace ID. Open Settings to see what is stored before removing again."
            ),
            stored=False,
            consistent=False,
            category="credential_store",
        )

    if _write_metadata("", ""):
        _forget_runtime_downgrade()
        logger.info("Anthropic API key removed; workspace metadata cleared.")
        return PairOutcome(
            outcome=APPLIED, message="API key removed.", stored=False, consistent=True,
        )

    logger.warning("The Anthropic API key was removed but its metadata could not be cleared.")
    return PairOutcome(
        outcome=METADATA_ORPHANED,
        message=(
            "The API key was removed, but its Workspace ID could not be cleared from this PC's "
            "settings. Press Remove again to finish — the key is already gone, so repeating it "
            "only clears the Workspace ID, and a new key entered before then would inherit it."
        ),
        stored=False,
        consistent=False,
        category="preferences",
    )
