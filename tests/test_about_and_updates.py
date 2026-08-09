"""Tests for the About section: version, the manual update check, and
the bundled open-source notices.

The property worth defending here is a negative one. JARVIS makes no
network request of its own accord — the only outbound traffic in the
whole application is a chat message to a provider the user configured.
A version check is exactly the sort of feature that quietly adds a
background poll, so these tests assert that it does not: the button
opens a page, and nothing about this computer is sent anywhere.
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.conftest import prime_session

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def client():
    from app.api.server import app
    with TestClient(app) as test_client:
        yield prime_session(test_client)


def _js() -> str:
    return (REPO_ROOT / "app" / "ui" / "static" / "app.js").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Version
# ---------------------------------------------------------------------------

def test_about_reports_the_running_version(client):
    from app import __phase__, __version__

    body = client.get("/about").json()

    assert body["version"] == __version__
    assert body["build"] == __phase__


def test_about_says_whether_this_is_an_installed_build(client):
    """"Running from source" and "installed" behave differently enough
    that a bug report needs to say which one it was."""
    assert client.get("/about").json()["packaged"] is False


def test_about_never_contains_a_credential(client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(type(settings), "effective_api_key", property(lambda self: "sk-about-leak"))

    assert "sk-" not in client.get("/about").text


def test_the_version_shown_matches_what_is_shipped():
    """One version string, three files that cannot import each other —
    the installer and the Windows resource block both carry it as
    literal text, so drift is only caught by asserting it."""
    from app import __version__

    installer = (REPO_ROOT / "packaging" / "jarvis.iss").read_text(encoding="utf-8")
    version_info = (REPO_ROOT / "packaging" / "version_info.txt").read_text(encoding="utf-8")

    assert f'#define MyAppVersion "{__version__}"' in installer
    assert __version__ in version_info


# ---------------------------------------------------------------------------
# The update check does not check anything by itself
# ---------------------------------------------------------------------------

def test_there_is_no_automatic_update_check_anywhere():
    """Asserted over the whole app rather than one module: a background
    poll added later would break the local-first promise wherever it was
    put."""
    import ast

    offenders = []
    for path in (REPO_ROOT / "app").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                first = node.body[0] if node.body else None
                if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) \
                        and isinstance(first.value.value, str):
                    docstrings.add(id(first.value))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                    and id(node) not in docstrings and "api.github.com" in node.value:
                offenders.append(f"{path.name}:{node.lineno}")

    assert offenders == [], f"an update endpoint is contacted from {offenders}"


def test_the_update_button_opens_a_page_rather_than_polling():
    js = _js()
    check = js[js.index("async function checkForUpdates"):js.index("async function refreshNotices")]

    assert "open website" in check, "it goes through the existing safe URL opener"
    assert "api.github.com" not in check


def test_the_releases_url_is_https_and_points_at_this_project(client):
    url = client.get("/about").json()["releases_url"]

    assert url.startswith("https://github.com/Dado211207/jarvis-windows-ai-assistant")
    assert url.endswith("/releases")


def test_the_page_states_that_nothing_is_sent(client):
    body = client.get("/ui/diagnostics").text

    assert "never checks for updates on its own" in body
    assert "nothing about this computer is sent anywhere" in body


@pytest.mark.parametrize("element_id", ["about-version", "about-build", "about-check-updates", "about-update-message"])
def test_the_about_page_shows_the_version_controls(client, element_id):
    assert f'id="{element_id}"' in client.get("/ui/diagnostics").text


# ---------------------------------------------------------------------------
# Open-source notices
# ---------------------------------------------------------------------------

def test_the_notices_are_readable_from_inside_the_app(client):
    """They ship with the product; a licence file that cannot be read
    from the product is not really shipped — the packaged app has no
    file manager pointed at its install directory."""
    body = client.get("/about/notices").json()

    assert body["available"] is True
    assert len(body["text"]) > 200


def test_the_notices_endpoint_serves_the_real_file(client):
    on_disk = (REPO_ROOT / "docs" / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")

    assert client.get("/about/notices").json()["text"] == on_disk


def test_missing_notices_are_reported_rather_than_faked(client, monkeypatch):
    """An empty licence section is a compliance problem to notice, not
    something to paper over with placeholder text."""
    monkeypatch.setattr("app.api.routes._notices_path", lambda: None)

    body = client.get("/about/notices").json()

    assert body["available"] is False
    assert body["text"] == ""


def test_an_unreadable_notices_file_does_not_break_the_page(client, monkeypatch, tmp_path):
    monkeypatch.setattr("app.api.routes._notices_path", lambda: tmp_path / "does-not-exist.md")

    assert client.get("/about/notices").json()["available"] is False


def test_the_notices_are_rendered_as_text_not_markup():
    js = _js()
    notices = js[js.index("async function refreshNotices"):js.index("function initDiagnostics")]

    assert "textContent" in notices
    assert "innerHTML" not in notices


def test_the_notices_section_is_on_the_about_page(client):
    body = client.get("/ui/diagnostics").text

    assert 'id="about-notices"' in body
    assert "Open-source components" in body


# ---------------------------------------------------------------------------
# Installer
# ---------------------------------------------------------------------------

def test_two_installers_cannot_race_on_the_same_directory():
    installer = (REPO_ROOT / "packaging" / "jarvis.iss").read_text(encoding="utf-8")

    assert "SetupMutex=" in installer


def test_the_installer_still_asks_before_deleting_user_data():
    """Re-asserted here because this file touches the installer: the
    uninstall default must stay "keep my data"."""
    installer = (REPO_ROOT / "packaging" / "jarvis.iss").read_text(encoding="utf-8")

    assert "MB_DEFBUTTON2" in installer, "the destructive answer must not be the default button"
    assert "{param:DELETEDATA|no}" in installer, "silent uninstall keeps data unless asked"


def test_the_installer_is_still_unsigned_and_says_so():
    """No self-signed certificate presented as trusted — see the header
    of packaging/jarvis.iss."""
    installer = (REPO_ROOT / "packaging" / "jarvis.iss").read_text(encoding="utf-8")

    # Checked over directive lines only. The file necessarily mentions
    # SignTool in the comment explaining why it is absent, and a plain
    # substring search cannot tell that apart from setting it.
    directives = [
        line.strip() for line in installer.splitlines()
        if line.strip() and not line.strip().startswith(";")
    ]

    assert not any(line.lower().startswith("signtool=") for line in directives)
    assert "unsigned" in installer
