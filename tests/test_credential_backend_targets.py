"""One logical secret must occupy one credential target, and a backend
exception must never be mistaken for proof that nothing happened.

Two defects, both invisible to a fake that models the credential store as a
single dictionary entry keyed by `(service, username)`:

**1. The pinned Windows backend keeps a copy of the secret it replaced.**
`requirements-windows.txt` pins `keyring==25.7.0`, whose
`WinVaultKeyring.set_password()` copies any existing credential to
`{existing_username}@{service}` *before* writing the replacement — and does
so unconditionally, not only on the username collision its own docstring
describes. With `SERVICE_NAME="JARVIS"` and `USERNAME="anthropic_api_key"`
that leaves two real entries after any key change:

    JARVIS
    anthropic_api_key@JARVIS        <- the key the user just replaced

Both were observed on a real machine with `cmdkey /list`. Reads never show
it: `_resolve_credential()` returns the plain target first, so the stale
secret is invisible to the application and still on disk.

**2. "The backend raised" does not mean "the store is unchanged."**
`set_password` above performs two writes, and `delete_password` performs up
to two deletes; either can mutate the store and *then* raise. Classifying
every non-timeout exception as `MUTATION_UNCHANGED` reports a postcondition
nobody observed, and — worse — skips the reconciliation, so a half-applied
replacement is never undone.

The fake below is a faithful re-implementation of the pinned backend's
target semantics, keyed by **target name**, with fault injection at each
point the real one can fail. `tests/test_windows_credential_targets.py`
proves the same premise against the real Windows Credential Manager on a
runner that has it; this file makes the behaviour testable everywhere, and
deterministically — every wait is an Event or a thread join, never a sleep.
"""

import sys
import threading
import types

import pytest

from tests.test_credential_replacement_safety import (
    JOIN_TIMEOUT,
    NEW_KEY,
    OLD_KEY,
    _Recorder,
    settle,
)

#: A third value, for the case where a residue must be told apart from both
#: the previous and the proposed key.
OTHER_KEY = "sk-ant-api03-A-THIRD-key-belonging-to-nobody"


# ---------------------------------------------------------------------------
# A fake that behaves like keyring 25.7.0's WinVaultKeyring
# ---------------------------------------------------------------------------

class _WindowsLikeKeyring:
    """Target-name semantics copied from the pinned backend.

    Deliberately *not* a dictionary keyed by `(service, username)`: that
    shape is what hid the compound target from every earlier test.
    """

    def __init__(self):
        self._lock = threading.Lock()
        #: target name -> (username, value)
        self.targets = {}
        #: every mutation, in order, as (operation, target). No values.
        self.applied = []
        self.errors = None  # filled in by _install

        self._raise_before_set = 0
        self._raise_after_compound_copy = 0
        self._raise_after_set = 0
        self._raise_before_delete = 0
        self._raise_after_first_delete = 0
        self._block_sets = 0
        self.release_set = threading.Event()
        self.set_entered = threading.Event()

    # -- the three methods app/core/credentials.py calls --------------------

    @staticmethod
    def _compound(username, service):
        return f"{username}@{service}"

    def get_password(self, service, username):
        with self._lock:
            entry = self.targets.get(service)
            if not entry or (username and entry[0] != username):
                entry = self.targets.get(self._compound(username, service))
            return entry[1] if entry else None

    def set_password(self, service, username, password):
        with self._lock:
            fail_before = self._take("_raise_before_set")
            fail_mid = self._take("_raise_after_compound_copy")
            fail_after = self._take("_raise_after_set")
            blocking = self._take("_block_sets")
        self.set_entered.set()
        if fail_before:
            raise RuntimeError("the credential backend refused before writing")
        if blocking:
            assert self.release_set.wait(JOIN_TIMEOUT), "the test never released the write"
        with self._lock:
            existing = self.targets.get(service)
            if existing:
                # Exactly what the pinned backend does, unconditionally.
                compound = self._compound(existing[0], service)
                self.targets[compound] = existing
                self.applied.append(("set", compound))
        if fail_mid:
            # The real shape of a partial write: the previous secret has been
            # copied to the compound target and the replacement never landed.
            raise RuntimeError("the credential backend failed between its two writes")
        with self._lock:
            self.targets[service] = (username, password)
            self.applied.append(("set", service))
        if fail_after:
            raise RuntimeError("the credential backend wrote, then failed")

    def delete_password(self, service, username):
        if self._take("_raise_before_delete"):
            raise RuntimeError("the credential backend refused before deleting")
        compound = self._compound(username, service)
        deleted = False
        for index, target in enumerate((service, compound)):
            with self._lock:
                entry = self.targets.get(target)
                if entry and entry[0] == username:
                    deleted = True
                    del self.targets[target]
                    self.applied.append(("delete", target))
            if index == 0 and deleted and self._take("_raise_after_first_delete"):
                raise RuntimeError("the credential backend deleted one target, then failed")
        if not deleted:
            raise self.errors.PasswordDeleteError(service)

    # -- what the test drives ----------------------------------------------

    def _take(self, name):
        value = getattr(self, name)
        if value > 0:
            setattr(self, name, value - 1)
            return True
        return False

    def seed(self, target, username, value):
        with self._lock:
            self.targets[target] = (username, value)

    def value_at(self, target):
        with self._lock:
            entry = self.targets.get(target)
            return entry[1] if entry else None

    def username_at(self, target):
        with self._lock:
            entry = self.targets.get(target)
            return entry[0] if entry else None

    def target_names(self):
        with self._lock:
            return sorted(self.targets)

    def holds_value_anywhere(self, value):
        with self._lock:
            return any(stored == value for _user, stored in self.targets.values())


def _install(monkeypatch, fake=None):
    fake = fake or _WindowsLikeKeyring()

    errors_module = types.ModuleType("keyring.errors")

    class PasswordDeleteError(Exception):
        pass

    errors_module.PasswordDeleteError = PasswordDeleteError
    fake.errors = errors_module

    keyring_module = types.ModuleType("keyring")
    keyring_module.get_password = fake.get_password
    keyring_module.set_password = fake.set_password
    keyring_module.delete_password = fake.delete_password
    keyring_module.errors = errors_module

    monkeypatch.setitem(sys.modules, "keyring", keyring_module)
    monkeypatch.setitem(sys.modules, "keyring.errors", errors_module)
    return fake


@pytest.fixture(autouse=True)
def _clean_keyring_import():
    sys.modules.pop("keyring", None)
    sys.modules.pop("keyring.errors", None)
    yield
    settle()
    sys.modules.pop("keyring", None)
    sys.modules.pop("keyring.errors", None)


def _compound_of(credentials, username=None):
    username = username or credentials.USERNAME
    return f"{username}@{credentials.SERVICE_NAME}"


# ---------------------------------------------------------------------------
# The fake has to keep matching the thing it stands in for
# ---------------------------------------------------------------------------

def test_the_pinned_backend_still_has_the_semantics_this_file_models():
    """A keyring upgrade that changed `set_password` would silently make
    every test below a test of nothing. Read the installed source and
    require the two behaviours the fake reproduces."""
    keyring_windows = pytest.importorskip(
        "keyring.backends.Windows",
        reason="keyring is a Windows packaging dependency; nothing to compare against",
    )
    import inspect

    set_source = inspect.getsource(keyring_windows.WinVaultKeyring.set_password)
    delete_source = inspect.getsource(keyring_windows.WinVaultKeyring.delete_password)

    assert "_compound_name" in set_source, (
        "set_password no longer writes a compound target; re-derive the design in "
        "app/core/credentials.py from the new source"
    )
    assert "existing_pw" in set_source
    assert "for target in service, compound" in delete_source, (
        "delete_password no longer walks both targets"
    )
    assert (
        keyring_windows.WinVaultKeyring._compound_name("anthropic_api_key", "JARVIS")
        == "anthropic_api_key@JARVIS"
    )


# ---------------------------------------------------------------------------
# Blocker 1 — one logical secret, one target
# ---------------------------------------------------------------------------

def test_a_first_save_touches_exactly_one_target(monkeypatch):
    from app.core import credentials

    fake = _install(monkeypatch)
    assert credentials.set_stored_api_key(NEW_KEY) is True
    settle()

    assert fake.target_names() == [credentials.SERVICE_NAME]


def test_a_replacement_leaves_no_copy_of_the_key_it_replaced(monkeypatch):
    """The defect the owner saw in `cmdkey /list`: two targets, the second
    holding the key that was just replaced."""
    from app.core import credentials

    fake = _install(monkeypatch)
    assert credentials.set_stored_api_key(OLD_KEY) is True
    settle()
    assert credentials.set_stored_api_key(NEW_KEY) is True
    settle()

    assert fake.value_at(credentials.SERVICE_NAME) == NEW_KEY
    assert fake.target_names() == [credentials.SERVICE_NAME], (
        f"a replacement left extra credential targets: {fake.target_names()}"
    )


def test_no_target_anywhere_still_holds_the_replaced_key(monkeypatch):
    """Stated as the property that actually matters, independently of which
    target name the backend happens to choose."""
    from app.core import credentials

    fake = _install(monkeypatch)
    credentials.set_stored_api_key(OLD_KEY)
    settle()
    credentials.set_stored_api_key(NEW_KEY)
    settle()

    assert fake.holds_value_anywhere(OLD_KEY) is False, (
        "the previous key survives somewhere in the credential store"
    )


def test_an_installation_that_already_has_both_targets_is_cleaned_up(monkeypatch):
    """Migration: the owner's machine already carries the residue. The next
    ordinary save has to leave it clean, without a special user action."""
    from app.core import credentials

    fake = _install(monkeypatch)
    fake.seed(credentials.SERVICE_NAME, credentials.USERNAME, OLD_KEY)
    fake.seed(_compound_of(credentials), credentials.USERNAME, OTHER_KEY)

    assert credentials.set_stored_api_key(NEW_KEY) is True
    settle()

    assert fake.value_at(credentials.SERVICE_NAME) == NEW_KEY
    assert fake.target_names() == [credentials.SERVICE_NAME]
    assert fake.holds_value_anywhere(OTHER_KEY) is False


def test_the_residue_is_reported_so_a_purge_cannot_claim_a_clean_store(monkeypatch):
    """Uninstall reporting: `owned_credential_status()` decides whether a
    full purge may claim success. It must see the compound target."""
    from app.core import credentials

    fake = _install(monkeypatch)
    fake.seed(_compound_of(credentials), credentials.USERNAME, OTHER_KEY)

    anthropic = credentials.OWNED_CREDENTIALS[0]
    reached, present = credentials.owned_credential_status(anthropic)

    assert reached is True
    assert present is True, (
        "a secret in the compound target was reported as no credential at all"
    )


def test_a_compound_target_belonging_to_someone_else_is_left_alone(monkeypatch):
    """Ownership is proven before anything is deleted. A target that does not
    carry JARVIS's own username is not JARVIS's to remove."""
    from app.core import credentials

    fake = _install(monkeypatch)
    stranger = f"someone-else@{credentials.SERVICE_NAME}"
    fake.seed(stranger, "someone-else", OTHER_KEY)

    credentials.set_stored_api_key(NEW_KEY)
    settle()

    assert fake.value_at(stranger) == OTHER_KEY, (
        "a credential JARVIS did not write was deleted"
    )
    assert fake.username_at(stranger) == "someone-else"


def test_removal_removes_both_targets(monkeypatch):
    from app.core import credentials

    fake = _install(monkeypatch)
    fake.seed(credentials.SERVICE_NAME, credentials.USERNAME, NEW_KEY)
    fake.seed(_compound_of(credentials), credentials.USERNAME, OLD_KEY)

    assert credentials.clear_stored_api_key() is True
    settle()

    assert fake.target_names() == []


@pytest.mark.parametrize("index", (0, 1, 2))
def test_every_owned_credential_gets_the_same_treatment(monkeypatch, index):
    """ElevenLabs and OpenAI live in the same store through the same code,
    so the same backend behaviour applies to them."""
    from app.core import credentials

    fake = _install(monkeypatch)
    owned = credentials.OWNED_CREDENTIALS[index]
    compound = f"{owned.username}@{credentials.SERVICE_NAME}"

    fake.seed(credentials.SERVICE_NAME, owned.username, OLD_KEY)
    fake.seed(compound, owned.username, OTHER_KEY)

    assert credentials.clear_owned_credential(owned) is True
    settle()

    assert fake.holds_value_anywhere(OLD_KEY) is False
    assert fake.holds_value_anywhere(OTHER_KEY) is False


def test_a_timed_out_replacement_that_lands_late_also_leaves_one_target(monkeypatch):
    """The reconciliation path must end in the same one-target state as the
    ordinary one — it writes through the same backend."""
    from app.core import credentials

    monkeypatch.setattr(credentials, "TIMEOUT_SECONDS", 0.2)
    fake = _install(monkeypatch)
    fake.seed(credentials.SERVICE_NAME, credentials.USERNAME, OLD_KEY)
    fake._block_sets = 1

    assert credentials.set_stored_api_key(NEW_KEY) is False
    assert fake.set_entered.wait(JOIN_TIMEOUT)
    fake.release_set.set()
    settle()

    assert fake.value_at(credentials.SERVICE_NAME) == OLD_KEY
    assert fake.target_names() == [credentials.SERVICE_NAME], (
        f"the reconciliation left extra targets: {fake.target_names()}"
    )


# ---------------------------------------------------------------------------
# Blocker 2 — an exception is not a postcondition
# ---------------------------------------------------------------------------

def test_a_backend_that_raises_before_touching_anything_is_provably_unchanged(monkeypatch):
    """The true negative, so the correction cannot be "call everything
    uncertain and stop thinking"."""
    from app.core import credentials

    fake = _install(monkeypatch)
    fake.seed(credentials.SERVICE_NAME, credentials.USERNAME, OLD_KEY)
    fake._raise_before_set = 1

    result = credentials.set_stored_api_key_detailed(NEW_KEY)
    settle()

    assert result.ok is False
    assert result.provably_unchanged is True
    assert fake.value_at(credentials.SERVICE_NAME) == OLD_KEY


def test_a_set_that_mutates_then_raises_is_not_called_provably_unchanged(monkeypatch):
    """`set_password` writes and *then* fails. Nothing observed says the
    store is as it was, so nothing may claim it."""
    from app.core import credentials

    fake = _install(monkeypatch)
    fake.seed(credentials.SERVICE_NAME, credentials.USERNAME, OLD_KEY)
    fake._raise_after_set = 1

    result = credentials.set_stored_api_key_detailed(NEW_KEY)
    settle()

    assert result.ok is False
    assert result.provably_unchanged is False, (
        "a backend that mutated the store and then raised was reported as "
        "having changed nothing"
    )


def test_a_set_that_mutates_then_raises_is_actively_restored(monkeypatch):
    """Recording a desired value is not restoring one. A worker has to
    apply it, or the previous key never comes back."""
    from app.core import credentials

    fake = _install(monkeypatch)
    fake.seed(credentials.SERVICE_NAME, credentials.USERNAME, OLD_KEY)
    fake._raise_after_set = 1

    credentials.set_stored_api_key(NEW_KEY)
    settle()

    assert fake.value_at(credentials.SERVICE_NAME) == OLD_KEY, (
        "a half-applied replacement was never reconciled back to the previous key"
    )
    assert fake.holds_value_anywhere(NEW_KEY) is False


def test_a_set_that_fails_between_its_two_writes_is_cleaned_up(monkeypatch):
    """The pinned backend's real partial shape: the previous secret has been
    copied to the compound target and the replacement never landed."""
    from app.core import credentials

    fake = _install(monkeypatch)
    fake.seed(credentials.SERVICE_NAME, credentials.USERNAME, OLD_KEY)
    fake._raise_after_compound_copy = 1

    result = credentials.set_stored_api_key_detailed(NEW_KEY)
    settle()

    assert result.ok is False
    assert fake.value_at(credentials.SERVICE_NAME) == OLD_KEY
    assert fake.target_names() == [credentials.SERVICE_NAME], (
        f"a partly-applied write left a second copy behind: {fake.target_names()}"
    )


def test_a_delete_that_mutates_then_raises_is_not_called_provably_unchanged(monkeypatch):
    from app.core import credentials

    fake = _install(monkeypatch)
    fake.seed(credentials.SERVICE_NAME, credentials.USERNAME, OLD_KEY)
    fake.seed(_compound_of(credentials), credentials.USERNAME, OTHER_KEY)
    fake._raise_after_first_delete = 1

    result = credentials.clear_stored_api_key_detailed()
    settle()

    assert result.ok is False
    assert result.provably_unchanged is False, (
        "a removal that deleted one target and then raised was reported as "
        "having changed nothing"
    )


def test_a_delete_that_raises_before_deleting_is_provably_unchanged(monkeypatch):
    from app.core import credentials

    fake = _install(monkeypatch)
    fake.seed(credentials.SERVICE_NAME, credentials.USERNAME, OLD_KEY)
    fake._raise_before_delete = 1

    result = credentials.clear_stored_api_key_detailed()
    settle()

    assert result.ok is False
    assert result.provably_unchanged is True
    assert fake.value_at(credentials.SERVICE_NAME) == OLD_KEY


def test_a_half_finished_removal_does_not_leave_the_secret_readable(monkeypatch):
    """If the plain target went and the compound one did not, the secret is
    still there — and `get_password` resolves straight through to it."""
    from app.core import credentials

    fake = _install(monkeypatch)
    fake.seed(credentials.SERVICE_NAME, credentials.USERNAME, OLD_KEY)
    fake.seed(_compound_of(credentials), credentials.USERNAME, OTHER_KEY)
    fake._raise_after_first_delete = 1

    credentials.clear_stored_api_key_detailed()
    settle()

    assert credentials.get_stored_api_key() == "", (
        "a secret survived a removal that reported itself finished"
    )


def test_credential_pair_never_reports_consistent_after_an_unproven_write(monkeypatch):
    """The route's answer, not just the store's state: a save whose outcome
    was never established must not be described as leaving a matching pair."""
    from app.core import credentials
    from app.core.ai import credential_pair

    fake = _install(monkeypatch)
    fake.seed(credentials.SERVICE_NAME, credentials.USERNAME, OLD_KEY)
    fake._raise_after_set = 1

    outcome = credential_pair.save(NEW_KEY, "", "verified")
    settle()

    assert outcome.ok is False
    assert "nothing was changed" not in outcome.message.lower()


def test_neither_key_reaches_a_log_on_any_partial_path(monkeypatch):
    from app.core import credentials

    recorder = _Recorder()
    monkeypatch.setattr(credentials, "logger", recorder)
    fake = _install(monkeypatch)
    fake.seed(credentials.SERVICE_NAME, credentials.USERNAME, OLD_KEY)
    fake.seed(_compound_of(credentials), credentials.USERNAME, OTHER_KEY)
    fake._raise_after_compound_copy = 1

    credentials.set_stored_api_key(NEW_KEY)
    settle()

    rendered = recorder.rendered()
    for secret in (OLD_KEY, NEW_KEY, OTHER_KEY):
        assert secret not in rendered
