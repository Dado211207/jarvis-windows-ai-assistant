"""Tests for app/core/diagnostics.py.

The report exists to be copied and pasted somewhere public, so the
central tests here plant real secret-shaped values in every place the
report reads from and assert none of them survive into the output. The
allowlist design is also tested directly: adding a secret-bearing
setting must not leak it, which is the property a denylist would quietly
lose.
"""

import pytest

from app.core import diagnostics


def _text() -> str:
    return diagnostics.render_report_text()


# ---------------------------------------------------------------------------
# The report is built at all, and stays useful
# ---------------------------------------------------------------------------

def test_report_contains_the_expected_sections():
    titles = {section.title for section in diagnostics.build_report()}
    for expected in ("Application", "System", "Runtime", "Local API", "Database", "AI providers", "Voice", "Locations"):
        assert expected in titles


def test_report_includes_the_version():
    from app import __version__
    assert __version__ in _text()


def test_report_states_the_api_is_loopback_only():
    assert "loopback only" in _text()


def test_report_includes_log_and_database_locations():
    """The two things support actually needs to ask for."""
    text = _text()
    assert "Logs:" in text
    assert "Path:" in text


def test_report_warns_that_paths_contain_a_username():
    """The one piece of personal data present is disclosed rather than
    glossed over."""
    assert "user name" in _text()


# ---------------------------------------------------------------------------
# Secrets must never appear — the property this module exists for
# ---------------------------------------------------------------------------

def test_configured_api_key_never_appears(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(type(settings), "effective_api_key", property(lambda self: "sk-live-abcdef1234567890"))
    monkeypatch.setattr(type(settings), "has_anthropic_key", property(lambda self: True))

    text = _text()

    assert "sk-live-abcdef1234567890" not in text
    assert "sk-" not in text


def test_provider_section_reports_availability_without_the_key(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(type(settings), "has_anthropic_key", property(lambda self: True))
    monkeypatch.setattr(type(settings), "effective_api_key", property(lambda self: "sk-should-not-leak"))

    text = _text()

    assert "available" in text
    assert "should-not-leak" not in text


def test_secret_shaped_values_are_redacted_wherever_they_appear(monkeypatch):
    """A secret that somehow reaches a value still gets caught, because
    every value goes through the same redactor used for child output.

    Uses jarvis_host, whose value really does flow verbatim into the
    "Bind address" line. An earlier version of this test used
    jarvis_ai_provider and failed — because selected_provider()
    normalises an unrecognised value away entirely, so the raw string
    never reached the report at all. That is stronger behaviour than
    redaction, but it made the test prove nothing about the redactor."""
    from app.config import settings
    monkeypatch.setattr(settings, "jarvis_host", "token=hunter2-should-be-hidden")

    text = _text()

    assert "hunter2-should-be-hidden" not in text
    assert "[REDACTED]" in text


def test_report_never_reads_the_configured_api_key_field(monkeypatch):
    """The design guarantee, stated as the property that is actually
    testable: the report is assembled from named fields, so a
    secret-bearing setting is absent because nothing asked for it — not
    because something filtered it out afterwards.

    (The obvious version of this test — adding a brand-new attribute to
    Settings — is impossible: pydantic rejects unknown fields outright,
    which is itself a second layer of the same protection.)"""
    from app.config import settings
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-raw-field-value-leak")

    assert "sk-raw-field-value-leak" not in _text()


def test_report_never_dumps_the_environment(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-env-value-should-not-appear")
    monkeypatch.setenv("JARVIS_SESSION_SECRET", "session-secret-should-not-appear")

    text = _text()

    assert "sk-env-value-should-not-appear" not in text
    assert "session-secret-should-not-appear" not in text


# ---------------------------------------------------------------------------
# Robustness — a diagnostic report must not crash when things are broken
# ---------------------------------------------------------------------------

def test_a_failing_section_does_not_break_the_report(monkeypatch):
    """A report that crashes is worthless exactly when it is most
    needed."""
    def _boom():
        raise RuntimeError("subsystem is down")

    monkeypatch.setattr(diagnostics, "_voice_section", _boom)

    sections = diagnostics.build_report()

    assert any("Could not be collected" in item["value"] for s in sections for item in s.items)
    assert len(sections) == 8, "a failed section is replaced, not dropped"


def test_render_never_raises_even_with_a_broken_database(monkeypatch):
    def _broken_db():
        raise OSError("database file is locked")

    monkeypatch.setattr("db.database.get_db", _broken_db)

    text = _text()

    assert "Not reachable" in text


def test_database_section_reports_status_without_leaking_a_path_secret(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(type(settings), "db_path", property(lambda self: "/tmp/token=abc123/jarvis.db"))

    assert "abc123" not in _text()
