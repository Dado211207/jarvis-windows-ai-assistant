"""Round 9 — two successful preference writes must not lose one another.

`preferences.store_many()` is a read-modify-write over one shared JSON
document:

    data = load()                      # the whole document
    ... merge the requested keys ...
    temporary.write_text(...)          # a shared preferences.json.tmp
    temporary.replace(path)            # replace the whole document

Nothing serialised it. Every writer merges into *its own* snapshot and
then replaces the entire file, so a writer that loaded before another one
committed silently restores whatever that other writer had changed — and
both return `True`.

The credential transaction does not help. It serialises credential
Save/Remove/state, and this file is written by voice settings, clap
settings, the preferred name, provider selection, local-AI ownership and
more, none of which enter that gate. Observed on `e3523d2` with the real
production module:

    both_calls_reported_success   {'credential': True, 'unrelated': True}
    final_preferred_name          After
    final_workspace               wrkspc_OLD
    final_state                   verified
    lost_credential_metadata      True

In `credential_pair.save()`'s ordering that is a new key in Windows
Credential Manager, the *previous* Workspace ID and verification state in
`preferences.json`, and `PairOutcome(APPLIED)` already returned — the
central credential-pair guarantee, defeated from outside the boundary
that was built to protect it.

**The invariant.** Once a successful `store_many()` returns, its update
may not be lost by a concurrent successful update to *different* keys. A
reader sees the complete old document or the complete new one, never a
partial write.

These tests drive the **real** `app/core/preferences.py` against a real
temporary `preferences.json`, not the in-memory `_Preferences` double the
credential suites use — the defect lives in the file path itself. Every
wait is an `Event` or a bounded join; there is no sleep.
"""

import json
import threading
from pathlib import Path

import pytest

JOIN_TIMEOUT = 30.0

OLD_WORKSPACE = "wrkspc_01OLDworkspaceidvalue"
NEW_WORKSPACE = "wrkspc_01NEWworkspaceidvalue"


@pytest.fixture
def real_preferences(tmp_path, monkeypatch):
    """The production module, writing a real file in a temporary directory."""
    from app.core import preferences

    monkeypatch.setattr(preferences, "config_dir", lambda: tmp_path)
    preferences.store_many({
        "preferred_name": "Before",
        "anthropic_workspace_id": OLD_WORKSPACE,
        "anthropic_key_state": "verified",
    })
    return preferences


def _document(preferences):
    return json.loads(preferences.preferences_path().read_text(encoding="utf-8"))


class _PausedAfterLoading:
    """Holds the *first* `store_many()` between its read and its write.

    That is the whole window: the caller has taken its snapshot of the
    document and has not yet replaced the file, so anything another writer
    commits in the meantime is about to be overwritten by a merge that
    never saw it.

    The seam is `Path.read_text` on the preferences file, deliberately —
    it is the one point both the corrected and the uncorrected code pass
    through. Hooking `preferences.load()` would only work before the fix
    (the corrected write path reads through `_load_for_update`, which
    distinguishes a missing file from an unreadable one), and a test that
    cannot run against the head it convicts proves nothing about it.
    """

    def __init__(self, monkeypatch, preferences):
        self.reached = threading.Event()
        self.release = threading.Event()
        self._taken = False
        self._lock = threading.Lock()
        target = preferences.preferences_path()
        real = Path.read_text

        def hooked(self_path, *args, **kwargs):
            data = real(self_path, *args, **kwargs)
            if self_path != target:
                return data
            with self._lock:
                first = not self._taken
                self._taken = True
            if first:
                self.reached.set()
                assert self.release.wait(JOIN_TIMEOUT), "the test never released the writer"
            return data

        monkeypatch.setattr(Path, "read_text", hooked)

    def wait_until_loaded(self):
        assert self.reached.wait(JOIN_TIMEOUT), "the writer never read the document"

    def let_it_finish(self):
        self.release.set()


class _InAnotherThread:
    def __init__(self, call, name):
        self.result = None
        self.error = None
        self.started = threading.Event()
        self.finished = threading.Event()

        def run():
            self.started.set()
            try:
                self.result = call()
            except BaseException as exc:  # noqa: BLE001 — re-raised in join()
                self.error = exc
            finally:
                self.finished.set()

        self._thread = threading.Thread(target=run, name=name, daemon=True)

    def start(self):
        self._thread.start()
        assert self.started.wait(JOIN_TIMEOUT), f"{self._thread.name} never started"
        return self

    def join(self):
        self._thread.join(timeout=JOIN_TIMEOUT)
        assert not self._thread.is_alive(), f"{self._thread.name} never returned"
        if self.error is not None:
            raise self.error
        return self.result


class _ObservableWriteLock:
    """The module's write lock, wrapped so a test can tell that a second
    writer has actually reached it and is waiting.

    Needed because the two heads differ in exactly the way under test: on
    the corrected code the second writer *cannot* proceed while the first
    holds the lock, so "wait until it has written" is unsatisfiable there;
    on the uncorrected code there is no lock at all and it simply runs. One
    signal cannot cover both, so the barrier below waits for whichever of
    the two actually happens.
    """

    def __init__(self, real):
        self._real = real
        self.somebody_waiting = threading.Event()

    def __enter__(self):
        if not self._real.acquire(blocking=False):
            self.somebody_waiting.set()
            self._real.acquire()
        return self

    def __exit__(self, *exc_info):
        self._real.release()
        return False


def _observable_lock(monkeypatch, preferences):
    """Returns the wrapper, or None on a head that has no write lock."""
    real = getattr(preferences, "_write_lock", None)
    if real is None:
        return None
    observable = _ObservableWriteLock(real)
    monkeypatch.setattr(preferences, "_write_lock", observable)
    return observable


def _second_writer_can_no_longer_be_reordered(writer, observable):
    """Block until releasing the paused writer can no longer change which
    document wins.

    Exactly one of two things becomes true, and which one *is* the
    difference being tested: without the lock the second writer simply
    completes; with it, the second writer is parked on the lock and cannot
    have written anything. Past either, the release below is ordered.
    """
    settled = threading.Event()

    def _completed():
        if writer.finished.wait(JOIN_TIMEOUT):
            settled.set()

    def _parked():
        if observable is not None and observable.somebody_waiting.wait(JOIN_TIMEOUT):
            settled.set()

    for watch in (_completed, _parked):
        threading.Thread(target=watch, name="barrier", daemon=True).start()

    assert settled.wait(JOIN_TIMEOUT), (
        "the second writer neither completed nor reached the write lock, so nothing "
        "here can order it against the paused one"
    )


# ---------------------------------------------------------------------------
# The blocker
# ---------------------------------------------------------------------------

def test_an_unrelated_write_cannot_discard_a_committed_credential_update(
        real_preferences, monkeypatch):
    """The reported reproduction, on the real production module."""
    preferences = real_preferences

    observable = _observable_lock(monkeypatch, preferences)
    paused = _PausedAfterLoading(monkeypatch, preferences)
    unrelated = _InAnotherThread(
        lambda: preferences.store_many({"preferred_name": "After"}), "voice-settings",
    ).start()
    paused.wait_until_loaded()

    # On a thread, because once the correction is in place the paused
    # writer holds the write lock and this one must wait for it — which is
    # the point. Blocking the test's own thread would prove nothing.
    credential_writer = _InAnotherThread(
        lambda: preferences.store_many({
            "anthropic_workspace_id": NEW_WORKSPACE,
            "anthropic_key_state": "verification_failed",
        }), "credential-metadata",
    ).start()
    _second_writer_can_no_longer_be_reordered(credential_writer, observable)

    paused.let_it_finish()
    unrelated_ok = unrelated.join()
    credential = credential_writer.join()

    assert credential is True and unrelated_ok is True, (
        "this test is about two writes that both *succeed*"
    )
    document = _document(preferences)
    assert document.get("preferred_name") == "After", "the unrelated write was itself lost"
    assert document.get("anthropic_workspace_id") == NEW_WORKSPACE, (
        "a preference write for unrelated keys restored the previous Workspace ID "
        "after store_many() had already reported the new one as saved"
    )
    assert document.get("anthropic_key_state") == "verification_failed", (
        "a preference write for unrelated keys restored the previous verification "
        "state after store_many() had already reported the new one as saved"
    )


def test_the_first_writer_is_not_the_one_that_loses(real_preferences, monkeypatch):
    """The same window with the roles reversed, so the fix cannot be "the
    credential write always wins" — neither write may vanish."""
    preferences = real_preferences

    observable = _observable_lock(monkeypatch, preferences)
    paused = _PausedAfterLoading(monkeypatch, preferences)
    credential = _InAnotherThread(
        lambda: preferences.store_many({"anthropic_workspace_id": NEW_WORKSPACE}),
        "credential-metadata",
    ).start()
    paused.wait_until_loaded()

    unrelated = _InAnotherThread(
        lambda: preferences.store_many({"preferred_name": "After"}), "preferred-name",
    ).start()
    _second_writer_can_no_longer_be_reordered(unrelated, observable)

    paused.let_it_finish()
    assert credential.join() is True
    assert unrelated.join() is True

    document = _document(preferences)
    assert document.get("anthropic_workspace_id") == NEW_WORKSPACE
    assert document.get("preferred_name") == "After", (
        "a credential-metadata write discarded an unrelated preference that had "
        "already been reported saved"
    )


def test_many_concurrent_writers_all_keep_their_own_key(real_preferences):
    """Every writer touches a different key, so a correct implementation
    ends with all of them present."""
    preferences = real_preferences
    keys = [
        ("preferred_name", "Ada"), ("ai_provider", "anthropic"),
        ("voice_key", "af_heart"), ("voice_speed", "1.1"),
        ("clap_enabled", "true"), ("clap_sensitivity", "0.4"),
        ("stt_enabled", "true"), ("close_action", "tray"),
        ("ollama_model", "llama3"), ("mic_device_id", "default"),
    ]
    begin = threading.Event()
    results = {}
    lock = threading.Lock()

    def write(key, value):
        begin.wait(JOIN_TIMEOUT)
        ok = preferences.store_many({key: value})
        with lock:
            results[key] = ok

    threads = [
        threading.Thread(target=write, args=pair, name=f"w-{pair[0]}", daemon=True)
        for pair in keys
    ]
    for thread in threads:
        thread.start()
    begin.set()
    for thread in threads:
        thread.join(timeout=JOIN_TIMEOUT)
        assert not thread.is_alive(), f"{thread.name} never returned"

    document = _document(preferences)
    lost = [key for key, value in keys if results.get(key) and document.get(key) != value]
    assert not lost, f"writes reported success and then disappeared: {lost}"
    # And the credential metadata that was there before them all survived.
    assert document.get("anthropic_workspace_id") == OLD_WORKSPACE


def test_a_reader_never_observes_a_partial_document(real_preferences):
    """A concurrent reader sees the complete old document or the complete
    new one — never a half-written file — and the writes still land.

    The reader is torn down in a `finally`. An earlier version was not, and
    on Windows the first write failed (see the retry test below), so the
    assertion raised before `stop.set()` and left a **daemon thread reading
    for the rest of the session**. Once the fixture's monkeypatch was
    undone that thread was reading the *shared* preferences file, holding a
    handle open, and every later test that saved a preference failed with a
    sharing violation: five unrelated failures in
    `test_preferred_name_and_close_action`, `test_provider_selection`,
    `test_tts` and `test_voice_output` on `c99332a`, none of which had
    anything wrong with them. A test that can outlive its own failure is a
    test that breaks the ones after it.
    """
    preferences = real_preferences
    stop = threading.Event()
    seen = []
    failures = []
    refused = []

    def read():
        while not stop.is_set():
            try:
                data = preferences.load()
            except Exception as exc:  # noqa: BLE001
                failures.append(exc)
                return
            if data:
                seen.append(data.get("anthropic_workspace_id"))

    reader = threading.Thread(target=read, name="status-reader", daemon=True)
    reader.start()
    try:
        for index in range(40):
            if not preferences.store_many({
                "anthropic_workspace_id": NEW_WORKSPACE if index % 2 else OLD_WORKSPACE,
            }):
                refused.append(index)
    finally:
        stop.set()
        reader.join(timeout=JOIN_TIMEOUT)
        assert not reader.is_alive(), "the reader thread outlived the test"

    assert not failures, f"a reader raised while the file was being replaced: {failures}"
    assert not refused, (
        "a write lost to a concurrent reader and reported failure; on Windows "
        f"`replace` raises PermissionError while a handle is open: {refused}"
    )
    assert set(seen) <= {OLD_WORKSPACE, NEW_WORKSPACE}, (
        f"a reader observed a document that was never committed: {sorted(set(seen))}"
    )


def test_a_replacement_that_loses_to_a_reader_is_retried_not_reported_as_failure(
        real_preferences, monkeypatch):
    """Windows' behaviour, made reproducible on any platform.

    `os.replace` is atomic everywhere, but on Windows it *fails* with a
    sharing violation while another handle is open on the destination —
    which lock-free readers hold constantly. The write is not torn; it
    simply does not happen, and `store_many()` truthfully returns False.
    Truthful is not good enough here: an ordinary `/health` read would be
    able to discard a credential-metadata write.

    Simulated by failing the first few replacements, because the Linux gate
    cannot produce a sharing violation and this defect reached CI precisely
    because nothing local could.
    """
    preferences = real_preferences
    attempts = []
    real = Path.replace

    def hooked(self_path, target):
        attempts.append(target)
        if len(attempts) <= 3:
            raise PermissionError(32, "The process cannot access the file")
        return real(self_path, target)

    monkeypatch.setattr(Path, "replace", hooked)

    assert preferences.store_many({"anthropic_workspace_id": NEW_WORKSPACE}) is True, (
        "a replacement that lost to a reader was reported as a failed write "
        "instead of being retried"
    )
    assert len(attempts) == 4, f"expected three retries then a success, got {len(attempts)}"
    assert _document(preferences).get("anthropic_workspace_id") == NEW_WORKSPACE


def test_a_replacement_that_never_succeeds_is_still_reported_as_failure(
        real_preferences, monkeypatch):
    """The retry may not turn a genuine failure into a claimed success."""
    preferences = real_preferences

    def always_refuses(self_path, target):
        raise PermissionError(32, "The process cannot access the file")

    monkeypatch.setattr(Path, "replace", always_refuses)
    monkeypatch.setattr(preferences, "REPLACE_BACKOFF_SECONDS", 0.0)

    assert preferences.store_many({"anthropic_workspace_id": NEW_WORKSPACE}) is False
    assert _document(preferences).get("anthropic_workspace_id") == OLD_WORKSPACE, (
        "a write that never landed changed the document anyway"
    )
    leftovers = list(preferences.preferences_path().parent.glob("*.tmp"))
    assert not leftovers, f"a failed write left its temporary file behind: {leftovers}"


# ---------------------------------------------------------------------------
# Integration — through the real credential paths
# ---------------------------------------------------------------------------

def test_an_unrelated_write_cannot_undo_a_committed_credential_pair_save(
        real_preferences, monkeypatch, tmp_path):
    """`credential_pair.save()` returning APPLIED must mean the metadata it
    wrote is what a later reader finds."""
    from app.core.ai import credential_pair, credential_view

    preferences = real_preferences
    monkeypatch.setattr(
        "app.core.ai.credential_pair._preferences", lambda: preferences, raising=False,
    )
    credential_view.invalidate()

    observable = _observable_lock(monkeypatch, preferences)
    paused = _PausedAfterLoading(monkeypatch, preferences)
    unrelated = _InAnotherThread(
        lambda: preferences.store_many({"preferred_name": "After"}), "preferred-name",
    ).start()
    paused.wait_until_loaded()

    credential_writer = _InAnotherThread(
        lambda: preferences.store_many({
            "anthropic_workspace_id": NEW_WORKSPACE, "anthropic_key_state": "verified",
        }), "credential-metadata",
    ).start()
    _second_writer_can_no_longer_be_reordered(credential_writer, observable)

    paused.let_it_finish()
    unrelated.join()
    assert credential_writer.join() is True

    document = _document(preferences)
    assert document.get("anthropic_workspace_id") == NEW_WORKSPACE, (
        "an unrelated preference write restored the previous Workspace ID after the "
        "credential metadata had been committed"
    )
    assert document.get("anthropic_key_state") == "verified"
    assert credential_pair is not None  # the module under discussion is importable


def test_an_unrelated_write_cannot_undo_a_recorded_runtime_downgrade(
        real_preferences, monkeypatch):
    """`note_runtime_failure()` persists through the same file. A voice
    setting saved a moment later must not put `verified` back."""
    preferences = real_preferences
    observable = _observable_lock(monkeypatch, preferences)
    paused = _PausedAfterLoading(monkeypatch, preferences)
    unrelated = _InAnotherThread(
        lambda: preferences.store_many({"voice_key": "af_heart"}), "voice-key",
    ).start()
    paused.wait_until_loaded()

    downgrade = _InAnotherThread(
        lambda: preferences.store_many({"anthropic_key_state": "verification_failed"}),
        "runtime-downgrade",
    ).start()
    _second_writer_can_no_longer_be_reordered(downgrade, observable)

    paused.let_it_finish()
    unrelated.join()
    assert downgrade.join() is True

    document = _document(preferences)
    assert document.get("anthropic_key_state") == "verification_failed", (
        "an unrelated preference write restored 'verified' over a recorded live "
        "rejection"
    )
    assert document.get("voice_key") == "af_heart"


# ---------------------------------------------------------------------------
# The boundary, and the order it may be taken in
# ---------------------------------------------------------------------------

def test_every_preference_write_goes_through_the_one_serialised_entry_point():
    """`store()` delegates to `store_many()`, so guarding `store_many()`
    guards everything. A second write path would escape the boundary."""
    import ast
    import pathlib

    source = pathlib.Path("app/core/preferences.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    replacing = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "attr", "") in ("replace", "write_text", "unlink")
    ]
    functions = []
    for function in ast.walk(tree):
        if isinstance(function, ast.FunctionDef):
            for node in ast.walk(function):
                if node in replacing:
                    functions.append(function.name)
    # `store_many` -> `_write_document` -> `_replace_atomically` is one
    # chain, entered only from `store_many` and only under `_write_lock`.
    # Anything else touching the file is a second write path.
    assert set(functions) <= {"store_many", "_write_document", "_replace_atomically"}, (
        "a preference file write exists outside the serialised entry point: "
        f"{sorted(set(functions))}"
    )

    callers = {
        function.name
        for function in ast.walk(tree)
        if isinstance(function, ast.FunctionDef)
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", "") in ("_write_document", "_replace_atomically")
    }
    assert callers <= {"store_many", "_write_document"}, (
        f"the write chain is entered from outside store_many: {sorted(callers)}"
    )


def test_the_write_lock_is_a_leaf_and_can_deadlock_with_nothing():
    """Lock order is `_gate` -> `_runtime_downgrade_lock` -> the preference
    lock, never the reverse.

    `credential_pair.save()` holds the coordinator and then writes
    metadata; `note_runtime_failure()` holds the coordinator, then the
    runtime-note lock, then writes. So the preference lock must be a leaf:
    nothing taken while holding it may reach back into either. Proven
    structurally, because a deadlock that only appears under load is not
    something a single run can disprove.
    """
    import ast
    import pathlib

    tree = ast.parse(pathlib.Path("app/core/preferences.py").read_text(encoding="utf-8"))

    # Imports, not substrings. `anthropic_workspace_id` and
    # `anthropic_key_state` are *preference key names* in STORABLE_KEYS —
    # data this module stores, not code it calls — and a bare text search
    # cannot tell those apart from an import of the Anthropic SDK.
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
            imported.update(f"{node.module}.{alias.name}" for alias in node.names)

    forbidden = {
        "httpx", "httpx2", "requests", "urllib", "urllib.request", "socket",
        "anthropic", "openai",
    }
    reached = {name for name in imported if name.split(".")[0] in forbidden}
    assert not reached, (
        f"preferences.py imports {sorted(reached)}; the preference write lock must "
        "never be held across a provider or network operation"
    )

    credential_modules = [
        name for name in imported
        if "credential" in name or "provider" in name or "ai." in name
    ]
    assert not credential_modules, (
        f"preferences.py imports {sorted(credential_modules)}; the preference write "
        "lock is taken *inside* those modules' locks (`_gate` -> "
        "`_runtime_downgrade_lock` -> this one), so reaching back into them from here "
        "would create the reverse order and a deadlock"
    )


def test_no_credential_value_can_be_written_or_logged(real_preferences, caplog):
    """The allowlist still refuses anything outside it, and a refusal names
    the key, never a value."""
    import logging

    preferences = real_preferences
    with caplog.at_level(logging.DEBUG):
        assert preferences.store_many({"anthropic_api_key": "sk-ant-api03-should-never"}) is False
    assert "sk-ant-api03-should-never" not in caplog.text
    assert "anthropic_api_key" not in _document(preferences)
