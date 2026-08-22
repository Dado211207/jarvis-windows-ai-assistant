"""Local AI: four states, a model chosen for the machine, and a Ready
that had to be earned.

Nothing here contacts a real Ollama, starts a real process or downloads
anything. The detection seam (`http_client`) and the executable lookup
are both injected, so every state is reachable deterministically.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.core import local_ai


class _Response:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


def _client(payload=None, status_code=200, boom=False):
    client = MagicMock()
    if boom:
        client.get.side_effect = OSError("connection refused")
    else:
        client.get.return_value = _Response(payload or {"models": []}, status_code)
    return client


# ---------------------------------------------------------------------------
# The four states
# ---------------------------------------------------------------------------

def test_nothing_installed_is_explained_without_a_shell_command():
    """The reported defect was a dead end plus a terminal command. A
    person who has never opened a terminal has to be able to act on
    this."""
    with patch.object(local_ai, "find_ollama_executable", return_value=None):
        state = local_ai.describe(http_client=_client(boom=True))

    assert state.status == local_ai.NOT_INSTALLED
    assert state.installed is False
    assert state.can_start is False
    assert state.download_url.startswith("https://")
    assert "ollama pull" not in state.next_step.lower()
    assert "$" not in state.next_step


def test_installed_but_not_running_is_its_own_state_with_a_button(tmp_path):
    """Previously indistinguishable from "not installed", which is the
    difference between a five-second fix and a download."""
    fake = tmp_path / "ollama.exe"
    fake.write_text("")
    with patch.object(local_ai, "find_ollama_executable", return_value=fake):
        state = local_ai.describe(http_client=_client(boom=True))

    assert state.status == local_ai.INSTALLED_NOT_RUNNING
    assert state.installed is True
    assert state.can_start is True, "an installed-but-stopped Ollama must be startable"


def test_running_with_no_models_says_what_is_missing(tmp_path):
    fake = tmp_path / "ollama.exe"
    fake.write_text("")
    with patch.object(local_ai, "find_ollama_executable", return_value=fake):
        state = local_ai.describe(http_client=_client({"models": []}))

    assert state.status == local_ai.RUNNING_NO_MODELS
    assert state.recommended_model in state.next_step


def test_ready_when_a_model_is_actually_installed(tmp_path):
    fake = tmp_path / "ollama.exe"
    fake.write_text("")
    with patch.object(local_ai, "find_ollama_executable", return_value=fake):
        state = local_ai.describe(http_client=_client({"models": [{"name": "llama3.1:8b"}]}))

    assert state.status == local_ai.READY
    assert state.usable is True
    assert "llama3.1:8b" in state.models


def test_every_state_offers_a_next_step():
    """A status with no next step is the defect this replaces."""
    for boom, payload, installed in (
        (True, None, False),
        (True, None, True),
        (False, {"models": []}, True),
        (False, {"models": [{"name": "llama3.1:8b"}]}, True),
    ):
        target = "/fake/ollama" if installed else None
        with patch.object(local_ai, "find_ollama_executable", return_value=target):
            state = local_ai.describe(http_client=_client(payload, boom=boom))
        assert state.headline.strip()
        assert state.next_step.strip(), f"{state.status} offered no next step"


# ---------------------------------------------------------------------------
# The model is chosen for this machine
# ---------------------------------------------------------------------------

def test_a_small_machine_is_recommended_a_small_model():
    assert local_ai.recommend_model(memory_gb=4).name == "llama3.2:1b"


def test_a_mid_range_machine_gets_the_mid_range_model():
    assert local_ai.recommend_model(memory_gb=8).name == "llama3.2:3b"


def test_a_sixteen_gigabyte_machine_gets_the_larger_model():
    """The machine this was reported from has 16 GB."""
    assert local_ai.recommend_model(memory_gb=16).name == "llama3.1:8b"


def test_unknown_memory_falls_back_to_the_smallest_rather_than_guessing_big():
    """Being conservative costs some quality. Being wrong the other way
    costs a machine that swaps itself to a standstill."""
    with patch.object(local_ai, "total_memory_gb", return_value=None):
        assert local_ai.recommend_model().name == "llama3.2:1b"


def test_every_recommendation_explains_itself():
    for memory in (2, 8, 16, 64):
        suggestion = local_ai.recommend_model(memory_gb=memory)
        assert suggestion.why.strip()
        assert suggestion.approximate_download.strip()


# ---------------------------------------------------------------------------
# Starting an installed Ollama
# ---------------------------------------------------------------------------

def test_starting_uses_an_argument_list_never_a_shell_string(tmp_path):
    """CLAUDE.md's rule: subprocess calls use explicit argument lists."""
    fake = tmp_path / "ollama.exe"
    fake.write_text("")
    with patch.object(local_ai, "find_ollama_executable", return_value=fake), \
         patch("subprocess.Popen") as popen:
        assert local_ai.start_ollama() is True

    args, kwargs = popen.call_args
    assert isinstance(args[0], list)
    assert kwargs.get("shell") in (None, False)


def test_starting_is_refused_when_nothing_is_installed():
    with patch.object(local_ai, "find_ollama_executable", return_value=None):
        assert local_ai.start_ollama() is False


def test_a_failed_start_reports_false_rather_than_raising(tmp_path):
    fake = tmp_path / "ollama.exe"
    fake.write_text("")
    with patch.object(local_ai, "find_ollama_executable", return_value=fake), \
         patch("subprocess.Popen", side_effect=OSError("denied")):
        assert local_ai.start_ollama() is False


# ---------------------------------------------------------------------------
# Ready means it answered
# ---------------------------------------------------------------------------

def test_verification_fails_when_the_server_is_not_available():
    result = local_ai.verify_with_real_inference(http_client=_client(boom=True))

    assert result.ok is False
    assert result.message.strip()


def test_verification_refuses_a_model_that_is_not_installed():
    """Naming what *is* installed, rather than only what is not."""
    result = local_ai.verify_with_real_inference(
        model="nonexistent:70b",
        http_client=_client({"models": [{"name": "llama3.1:8b"}]}),
    )

    assert result.ok is False
    assert "llama3.1:8b" in result.message


def test_verification_succeeds_only_on_real_generated_text():
    reply = SimpleNamespace(content="ready")
    with patch("app.core.ai.ollama_provider.OllamaProvider") as provider:
        provider.return_value.generate.return_value = reply
        result = local_ai.verify_with_real_inference(
            model="llama3.1:8b",
            http_client=_client({"models": [{"name": "llama3.1:8b"}]}),
        )

    assert result.ok is True
    assert result.model == "llama3.1:8b"


def test_an_empty_answer_is_not_ready():
    """A model that loads and returns nothing is broken, and calling
    that Ready is exactly the false claim this exists to stop."""
    with patch("app.core.ai.ollama_provider.OllamaProvider") as provider:
        provider.return_value.generate.return_value = SimpleNamespace(content="   ")
        result = local_ai.verify_with_real_inference(
            model="llama3.1:8b",
            http_client=_client({"models": [{"name": "llama3.1:8b"}]}),
        )

    assert result.ok is False
    assert "no answer" in result.message.lower()


def test_a_timeout_says_so_rather_than_reporting_a_generic_failure():
    class _Timeout(Exception):
        category = SimpleNamespace(value="timeout")

    with patch("app.core.ai.ollama_provider.OllamaProvider") as provider:
        provider.return_value.generate.side_effect = _Timeout("too slow")
        result = local_ai.verify_with_real_inference(
            model="llama3.1:8b",
            http_client=_client({"models": [{"name": "llama3.1:8b"}]}),
        )

    assert result.ok is False
    assert "in time" in result.message.lower()


# ---------------------------------------------------------------------------
# The rule this module is built around
# ---------------------------------------------------------------------------

def test_this_module_never_pulls_a_model():
    """Downloading belongs to app/core/local_ai_models.py, not here.

    This module answers "where is local AI up to?" and is read on every
    status poll and every page render. A download issued from a describe()
    path would be gigabytes starting because somebody opened a page.
    Asserted here as well as by the repository-wide AST test, because
    this is the module most likely to grow one by accident."""
    import ast
    from pathlib import Path

    source = Path(local_ai.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    docstrings = {
        id(node)
        for parent in ast.walk(tree)
        if isinstance(parent, (ast.Module, ast.ClassDef, ast.FunctionDef))
        for node in [ast.get_docstring(parent, clean=False) and parent.body[0].value]
        if node is not None
    }

    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in docstrings:
                continue
            assert "api/pull" not in node.value, "a model download must never be issued from here"


# ---------------------------------------------------------------------------
# The endpoints and the page
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient

    from app.api.server import app as jarvis_app
    from tests.conftest import prime_session

    with TestClient(jarvis_app, raise_server_exceptions=True) as test_client:
        yield prime_session(test_client)


def test_status_endpoint_always_offers_a_next_step(client):
    body = client.get("/local-ai/status").json()

    assert body["status"] in {
        local_ai.NOT_INSTALLED, local_ai.INSTALLED_NOT_RUNNING,
        local_ai.RUNNING_NO_MODELS, local_ai.READY,
    }
    assert body["headline"].strip()
    assert body["next_step"].strip()
    assert body["recommended_model"].strip()


def test_starting_is_refused_and_explained_when_nothing_is_installed(client):
    with patch.object(local_ai, "find_ollama_executable", return_value=None):
        body = client.post("/local-ai/start", json={}).json()

    assert body["started"] is False
    assert "not installed" in body["message"].lower()
    assert "does not install it for you" in body["message"].lower()


def test_start_requires_the_session_token(client):
    from app.api.session import HEADER_NAME

    response = client.post("/local-ai/start", json={}, headers={HEADER_NAME: "wrong"})

    assert response.status_code == 403


def test_verify_requires_the_session_token(client):
    from app.api.session import HEADER_NAME

    response = client.post("/local-ai/verify", json={}, headers={HEADER_NAME: "wrong"})

    assert response.status_code == 403


def test_verify_reports_the_real_outcome(client):
    with patch.object(
        local_ai, "verify_with_real_inference",
        return_value=local_ai.VerificationResult(ok=True, message="ok", model="llama3.1:8b", reply="ready"),
    ):
        body = client.post("/local-ai/verify", json={}).json()

    assert body["ok"] is True
    assert body["model"] == "llama3.1:8b"


def test_the_settings_page_carries_the_local_ai_controls(client):
    html = client.get("/ui/settings").text

    for element in ("local-ai-headline", "local-ai-next", "local-ai-recommended",
                    "local-ai-start", "local-ai-test", "local-ai-download"):
        assert element in html, f"the Settings page is missing {element}"


def test_the_settings_page_states_that_nothing_downloads_unasked(client):
    """The boundary moved but did not disappear. JARVIS now installs
    Ollama and downloads a model; the page has to say that neither
    happens until a button is pressed, and that Ollama is somebody
    else's software which JARVIS does not remove."""
    html = client.get("/ui/settings").text.lower()

    assert "nothing here downloads until you press a button" in html
    assert "never removes an ollama it did not install" in html


def test_the_installer_never_claims_to_include_local_ai():
    """Local AI is optional and is not shipped. The installer must not
    imply otherwise — a promise made at install time is the one a user
    remembers when the feature turns out to need a separate download.

    Scoped to everything before the uninstall handler, because the
    uninstall prompt now mentions Ollama on purpose: to say it is *not*
    removed. Naming what an uninstaller leaves alone is the opposite of
    claiming it was included, and the next test pins that.
    """
    from pathlib import Path

    repo = Path(__file__).resolve().parent.parent
    installer = (repo / "packaging" / "jarvis.iss").read_text(encoding="utf-8").lower()
    install_time = installer.split("procedure runapplicationcleanup", 1)[0]

    for claim in ("ollama", "local ai", "local llm", "language model"):
        assert claim not in install_time, (
            f"the installer text mentions {claim!r}; it must not imply local AI is included"
        )


def test_the_uninstaller_promises_not_to_remove_ollama():
    """The other side of the same boundary. JARVIS may install Ollama
    now, and it still never removes it: that would be deciding, on
    somebody's behalf, that they no longer want local AI because they no
    longer want JARVIS."""
    from pathlib import Path

    repo = Path(__file__).resolve().parent.parent
    installer = (repo / "packaging" / "jarvis.iss").read_text(encoding="utf-8")
    uninstall_text = installer.split("procedure RunApplicationCleanup", 1)[1]

    assert "never removes Ollama or its models" in uninstall_text


def test_nothing_bundles_ollama_with_the_application():
    """It is a separate program the user installs. If it ever appeared in
    a requirements file or the PyInstaller spec, the boundary the Local
    AI page describes would be a false statement."""
    from pathlib import Path

    repo = Path(__file__).resolve().parent.parent
    for name in ("requirements.txt", "requirements-windows.txt", "requirements-voice.txt",
                 "packaging/jarvis.spec"):
        path = repo / name
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8").lower()
        assert "ollama" not in text, f"{name} must not bundle or require Ollama"
