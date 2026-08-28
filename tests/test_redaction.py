"""Tests for app/core/redaction.py."""

from app.core.redaction import redact_params, redact_value


def test_redact_value_masks_password_like_keys():
    assert redact_value("password", "hunter2") == "***redacted***"
    assert redact_value("api_key", "sk-abc123") == "***redacted***"
    assert redact_value("user_token", "xyz") == "***redacted***"


def test_redact_value_leaves_normal_values_alone():
    assert redact_value("app_name", "notepad") == "notepad"


def test_redact_value_truncates_long_strings():
    long_value = "x" * 500
    result = redact_value("note", long_value)
    assert len(result) < len(long_value)
    assert result.endswith("(truncated)")


def test_redact_value_passes_through_non_strings():
    assert redact_value("count", 5) == 5
    assert redact_value("enabled", True) is True


def test_redact_params_applies_to_every_key():
    params = {"app_name": "notepad", "password": "hunter2"}
    result = redact_params(params)
    assert result["app_name"] == "notepad"
    assert result["password"] == "***redacted***"


def test_redact_params_does_not_mutate_the_original():
    params = {"password": "hunter2"}
    redact_params(params)
    assert params["password"] == "hunter2"


def test_redact_params_empty_dict():
    assert redact_params({}) == {}


def test_redact_value_clipboard_key_is_masked():
    """read_clipboard's own output must never appear un-redacted in an
    audit record."""
    assert redact_value("clipboard_content", "some secret text") == "***redacted***"
