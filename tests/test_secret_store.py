"""Tests for the secure API-key storage abstraction (app/core/secret_store.py).

Runs on Linux CI (no pywin32, no real DPAPI) by mocking the two internal
DPAPI wrapper functions and the availability check. The "unavailable"
behaviour is exercised directly against the real, unmocked module — on
Linux that IS the true is_available() outcome, so those tests double as a
correctness check on the non-Windows fallback path.
"""

from unittest.mock import patch

import pytest

from app.core import secret_store


@pytest.fixture(autouse=True)
def _isolated_config_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_APPDATA_OVERRIDE", str(tmp_path))
    with patch("app.core.secret_store.paths.is_frozen", return_value=True):
        yield


# --- shape / masking helpers, no storage involved ---

def test_looks_like_anthropic_key_accepts_valid_shape():
    assert secret_store.looks_like_anthropic_key("sk-ant-abcdefghijklmnop") is True


@pytest.mark.parametrize("bad", ["", "not-a-key", "sk-ant-short", "sk-openai-abcdefghijklmnop"])
def test_looks_like_anthropic_key_rejects_bad_shape(bad):
    assert secret_store.looks_like_anthropic_key(bad) is False


def test_mask_api_key_never_returns_full_key():
    key = "sk-ant-abcdefghijklmnopqrstuvwxyz"
    masked = secret_store.mask_api_key(key)
    assert key not in masked
    assert masked.startswith("sk-ant")
    assert masked.endswith(key[-4:])


def test_mask_api_key_short_string_fully_masked():
    assert secret_store.mask_api_key("short") == "*****"


def test_mask_api_key_empty_string():
    assert secret_store.mask_api_key("") == ""


# --- availability on this (non-Windows) platform ---

def test_is_available_false_on_non_windows():
    with patch("app.core.secret_store._is_windows", return_value=False):
        assert secret_store.is_available() is False


def test_backend_name_reports_unavailable_on_non_windows():
    with patch("app.core.secret_store._is_windows", return_value=False):
        assert "unavailable" in secret_store.backend_name()


def test_save_raises_when_unavailable():
    with patch("app.core.secret_store._is_windows", return_value=False):
        with pytest.raises(secret_store.SecretStoreError):
            secret_store.save_api_key("sk-ant-abcdefghijklmnop")


def test_save_rejects_empty_key():
    with pytest.raises(secret_store.SecretStoreError):
        secret_store.save_api_key("")
    with pytest.raises(secret_store.SecretStoreError):
        secret_store.save_api_key("   ")


def test_load_returns_none_when_nothing_stored():
    assert secret_store.load_api_key() is None


def test_has_api_key_false_when_nothing_stored():
    assert secret_store.has_api_key() is False


def test_delete_is_idempotent_when_nothing_stored():
    secret_store.delete_api_key()  # must not raise


# --- full round trip, DPAPI mocked out ---

def _fake_protect(plaintext, *_args, **_kwargs):
    return b"ENCRYPTED:" + plaintext


def _fake_unprotect(ciphertext, *_args, **_kwargs):
    assert ciphertext.startswith(b"ENCRYPTED:")
    return (None, ciphertext[len(b"ENCRYPTED:"):])


@pytest.fixture
def _mock_dpapi():
    with patch("app.core.secret_store.is_available", return_value=True), \
         patch("app.core.secret_store._dpapi_protect", side_effect=_fake_protect), \
         patch("app.core.secret_store._dpapi_unprotect", side_effect=lambda c: _fake_unprotect(c)[1]):
        yield


def test_save_then_load_round_trip(_mock_dpapi):
    secret_store.save_api_key("sk-ant-abcdefghijklmnop")
    assert secret_store.has_api_key() is True
    assert secret_store.load_api_key() == "sk-ant-abcdefghijklmnop"


def test_save_strips_whitespace(_mock_dpapi):
    secret_store.save_api_key("  sk-ant-abcdefghijklmnop  ")
    assert secret_store.load_api_key() == "sk-ant-abcdefghijklmnop"


def test_delete_removes_stored_key(_mock_dpapi):
    secret_store.save_api_key("sk-ant-abcdefghijklmnop")
    secret_store.delete_api_key()
    assert secret_store.has_api_key() is False
    assert secret_store.load_api_key() is None


def test_load_returns_none_on_corrupted_blob(_mock_dpapi, tmp_path):
    secret_store.save_api_key("sk-ant-abcdefghijklmnop")
    path = secret_store._secret_file_path()
    path.write_bytes(b"not-a-real-dpapi-blob")
    with patch("app.core.secret_store._dpapi_unprotect", side_effect=Exception("bad blob")):
        assert secret_store.load_api_key() is None


def test_save_wraps_unexpected_backend_errors(_mock_dpapi):
    with patch("app.core.secret_store._dpapi_protect", side_effect=RuntimeError("boom")):
        with pytest.raises(secret_store.SecretStoreError):
            secret_store.save_api_key("sk-ant-abcdefghijklmnop")


def test_backend_name_windows_dpapi_when_available(_mock_dpapi):
    with patch("app.core.secret_store._is_windows", return_value=True):
        assert secret_store.backend_name() == "windows-dpapi"
