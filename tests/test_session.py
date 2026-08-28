"""Tests for app/api/session.py — the CSRF/mutation session token store."""

import time

from app.api.session import SessionTokenStore


def test_current_mints_a_token_on_first_call():
    store = SessionTokenStore()
    token = store.current()
    assert token
    assert isinstance(token, str)
    assert len(token) > 20  # secrets.token_urlsafe(32) is well over 20 chars


def test_current_returns_the_same_token_on_repeat_calls():
    store = SessionTokenStore()
    a = store.current()
    b = store.current()
    assert a == b


def test_current_tokens_are_unpredictable_across_instances():
    a = SessionTokenStore().current()
    b = SessionTokenStore().current()
    assert a != b


def test_is_valid_accepts_the_current_token():
    store = SessionTokenStore()
    token = store.current()
    assert store.is_valid(token) is True


def test_is_valid_rejects_wrong_token():
    store = SessionTokenStore()
    store.current()
    assert store.is_valid("not-the-real-token") is False


def test_is_valid_rejects_missing_token():
    store = SessionTokenStore()
    store.current()
    assert store.is_valid(None) is False
    assert store.is_valid("") is False


def test_is_valid_false_before_any_token_minted():
    store = SessionTokenStore()
    assert store.is_valid("anything") is False


def test_token_expires_after_ttl():
    store = SessionTokenStore(ttl_seconds=0.05)
    token = store.current()
    assert store.is_valid(token) is True
    time.sleep(0.1)
    assert store.is_valid(token) is False


def test_current_mints_a_fresh_token_after_expiry():
    store = SessionTokenStore(ttl_seconds=0.05)
    first = store.current()
    time.sleep(0.1)
    second = store.current()
    assert first != second
    assert store.is_valid(second) is True
    assert store.is_valid(first) is False


def test_rotate_invalidates_the_previous_token_immediately():
    store = SessionTokenStore()
    old = store.current()
    new = store.rotate()
    assert old != new
    assert store.is_valid(old) is False
    assert store.is_valid(new) is True
