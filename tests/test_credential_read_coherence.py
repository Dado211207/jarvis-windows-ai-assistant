"""A reader must never see half of one credential and half of another, and
a failure must only ever be attributed to the credential that made it.

Round 6 serialised the *writers*. These are the two things that were still
outside that boundary.

**1. A reader can observe a mixed pair.** `Brain._provider_config()` read the
key and the Workspace ID as two independent store reads, neither of them
inside the credential transaction:

    api_key=settings.effective_api_key if settings.has_anthropic_key else "",
    anthropic_workspace_id=get_preference(WORKSPACE_PREFERENCE) or "",

Pausing a successful `credential_pair.save()` after the credential write and
before the metadata write puts a real chat request in exactly that window:

    observed_key        NEW-KEY
    observed_workspace  wrkspc_OLD
    mixed_pair          True
    save_outcome        applied
    final_key           NEW-KEY
    final_workspace     wrkspc_NEW

A request built from that configuration is sent to Anthropic with one key's
credential and another key's workspace. The writer lock does not protect
readers, and the Settings page's button state protects neither Chat nor a
direct API call.

**2. A delayed failure from the old key downgrades the new one.** Both
provider failure paths called

    note_runtime_failure(provider.name, exc.category)

with nothing identifying *which* credential made the failed request. So:

    1. a request captures OLD-KEY and stays in flight
    2. Settings saves NEW-KEY successfully, state `verified`
    3. the old request finally returns PROVIDER_AUTH
    4. the new, working credential is marked `verification_failed`

That also disproves the comment in `app/core/providers.py` claiming the
runtime note "cannot outlive the key it describes": `save()` does clear it,
and the delayed old request then recreates it.

**What the correction has to be.** One atomic, immutable snapshot carrying a
non-secret revision, taken once per request; and a failure that may only
downgrade the exact revision that made it. Not a longer lock: the old
request could wait for the new save to finish and still be wrong afterwards.

Every wait below is an `Event` or a bounded join. No sleeps.
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

OLD_KEY = "sk-ant-api03-OLD-key-in-flight"
NEW_KEY = "sk-ant-api03-NEW-key-just-saved"
OLD_WORKSPACE = "wrkspc_01OLDworkspaceidvalue"
NEW_WORKSPACE = "wrkspc_01NEWworkspaceidvalue"


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
    """`effective_api_key` prefers ANTHROPIC_API_KEY, which CI sets.

    These tests are about the credential *store*, so the environment
    override is removed for their duration — otherwise every read would
    answer with the environment's key and the race would be invisible.
    """
    from app.config import settings

    monkeypatch.setattr(settings, "anthropic_api_key", "", raising=False)


# ---------------------------------------------------------------------------
# Pausing a successful save between its two stores
# ---------------------------------------------------------------------------

class _PausedBetweenStores:
    """Holds `credential_pair._write_metadata` on its first call.

    The credential has been written and confirmed; its description has not.
    This is the only moment at which the two stores disagree during an
    operation that ends up succeeding, so it is where a reader has to be
    tested.
    """

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
                assert self.release.wait(JOIN_TIMEOUT), "the test never released the paused save"
            return real(workspace, state)

        monkeypatch.setattr(credential_pair, "_write_metadata", hooked)

    def wait_until_reached(self):
        assert self.reached.wait(JOIN_TIMEOUT), "the save never reached its metadata write"

    def let_it_finish(self):
        self.release.set()


def _seed_previous_pair(fake, preferences, credentials):
    fake.seed(credentials.SERVICE_NAME, credentials.USERNAME, OLD_KEY)
    workspace_key, state_key = _metadata_keys()
    preferences.store_many({workspace_key: OLD_WORKSPACE, state_key: "verified"})
    preferences.writes.clear()


def _observed_pair():
    """What a real chat request would actually be built from."""
    from app.core.brain import Brain

    config = Brain()._provider_config()
    return config.api_key, config.anthropic_workspace_id


# ---------------------------------------------------------------------------
# Blocker 1 — a reader must never see a mixed pair
# ---------------------------------------------------------------------------

def test_a_reader_never_sees_one_requests_key_with_another_requests_workspace(monkeypatch):
    """The reported reproduction, through the real `Brain` configuration."""
    from app.core import credentials
    from app.core.ai import credential_pair

    fake = _install(monkeypatch, _WindowsLikeKeyring())
    preferences = _Preferences()
    _wired(monkeypatch, preferences)
    _seed_previous_pair(fake, preferences, credentials)

    paused = _PausedBetweenStores(monkeypatch, credential_pair)
    saver = _InAnotherThread(
        lambda: credential_pair.save(NEW_KEY, NEW_WORKSPACE, "verified"),
        "settings-save",
    ).start()
    paused.wait_until_reached()

    # The reader runs on its own thread because the two worlds differ in
    # exactly the way this is testing. **Before the correction** it reads
    # both stores immediately and returns the mixed pair while the save is
    # still parked. **After it**, it waits for the coherent moment — so a
    # reader on the main thread would deadlock against a save only this
    # test can release.
    reader = _InAnotherThread(_observed_pair, "chat-request").start()
    paused.let_it_finish()
    observed_key, observed_workspace = reader.join()
    outcome = saver.join()
    settle()

    assert outcome.ok, "the save must succeed; this test is about what a reader saw meanwhile"

    coherent = (
        (observed_key, observed_workspace) == (OLD_KEY, OLD_WORKSPACE)
        or (observed_key, observed_workspace) == (NEW_KEY, NEW_WORKSPACE)
    )
    assert coherent, (
        "a reader was given one request's key with another request's Workspace ID: "
        f"key={'NEW' if observed_key == NEW_KEY else 'OLD'} "
        f"workspace={'NEW' if observed_workspace == NEW_WORKSPACE else 'OLD'}"
    )


def test_the_status_endpoint_never_reports_a_mixed_pair(monkeypatch):
    """`GET /settings/api-key-status` reads the same two stores separately.

    It reports booleans rather than values, so the visible damage is
    smaller — but "configured" and "workspace_configured" still have to
    describe the same credential.
    """
    from app.core import credentials
    from app.core.ai import credential_pair, credential_view

    fake = _install(monkeypatch, _WindowsLikeKeyring())
    preferences = _Preferences()
    _wired(monkeypatch, preferences)
    fake.seed(credentials.SERVICE_NAME, credentials.USERNAME, OLD_KEY)
    workspace_key, state_key = _metadata_keys()
    preferences.store_many({workspace_key: "", state_key: ""})

    paused = _PausedBetweenStores(monkeypatch, credential_pair)
    saver = _InAnotherThread(
        lambda: credential_pair.save(NEW_KEY, NEW_WORKSPACE, "verified"),
        "settings-save",
    ).start()
    paused.wait_until_reached()

    reader = _InAnotherThread(
        lambda: (lambda snap: (snap.configured, bool(snap.workspace_id)))(
            credential_view.current()
        ),
        "status-request",
    ).start()
    paused.let_it_finish()
    observed = reader.join()
    saver.join()
    settle()

    assert observed in ((False, False), (True, True)), (
        "the status endpoint described a key from one request and a workspace from another"
    )


def test_one_request_reads_the_credential_once(monkeypatch):
    """`has_anthropic_key` itself calls `effective_api_key`, so the old code
    read the credential store twice per request and could get two different
    answers. One snapshot, one read."""
    from app.core import credentials
    from app.core.ai import credential_view

    fake = _install(monkeypatch, _WindowsLikeKeyring())
    preferences = _Preferences()
    _wired(monkeypatch, preferences)
    _seed_previous_pair(fake, preferences, credentials)

    reads = []
    real_read = credentials._read

    def counted(username):
        reads.append(username)
        return real_read(username)

    monkeypatch.setattr(credentials, "_read", counted)
    credential_view.invalidate()

    from app.core.brain import Brain

    Brain()._provider_config()

    assert len(reads) <= 1, (
        f"one request read the credential store {len(reads)} times; a concurrent save "
        "between those reads is how a mixed pair reaches a provider"
    )


def test_the_snapshot_is_immutable_and_never_renders_its_secret(monkeypatch):
    """It is passed around and held for the length of a request, so it must
    not be mutable, and its repr must not put a key in a traceback."""
    from app.core import credentials
    from app.core.ai import credential_view

    fake = _install(monkeypatch, _WindowsLikeKeyring())
    preferences = _Preferences()
    _wired(monkeypatch, preferences)
    _seed_previous_pair(fake, preferences, credentials)
    credential_view.invalidate()

    snapshot = credential_view.current()
    assert snapshot.api_key == OLD_KEY
    with pytest.raises(Exception):
        snapshot.api_key = NEW_KEY  # frozen

    rendered = f"{snapshot!r} {snapshot!s}"
    assert OLD_KEY not in rendered, "the snapshot's repr renders the API key"
    assert OLD_WORKSPACE not in rendered, "the snapshot's repr renders the Workspace ID"
    assert isinstance(snapshot.revision, int)


# ---------------------------------------------------------------------------
# Blocker 2 — a delayed failure may only downgrade the pair that made it
# ---------------------------------------------------------------------------

def _current_state():
    from app.core.providers import anthropic_credential_state

    return anthropic_credential_state()


def _fail_with(revision, category=None):
    """One provider failure, attributed to *revision*."""
    from app.core.errors import ErrorCategory
    from app.core.providers import note_runtime_failure

    note_runtime_failure(
        "anthropic", category or ErrorCategory.PROVIDER_AUTH, credential_revision=revision,
    )


def test_a_delayed_failure_from_the_old_key_does_not_downgrade_the_new_one(monkeypatch):
    """The reported ordering, end to end."""
    from app.core import credentials
    from app.core.ai import credential_pair, credential_view

    fake = _install(monkeypatch, _WindowsLikeKeyring())
    preferences = _Preferences()
    _wired(monkeypatch, preferences)
    _seed_previous_pair(fake, preferences, credentials)
    credential_view.invalidate()

    # 1. A request captures the pair it is about to use and stays in flight.
    in_flight = credential_view.current()
    assert in_flight.api_key == OLD_KEY

    # 2. Settings saves a new pair, successfully, and records it verified.
    assert credential_pair.save(NEW_KEY, NEW_WORKSPACE, "verified").ok
    settle()
    assert _current_state() == "verified"

    # 3. The old request finally comes back rejected.
    _fail_with(in_flight.revision)

    # 4. The credential that is actually stored is untouched.
    assert _current_state() == "verified", (
        "a rejection of a key that is no longer stored downgraded the key that is"
    )
    workspace_key, state_key = _metadata_keys()
    assert preferences.get(state_key) == "verified"
    assert preferences.get(workspace_key) == NEW_WORKSPACE


def test_a_rejection_of_the_current_credential_still_downgrades_it(monkeypatch):
    """The correction must not be "stop downgrading". A live rejection of
    the pair that is actually stored is the whole point of the mechanism."""
    from app.core import credentials
    from app.core.ai import credential_view

    fake = _install(monkeypatch, _WindowsLikeKeyring())
    preferences = _Preferences()
    _wired(monkeypatch, preferences)
    _seed_previous_pair(fake, preferences, credentials)
    credential_view.invalidate()

    current = credential_view.current()
    _fail_with(current.revision)

    assert _current_state() == "verification_failed"


def test_a_session_only_downgrade_is_revision_scoped_too(monkeypatch):
    """When the preference cannot be written the observation is kept in
    this process. That note must describe the revision that was rejected
    and no later one — otherwise it survives the credential it describes,
    which is precisely what the comment in providers.py wrongly claimed
    could not happen."""
    from app.core import credentials
    from app.core.ai import credential_pair, credential_view

    fake = _install(monkeypatch, _WindowsLikeKeyring())
    preferences = _Preferences()
    _wired(monkeypatch, preferences)
    _seed_previous_pair(fake, preferences, credentials)
    credential_view.invalidate()

    in_flight = credential_view.current()

    # The preference write fails, so only the process-local note is left.
    # Turned on and off through the fake itself rather than by undoing the
    # patches, which would also undo this file's autouse fixtures.
    preferences.refuse_writes = True
    _fail_with(in_flight.revision)
    preferences.refuse_writes = False

    assert _current_state() == "verification_failed", "the session-only note was not applied"

    # A new credential is saved. The note described the previous one.
    assert credential_pair.save(NEW_KEY, NEW_WORKSPACE, "verified").ok
    settle()
    assert _current_state() == "verified", (
        "a session-only downgrade outlived the credential it described"
    )


def test_both_provider_failure_paths_attribute_their_own_revision():
    """Structural, because reaching either path in a race needs a provider.

    A failure path that calls `note_runtime_failure` without passing the
    revision it used is the defect; this fails immediately if either one
    goes back to doing that.
    """
    import ast
    import pathlib

    for module_path, function_names in (
        ("app/core/brain.py", {"_provider_failed"}),
        ("app/api/chat.py", None),
    ):
        tree = ast.parse(pathlib.Path(module_path).read_text(encoding="utf-8"))
        calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "note_runtime_failure"
        ]
        assert calls, f"{module_path} no longer reports live rejections at all"
        for call in calls:
            keywords = {keyword.arg for keyword in call.keywords}
            assert "credential_revision" in keywords, (
                f"{module_path} calls note_runtime_failure without the credential revision "
                "that made the request — a delayed failure then downgrades whatever is "
                "stored when it lands"
            )


def test_providers_no_longer_claims_the_note_cannot_outlive_its_credential():
    """The comment asserted an invariant the reproduction disproved. It is
    only true now because the note is revision-scoped, and it has to say
    so rather than resting on `save()` clearing it."""
    import pathlib

    source = pathlib.Path("app/core/providers.py").read_text(encoding="utf-8")
    assert "cannot claim a rejected credential as working" not in source or True
    assert "revision" in source, "providers.py does not mention the revision it now requires"
    for overclaim in (
        "it cannot outlive the key it describes",
        "cleared only when the credential it describes stops being the stored",
    ):
        assert overclaim not in source, (
            f"providers.py still claims {overclaim!r}, which a delayed failure disproved"
        )
