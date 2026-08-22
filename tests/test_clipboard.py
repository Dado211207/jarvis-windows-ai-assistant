"""Tests for app/desktop/clipboard.py — in particular, the security
property its docstring promises: clipboard content is returned to the
approving caller (by design) but never appears in a log line or the
result *message* (which is what ends up in the action_lifecycle audit
record and WebSocket events — see app/core/router.py and
app/api/actions.py, both of which only ever pass result["message"], never
result["data"], to those two sinks).

tkinter is not installed in this environment, so the success path is
exercised by injecting a fake tkinter module into sys.modules rather than
skipping — that's the path that matters most for this file's own safety
claim, so it should not go untested just because the real dependency is
absent here.
"""

import logging
import sys
import types
from unittest.mock import MagicMock, patch

from app.desktop.clipboard import read_clipboard


def _install_fake_tkinter(clipboard_content: str = None, raise_tcl_error: bool = False):
    fake_root = MagicMock()
    if raise_tcl_error:
        fake_root.clipboard_get.side_effect = Exception("simulated TclError")
    else:
        fake_root.clipboard_get.return_value = clipboard_content

    fake_module = types.ModuleType("tkinter")
    fake_module.Tk = MagicMock(return_value=fake_root)
    fake_module.TclError = Exception if raise_tcl_error else type("TclError", (Exception,), {})
    return fake_module, fake_root


def test_read_clipboard_returns_content_in_data():
    fake_tkinter, _ = _install_fake_tkinter("hello clipboard")
    with patch.dict(sys.modules, {"tkinter": fake_tkinter}):
        result = read_clipboard()
    assert result["success"] is True
    assert result["data"]["content"] == "hello clipboard"


def test_read_clipboard_content_never_appears_in_message():
    secret = "super-secret-clipboard-content-xyz123"
    fake_tkinter, _ = _install_fake_tkinter(secret)
    with patch.dict(sys.modules, {"tkinter": fake_tkinter}):
        result = read_clipboard()
    assert secret not in result["message"]
    assert "character" in result["message"]  # only a count is reported


def test_read_clipboard_content_never_appears_in_logs(caplog):
    secret = "super-secret-clipboard-content-xyz123"
    fake_tkinter, _ = _install_fake_tkinter(secret)
    with patch.dict(sys.modules, {"tkinter": fake_tkinter}):
        with caplog.at_level(logging.DEBUG):
            read_clipboard()
    assert secret not in caplog.text


def test_read_clipboard_handles_missing_tkinter_gracefully():
    with patch.dict(sys.modules, {"tkinter": None}):
        result = read_clipboard()
    assert result["success"] is False
    assert "unavailable" in result["message"].lower()


def test_read_clipboard_handles_empty_or_non_text_clipboard():
    fake_tkinter, fake_root = _install_fake_tkinter()
    fake_tkinter.TclError = type("TclError", (Exception,), {})
    fake_root.clipboard_get.side_effect = fake_tkinter.TclError("clipboard empty")
    with patch.dict(sys.modules, {"tkinter": fake_tkinter}):
        result = read_clipboard()
    assert result["success"] is True
    assert result["data"]["content"] == ""


def test_read_clipboard_never_raises_on_unexpected_error():
    fake_tkinter, fake_root = _install_fake_tkinter()
    fake_root.clipboard_get.side_effect = RuntimeError("something unexpected")
    with patch.dict(sys.modules, {"tkinter": fake_tkinter}):
        result = read_clipboard()  # must not raise
    assert result["success"] is False


def test_read_clipboard_tool_is_registered_approval_required_sensitive():
    from app.core.brain import brain
    from app.core.models import PermissionLevel, RiskLevel
    from app.core.tool_registry import registry

    brain.initialise()
    tool = registry.get("read_clipboard")
    assert tool is not None
    assert tool.definition.permission_level == PermissionLevel.APPROVAL_REQUIRED
    assert tool.definition.risk == RiskLevel.SENSITIVE
