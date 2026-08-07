"""Tests for app/core/errors.py — the safe error envelope that is the
only permitted boundary between a raw exception and anything a client
(REST response, WebSocket event, rendered page) can see.
"""

import logging

import pytest

from app.core.errors import ErrorCategory, SafeError, classify_anthropic_exception, to_safe_error


# --- classify_anthropic_exception() ---

@pytest.mark.parametrize("type_name,expected", [
    ("AuthenticationError", ErrorCategory.PROVIDER_AUTH),
    ("PermissionDeniedError", ErrorCategory.PROVIDER_AUTH),
    ("RateLimitError", ErrorCategory.PROVIDER_RATE_LIMIT),
    ("APITimeoutError", ErrorCategory.PROVIDER_TIMEOUT),
    ("APIConnectionError", ErrorCategory.PROVIDER_UNAVAILABLE),
    ("SomethingElseEntirely", ErrorCategory.PROVIDER_ERROR),
])
def test_classify_anthropic_exception_by_type_name(type_name, expected):
    exc_type = type(type_name, (Exception,), {})
    exc = exc_type("irrelevant message content")
    assert classify_anthropic_exception(exc) == expected


def test_classify_never_inspects_the_exception_message():
    """Classification must be safe to call on an untrusted exception —
    it looks only at the type name, never the message, which is exactly
    the field that might carry sensitive detail."""
    exc = TimeoutError("sk-ant-should-never-be-read-for-classification")
    # Should not raise, and should classify purely from the type.
    assert classify_anthropic_exception(exc) == ErrorCategory.PROVIDER_TIMEOUT


# --- to_safe_error() ---

def test_to_safe_error_never_includes_raw_message():
    exc = RuntimeError("super secret sk-ant-abc123 at /home/user/private/file.txt")
    safe = to_safe_error(exc, category=ErrorCategory.INTERNAL_ERROR)
    dumped = safe.model_dump_json()
    assert "sk-ant-abc123" not in dumped
    assert "/home/user/private/file.txt" not in dumped
    assert "super secret" not in dumped


def test_to_safe_error_has_a_stable_safe_message_per_category():
    exc = RuntimeError("x")
    a = to_safe_error(exc, category=ErrorCategory.PROVIDER_TIMEOUT)
    b = to_safe_error(exc, category=ErrorCategory.PROVIDER_TIMEOUT)
    assert a.message == b.message  # same category -> same safe message
    assert a.correlation_id != b.correlation_id  # but a fresh id each time


def test_to_safe_error_correlation_id_is_unique_per_call():
    exc = RuntimeError("x")
    ids = {to_safe_error(exc).correlation_id for _ in range(20)}
    assert len(ids) == 20


def test_to_safe_error_logs_full_detail_server_side_only(caplog):
    """The real exception text IS expected in the server-side log —
    that's the point (a developer can find the real cause from a
    correlation_id) — it just must never appear in the returned
    SafeError itself."""
    exc = RuntimeError("this detail belongs only in the server log")
    with caplog.at_level(logging.ERROR):
        safe = to_safe_error(exc, category=ErrorCategory.INTERNAL_ERROR)

    assert "this detail belongs only in the server log" in caplog.text
    assert safe.correlation_id in caplog.text  # log line is findable by the id the client saw


def test_to_safe_error_default_category_is_internal_error():
    safe = to_safe_error(RuntimeError("x"))
    assert safe.category == ErrorCategory.INTERNAL_ERROR


def test_safe_error_every_category_has_a_message():
    for category in ErrorCategory:
        safe = to_safe_error(RuntimeError("x"), category=category)
        assert safe.message
        assert isinstance(safe.message, str)
