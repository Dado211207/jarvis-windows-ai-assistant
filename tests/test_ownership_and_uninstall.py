""""Remove everything JARVIS owns" is only a promise if there is a list.

Without one it degrades into whichever paths somebody happened to
remember, which is how an uninstalled application leaves a Startup
shortcut pointing at a deleted executable and an API key sitting in
Windows Credential Manager.

The list is app/core/ownership.py. These tests hold three things down:
that it is complete, that the removal does what each entry says, and —
the part that matters most — that the things which are deliberately not
ours are never touched. An uninstaller that removes a shared Windows
component because one of its users left is a bug in that uninstaller.

Nothing here deletes anything real: the data directory is a tmp_path and
the credential store is patched.
"""

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from app.core import ownership

REPO_ROOT = Path(__file__).resolve().parent.parent
ISS_PATH = REPO_ROOT / "packaging" / "jarvis.iss"


def _iss() -> str:
    return ISS_PATH.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# The manifest
# ---------------------------------------------------------------------------

def test_the_manifest_covers_both_halves():
    """The installer knows what it installed. Only the application knows
    what it created while running — and that is the half that used to be
    missed."""
    setup_keys = {item.key for item in ownership.INSTALLED_BY_SETUP}
    runtime_keys = {item.key for item in ownership.CREATED_AT_RUNTIME}

    assert {"install_dir", "start_menu", "uninstall_entry"} <= setup_keys
    assert {"runtime_startup_shortcut", "credential", "data_dir"} <= runtime_keys


def test_every_entry_says_what_it_is_and_where():
    for item in ownership.INSTALLED_BY_SETUP + ownership.CREATED_AT_RUNTIME:
        assert item.what.strip()
        assert item.where.strip()


def test_the_things_that_are_never_ours_are_named_with_a_reason():
    keys = {item.key for item in ownership.NEVER_REMOVED}

    assert {"webview2", "vcredist", "ollama", "notes"} <= keys
    for item in ownership.NEVER_REMOVED:
        assert item.why.strip(), f"{item.key} is excluded without saying why"


def test_data_and_credentials_are_never_removed_by_an_ordinary_uninstall():
    """Somebody reinstalling next week wants their settings back. The
    difference between "uninstall" and "uninstall and forget me" is a
    choice, never an inference."""
    for item in ownership.CREATED_AT_RUNTIME:
        if item.key in ("credential", "data_dir"):
            assert item.always is False


def test_the_sign_in_shortcut_goes_on_every_uninstall():
    """Not a preference: it points at an executable that is about to
    stop existing, so leaving it means Windows tries to launch a deleted
    file at every sign-in."""
    shortcut = next(i for i in ownership.CREATED_AT_RUNTIME if i.key == "runtime_startup_shortcut")

    assert shortcut.always is True


# ---------------------------------------------------------------------------
# What removal actually does
# ---------------------------------------------------------------------------

def test_an_ordinary_uninstall_keeps_the_data_and_the_key(tmp_path):
    data = tmp_path / "JARVIS"
    data.mkdir()
    (data / "jarvis.db").write_text("rows")

    with patch.object(ownership, "data_dir", return_value=data), \
         patch.object(ownership, "startup_shortcut_path", return_value=None), \
         patch("app.core.credentials.clear_stored_api_key") as clear:
        report = ownership.remove(purge_data=False)

    assert data.exists(), "an ordinary uninstall must not delete user data"
    clear.assert_not_called()
    assert any("settings" in line for line in report.kept)


def test_a_complete_uninstall_removes_the_data_and_the_key(tmp_path):
    data = tmp_path / "JARVIS"
    (data / "data").mkdir(parents=True)
    (data / "data" / "jarvis.db").write_text("rows")

    with patch.object(ownership, "data_dir", return_value=data), \
         patch.object(ownership, "startup_shortcut_path", return_value=None), \
         patch("app.core.credentials.get_stored_api_key", return_value="sk-ant-something"), \
         patch("app.core.credentials.clear_stored_api_key", return_value=True) as clear:
        report = ownership.remove(purge_data=True)

    assert not data.exists()
    clear.assert_called_once()
    assert any("API key" in line for line in report.removed)


def test_the_sign_in_shortcut_is_removed_either_way(tmp_path):
    shortcut = tmp_path / "JARVIS.lnk"
    shortcut.write_text("shortcut")

    with patch.object(ownership, "startup_shortcut_path", return_value=shortcut), \
         patch.object(ownership, "data_dir", return_value=tmp_path / "JARVIS"):
        report = ownership.remove(purge_data=False)

    assert not shortcut.exists()
    assert any("sign-in shortcut" in line for line in report.removed)


def test_it_refuses_to_delete_something_that_is_not_the_data_folder(tmp_path):
    """A guard, not a formality: this deletes a tree, and a data_dir()
    that resolved to a home directory because an environment variable
    was empty must stop rather than proceed."""
    not_ours = tmp_path / "Documents"
    not_ours.mkdir()
    (not_ours / "important.txt").write_text("a person's file")

    with patch.object(ownership, "data_dir", return_value=not_ours), \
         patch.object(ownership, "startup_shortcut_path", return_value=None), \
         patch("app.core.credentials.get_stored_api_key", return_value=""):
        report = ownership.remove(purge_data=True)

    assert (not_ours / "important.txt").exists()
    assert any("Refused" in line for line in report.failed)


def test_removal_never_raises(tmp_path):
    """An uninstaller cannot usefully fail — the files are going
    regardless — so the useful behaviour is to report accurately rather
    than abort and leave a half-removed installation."""
    with patch.object(ownership, "startup_shortcut_path", side_effect=OSError("boom")), \
         patch.object(ownership, "data_dir", side_effect=OSError("boom")), \
         patch("app.core.credentials.get_stored_api_key", side_effect=OSError("boom")):
        report = ownership.remove(purge_data=True)

    assert report.failed, "a failure must be reported, not swallowed"


@pytest.mark.parametrize("purge", [False, True])
def test_shared_components_and_ollama_are_kept_in_both_modes(tmp_path, purge):
    with patch.object(ownership, "data_dir", return_value=tmp_path / "JARVIS"), \
         patch.object(ownership, "startup_shortcut_path", return_value=None), \
         patch("app.core.credentials.get_stored_api_key", return_value=""):
        report = ownership.remove(purge_data=purge)

    kept = " ".join(report.kept)
    assert "WebView2" in kept
    assert "Visual C++" in kept
    assert "Ollama" in kept
    assert "Notes" in kept


def test_nothing_in_the_removal_path_touches_ollama():
    """Even when JARVIS installed it. Removing it would be deciding, on
    somebody's behalf, that they no longer want local AI because they no
    longer want JARVIS."""
    import inspect

    source = inspect.getsource(ownership)
    body = source.split("NEVER_REMOVED = (", 1)[1].split(")\n", 1)[1]

    assert "ollama" not in body.lower().replace("no business", "")


# ---------------------------------------------------------------------------
# The entry point the uninstaller calls
# ---------------------------------------------------------------------------

def test_it_is_reachable_as_a_subcommand_of_the_real_executable():
    entry = (REPO_ROOT / "run_jarvis.py").read_text(encoding="utf-8")

    assert "--uninstall-cleanup" in entry
    assert "uninstall" in entry


def test_running_it_reports_machine_readably_and_removes_nothing_by_default():
    completed = subprocess.run(
        [sys.executable, str(REPO_ROOT / "run_jarvis.py"), "--uninstall-cleanup"],
        capture_output=True, text=True, timeout=120,
    )

    assert completed.returncode == 0
    assert "UNINSTALL_JSON " in completed.stdout
    payload = json.loads(completed.stdout.split("UNINSTALL_JSON ", 1)[1].splitlines()[0])
    assert payload["purge_data"] is False
    assert any("WebView2" in line for line in payload["kept"])


def test_purging_is_never_the_default():
    """The flag is typed on purpose. An uninstaller that deletes data
    because nobody said not to is the wrong default."""
    import inspect

    from app.launcher import uninstall

    source = inspect.getsource(uninstall.run)

    assert '"--purge-data" in argv' in source


# ---------------------------------------------------------------------------
# The installer's half
# ---------------------------------------------------------------------------

def test_the_installer_asks_the_application_to_clean_up_after_itself():
    """Only the application knows how the API key was stored. An
    installer guessing at a Credential Manager target name is how an
    uninstall leaves a secret behind while reporting success."""
    content = _iss()

    assert "--uninstall-cleanup" in content
    assert "RunApplicationCleanup" in content


def test_the_cleanup_runs_while_the_executable_still_exists():
    """usUninstall, not usPostUninstall: at usPostUninstall the file it
    needs to run has already been deleted."""
    content = _iss()
    cleanup_call = content.split("procedure CurUninstallStepChanged", 1)[1]
    us_uninstall = cleanup_call.index("usUninstall")
    us_post = cleanup_call.index("usPostUninstall")

    assert us_uninstall < us_post
    assert cleanup_call.index("RunApplicationCleanup") > us_uninstall


def test_the_prompt_says_what_each_answer_means():
    content = _iss()

    assert "Remove everything JARVIS owns?" in content
    assert "Choosing No" in content
    assert "Choosing Yes" in content
    assert "MB_DEFBUTTON2" in content, "the destructive answer must not be the default"


def test_the_prompt_names_what_is_never_removed():
    content = _iss()

    assert "WebView2" in content
    assert "Visual C++" in content
    assert "Ollama" in content
    assert "JARVIS_Notes" in content


def test_a_silent_uninstall_still_preserves_data_without_the_flag():
    content = _iss()

    assert "{param:DELETEDATA|no}" in content


def test_the_installer_never_removes_a_shared_component():
    """Regression guard with teeth: no DelTree may name anything outside
    JARVIS's own data directory."""
    import re

    content = _iss()
    deletions = re.findall(r"DelTree\(([^,]+),", content)

    assert deletions, "expected the uninstaller to delete the data directory"
    for target in deletions:
        assert target.strip() == "DataDir", f"DelTree targets {target.strip()}"


# ---------------------------------------------------------------------------
# The four automated uninstall cases
# ---------------------------------------------------------------------------

def test_the_clean_install_script_covers_all_four_cases():
    script = (REPO_ROOT / "scripts" / "test_clean_install.py").read_text(encoding="utf-8")

    # 1. uninstall preserves data;  2. it still removes the sign-in shortcut;
    # 3. complete removal deletes data;  4. neither touches shared components.
    assert "Verify user data was PRESERVED by default" in script
    assert "Verify the sign-in shortcut was removed even so" in script
    assert "Verify user data was REMOVED" in script
    assert script.count("_webview2_present()") >= 3, (
        "both uninstall phases must check the shared runtime survived"
    )


def test_the_script_checks_the_startup_shortcut_at_the_documented_path():
    script = (REPO_ROOT / "scripts" / "test_clean_install.py").read_text(encoding="utf-8")

    assert "expected_startup_shortcut" in script
    assert '"Startup" / "JARVIS.lnk"' in script
