"""Tests for app/core/credentials.py. Never touches the real keyring
package's backend detection — a fake module is injected via sys.modules
instead, for two reasons: it keeps these tests deterministic regardless
of what credential backend (if any) happens to be available wherever
they run, and — concretely, not hypothetically — this project's own
Linux development sandbox has a broken cryptography/cffi native
extension that makes the real keyring.get_password() crash with a
pyo3_runtime.PanicException a plain try/except does not catch. Testing
against that would be testing an environment bug, not this module.
"""

import sys
import types

import pytest


class _FakeKeyring:
    def __init__(self):
        self._store = {}
        self.get_password_calls = []
        self.set_password_calls = []
        self.delete_password_calls = []

    def get_password(self, service, username):
        self.get_password_calls.append((service, username))
        return self._store.get((service, username))

    def set_password(self, service, username, value):
        self.set_password_calls.append((service, username, value))
        self._store[(service, username)] = value

    def delete_password(self, service, username):
        self.delete_password_calls.append((service, username))
        if (service, username) not in self._store:
            raise self.errors.PasswordDeleteError("not found")
        del self._store[(service, username)]


def _install_fake_keyring(monkeypatch, fake=None):
    fake = fake or _FakeKeyring()

    errors_module = types.ModuleType("keyring.errors")

    class PasswordDeleteError(Exception):
        pass

    class KeyringError(Exception):
        pass

    errors_module.PasswordDeleteError = PasswordDeleteError
    errors_module.KeyringError = KeyringError
    # Minimal single-purpose fakes (_Explodes, _Panics, _Hangs, ...) only
    # define the one method the test cares about — fall back to a no-op
    # for whichever of the three this fake doesn't implement.
    fake.errors = getattr(fake, "errors", errors_module)

    def _unused(*_args, **_kwargs):
        return None

    keyring_module = types.ModuleType("keyring")
    keyring_module.get_password = getattr(fake, "get_password", _unused)
    keyring_module.set_password = getattr(fake, "set_password", _unused)
    keyring_module.delete_password = getattr(fake, "delete_password", _unused)
    keyring_module.errors = errors_module

    monkeypatch.setitem(sys.modules, "keyring", keyring_module)
    monkeypatch.setitem(sys.modules, "keyring.errors", errors_module)
    return fake


@pytest.fixture(autouse=True)
def _reload_credentials_module():
    """credentials.py imports keyring lazily inside each function, so no
    module-reload is strictly required — this fixture just guarantees a
    clean sys.modules entry per test regardless of import order."""
    sys.modules.pop("keyring", None)
    sys.modules.pop("keyring.errors", None)
    yield
    sys.modules.pop("keyring", None)
    sys.modules.pop("keyring.errors", None)


# ---------------------------------------------------------------------------
# get_stored_api_key
# ---------------------------------------------------------------------------

def test_get_stored_api_key_returns_empty_when_nothing_stored(monkeypatch):
    from app.core import credentials
    _install_fake_keyring(monkeypatch)
    assert credentials.get_stored_api_key() == ""


def test_get_stored_api_key_returns_stored_value(monkeypatch):
    from app.core import credentials
    fake = _install_fake_keyring(monkeypatch)
    fake._store[(credentials.SERVICE_NAME, credentials.USERNAME)] = "sk-ant-real-key"
    assert credentials.get_stored_api_key() == "sk-ant-real-key"


def test_get_stored_api_key_empty_when_package_not_installed(monkeypatch):
    from app.core import credentials
    monkeypatch.setitem(sys.modules, "keyring", None)  # import keyring -> ImportError
    assert credentials.get_stored_api_key() == ""


def test_get_stored_api_key_empty_when_backend_raises(monkeypatch):
    from app.core import credentials

    class _Explodes:
        def get_password(self, service, username):
            raise RuntimeError("backend is broken")

    _install_fake_keyring(monkeypatch, _Explodes())
    assert credentials.get_stored_api_key() == ""


def test_get_stored_api_key_empty_on_uncatchable_style_exception(monkeypatch):
    """Reproduces the actual failure class found in this project's own
    sandbox: an exception that a same-thread `except Exception` would
    miss. BaseException (not Exception) stands in for it here."""
    from app.core import credentials

    class _NotAnException(BaseException):
        pass

    class _Panics:
        def get_password(self, service, username):
            raise _NotAnException("simulated pyo3-style panic")

    _install_fake_keyring(monkeypatch, _Panics())
    assert credentials.get_stored_api_key() == ""


def test_get_stored_api_key_empty_on_timeout(monkeypatch):
    from app.core import credentials
    import time

    monkeypatch.setattr(credentials, "TIMEOUT_SECONDS", 0.1)

    class _Hangs:
        def get_password(self, service, username):
            time.sleep(2)
            return "too-late"

    _install_fake_keyring(monkeypatch, _Hangs())
    assert credentials.get_stored_api_key() == ""


# ---------------------------------------------------------------------------
# set_stored_api_key
# ---------------------------------------------------------------------------

def test_set_stored_api_key_stores_and_roundtrips(monkeypatch):
    from app.core import credentials
    _install_fake_keyring(monkeypatch)
    assert credentials.set_stored_api_key("sk-ant-new-key") is True
    assert credentials.get_stored_api_key() == "sk-ant-new-key"


def test_set_stored_api_key_false_when_package_not_installed(monkeypatch):
    from app.core import credentials
    monkeypatch.setitem(sys.modules, "keyring", None)
    assert credentials.set_stored_api_key("sk-ant-x") is False


def test_set_stored_api_key_false_when_backend_raises(monkeypatch):
    from app.core import credentials

    class _Explodes:
        def set_password(self, service, username, value):
            raise RuntimeError("write failed")

    _install_fake_keyring(monkeypatch, _Explodes())
    assert credentials.set_stored_api_key("sk-ant-x") is False


# ---------------------------------------------------------------------------
# clear_stored_api_key
# ---------------------------------------------------------------------------

def test_clear_stored_api_key_removes_existing_value(monkeypatch):
    from app.core import credentials
    _install_fake_keyring(monkeypatch)
    credentials.set_stored_api_key("sk-ant-to-remove")

    assert credentials.clear_stored_api_key() is True
    assert credentials.get_stored_api_key() == ""


def test_clear_stored_api_key_true_when_already_absent(monkeypatch):
    from app.core import credentials
    _install_fake_keyring(monkeypatch)
    assert credentials.clear_stored_api_key() is True


def test_clear_stored_api_key_false_when_package_not_installed(monkeypatch):
    from app.core import credentials
    monkeypatch.setitem(sys.modules, "keyring", None)
    assert credentials.clear_stored_api_key() is False


# ---------------------------------------------------------------------------
# The uninstall ownership registry
# ---------------------------------------------------------------------------

def test_every_registered_credential_roundtrips_through_generic_cleanup(monkeypatch):
    """Adding a provider to the registry automatically exercises it here."""
    from app.core import credentials

    _install_fake_keyring(monkeypatch)
    for credential in credentials.OWNED_CREDENTIALS:
        assert credentials._set(credential.username, f"{credential.key}-secret")
        assert credentials.owned_credential_status(credential) == (True, True)
        assert credentials.clear_owned_credential(credential) is True
        assert credentials.owned_credential_status(credential) == (True, False)


def test_generic_cleanup_distinguishes_unreachable_store_from_absent_key(monkeypatch):
    from app.core import credentials

    monkeypatch.setitem(sys.modules, "keyring", None)
    for credential in credentials.OWNED_CREDENTIALS:
        assert credentials.owned_credential_status(credential) == (False, False)


# ---------------------------------------------------------------------------
# _run_isolated
# ---------------------------------------------------------------------------

def test_run_isolated_returns_success_and_result():
    from app.core import credentials
    ok, value = credentials._run_isolated(lambda: 42)
    assert (ok, value) == (True, 42)


def test_run_isolated_returns_false_on_exception():
    from app.core import credentials

    def _boom():
        raise ValueError("nope")

    ok, value = credentials._run_isolated(_boom)
    assert (ok, value) == (False, None)


def test_run_isolated_returns_false_on_base_exception():
    from app.core import credentials

    class _Weird(BaseException):
        pass

    def _boom():
        raise _Weird("not a normal Exception subclass")

    ok, value = credentials._run_isolated(_boom)
    assert (ok, value) == (False, None)
