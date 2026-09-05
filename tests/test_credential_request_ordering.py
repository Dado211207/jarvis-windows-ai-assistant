"""Two `POST /settings/api-key` requests, and what the second one is
allowed to do to the first.

Round 6 serialised `credential_pair.save()`. The route does more than call
it: it makes a **network request to Anthropic first** and only then enters
the coordinator —

    verification = verify_anthropic_key(key, workspace)   # network
    ...
    outcome = credential_pair.save(key, workspace, state) # coordinator

— so two requests can verify out of order and reach the coordinator in the
opposite order to the one they were made in. Round 6's tests called
`save()` directly and therefore proved nothing about this.

**The rule, stated before the fix.** *The request admitted later wins.*
Admission is when the request reaches the endpoint, not when its
verification returns: that is the order the person actually acted in, and
it is the only order they can observe. So

  * an older request whose verification finishes late is **refused**,
    truthfully, rather than silently overwriting the newer one;
  * a newer request never waits for an older one's network call;
  * Save and Remove are ordered against each other by the same rule, since
    they change the same credential.

The alternative — last writer wins — makes the outcome depend on Anthropic's
latency, so pressing Save twice with a slow first check would leave the
first key stored. And holding the coordinator across the verification is
not an option: that is a network request under a lock every other reader
and writer needs.

This file also covers the busy outcome, which Round 6 tested against a
transaction that touched neither store — the one case where its hard-coded
answers were accidentally true.

Every wait is an `Event` or a bounded join.
"""

import threading

import pytest

from tests.test_credential_backend_targets import _WindowsLikeKeyring, _install
from tests.test_credential_pair_transaction import (
    _InAnotherThread,
    _Preferences,
    _metadata_keys,
    _wired,
)
from tests.test_credential_replacement_safety import JOIN_TIMEOUT, settle

OLD_KEY = "sk-ant-api03-OLDER-request-key"
NEW_KEY = "sk-ant-api03-NEWER-request-key"
OLD_WORKSPACE = "wrkspc_01OLDERrequestworkspace"
NEW_WORKSPACE = "wrkspc_01NEWERrequestworkspace"


@pytest.fixture(autouse=True)
def _clean_keyring_import():
    import sys

    sys.modules.pop("keyring", None)
    sys.modules.pop("keyring.errors", None)
    yield
    settle()
    sys.modules.pop("keyring", None)
    sys.modules.pop("keyring.errors", None)


@pytest.fixture(autouse=True)
def _no_env_key(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "anthropic_api_key", "", raising=False)


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from app.api.server import app as jarvis_app
    from tests.conftest import prime_session

    with TestClient(jarvis_app, raise_server_exceptions=True) as started:
        yield prime_session(started)


class _ControlledVerification:
    """`verify_anthropic_key`, with the first call held open.

    Both calls succeed. The point is only *when* each returns, so the two
    requests can be made to reach the coordinator in the opposite order to
    the one they were admitted in.
    """

    def __init__(self, monkeypatch):
        self.first_entered = threading.Event()
        self.release_first = threading.Event()
        self._seen = 0
        self._lock = threading.Lock()

        from app.core.ai import key_check

        def hooked(api_key, workspace_id="", provider_factory=None):
            with self._lock:
                self._seen += 1
                first = self._seen == 1
            if first:
                self.first_entered.set()
                assert self.release_first.wait(JOIN_TIMEOUT), (
                    "the test never released the first verification"
                )
            return key_check.KeyVerification(
                ok=True, message="verified", category=None, worth_storing=True,
            )

        import app.api.routes as routes_module

        monkeypatch.setattr(routes_module, "verify_anthropic_key", hooked, raising=False)
        monkeypatch.setattr(key_check, "verify_anthropic_key", hooked)

    def wait_for_first(self):
        assert self.first_entered.wait(JOIN_TIMEOUT), "the first request never verified"

    def let_first_finish(self):
        self.release_first.set()


def _final_pair(credentials, fake, preferences):
    workspace_key, state_key = _metadata_keys()
    settle()
    return {
        "key": fake.value_at(credentials.SERVICE_NAME),
        "workspace": preferences.get(workspace_key, ""),
        "state": preferences.get(state_key, ""),
    }


# ---------------------------------------------------------------------------
# Blocker 3 — ordering at the HTTP boundary
# ---------------------------------------------------------------------------

def test_an_older_save_whose_check_finishes_late_does_not_overwrite_a_newer_one(
        client, monkeypatch):
    """The reproduction: the older request is admitted first, verifies
    slowly, and must not land on top of the newer one."""
    from app.core import credentials

    fake = _install(monkeypatch, _WindowsLikeKeyring())
    preferences = _Preferences()
    _wired(monkeypatch, preferences)
    verification = _ControlledVerification(monkeypatch)

    older = _InAnotherThread(
        lambda: client.post(
            "/settings/api-key", json={"api_key": OLD_KEY, "workspace_id": OLD_WORKSPACE},
        ),
        "older-post",
    ).start()
    verification.wait_for_first()

    newer = client.post(
        "/settings/api-key", json={"api_key": NEW_KEY, "workspace_id": NEW_WORKSPACE},
    )
    assert newer.status_code == 200
    assert newer.json()["success"] is True, "the newer request must succeed"

    verification.let_first_finish()
    older_response = older.join()
    assert older_response.status_code == 200

    pair = _final_pair(credentials, fake, preferences)
    assert pair["key"] == NEW_KEY, (
        "an older request whose Anthropic check finished late overwrote a newer accepted save"
    )
    assert pair["workspace"] == NEW_WORKSPACE

    older_body = older_response.json()
    assert older_body["success"] is False, (
        "the older request reported success for a key that is not stored"
    )
    assert "newer" in older_body["message"].lower() or "another change" in older_body["message"].lower(), (
        f"the older request did not say why it was not applied: {older_body['message']!r}"
    )


def test_an_older_remove_whose_turn_comes_late_does_not_delete_a_newer_save(
        client, monkeypatch):
    """Save and Remove change the same credential, so the same rule orders
    them. Remove does no network work, so it is admitted and applied
    promptly; the older Save must then be refused rather than restoring the
    key the user just removed."""
    from app.core import credentials

    fake = _install(monkeypatch, _WindowsLikeKeyring())
    preferences = _Preferences()
    _wired(monkeypatch, preferences)
    fake.seed(credentials.SERVICE_NAME, credentials.USERNAME, OLD_KEY)
    verification = _ControlledVerification(monkeypatch)

    older = _InAnotherThread(
        lambda: client.post(
            "/settings/api-key", json={"api_key": NEW_KEY, "workspace_id": NEW_WORKSPACE},
        ),
        "older-save-post",
    ).start()
    verification.wait_for_first()

    removal = client.post("/settings/api-key/remove", json={})
    assert removal.status_code == 200
    assert removal.json()["success"] is True, "the removal must succeed"

    verification.let_first_finish()
    older_response = older.join()

    pair = _final_pair(credentials, fake, preferences)
    assert pair["key"] is None, (
        "a Save admitted before a Remove put the key back after the user removed it"
    )
    assert pair["workspace"] == ""
    assert older_response.json()["success"] is False


def test_two_saves_in_order_still_both_behave_normally(client, monkeypatch):
    """The correction must not refuse ordinary sequential saves."""
    from app.core import credentials

    fake = _install(monkeypatch, _WindowsLikeKeyring())
    preferences = _Preferences()
    _wired(monkeypatch, preferences)

    from app.core.ai import key_check
    import app.api.routes as routes_module

    def always_fine(api_key, workspace_id="", provider_factory=None):
        return key_check.KeyVerification(
            ok=True, message="verified", category=None, worth_storing=True,
        )

    monkeypatch.setattr(routes_module, "verify_anthropic_key", always_fine, raising=False)
    monkeypatch.setattr(key_check, "verify_anthropic_key", always_fine)

    first = client.post(
        "/settings/api-key", json={"api_key": OLD_KEY, "workspace_id": OLD_WORKSPACE},
    )
    second = client.post(
        "/settings/api-key", json={"api_key": NEW_KEY, "workspace_id": NEW_WORKSPACE},
    )
    assert first.json()["success"] is True
    assert second.json()["success"] is True

    pair = _final_pair(credentials, fake, preferences)
    assert pair["key"] == NEW_KEY
    assert pair["workspace"] == NEW_WORKSPACE


def test_the_coordinator_is_not_held_across_the_anthropic_check():
    """A network request under the credential lock would block every reader
    and writer for as long as Anthropic takes to answer."""
    import ast
    import pathlib

    tree = ast.parse(pathlib.Path("app/api/routes.py").read_text(encoding="utf-8"))
    route = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "set_api_key"
    )
    for node in ast.walk(route):
        if isinstance(node, ast.With):
            body = ast.dump(node)
            assert "verify_anthropic_key" not in body, (
                "the Anthropic verification happens inside a `with` block in set_api_key; "
                "if that is the credential coordinator, every reader waits on the network"
            )


# ---------------------------------------------------------------------------
# Blocker 4 — a busy outcome may not report state it never observed
# ---------------------------------------------------------------------------

class _PausedBetweenStores:
    def __init__(self, monkeypatch, credential_pair):
        self.reached = threading.Event()
        self.release = threading.Event()
        self._held = False
        self._lock = threading.Lock()
        real = credential_pair._write_metadata

        def hooked(workspace, state):
            with self._lock:
                first = not self._held
                self._held = True
            if first:
                self.reached.set()
                assert self.release.wait(JOIN_TIMEOUT)
            return real(workspace, state)

        monkeypatch.setattr(credential_pair, "_write_metadata", hooked)

    def wait_until_reached(self):
        assert self.reached.wait(JOIN_TIMEOUT)

    def let_it_finish(self):
        self.release.set()


def test_a_busy_outcome_does_not_guess_the_state_it_could_not_observe(monkeypatch):
    """The real busy case is a first transaction paused *between* the two
    stores — exactly when the pair is transiently inconsistent.

    "This request did not start" is true and is the useful thing to say.
    `stored` and `consistent` are statements about the installation, and
    this request established neither, so it must not assert them.
    """
    from app.core.ai import credential_pair, credential_transaction

    fake = _install(monkeypatch, _WindowsLikeKeyring())
    preferences = _Preferences()
    _wired(monkeypatch, preferences)
    paused = _PausedBetweenStores(monkeypatch, credential_pair)

    saver = _InAnotherThread(
        lambda: credential_pair.save(NEW_KEY, NEW_WORKSPACE, "verified"),
        "first-save",
    ).start()
    paused.wait_until_reached()

    monkeypatch.setattr(credential_transaction, "WAIT_SECONDS", 0.0)
    busy_save = credential_pair.save(OLD_KEY, OLD_WORKSPACE, "verified")
    busy_remove = credential_pair.clear()

    paused.let_it_finish()
    assert saver.join().ok
    settle()

    for outcome in (busy_save, busy_remove):
        assert not outcome.ok
        assert outcome.stored is None, (
            "a request that never started asserted where the key ended up"
        )
        assert outcome.consistent is None, (
            "a request that never started asserted whether the two stores agree"
        )
        lowered = outcome.message.lower()
        assert "did not" in lowered or "not start" in lowered
        # It must not claim the installation is untouched: another change
        # is running and may be part-way through.
        assert "nothing was changed" not in lowered, (
            "a busy outcome claimed the installation was unchanged while another "
            "transaction was part-way between the two stores"
        )


def test_the_busy_response_reaches_the_page_without_a_false_stored_flag(client, monkeypatch):
    """`stored`/`consistent` are relayed to the browser, which uses them to
    decide whether to clear the key box. Unknown must not read as false."""
    from app.core.ai import credential_pair, credential_transaction

    _install(monkeypatch, _WindowsLikeKeyring())
    preferences = _Preferences()
    _wired(monkeypatch, preferences)

    from app.core.ai import key_check
    import app.api.routes as routes_module

    def always_fine(api_key, workspace_id="", provider_factory=None):
        return key_check.KeyVerification(
            ok=True, message="verified", category=None, worth_storing=True,
        )

    monkeypatch.setattr(routes_module, "verify_anthropic_key", always_fine, raising=False)
    monkeypatch.setattr(key_check, "verify_anthropic_key", always_fine)
    paused = _PausedBetweenStores(monkeypatch, credential_pair)

    saver = _InAnotherThread(
        lambda: credential_pair.save(NEW_KEY, NEW_WORKSPACE, "verified"),
        "first-save",
    ).start()
    paused.wait_until_reached()

    monkeypatch.setattr(credential_transaction, "WAIT_SECONDS", 0.0)
    response = client.post(
        "/settings/api-key", json={"api_key": OLD_KEY, "workspace_id": OLD_WORKSPACE},
    )

    paused.let_it_finish()
    saver.join()
    settle()

    body = response.json()
    assert response.status_code == 200
    assert body["success"] is False
    assert body["stored"] is None, "an unknown stored state was reported as a definite False"
    assert body["consistent"] is None


# ---------------------------------------------------------------------------
# Blocker 5 — the coordinator may not claim to detect a bypass
# ---------------------------------------------------------------------------

def test_every_credential_and_metadata_write_goes_through_the_coordinator():
    """`Transaction.is_newest` cannot detect a writer that never entered
    the coordinator: such a writer does not touch the counter it reads.

    This is the check that actually catches one. Only the modules below may
    call the credential mutators or write the pair's metadata keys; adding
    a caller anywhere else fails here, before it can race anything.
    """
    import ast
    import pathlib

    mutators = {
        "set_stored_api_key", "set_stored_api_key_detailed",
        "clear_stored_api_key", "clear_stored_api_key_detailed",
    }
    allowed_mutator_modules = {
        # The only writer of the pair, and it holds the coordinator.
        "app/core/ai/credential_pair.py",
        # Full uninstall, which removes every owned credential after the
        # application has been told to stop. Deliberately outside the
        # coordinator: there is nothing left to be consistent with.
        "app/core/ownership.py",
    }

    offenders = []
    for path in sorted(pathlib.Path("app").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text)
        called = {
            node.func.attr for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        } | {
            node.func.id for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        # `as_posix()` deliberately: `str(path)` yields backslashes on
        # Windows, so comparing it against the allowlist above made every
        # allowed module look like an offender there. The first version of
        # this test passed on Linux and failed the Windows Build job.
        name = path.as_posix()
        if called & mutators and name not in allowed_mutator_modules:
            offenders.append(name)

    assert offenders == [], (
        f"these modules mutate the credential without the coordinator: {offenders}. "
        "Add them to the allowlist only with a reason; a writer outside the "
        "coordinator cannot be detected by any counter inside it."
    )


def test_the_coordinator_does_not_claim_its_counter_detects_an_escaped_writer():
    """The comment said `is_newest` was a tripwire for a path that bypasses
    this module. It is not: a bypassing writer never increments
    `_generation`, so the comparison stays true. The assertion is worth
    keeping as an internal invariant; the claim was not."""
    import pathlib

    for module_path in (
        "app/core/ai/credential_transaction.py",
        "app/core/ai/credential_pair.py",
        "docs/ai/PROJECT_STATE.md",
    ):
        source = pathlib.Path(module_path).read_text(encoding="utf-8")
        for overclaim in (
            "escapes this module is caught",
            "escapes the coordinator",
            "a future code path that reaches a store without",
            "tripwire for a future path",
        ):
            assert overclaim not in source, (
                f"{module_path} still claims its counter detects a writer that bypasses it: "
                f"{overclaim!r}"
            )


def test_the_work_log_marks_the_withdrawn_tripwire_claim_as_withdrawn():
    """The work log is chronological, so round 6's wrong claim stays in it —
    rewriting it would make the mistake look like it never happened, and
    this repository's own conventions keep superseded statements next to
    their correction.

    What it may not do is state a withdrawn claim in the present tense with
    nothing nearby saying so. The round 6 paragraph carries the withdrawal
    marker; this fails if someone removes it while leaving the claim.
    """
    import pathlib

    log = pathlib.Path("docs/ai/WORK_LOG.md").read_text(encoding="utf-8")
    claim = (
        "so a future path that\nreaches a store without going through the coordinator is "
        "caught by an\nassertion rather than by a race"
    )
    if claim in log:
        after = log.split(claim, 1)[1]
        assert "Withdrawn in round 7" in after[:900], (
            "the work log states the withdrawn tripwire claim with no withdrawal marker "
            "following it; a reader stopping at round 6 would take it as true"
        )
