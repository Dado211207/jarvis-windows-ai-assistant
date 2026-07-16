"""Fail-closed audit for app/core/secret_store.py.

Confirms the specific properties a secure secret store must have, beyond
the happy-path round-trip already covered in tests/test_secret_store.py:
DPAPI is invoked in user-scope (not machine-scope), a DPAPI failure never
leaves a plaintext file behind, nothing is ever written except what the
DPAPI wrapper actually returned, and JARVIS_TEST_MODE/JARVIS_APPDATA_OVERRIDE
(the CI test-mode env vars) have zero effect on the encryption/availability
logic itself — they only ever redirect *where* paths.py points, never
*whether* or *how* encryption happens.

What this file cannot verify from a Linux container: that real Windows
DPAPI actually refuses to decrypt data encrypted by a different Windows
user account. That is a Windows OS-level guarantee this module relies on
(CryptProtectData with no CRYPTPROTECT_LOCAL_MACHINE flag is user-scoped by
definition) rather than something a cross-platform test suite can exercise
directly — see docs/SECURITY.md and the module docstring.
"""

import inspect
from unittest.mock import patch

import pytest

from app.core import secret_store


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_APPDATA_OVERRIDE", str(tmp_path))
    with patch("app.core.secret_store.paths.is_frozen", return_value=True):
        yield


# --- DPAPI invoked in user scope, not machine scope ---

def test_dpapi_protect_uses_no_local_machine_flag():
    """CRYPTPROTECT_LOCAL_MACHINE is 0x4 — passing flags=0 is what makes this
    user-scoped (tied to the calling Windows account), which is the whole
    basis for "another Windows account cannot decrypt this"."""
    captured = {}

    def fake_protect(blob, description, entropy, reserved, prompt_struct, flags):
        captured["flags"] = flags
        captured["entropy"] = entropy
        return b"fake-encrypted"

    fake_win32crypt = type("FakeWin32Crypt", (), {"CryptProtectData": staticmethod(fake_protect)})
    with patch.dict("sys.modules", {"win32crypt": fake_win32crypt}):
        secret_store._dpapi_protect(b"sk-ant-whatever")

    CRYPTPROTECT_LOCAL_MACHINE = 0x4
    assert captured["flags"] == 0
    assert (captured["flags"] & CRYPTPROTECT_LOCAL_MACHINE) == 0
    assert captured["entropy"]  # a fixed additional-entropy value is supplied


def test_dpapi_unprotect_passes_matching_entropy():
    captured = {}

    def fake_unprotect(blob, entropy, reserved, prompt_struct, flags):
        captured["entropy"] = entropy
        return (None, b"sk-ant-whatever")

    fake_win32crypt = type("FakeWin32Crypt", (), {"CryptUnprotectData": staticmethod(fake_unprotect)})
    with patch.dict("sys.modules", {"win32crypt": fake_win32crypt}):
        result = secret_store._dpapi_unprotect(b"fake-encrypted")

    assert result == b"sk-ant-whatever"
    assert captured["entropy"] == secret_store._ENTROPY


# --- a DPAPI failure never leaves plaintext on disk ---

def test_save_writes_no_file_at_all_when_dpapi_protect_fails():
    path = secret_store._secret_file_path()
    with patch("app.core.secret_store.is_available", return_value=True), \
         patch("app.core.secret_store._dpapi_protect", side_effect=RuntimeError("simulated DPAPI failure")):
        with pytest.raises(secret_store.SecretStoreError):
            secret_store.save_api_key("sk-ant-verysecretvalue000000")
    assert not path.exists()


def test_save_writes_no_file_when_secure_storage_unavailable():
    path = secret_store._secret_file_path()
    with patch("app.core.secret_store.is_available", return_value=False):
        with pytest.raises(secret_store.SecretStoreError):
            secret_store.save_api_key("sk-ant-verysecretvalue000000")
    assert not path.exists()


def test_only_the_dpapi_wrapper_output_is_ever_written(tmp_path):
    """save_api_key must never write api_key.encode() directly — only
    whatever _dpapi_protect() returned."""
    sentinel = b"totally-opaque-ciphertext-marker"
    with patch("app.core.secret_store.is_available", return_value=True), \
         patch("app.core.secret_store._dpapi_protect", return_value=sentinel):
        secret_store.save_api_key("sk-ant-verysecretvalue000000")

    written = secret_store._secret_file_path().read_bytes()
    assert written == sentinel
    assert b"verysecretvalue000000" not in written


# --- test-mode env vars never weaken the encryption/availability logic ---

def test_test_mode_env_var_does_not_affect_availability(monkeypatch):
    monkeypatch.setenv("JARVIS_TEST_MODE", "1")
    with patch("app.core.secret_store._is_windows", return_value=False):
        assert secret_store.is_available() is False  # unaffected by test mode


def test_test_mode_env_var_does_not_bypass_dpapi_requirement(monkeypatch):
    monkeypatch.setenv("JARVIS_TEST_MODE", "1")
    path = secret_store._secret_file_path()
    with patch("app.core.secret_store.is_available", return_value=False):
        with pytest.raises(secret_store.SecretStoreError):
            secret_store.save_api_key("sk-ant-verysecretvalue000000")
    assert not path.exists()


def test_appdata_override_only_redirects_location_not_encryption(tmp_path, monkeypatch):
    """JARVIS_APPDATA_OVERRIDE must change *where* the file lands, never
    *whether* it's encrypted."""
    other_dir = tmp_path / "other-appdata-root"
    monkeypatch.setenv("JARVIS_APPDATA_OVERRIDE", str(other_dir))
    sentinel = b"still-opaque-ciphertext"
    with patch("app.core.secret_store.is_available", return_value=True), \
         patch("app.core.secret_store._dpapi_protect", return_value=sentinel) as mock_protect:
        secret_store.save_api_key("sk-ant-verysecretvalue000000")

    mock_protect.assert_called_once()  # encryption still happened
    path = secret_store._secret_file_path()
    assert str(other_dir) in str(path)  # location was redirected
    assert path.read_bytes() == sentinel  # content is still the encrypted form


# --- signature sanity: confirms the fakes above match the real call shape ---

def test_dpapi_protect_signature_matches_win32crypt_expectations():
    sig = inspect.signature(secret_store._dpapi_protect)
    assert list(sig.parameters) == ["plaintext"]


def test_dpapi_unprotect_signature_matches_win32crypt_expectations():
    sig = inspect.signature(secret_store._dpapi_unprotect)
    assert list(sig.parameters) == ["ciphertext"]
