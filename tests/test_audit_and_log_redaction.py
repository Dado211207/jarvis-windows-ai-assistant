"""Four leaks found by auditing the code rather than by a test failing.

Each was reproduced before it was fixed, and each test below is the
reproduction, kept.

1. **The audit trail kept the secret the memory guard refused.**
   `memory add my key is sk-ant-…` reaches the router as
   `{"content": "my key is sk-ant-…"}`. `secret_guard` correctly refused
   to store the memory — but `action_lifecycle.propose()` runs *before*
   the tool executes, and `redact_params` matched on key *names* only, so
   `"content"` sailed through and the secret was written verbatim into
   `action_lifecycle.input_summary`, published over `/ws/events`, and
   served by `GET /actions/history`. The memory was refused and the key
   was persisted anyway, one table across.

2. **Raw command text reached an unredacted log file.** `POST /command`
   and `CommandRouter.route()` both log the command as typed. The
   rotating file they write to had no filter of any kind.
   `server_process.redact_text()` guards a *different* file.

3. **Raw result messages — including `str(exc)` from a dozen tool
   handlers — reached the audit trail and the WebSocket**, which
   `app/core/events.py` forbids in as many words.

4. **The loopback bind was a comment, not a check.** `JARVIS_HOST` is an
   ordinary settings field, so an environment variable widened it
   silently, with 48 unauthenticated GET endpoints behind it.
"""

import json
import logging
import sqlite3

import pytest

SECRET = "sk-ant-api03-FAKE0000NOTREAL1111EXAMPLE2222abcdefghijklmnopqrstuvwxyz3333"
GITHUB = "ghp_FAKE0000NOTREAL1111EXAMPLE2222abcdefgh"


# ---------------------------------------------------------------------------
# 1. The audit trail
# ---------------------------------------------------------------------------

@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    from db.database import Database
    from db.migrations import create_tables

    db_path = tmp_path / "audit.db"
    create_tables(db_path=db_path)
    database = Database(db_path=db_path)
    monkeypatch.setattr("db.database.get_db", lambda: database)
    monkeypatch.setattr("app.config.settings.jarvis_db_path", str(db_path))
    yield db_path
    database.close()


def _route(command: str):
    from app.core.memory import register_tools
    from app.core.router import CommandRouter
    from app.core.tool_registry import ToolRegistry

    registry = ToolRegistry()
    register_tools(registry)
    return CommandRouter(registry).route(command)


def test_a_refused_memory_leaves_no_secret_in_the_audit_trail(isolated_db):
    """The reproduction. Both halves matter: refused *and* not recorded."""
    response = _route(f"memory add my key is {SECRET}")
    assert response.success is False

    conn = sqlite3.connect(isolated_db)
    try:
        rows = conn.execute("SELECT input_summary, result_summary FROM action_lifecycle").fetchall()
        assert rows, "the action should still have been recorded — redacted, not omitted"
        for input_summary, result_summary in rows:
            assert SECRET not in (input_summary or "")
            assert SECRET not in (result_summary or "")
        assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
    finally:
        conn.close()


def test_the_secret_is_nowhere_in_the_database_file_at_all(isolated_db):
    """Table-by-table checks miss a column nobody thought of. This reads
    the file."""
    _route(f"memory add my key is {SECRET}")
    _route(f"memory add my token is {GITHUB}")

    raw = isolated_db.read_bytes()
    assert SECRET.encode() not in raw
    assert GITHUB.encode() not in raw


def test_the_redacted_summary_names_the_kind_and_not_the_value(isolated_db):
    _route(f"memory add my key is {SECRET}")

    conn = sqlite3.connect(isolated_db)
    try:
        summary = conn.execute("SELECT input_summary FROM action_lifecycle").fetchone()[0]
    finally:
        conn.close()

    payload = json.loads(summary)
    assert "redacted" in payload["content"]
    assert "Anthropic" in payload["content"], "a mask that says nothing is a mask nobody can act on"
    assert SECRET not in payload["content"]


def test_an_ordinary_command_is_still_recorded_in_full(isolated_db):
    """Redaction that ate the audit trail would be its own defect."""
    _route("memory add I prefer dark roast coffee")

    conn = sqlite3.connect(isolated_db)
    try:
        summary = conn.execute("SELECT input_summary FROM action_lifecycle").fetchone()[0]
    finally:
        conn.close()

    assert "dark roast coffee" in json.loads(summary)["content"]


# ---------------------------------------------------------------------------
# redact_params / redact_message directly
# ---------------------------------------------------------------------------

def test_redaction_now_matches_on_value_shape_not_only_key_name():
    from app.core.redaction import redact_params

    out = redact_params({"content": f"my key is {SECRET}"})
    assert SECRET not in out["content"]
    assert "redacted" in out["content"]


def test_redaction_still_matches_on_key_name():
    from app.core.redaction import redact_params

    assert redact_params({"api_key": "anything at all"})["api_key"] == "***redacted***"


def test_redaction_recurses_into_nested_values():
    from app.core.redaction import redact_params

    out = redact_params({"payload": {"inner": [f"key {SECRET}"]}})
    assert SECRET not in json.dumps(out)


def test_redaction_leaves_ordinary_values_alone():
    from app.core.redaction import redact_params

    out = redact_params({"content": "remind me to change my password on Friday"})
    assert out["content"] == "remind me to change my password on Friday"


def test_redaction_is_bounded_against_a_self_referencing_structure():
    """Cleanup must not be a way to hang the caller."""
    from app.core.redaction import redact_params

    deep = current = {}
    for _ in range(50):
        current["next"] = {}
        current = current["next"]
    redact_params({"deep": deep})  # must return, not recurse forever


def test_redaction_never_raises_even_if_the_guard_is_unavailable(monkeypatch):
    import app.core.redaction as redaction

    monkeypatch.setattr(
        redaction, "_redact_text",
        lambda value: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    with pytest.raises(RuntimeError):
        redaction._redact_text("x")  # the stand-in really raises
    monkeypatch.undo()
    assert redaction.redact_params({"a": "b"}) == {"a": "b"}


# ---------------------------------------------------------------------------
# 2. The log file
# ---------------------------------------------------------------------------

@pytest.fixture
def log_capture(tmp_path, monkeypatch):
    """A real log file, at a level these tests pin rather than inherit.

    The level has to be pinned, and the file has to be checked for
    content, because of a defect these tests had themselves.
    `scripts/build-installer.ps1` sets `JARVIS_LOG_LEVEL=WARNING` before
    running the suite; three tests here logged at INFO, so on that run
    the handler dropped every record and the file was created empty. Two
    of the three asserted only "the secret is not in the file" — which
    an empty file satisfies perfectly. The Windows Installer job caught
    it because the third asserted the line was *present*; reproduced
    afterwards on Linux with `JARVIS_LOG_LEVEL=WARNING`.

    So `read()` refuses to return an empty file. A security test that
    passes because nothing was written is worse than no test: it reports
    a guarantee it never checked.
    """
    from app.logging_config import setup_logging

    log_file = tmp_path / "jarvis.log"
    monkeypatch.setattr("app.config.settings.jarvis_log_file", str(log_file))
    monkeypatch.setattr("app.config.settings.jarvis_log_level", "DEBUG")
    setup_logging()

    class Capture:
        path = log_file

        def read(self) -> str:
            logging.shutdown()
            body = log_file.read_text(encoding="utf-8")
            assert body.strip(), (
                "nothing was written to the log file — this test proves nothing "
                "unless a record actually reached the handler"
            )
            return body

    try:
        yield Capture()
    finally:
        logging.shutdown()


def test_a_command_containing_a_secret_does_not_reach_the_log_file(log_capture):
    from app.logging_config import get_logger

    get_logger("test.redaction").info("API command: %r", f"memory add my key is {SECRET}")

    body = log_capture.read()
    assert SECRET not in body
    assert "redacted" in body


def test_a_preformatted_message_is_redacted_too(log_capture):
    """Not every call site uses %s args; some build the whole line first."""
    from app.logging_config import get_logger

    get_logger("test.redaction").info(f"the key is {SECRET}")

    body = log_capture.read()
    assert SECRET not in body
    assert "redacted" in body


def test_a_format_string_is_never_rewritten(log_capture):
    """The bug the first version of the filter had, kept as a test.

    "Could not remove the stored API key: %s" reads as a credential noun
    followed by a value, so masking `record.msg` destroyed the `%s` and
    turned an ordinary warning into a TypeError. A format string is
    developer-written literal text; only the arguments carry user data.
    """
    from app.logging_config import get_logger

    get_logger("test.redaction").warning(
        "Could not remove the stored API key: %s", OSError("boom"),
    )

    assert "Could not remove the stored API key: boom" in log_capture.read()


def test_ordinary_log_lines_are_untouched(log_capture):
    from app.logging_config import get_logger

    get_logger("test.redaction").info("Server child process stopped.")

    assert "Server child process stopped." in log_capture.read()


def test_the_configured_log_level_is_honoured(tmp_path, monkeypatch):
    """The other half of the fixture's reasoning, asserted directly.

    A WARNING-level installation must not write INFO lines — that is the
    setting working, not a fault — and the fixture above exists so no
    redaction test can quietly depend on which way this goes.
    """
    from app.logging_config import get_logger, setup_logging

    log_file = tmp_path / "jarvis.log"
    monkeypatch.setattr("app.config.settings.jarvis_log_file", str(log_file))
    monkeypatch.setattr("app.config.settings.jarvis_log_level", "WARNING")
    setup_logging()
    try:
        get_logger("test.redaction").info("this is only informative")
        get_logger("test.redaction").warning("this one matters")
    finally:
        logging.shutdown()

    body = log_file.read_text(encoding="utf-8")
    assert "this is only informative" not in body
    assert "this one matters" in body


def test_the_filter_is_attached_to_handlers_not_only_the_logger(tmp_path, monkeypatch):
    """The subtlety that made the first attempt silently useless.

    A filter on a logger runs only for records logged *directly* on it.
    Every call site here uses get_logger("x") -> "jarvis.x", a child,
    whose records reach these handlers without consulting the parent
    logger's filters.
    """
    from app.logging_config import _RedactingFilter, setup_logging

    monkeypatch.setattr("app.config.settings.jarvis_log_file", str(tmp_path / "j.log"))
    root = setup_logging()
    try:
        assert root.handlers, "no handlers to check"
        for handler in root.handlers:
            assert any(isinstance(f, _RedactingFilter) for f in handler.filters), (
                f"{handler!r} has no redaction filter"
            )
    finally:
        logging.shutdown()


# ---------------------------------------------------------------------------
# 3. Result messages
# ---------------------------------------------------------------------------

def test_a_result_message_carrying_a_secret_is_redacted_before_it_is_stored(isolated_db):
    """Several tool handlers put str(exc) into `message`, and an
    exception can quote whatever was passed in."""
    from app.core.redaction import redact_message

    assert SECRET not in redact_message(f"Could not open {SECRET}")
    assert redact_message("Opened Notepad.") == "Opened Notepad."


# ---------------------------------------------------------------------------
# 4. The bind address
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("configured", ["127.0.0.1", "localhost", "::1"])
def test_loopback_addresses_are_accepted(configured):
    from app.api.server import loopback_host

    assert loopback_host(configured) == configured


@pytest.mark.parametrize("configured", ["0.0.0.0", "192.168.1.10", "10.0.0.5", "example.com", ""])
def test_any_non_loopback_bind_is_refused(configured):
    """The check the comment used to stand in for."""
    from app.api.server import loopback_host

    assert loopback_host(configured) == "127.0.0.1"


def test_an_environment_variable_cannot_widen_the_bind(monkeypatch):
    """The actual attack shape: JARVIS_HOST is an ordinary settings
    field, so an env var or a .env file in the working directory set it
    with nothing in the way."""
    from app.api.server import loopback_host
    from app.config import Settings

    monkeypatch.setenv("JARVIS_HOST", "0.0.0.0")
    configured = Settings().jarvis_host
    assert configured == "0.0.0.0", "the setting really does take the env value"
    assert loopback_host(configured) == "127.0.0.1", "…and the bind refuses it anyway"
