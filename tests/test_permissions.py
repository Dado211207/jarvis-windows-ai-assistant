"""Tests for the permission system."""

import pytest
from app.core.models import PermissionLevel
from app.core.permissions import (
    ApprovalRequiredError,
    PermissionDeniedError,
    check_permission,
)


def test_safe_permission_passes():
    check_permission("system_status", PermissionLevel.SAFE)  # must not raise


def test_blocked_permission_raises():
    with pytest.raises(PermissionDeniedError):
        check_permission("some_tool", PermissionLevel.BLOCKED)


def test_blocked_keyword_raises():
    with pytest.raises(PermissionDeniedError):
        check_permission("steal_password", PermissionLevel.SAFE)


def test_approval_required_raises():
    with pytest.raises(ApprovalRequiredError):
        check_permission("delete_files", PermissionLevel.APPROVAL_REQUIRED)


def test_blocked_message_is_descriptive():
    with pytest.raises(PermissionDeniedError) as exc:
        check_permission("read_browser_passwords", PermissionLevel.BLOCKED)
    assert "blocked" in str(exc.value).lower() or "not permitted" in str(exc.value).lower()


def test_approval_message_mentions_approval():
    with pytest.raises(ApprovalRequiredError) as exc:
        check_permission("send_email", PermissionLevel.APPROVAL_REQUIRED)
    assert "approval" in str(exc.value).lower()


def test_all_safe_tools_pass():
    safe_tools = ["system_status", "take_screenshot", "add_memory", "search_memory", "help"]
    for tool in safe_tools:
        check_permission(tool, PermissionLevel.SAFE)  # must not raise


@pytest.mark.parametrize("blocked_name", [
    "steal_password",
    "read_browser_passwords",
    "bypass_login",
    "keylogger",
    "screen_spy",
])
def test_hardcoded_blocked_keywords(blocked_name: str):
    with pytest.raises(PermissionDeniedError):
        check_permission(blocked_name, PermissionLevel.SAFE)
