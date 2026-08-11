"""Setting local AI up from inside JARVIS.

The product's owner decided that a person who wants local AI should be
able to get it from a button rather than a set of instructions. This
reverses a rule that used to be non-negotiable, so the things that
replace it are worth holding down hard:

  * nothing is downloaded, installed or pulled without a person pressing
    something;
  * the downloaded executable is verified as Ollama's before it is run,
    and deleted rather than run if it is not;
  * an Ollama that was already on the machine is used, never reinstalled
    over and never taken ownership of;
  * every long job is cancellable, retryable and resumable, and says
    which of those to do when it fails;
  * Ready still means real generated text came back.

Nothing here downloads anything or runs an installer: httpx, the
subprocess call and the signature check are all patched.
"""

from unittest.mock import MagicMock, patch

import pytest

from app.core import local_ai, local_ai_install, local_ai_models, machine


@pytest.fixture(autouse=True)
def clean_jobs():
    """Both background jobs start from idle in every test — they are
    module singletons, and a state left behind by one test would make the
    next one describe something that is not happening."""
    local_ai_install.ollama_installer._state = local_ai_install.InstallState()
    local_ai_models.model_puller._state = local_ai_models.PullState()
    yield
    local_ai_install.ollama_installer._state = local_ai_install.InstallState()
    local_ai_models.model_puller._state = local_ai_models.PullState()


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from app.api.server import app as jarvis_app
    from tests.conftest import prime_session

    with TestClient(jarvis_app, raise_server_exceptions=True) as test_client:
        yield prime_session(test_client)


# ---------------------------------------------------------------------------
# Consent: the plan is available before, and only before, anything happens
# ---------------------------------------------------------------------------

def test_the_plan_names_everything_a_person_needs_to_decide(client):
    body = client.get("/local-ai/plan").json()

    assert body["url"].startswith("https://")
    assert body["publisher"] == "Ollama"
    assert body["licence"]
    assert body["approximate_size"]
    assert body["model"]
    assert body["model_download"]
    assert body["hardware"], "a consent screen must say what this computer has"
    assert "signature" in body["verification"].lower() or "signed" in body["verification"].lower()


def test_asking_what_would_be_downloaded_downloads_nothing(client):
    with patch("httpx.stream") as stream, patch("subprocess.run") as run:
        client.get("/local-ai/plan")

    stream.assert_not_called()
    run.assert_not_called()


def test_the_plan_says_when_the_disk_is_too_small(client):
    small = machine.Machine(
        memory_gb=16.0, cpu_cores=8, cpu_name="x86", gpus=[],
        free_disk_gb=1.0, models_path="C:/models",
    )
    with patch.object(machine, "inspect", return_value=small):
        body = client.get("/local-ai/plan").json()

    assert body["enough_disk"] is False


def test_unknown_free_space_is_not_reported_as_enough(client):
    """None must not become a yes: telling somebody there is room when it
    could not be measured is how a 5 GB download fails at the end."""
    unknown = machine.Machine(
        memory_gb=16.0, cpu_cores=8, cpu_name="x86", gpus=[],
        free_disk_gb=None, models_path="C:/models",
    )
    with patch.object(machine, "inspect", return_value=unknown):
        body = client.get("/local-ai/plan").json()

    assert body["enough_disk"] is None


# ---------------------------------------------------------------------------
# Installing Ollama
# ---------------------------------------------------------------------------

def test_installing_requires_the_session_token():
    from fastapi.testclient import TestClient

    from app.api.server import app as jarvis_app

    with TestClient(jarvis_app, raise_server_exceptions=True) as bare:
        assert bare.post("/local-ai/install", json={}).status_code == 403
        assert bare.post("/local-ai/pull", json={"model": "x"}).status_code == 403


def test_an_existing_ollama_is_left_alone(client):
    """Somebody else's installation is theirs. Reinstalling over it
    without being asked is exactly the ownership problem this must not
    have."""
    with patch.object(local_ai, "is_installed", return_value=True), \
         patch.object(local_ai_install.ollama_installer, "start") as start:
        body = client.post("/local-ai/install", json={}).json()

    start.assert_not_called()
    assert body["started"] is False
    assert "already installed" in body["message"]


def test_the_download_address_is_ollamas_own():
    assert local_ai_install.INSTALLER_URL.startswith("https://ollama.com/")
    assert "ollama.com" in local_ai_install.ALLOWED_HOSTS


def test_a_redirect_off_the_trusted_hosts_is_refused(tmp_path):
    """"We downloaded an .exe from wherever that URL ended up pointing"
    is not a security story."""
    response = MagicMock()
    response.url = "https://somewhere-else.example/OllamaSetup.exe"
    response.headers = {"content-length": "10"}
    response.iter_bytes.return_value = [b"x" * 10]
    response.raise_for_status.return_value = None
    stream = MagicMock()
    stream.__enter__.return_value = response

    installer = local_ai_install.OllamaInstaller()
    with patch("httpx.stream", return_value=stream):
        ok = installer._download(tmp_path / "OllamaSetup.exe")

    assert ok is False
    assert installer.state().status == local_ai_install.ERROR
    assert "does not trust" in installer.state().message


def test_an_unsigned_download_is_deleted_and_never_run(tmp_path):
    """The most dangerous thing in this product is running a downloaded
    executable, so the check before it has no "continue anyway"."""
    from app.core import authenticode

    installer_path = tmp_path / "OllamaSetup.exe"
    installer_path.write_bytes(b"not really ollama")

    installer = local_ai_install.OllamaInstaller()
    verdict = authenticode.SignatureVerdict(
        trusted=False, signer="", detail="The file is not signed at all.",
    )
    with patch.object(authenticode, "verify", return_value=verdict), \
         patch.object(authenticode, "sha256", return_value="abc"), \
         patch("subprocess.run") as run:
        ok = installer._verify(installer_path)

    assert ok is False
    assert not installer_path.exists(), "an unverified installer must not be left on disk"
    run.assert_not_called()
    assert "not signed" in installer.state().message


def test_a_signature_from_the_wrong_publisher_is_refused(tmp_path):
    """A valid signature only proves somebody signed it. Anybody can buy
    a code-signing certificate."""
    from app.core import authenticode

    installer_path = tmp_path / "OllamaSetup.exe"
    installer_path.write_bytes(b"signed by someone else")

    verdict = authenticode.SignatureVerdict(
        trusted=False, signer="Some Other Company Ltd",
        detail="The file is signed, but by “Some Other Company Ltd” rather than Ollama.",
    )
    installer = local_ai_install.OllamaInstaller()
    with patch.object(authenticode, "verify", return_value=verdict), \
         patch.object(authenticode, "sha256", return_value="abc"):
        assert installer._verify(installer_path) is False

    assert "Some Other Company Ltd" in installer.state().message


def test_the_expected_publisher_is_checked_not_just_the_signature():
    """Regression guard: dropping the publisher argument would leave a
    check that passes for any signed file at all."""
    import inspect

    source = inspect.getsource(local_ai_install.OllamaInstaller._verify)

    assert "expected_publisher" in source
    assert "EXPECTED_PUBLISHER" in source


def test_the_sha256_is_recorded_so_the_file_can_be_checked_independently(tmp_path):
    from app.core import authenticode

    installer_path = tmp_path / "OllamaSetup.exe"
    installer_path.write_bytes(b"whatever")
    verdict = authenticode.SignatureVerdict(trusted=True, signer="Ollama", detail="ok")

    installer = local_ai_install.OllamaInstaller()
    with patch.object(authenticode, "verify", return_value=verdict):
        installer._verify(installer_path)

    assert installer.state().sha256, "the hash of what was downloaded must be shown"
    assert installer.state().signer == "Ollama"


def test_the_installer_is_run_with_an_argument_list_never_a_shell_string(tmp_path):
    installer_path = tmp_path / "OllamaSetup.exe"
    installer_path.write_bytes(b"x")

    installer = local_ai_install.OllamaInstaller()
    with patch("subprocess.run", return_value=MagicMock(returncode=0)) as run:
        installer._execute(installer_path)

    args, kwargs = run.call_args
    assert isinstance(args[0], list)
    assert kwargs.get("shell") in (None, False)


def test_the_installer_is_not_run_silently(tmp_path):
    """Ollama's own installer is shown. The person sees whose software is
    being installed and approves Windows' own elevation prompt — a silent
    install of somebody else's software is what this must not do."""
    import inspect

    source = inspect.getsource(local_ai_install.OllamaInstaller._execute)

    for silent_flag in ("/VERYSILENT", "/SILENT", "/quiet", "/S"):
        assert silent_flag not in source


def test_it_is_refused_on_a_platform_with_no_ollama_installer():
    installer = local_ai_install.OllamaInstaller()
    with patch("sys.platform", "linux"):
        assert installer.start() is False
    assert "Windows" in installer.state().message


def test_jarvis_records_that_it_installed_ollama_itself():
    """The uninstaller has to tell "we put this here" apart from "this
    was already here"."""
    assert local_ai_install.installed_by_jarvis() is False

    local_ai_install._remember_we_installed_it()

    assert local_ai_install.installed_by_jarvis() is True


# ---------------------------------------------------------------------------
# Pulling a model
# ---------------------------------------------------------------------------

def _pull_lines(*payloads):
    import json

    response = MagicMock()
    response.status_code = 200
    response.iter_lines.return_value = [json.dumps(p) for p in payloads]
    stream = MagicMock()
    stream.__enter__.return_value = response
    return stream


def test_progress_is_reported_while_a_model_downloads():
    puller = local_ai_models.ModelPuller()
    stream = _pull_lines(
        {"status": "pulling manifest"},
        {"status": "pulling abc", "total": 1000, "completed": 250},
    )
    with patch("httpx.stream", return_value=stream):
        puller._run("llama3.2:3b")

    # The stream ended without success, which is reported as unfinished —
    # what matters here is that the numbers arrived on the way.
    assert puller.state().bytes_total == 1000
    assert puller.state().bytes_downloaded == 250


def test_success_is_only_reported_when_ollama_says_success():
    puller = local_ai_models.ModelPuller()
    stream = _pull_lines(
        {"status": "pulling abc", "total": 100, "completed": 100},
        {"status": "verifying sha256 digest"},
        {"status": "success"},
    )
    answered = local_ai.VerificationResult(ok=True, message="It answered.", model="llama3.2:3b")
    with patch("httpx.stream", return_value=stream), \
         patch.object(local_ai, "verify_with_real_inference", return_value=answered):
        puller._run("llama3.2:3b")

    assert puller.state().status == local_ai_models.COMPLETE


def test_a_model_that_downloads_but_will_not_answer_is_not_reported_as_done():
    """Downloaded is not working. Not enough memory, an unsupported
    quantisation or a broken runtime all produce a perfect download and
    a model that cannot load — and without this, that appears for the
    first time when somebody tries to have a conversation."""
    puller = local_ai_models.ModelPuller()
    stream = _pull_lines({"status": "success"})
    refused = local_ai.VerificationResult(
        ok=False, message="The model did not answer in time.", model="llama3.2:3b",
    )
    with patch("httpx.stream", return_value=stream), \
         patch.object(local_ai, "verify_with_real_inference", return_value=refused):
        puller._run("llama3.2:3b")

    assert puller.state().status == local_ai_models.ERROR
    assert "did not answer" in puller.state().message


def test_ready_is_proven_by_real_generated_text():
    """Regression guard for the sentence "Ready means something": a pull
    that skipped the probe would report a working local AI on the basis
    of a file existing."""
    import inspect

    source = inspect.getsource(local_ai_models.ModelPuller._run)

    assert "_prove_it_answers" in source


def test_a_stream_that_stops_early_is_not_called_finished():
    """Reporting a model as installed when it might not be is the failure
    this exists to avoid."""
    puller = local_ai_models.ModelPuller()
    stream = _pull_lines({"status": "pulling abc", "total": 100, "completed": 40})
    with patch("httpx.stream", return_value=stream):
        puller._run("llama3.2:3b")

    assert puller.state().status == local_ai_models.ERROR
    assert "Retry" in puller.state().message


def test_verifying_is_its_own_visible_state():
    puller = local_ai_models.ModelPuller()
    puller._consume('{"status": "verifying sha256 digest"}', "llama3.2:3b")

    assert puller.state().status == local_ai_models.VERIFYING


def test_cancelling_keeps_what_already_downloaded():
    puller = local_ai_models.ModelPuller()
    puller.cancel()
    stream = _pull_lines({"status": "pulling abc", "total": 100, "completed": 40})
    with patch("httpx.stream", return_value=stream):
        puller._run("llama3.2:3b")

    assert puller.state().status == local_ai_models.CANCELLED
    assert "kept" in puller.state().message


@pytest.mark.parametrize("error,expected", [
    ("no space left on device", "disk space"),
    ("digest mismatch", "damaged"),
    ("model 'nope' not found", "does not publish"),
    ("connection reset by peer", "connection"),
])
def test_each_failure_names_its_own_fix(error, expected):
    """"The download failed" tells nobody what to do. A full disk, a
    corrupted layer, a wrong name and a dropped connection have four
    different answers."""
    assert expected in local_ai_models._explain(error, "llama3.2:3b").lower()


def test_a_corrupted_layer_is_fixed_by_retrying_not_by_starting_over():
    message = local_ai_models._explain("digest mismatch", "llama3.2:3b")

    assert "Retry" in message
    assert "the rest is kept" in message


def test_pulling_is_refused_when_there_is_nowhere_to_put_it(client):
    with patch.object(local_ai, "is_installed", return_value=False):
        body = client.post("/local-ai/pull", json={"model": "llama3.2:3b"}).json()

    assert body["started"] is False
    assert "not installed" in body["message"]


def test_the_default_model_is_the_one_this_machine_can_run(client):
    """Pressing the button on a small computer must not start a download
    that will not run on it."""
    with patch.object(local_ai, "is_installed", return_value=True), \
         patch.object(machine, "memory_gb", return_value=6.0), \
         patch.object(local_ai_models.model_puller, "start", return_value=True) as start:
        client.post("/local-ai/pull", json={"model": ""})

    assert start.call_args.args[0] == "llama3.2:1b"


def test_the_recommendation_and_the_consent_screen_read_the_same_hardware():
    """One reader. Two would eventually disagree, and the one deciding
    which model to download disagreeing with the one shown on the consent
    screen is a particularly confusing way to fail."""
    import inspect

    source = inspect.getsource(local_ai.total_memory_gb)

    assert "machine.memory_gb()" in source


# ---------------------------------------------------------------------------
# The ten states
# ---------------------------------------------------------------------------

def test_all_ten_states_exist():
    assert len(local_ai.ALL_STATES) == 10
    assert len(set(local_ai.ALL_STATES)) == 10


def test_a_running_install_is_reported_instead_of_not_installed():
    """While Ollama's installer is on screen, "not installed" is a true
    statement and a useless one."""
    local_ai_install.ollama_installer._state = local_ai_install.InstallState(
        status=local_ai_install.DOWNLOADING, message="Downloading…",
        bytes_downloaded=50, bytes_total=100,
    )
    with patch.object(local_ai, "is_installed", return_value=False):
        state = local_ai.describe()

    assert state.status == local_ai.INSTALLING
    assert state.busy is True
    assert state.percent == 50


def test_a_failed_install_is_its_own_state_with_the_reason():
    local_ai_install.ollama_installer._state = local_ai_install.InstallState(
        status=local_ai_install.ERROR, message="The file is not signed at all.",
    )
    state = local_ai.describe()

    assert state.status == local_ai.FAILED
    assert "not signed" in state.detail
    assert state.next_step.strip()


def test_a_running_pull_is_reported_with_progress():
    local_ai_models.model_puller._state = local_ai_models.PullState(
        status=local_ai_models.DOWNLOADING, model="llama3.2:3b",
        message="Downloading…", bytes_downloaded=1, bytes_total=4,
    )
    with patch.object(local_ai, "is_installed", return_value=True):
        state = local_ai.describe()

    assert state.status == local_ai.DOWNLOADING_MODEL
    assert state.percent == 25


def test_every_state_still_offers_a_next_step():
    """Including the new ones. A state with nothing to do about it is the
    original defect."""
    for setup in (
        lambda: local_ai_install.ollama_installer._state.__setattr__("status", local_ai_install.DOWNLOADING),
        lambda: local_ai_install.ollama_installer._state.__setattr__("status", local_ai_install.ERROR),
        lambda: local_ai_models.model_puller._state.__setattr__("status", local_ai_models.DOWNLOADING),
        lambda: local_ai_models.model_puller._state.__setattr__("status", local_ai_models.ERROR),
    ):
        local_ai_install.ollama_installer._state = local_ai_install.InstallState()
        local_ai_models.model_puller._state = local_ai_models.PullState()
        setup()
        state = local_ai.describe()
        assert state.headline.strip()
        assert state.next_step.strip(), f"{state.status} offered no next step"


def test_an_ollama_that_was_already_here_is_said_to_be_the_users(client):
    with patch.object(local_ai, "is_installed", return_value=True), \
         patch.object(local_ai_install, "installed_by_jarvis", return_value=False):
        state = local_ai.describe()

    if state.status == local_ai.INSTALLED_NOT_RUNNING:
        assert "before JARVIS" in state.detail


# ---------------------------------------------------------------------------
# The rest of the product does not depend on any of this
# ---------------------------------------------------------------------------

def test_a_broken_local_ai_setup_does_not_break_the_status_endpoint(client):
    """Settings polls this while a download runs. A 500 would stop the
    progress bar and leave somebody watching a frozen screen."""
    with patch.object(local_ai, "describe", side_effect=OSError("everything is broken")):
        response = client.get("/local-ai/status")

    assert response.status_code == 200
    assert response.json()["status"] == local_ai.FAILED
    assert response.json()["next_step"].strip()
    assert client.get("/health").status_code == 200


def test_anthropic_chat_never_consults_local_ai_setup():
    """Local AI failing, being skipped, or never being set up must leave
    the rest of the product exactly as it was."""
    import inspect

    from app.core.ai import anthropic_provider

    source = inspect.getsource(anthropic_provider)

    assert "local_ai" not in source
