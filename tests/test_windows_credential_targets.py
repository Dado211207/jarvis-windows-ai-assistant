"""Does the *pinned* Windows keyring backend really keep a second copy of
the previous secret? Asked of the real Windows Credential Manager.

`requirements-windows.txt` pins `keyring==25.7.0`. That version's
`WinVaultKeyring.set_password()` (keyring/backends/Windows.py) does this
when a credential already exists under the service target:

    def set_password(self, service, username, password):
        existing_pw = self._read_credential(service)
        if existing_pw:
            # resave the existing password using a compound target
            existing_username = existing_pw['UserName']
            target = self._compound_name(existing_username, service)
            self._set_password(target, existing_username, existing_pw.value)
        self._set_password(service, username, str(password))

Note what the code does and its own docstring does not: it copies the
existing credential to `{username}@{service}` **unconditionally**, not only
on a username collision. So replacing one logical secret writes the
previous secret to a second target and leaves it there.

For JARVIS that means `SERVICE_NAME="JARVIS"` plus
`USERNAME="anthropic_api_key"` yields two real Credential Manager entries
after any key replacement — `JARVIS` and `anthropic_api_key@JARVIS` — which
is exactly what the owner observed on a real machine with
`cmdkey /list | findstr /i "JARVIS"`. Reading never reveals it:
`_resolve_credential()` returns the plain target first, so the stale copy is
invisible to the application while remaining on disk.

**This file proves the premise on a real Windows Credential Manager**, so
the correction in app/core/credentials.py rests on observed behaviour of the
pinned version rather than on a fake that agrees with our reading of it.
tests/test_credential_backend_targets.py then models the same semantics
deterministically everywhere else.

Where it runs: only where a real Windows credential store *and* the pinned
backend are both present — in practice the Windows Installer job, which
installs requirements-windows.txt. Skipped, with the reason, everywhere
else; `requirements.txt` deliberately does not carry keyring.

**Safety, because this writes to the real credential store of whatever
machine runs it:**

  * a UUID-suffixed service name that cannot collide with anything, and
    deliberately containing neither "JARVIS" nor "anthropic" — so it can
    never be mistaken for a production target in a `cmdkey /list`
  * a disposable username, never `anthropic_api_key`
  * obviously-labelled dummy values, never printed, never logged, and never
    asserted on by value: every assertion below is a Boolean or a
    target-exists check
  * both targets removed in a `finally`, and the removal **verified** by
    reading both back — the test fails if it cannot confirm its own cleanup
  * nothing here touches, reads or enumerates any other credential
"""

import sys
import uuid

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="writes to a real Windows Credential Manager; nothing to probe elsewhere",
)


def _win_vault():
    """The pinned backend, or a skip explaining exactly what is missing."""
    try:
        from keyring.backends import Windows as windows_backend
    except ImportError:  # pragma: no cover - depends on the runner
        pytest.skip("keyring is not installed (it is in requirements-windows.txt only)")
    if not windows_backend.WinVaultKeyring.viable:  # pragma: no cover
        pytest.skip("WinVaultKeyring is not viable here (pywin32 / win32ctypes missing)")
    return windows_backend.WinVaultKeyring()


class _Disposable:
    """One throwaway credential identity, and the promise to clean it up."""

    def __init__(self):
        token = uuid.uuid4().hex
        # Deliberately not "JARVIS" and not "anthropic": a listing filtered
        # for either must never show anything this test created.
        self.service = f"kr-selftest-{token}"
        self.username = f"selftest-user-{token}"
        self.compound = f"{self.username}@{self.service}"
        # Distinct, obviously fake, and never printed or compared to a
        # literal in an assertion message.
        self.first = f"FIRST-DUMMY-VALUE-{token}"
        self.second = f"SECOND-DUMMY-VALUE-{token}"

    def targets(self):
        return (self.service, self.compound)


def _read_target(vault, target, username):
    """Read one exact target name, or None.

    Goes through the backend's public `get_password` with the *target* as
    the service, so nothing here reaches into private helpers. A non-None
    result also proves the stored username matches: `_resolve_credential`
    falls through to a doubly-compound name when it does not, and that name
    is never written.
    """
    return vault.get_password(target, username)


def _delete_target(vault, target, username):
    from keyring.errors import PasswordDeleteError

    try:
        vault.delete_password(target, username)
    except PasswordDeleteError:
        pass  # already absent is the state we want


def test_the_pinned_backend_keeps_the_previous_secret_in_a_compound_target():
    """The premise, on the real store: replacing one logical secret leaves
    two targets, the second holding the value that was replaced."""
    vault = _win_vault()
    disposable = _Disposable()
    observations = {}

    try:
        # 1. A first save, on a service target nothing has used before.
        vault.set_password(disposable.service, disposable.username, disposable.first)
        observations["compound_after_first_save"] = (
            _read_target(vault, disposable.compound, disposable.username) is not None
        )

        # 2. Replace it. This is the ordinary "the user pasted a new key" path.
        vault.set_password(disposable.service, disposable.username, disposable.second)

        plain_now = _read_target(vault, disposable.service, disposable.username)
        compound_now = _read_target(vault, disposable.compound, disposable.username)

        observations["plain_holds_replacement"] = plain_now == disposable.second
        observations["compound_exists_after_replacement"] = compound_now is not None
        observations["compound_holds_previous_secret"] = compound_now == disposable.first
    finally:
        for target in disposable.targets():
            _delete_target(vault, target, disposable.username)
        # Cleanup is verified, not assumed. A test that writes to the real
        # credential store and cannot prove it tidied up has to fail.
        leftovers = [
            target for target in disposable.targets()
            if _read_target(vault, target, disposable.username) is not None
        ]
        assert leftovers == [], (
            "this test could not confirm removal of the disposable targets it created"
        )

    assert observations["compound_after_first_save"] is False, (
        "a first save should touch exactly one target"
    )
    assert observations["plain_holds_replacement"] is True
    assert observations["compound_exists_after_replacement"] is True, (
        "the pinned backend no longer creates a compound target — the design in "
        "app/core/credentials.py assumes it does; re-read keyring/backends/Windows.py"
    )
    assert observations["compound_holds_previous_secret"] is True, (
        "the compound target exists but does not hold the replaced value"
    )


def test_jarvis_leaves_no_second_copy_after_a_replacement():
    """The correction, on the real store.

    `app/core/credentials.py` must end a replacement with exactly one target
    holding exactly the new value — no hidden copy of the key it replaced.
    The module's two identity constants are redirected to disposable names
    for the duration, so this exercises the real code path against the real
    backend without ever naming a production target.
    """
    from app.core import credentials

    vault = _win_vault()
    disposable = _Disposable()
    observations = {}

    original_service = credentials.SERVICE_NAME
    original_username = credentials.USERNAME
    credentials.SERVICE_NAME = disposable.service
    credentials.USERNAME = disposable.username
    try:
        assert credentials.set_stored_api_key(disposable.first) is True
        assert credentials.set_stored_api_key(disposable.second) is True
        credentials.wait_for_pending_mutations()

        observations["reads_back_the_replacement"] = (
            credentials.get_stored_api_key() == disposable.second
        )
        observations["no_compound_target"] = (
            _read_target(vault, disposable.compound, disposable.username) is None
        )
    finally:
        credentials.SERVICE_NAME = original_service
        credentials.USERNAME = original_username
        for target in disposable.targets():
            _delete_target(vault, target, disposable.username)
        leftovers = [
            target for target in disposable.targets()
            if _read_target(vault, target, disposable.username) is not None
        ]
        assert leftovers == [], (
            "this test could not confirm removal of the disposable targets it created"
        )

    assert observations["reads_back_the_replacement"] is True
    assert observations["no_compound_target"] is True, (
        "a replacement left the previous secret in a second Credential Manager target"
    )


def test_jarvis_removal_leaves_no_target_behind():
    """Removal must leave the store with neither target, even when the
    compound one was created by an earlier replacement."""
    from app.core import credentials

    vault = _win_vault()
    disposable = _Disposable()
    observations = {}

    original_service = credentials.SERVICE_NAME
    original_username = credentials.USERNAME
    credentials.SERVICE_NAME = disposable.service
    credentials.USERNAME = disposable.username
    try:
        # Create the residue the way the backend does, then remove through
        # JARVIS and require both targets to be gone.
        vault.set_password(disposable.service, disposable.username, disposable.first)
        vault.set_password(disposable.service, disposable.username, disposable.second)
        observations["residue_present_before_removal"] = (
            _read_target(vault, disposable.compound, disposable.username) is not None
        )

        assert credentials.clear_stored_api_key() is True
        credentials.wait_for_pending_mutations()

        observations["plain_gone"] = (
            _read_target(vault, disposable.service, disposable.username) is None
        )
        observations["compound_gone"] = (
            _read_target(vault, disposable.compound, disposable.username) is None
        )
    finally:
        credentials.SERVICE_NAME = original_service
        credentials.USERNAME = original_username
        for target in disposable.targets():
            _delete_target(vault, target, disposable.username)
        leftovers = [
            target for target in disposable.targets()
            if _read_target(vault, target, disposable.username) is not None
        ]
        assert leftovers == [], (
            "this test could not confirm removal of the disposable targets it created"
        )

    assert observations["residue_present_before_removal"] is True
    assert observations["plain_gone"] is True
    assert observations["compound_gone"] is True, (
        "removal left the previous secret in the compound target"
    )
