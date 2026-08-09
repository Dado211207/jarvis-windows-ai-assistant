"""Tests for the Phase 7 Windows actions: reading notes back, the local
clock, a memory snapshot, and locking the screen.

The notes tests matter most. `read_note` takes a name straight from
whatever the user typed, so the containment check is the whole safety
story — and it is tested against a real temporary directory with real
files and real symlinks, not a mocked Path, because a mock would happily
agree with a broken implementation.
"""

import platform
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.desktop import notes as notes_module
from app.desktop import session as session_module
from app.desktop import system as system_module


@pytest.fixture
def notes_dir(tmp_path, monkeypatch):
    """A real notes folder on disk. Nothing here touches the developer's
    own ~/Documents/JARVIS_Notes."""
    directory = tmp_path / "JARVIS_Notes"
    directory.mkdir()
    monkeypatch.setattr(notes_module, "NOTES_DIR", directory)
    return directory


# ---------------------------------------------------------------------------
# list_notes
# ---------------------------------------------------------------------------

def test_no_notes_yet_says_how_to_make_one(notes_dir):
    result = notes_module.list_notes()

    assert result["success"] is True
    assert result["data"]["notes"] == []
    assert "create note" in result["message"]


def test_a_missing_notes_folder_is_not_an_error(tmp_path, monkeypatch):
    """Nobody has made a note yet — that is the normal first-run state."""
    monkeypatch.setattr(notes_module, "NOTES_DIR", tmp_path / "never-created")

    result = notes_module.list_notes()

    assert result["success"] is True
    assert result["data"]["notes"] == []


def test_notes_are_listed_newest_first(notes_dir):
    import os
    import time

    for index, name in enumerate(["oldest.txt", "middle.txt", "newest.txt"]):
        path = notes_dir / name
        path.write_text("note body", encoding="utf-8")
        os.utime(path, (time.time() + index, time.time() + index))

    listed = [n["filename"] for n in notes_module.list_notes()["data"]["notes"]]

    assert listed == ["newest.txt", "middle.txt", "oldest.txt"]


def test_a_long_list_is_capped_and_says_so(notes_dir):
    for index in range(notes_module.MAX_NOTES_LISTED + 10):
        (notes_dir / f"note_{index:03d}.txt").write_text("x", encoding="utf-8")

    result = notes_module.list_notes()

    assert len(result["data"]["notes"]) == notes_module.MAX_NOTES_LISTED
    assert result["data"]["total"] == notes_module.MAX_NOTES_LISTED + 10
    assert "most recent" in result["message"]


def test_only_text_notes_are_listed(notes_dir):
    (notes_dir / "a-note.txt").write_text("x", encoding="utf-8")
    (notes_dir / "something.exe").write_bytes(b"MZ")
    (notes_dir / "subfolder").mkdir()

    listed = [n["filename"] for n in notes_module.list_notes()["data"]["notes"]]

    assert listed == ["a-note.txt"]


# ---------------------------------------------------------------------------
# read_note — containment is the whole point
# ---------------------------------------------------------------------------

def test_a_note_can_be_read_back(notes_dir):
    (notes_dir / "shopping.txt").write_text("milk\nbread", encoding="utf-8")

    result = notes_module.read_note("shopping.txt")

    assert result["success"] is True
    assert result["data"]["content"] == "milk\nbread"


def test_a_note_written_by_create_note_can_be_read_back(notes_dir):
    """The round trip a user actually performs."""
    created = notes_module.create_note("remember the milk")

    result = notes_module.read_note(created["data"]["filename"])

    assert result["success"] is True
    assert "remember the milk" in result["data"]["content"]


@pytest.mark.parametrize("attempt", [
    "../../../etc/passwd",
    "..\\..\\Windows\\System32\\config\\SAM",
    "/etc/shadow",
    "C:\\Windows\\win.ini",
    "subdir/note.txt",
    "..",
    ".",
    "",
    "   ",
])
def test_a_path_is_refused_rather_than_repaired(notes_dir, attempt):
    """Refusing beats sanitising: quietly turning an escape attempt into
    a valid name hides that it happened."""
    result = notes_module.read_note(attempt)

    assert result["success"] is False
    assert "valid note name" in result["message"]


def test_a_symlink_out_of_the_folder_is_refused(notes_dir, tmp_path):
    """The check resolves the path, so a link planted inside the folder
    cannot be used to read something outside it."""
    secret = tmp_path / "secret.txt"
    secret.write_text("not yours", encoding="utf-8")
    link = notes_dir / "innocent.txt"
    try:
        link.symlink_to(secret)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available in this environment")

    result = notes_module.read_note("innocent.txt")

    assert result["success"] is False
    assert "not yours" not in result["message"]


def test_a_note_that_does_not_exist_says_how_to_find_one(notes_dir):
    result = notes_module.read_note("nope.txt")

    assert result["success"] is False
    assert "list notes" in result["message"]


def test_a_very_large_note_is_truncated_and_says_so(notes_dir):
    (notes_dir / "big.txt").write_text("x" * (notes_module.MAX_NOTE_READ_BYTES + 500), encoding="utf-8")

    result = notes_module.read_note("big.txt")

    assert result["success"] is True
    assert result["data"]["truncated"] is True
    assert len(result["data"]["content"]) == notes_module.MAX_NOTE_READ_BYTES
    assert "first" in result["message"]


def test_a_note_that_is_not_valid_text_still_reads(notes_dir):
    """A file someone dropped in the folder must not raise."""
    (notes_dir / "binary.txt").write_bytes(b"\xff\xfe\x00 not utf-8")

    assert notes_module.read_note("binary.txt")["success"] is True


# ---------------------------------------------------------------------------
# The clock
# ---------------------------------------------------------------------------

def test_the_time_comes_from_the_computer_not_the_model():
    """Without this route the question falls through to the AI, which
    does not know what time it is and will confidently guess."""
    from datetime import datetime

    result = system_module.get_current_time()

    assert result["success"] is True
    assert str(datetime.now().year) in result["data"]["date"]
    assert result["data"]["iso"]


def test_asking_the_time_never_reaches_the_ai():
    from app.core.router import find_route

    for phrasing in ["time", "what time is it", "what time is it?", "date", "today's date"]:
        match = find_route(phrasing)
        assert match is not None, f"{phrasing!r} would have gone to the AI"
        assert match[0].tool_name == "current_time"


# ---------------------------------------------------------------------------
# Top processes
# ---------------------------------------------------------------------------

def _fake_process(name, rss_mb):
    proc = MagicMock()
    proc.info = {"name": name, "memory_info": MagicMock(rss=rss_mb * 1024 ** 2)}
    return proc


def test_processes_are_ranked_by_memory():
    processes = [_fake_process("small.exe", 10), _fake_process("huge.exe", 900), _fake_process("mid.exe", 100)]

    with patch("psutil.process_iter", return_value=processes):
        result = system_module.get_top_processes()

    assert [p["name"] for p in result["data"]["processes"]] == ["huge.exe", "mid.exe", "small.exe"]


def test_the_process_list_is_capped():
    processes = [_fake_process(f"p{i}.exe", i) for i in range(50)]

    with patch("psutil.process_iter", return_value=processes):
        result = system_module.get_top_processes()

    assert len(result["data"]["processes"]) == system_module.TOP_PROCESS_COUNT


def test_a_process_that_exits_mid_scan_is_skipped_not_fatal():
    class _Vanished:
        @property
        def info(self):
            raise RuntimeError("process no longer exists")

    with patch("psutil.process_iter", return_value=[_Vanished(), _fake_process("alive.exe", 5)]):
        result = system_module.get_top_processes()

    assert result["success"] is True
    assert [p["name"] for p in result["data"]["processes"]] == ["alive.exe"]


def test_nothing_about_processes_is_persisted():
    """A snapshot on request, never monitoring — see CLAUDE.md's ban on
    surveillance tools."""
    db = MagicMock()
    with patch("psutil.process_iter", return_value=[_fake_process("a.exe", 1)]), \
         patch("db.database.get_db", return_value=db):
        system_module.get_top_processes()

    db.add_memory.assert_not_called()
    db.add_conversation.assert_not_called()


# ---------------------------------------------------------------------------
# Locking the screen
# ---------------------------------------------------------------------------

def test_locking_calls_the_windows_api_and_nothing_else():
    """No subprocess, no shell — the same call Win+L makes."""
    user32 = MagicMock()
    user32.LockWorkStation.return_value = 1
    windll = MagicMock(user32=user32)

    with patch("platform.system", return_value="Windows"), \
         patch.dict("sys.modules", {"ctypes": MagicMock(windll=windll)}), \
         patch("subprocess.Popen") as popen, \
         patch("subprocess.run") as run:
        result = session_module.lock_workstation()

    assert result["success"] is True
    user32.LockWorkStation.assert_called_once_with()
    popen.assert_not_called()
    run.assert_not_called()


def test_a_refused_lock_is_reported_not_assumed_to_have_worked():
    """Windows returns zero when it declines; treating that as success
    would tell someone their screen is locked when it is not."""
    user32 = MagicMock()
    user32.LockWorkStation.return_value = 0

    with patch("platform.system", return_value="Windows"), \
         patch.dict("sys.modules", {"ctypes": MagicMock(windll=MagicMock(user32=user32))}):
        result = session_module.lock_workstation()

    assert result["success"] is False
    assert "refused" in result["message"]


def test_locking_off_windows_says_so_rather_than_guessing():
    with patch("platform.system", return_value="Linux"):
        result = session_module.lock_workstation()

    assert result["success"] is False
    assert "only available on Windows" in result["message"]


def test_lock_is_the_only_session_action_that_exists():
    """Sign out, restart, sleep and shut down all end running programs
    and can lose unsaved work elsewhere. Lock cannot, which is why it is
    the only one here — asserted so a later addition is a decision, not
    a drift."""
    import inspect

    defined_here = [
        name for name, value in vars(session_module).items()
        if inspect.isfunction(value) and value.__module__ == session_module.__name__
    ]
    action_names = [name for name in defined_here if name != "register_tools"]

    assert action_names == ["lock_workstation"]


def test_locking_the_screen_is_not_hidden_behind_an_approval_prompt():
    """The worst case of an unwanted lock is typing a password. An
    approval prompt for it would only teach people to click through
    prompts."""
    from app.core.models import RiskLevel
    from app.core.policy import PolicyAction, evaluate
    from app.core.tool_registry import ToolRegistry

    registry = ToolRegistry()
    session_module.register_tools(registry)
    definition = registry.get("lock_workstation").definition

    assert definition.risk == RiskLevel.REVERSIBLE
    assert evaluate(definition.risk, "lock_workstation").action == PolicyAction.AUTO_EXECUTE


def test_the_lock_tool_declares_it_is_windows_only():
    from app.core.tool_registry import ToolRegistry

    registry = ToolRegistry()
    session_module.register_tools(registry)

    assert registry.get("lock_workstation").definition.platform == ["windows"]


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("phrasing,expected", [
    ("list notes", "list_notes"),
    ("show notes", "list_notes"),
    ("notes", "list_notes"),
    ("read note shopping.txt", "read_note"),
    ("open note shopping.txt", "read_note"),
    ("top processes", "top_processes"),
    ("what's using my memory?", "top_processes"),
    ("lock", "lock_workstation"),
    ("lock screen", "lock_workstation"),
    ("lock my computer", "lock_workstation"),
])
def test_phrasings_reach_the_right_tool(phrasing, expected):
    from app.core.router import find_route

    match = find_route(phrasing)
    assert match is not None, f"{phrasing!r} matched no route"
    assert match[0].tool_name == expected


def test_listing_notes_is_never_read_as_reading_a_note_called_notes():
    from app.core.router import find_route

    assert find_route("list notes")[0].tool_name == "list_notes"


def test_opening_the_notes_folder_still_opens_the_folder():
    """A Phase 6 route that the new note routes must not have shadowed."""
    from app.core.router import find_route

    assert find_route("open notes folder")[0].tool_name == "open_folder"


def test_every_new_tool_is_registered_and_reachable():
    from app.core.brain import brain
    from app.core.tool_registry import registry

    brain.initialise()
    for name in ("list_notes", "read_note", "current_time", "top_processes", "lock_workstation"):
        assert registry.get(name) is not None, f"{name} is not registered"


def test_no_new_tool_needs_approval_or_is_destructive():
    """Phase 7's whole premise: these are safe actions."""
    from app.core.brain import brain
    from app.core.models import PermissionLevel, RiskLevel
    from app.core.tool_registry import registry

    brain.initialise()
    for name in ("list_notes", "read_note", "current_time", "top_processes", "lock_workstation"):
        definition = registry.get(name).definition
        assert definition.permission_level == PermissionLevel.SAFE
        assert definition.risk in (RiskLevel.READ_ONLY, RiskLevel.REVERSIBLE)
