"""
Tests for Phase 8: Persistent Settings & Personality Memory.

Coverage:
- secret_guard: detects tokens/credentials, passes benign preferences
- settings_service: defaults, valid update, reject unknown/locked/enum/range/secret
- settings persistence across a DB-singleton reload
- preferences tools: remember (explicit), list, search, forget, secret refusal
- clear_preferences requires approval (never executes directly)
- router mapping for all new Phase 8 phrases
- /settings and /preferences API endpoints (validate, no secrets, no clear-all route)
- UI: Settings page loads; Memory page explicit/local-only wording; Help has commands
- Security: no innerHTML, no sk-ant-, no external CDN; version 0.1.7 / Phase 8
- Regression: no OCR/browser/microphone/STT/wake word introduced; clear_logs still gated
"""

import pytest


# ── Isolated temp DB so tests never touch the real jarvis.db ──────────────────

@pytest.fixture(scope="module")
def temp_db(tmp_path_factory):
    from app.config import settings as app_settings
    import db.database as dbmod

    old_path = app_settings.jarvis_db_path
    path = tmp_path_factory.mktemp("p8db") / "jarvis_test.db"
    app_settings.jarvis_db_path = str(path)
    dbmod._db_instance = None

    from db.migrations import create_tables
    create_tables()
    yield
    dbmod._db_instance = None
    app_settings.jarvis_db_path = old_path


@pytest.fixture(autouse=True)
def _clean(temp_db):
    """Start every test with empty settings + preferences tables."""
    from db.database import get_db
    conn = get_db()._get_conn()
    conn.execute("DELETE FROM preferences")
    conn.execute("DELETE FROM settings")
    conn.commit()
    yield


@pytest.fixture(scope="module")
def api_client(temp_db):
    from fastapi.testclient import TestClient
    from app.api.server import app as jarvis_app
    with TestClient(jarvis_app, raise_server_exceptions=True) as client:
        yield client


@pytest.fixture(autouse=True)
def reset_pending_store():
    from app.core.pending_actions import pending_store
    with pending_store._lock:
        pending_store._actions.clear()
    yield
    with pending_store._lock:
        pending_store._actions.clear()


# ── secret_guard ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "sk-ant-api03-abcdefghijklmnopqrstuvwxyz012345",
    "my openai key sk-abcdefghijklmnopqrstuvwxyz012345",
    "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
    "github_pat_11ABCDEFG0abcdefghij_klmnopqrstuvwxyz0123456789",
    "nfp_abcdefghijklmnopqrstuvwxyz0123456789",
    "AKIAIOSFODNN7EXAMPLE",
    "my password is hunter2secret",
    "token = abcdefghijklmnopqrstuv",
])
def test_secret_guard_detects(text):
    from app.core.secret_guard import find_secret
    assert find_secret(text) is not None


@pytest.mark.parametrize("text", [
    "I prefer short direct answers",
    "call me Dragan",
    "my JARVIS should call me Dragan",
    "I prefer dark UI",
    "respond without diacritics",
    "set language to Serbian",
])
def test_secret_guard_allows_benign(text):
    from app.core.secret_guard import find_secret
    assert find_secret(text) is None


# ── settings_service ──────────────────────────────────────────────────────────

def test_settings_defaults():
    from app.core import settings_service as ss
    d = ss.DEFAULTS
    assert d["assistant_name"] == "JARVIS"
    assert d["preferred_response_style"] == "balanced"
    assert d["safety_mode"] == "on"


def test_settings_get_all_has_all_keys():
    from app.core import settings_service as ss
    current = ss.get_all()
    for spec in ss.SETTINGS_SPECS:
        assert spec.key in current


def test_settings_update_valid():
    from app.core import settings_service as ss
    ok, msg = ss.update("assistant_name", "Friday")
    assert ok
    assert ss.get_all()["assistant_name"] == "Friday"


def test_settings_update_enum_normalises():
    from app.core import settings_service as ss
    ok, _ = ss.update("preferred_response_style", "SHORT")
    assert ok
    assert ss.get_all()["preferred_response_style"] == "short"


def test_settings_reject_unknown_key():
    from app.core import settings_service as ss
    ok, msg = ss.update("evil_key", "x")
    assert not ok
    assert "not a recognised setting" in msg.lower()


def test_settings_reject_locked_safety_mode():
    from app.core import settings_service as ss
    ok, msg = ss.update("safety_mode", "off")
    assert not ok
    assert ss.get_all()["safety_mode"] == "on"


def test_settings_reject_bad_enum():
    from app.core import settings_service as ss
    ok, _ = ss.update("preferred_response_style", "verbose-ish")
    assert not ok


def test_settings_reject_out_of_range_int():
    from app.core import settings_service as ss
    assert ss.update("tts_rate", "9999")[0] is False
    assert ss.update("tts_rate", "10")[0] is False
    assert ss.update("tts_rate", "180")[0] is True


def test_settings_reject_secret_value():
    from app.core import settings_service as ss
    ok, msg = ss.update("user_display_name", "sk-ant-api03-abcdefghijklmnopqrstuv012345")
    assert not ok
    assert "secret" in msg.lower() or "api key" in msg.lower()


def test_settings_reject_too_long():
    from app.core import settings_service as ss
    ok, _ = ss.update("assistant_name", "x" * 500)
    assert not ok


def test_settings_persist_across_reload():
    from app.core import settings_service as ss
    import db.database as dbmod
    ss.update("assistant_name", "Persisted")
    # Simulate a service restart: drop the DB singleton, re-open the same file.
    dbmod._db_instance = None
    assert ss.get_all()["assistant_name"] == "Persisted"


# ── preferences (personality memory) ──────────────────────────────────────────

def test_remember_preference_saves():
    from app.core.preferences import remember_preference, list_preferences
    r = remember_preference("I prefer short answers")
    assert r["success"] is True
    listed = list_preferences()
    assert listed["success"] is True
    assert len(listed["data"]) == 1


def test_remember_detects_category():
    from app.core.preferences import remember_preference
    assert remember_preference("call me Dragan")["data"]["category"] == "profile"
    assert remember_preference("I prefer dark UI theme")["data"]["category"] == "ui"


def test_remember_refuses_secret():
    from app.core.preferences import remember_preference, list_preferences
    r = remember_preference("my password is hunter2secret")
    assert r["success"] is False
    assert list_preferences()["data"] == []


def test_search_preferences_finds():
    from app.core.preferences import remember_preference, search_preferences
    remember_preference("I prefer short answers")
    remember_preference("I like dark themes")
    res = search_preferences("dark")
    assert res["success"] is True
    assert len(res["data"]) == 1


def test_forget_single_preference():
    from app.core.preferences import remember_preference, forget_preference, list_preferences
    remember_preference("I dislike long preambles")
    r = forget_preference("preambles")
    assert r["success"] is True
    assert list_preferences()["data"] == []


def test_forget_ambiguous_requires_specificity():
    from app.core.preferences import remember_preference, forget_preference
    remember_preference("I prefer short answers")
    remember_preference("I prefer short meetings")
    r = forget_preference("short")
    assert r["success"] is False  # two matches → must be specific


def test_clear_preferences_wipes_all():
    from app.core.preferences import remember_preference, clear_preferences, list_preferences
    remember_preference("one")
    remember_preference("two")
    r = clear_preferences()
    assert r["success"] is True
    assert list_preferences()["data"] == []


# ── router mapping ────────────────────────────────────────────────────────────

def _router_tool(command: str) -> str:
    from app.core.router import ROUTES
    for route in ROUTES:
        kwargs = route.match(command)
        if kwargs is not None:
            return route.tool_name
    return ""


@pytest.mark.parametrize("cmd,tool", [
    ("settings", "show_settings"),
    ("show settings", "show_settings"),
    ("set assistant name to JARVIS", "update_setting"),
    ("set my name to Dragan", "update_setting"),
    ("set language to Serbian", "update_setting"),
    ("set response style to short", "update_setting"),
    ("set tone to direct", "update_setting"),
    ("remember that I prefer short answers", "remember_preference"),
    ("save preference I like dark mode", "remember_preference"),
    ("what do you remember", "list_preferences"),
    ("show memory", "list_preferences"),
    ("search memory short", "search_preferences"),
    ("forget dark mode", "forget_preference"),
    ("clear memory", "clear_preferences"),
])
def test_router_maps_phase8(cmd, tool):
    assert _router_tool(cmd) == tool


def test_router_set_name_extracts_value():
    from app.core.router import ROUTES
    for route in ROUTES:
        if route.pattern.startswith(r"^set\s+assistant"):
            assert route.match("set assistant name to Friday") == {
                "key": "assistant_name", "value": "Friday"}
            break


# ── clear_preferences requires approval ───────────────────────────────────────

def test_clear_memory_requires_approval(api_client):
    r = api_client.post("/command", json={"command": "clear memory"})
    assert r.status_code == 200
    body = r.json()
    assert body["requires_approval"] is True
    assert body["pending_action_id"]


def test_clear_memory_does_not_execute_without_confirm(api_client):
    from app.core.preferences import remember_preference, list_preferences
    remember_preference("keep me")
    api_client.post("/command", json={"command": "clear memory"})
    # Still present — approval pending, not executed.
    assert len(list_preferences()["data"]) == 1


def test_forget_is_not_approval_gated(api_client):
    r = api_client.post("/command", json={"command": "forget nonexistent-xyz"})
    assert r.status_code == 200
    assert r.json().get("requires_approval", False) is False


# ── /settings API ─────────────────────────────────────────────────────────────

def test_api_get_settings(api_client):
    r = api_client.get("/settings")
    assert r.status_code == 200
    assert r.json()["safety_mode"] == "on"


def test_api_get_defaults(api_client):
    r = api_client.get("/settings/defaults")
    assert r.status_code == 200
    assert r.json()["assistant_name"] == "JARVIS"


def test_api_patch_settings_valid(api_client):
    r = api_client.patch("/settings", json={"values": {"assistant_name": "Vision"}})
    body = r.json()
    assert body["success"] is True
    assert body["settings"]["assistant_name"] == "Vision"


def test_api_patch_rejects_secret_and_locked(api_client):
    r = api_client.patch("/settings", json={"values": {
        "user_display_name": "sk-ant-api03-abcdefghijklmnopqrstuv012345",
        "safety_mode": "off",
    }})
    body = r.json()
    assert body["success"] is False
    assert "user_display_name" in body["errors"]
    assert "safety_mode" in body["errors"]
    assert body["settings"]["safety_mode"] == "on"


def test_api_settings_never_leaks_key(api_client):
    api_client.patch("/settings", json={"values": {"assistant_name": "Test"}})
    r = api_client.get("/settings")
    assert "ANTHROPIC_API_KEY" not in r.text
    assert "sk-ant-" not in r.text


# ── /preferences API ──────────────────────────────────────────────────────────

def test_api_preferences_crud(api_client):
    # create
    r = api_client.post("/preferences", json={"text": "I prefer concise replies"})
    assert r.json()["success"] is True
    # list
    items = api_client.get("/preferences").json()
    assert len(items) == 1
    pid = items[0]["id"]
    # search
    assert len(api_client.get("/preferences/search", params={"q": "concise"}).json()) == 1
    # delete
    assert api_client.delete(f"/preferences/{pid}").json()["success"] is True
    assert api_client.get("/preferences").json() == []


def test_api_preferences_rejects_secret(api_client):
    r = api_client.post("/preferences", json={"text": "my api key is sk-ant-api03-abcdefghij012345"})
    assert r.json()["success"] is False
    assert api_client.get("/preferences").json() == []


def test_api_preferences_delete_missing_404(api_client):
    assert api_client.delete("/preferences/999999").status_code == 404


def test_api_no_clear_all_preferences_route(api_client):
    # Wiping all memory must go through the approval-gated tool, not a plain DELETE.
    assert api_client.delete("/preferences").status_code == 405


# ── UI ────────────────────────────────────────────────────────────────────────

def test_settings_page_loads(api_client):
    r = api_client.get("/ui/settings")
    assert r.status_code == 200
    html = r.text
    assert "settings-form" in html
    assert "set-assistant_name" in html
    assert "sidebar" in html


def test_settings_nav_link_present(api_client):
    r = api_client.get("/ui/")
    assert "/ui/settings" in r.text


def test_settings_page_active_nav(api_client):
    r = api_client.get("/ui/settings")
    assert "sidebar-link active" in r.text


def test_memory_page_explicit_local_wording(api_client):
    html = api_client.get("/ui/memory").text.lower()
    assert "local" in html
    assert "explicit" in html or "only when you ask" in html or "only" in html
    assert "never" in html


def test_memory_page_keeps_ids(api_client):
    html = api_client.get("/ui/memory").text
    assert "memory-search" in html
    assert "memory-list" in html


def test_help_has_settings_and_memory_commands(api_client):
    html = api_client.get("/ui/help").text
    assert "remember that" in html
    assert "what do you remember" in html
    assert "set assistant name to" in html
    assert "clear memory" in html


def test_help_lists_never_stored_items(api_client):
    html = api_client.get("/ui/help").text.lower()
    assert "password" in html
    assert "token" in html


# ── Security / regression ─────────────────────────────────────────────────────

def test_js_still_no_innerhtml(api_client):
    assert "innerHTML" not in api_client.get("/ui/static/app.js").text


def test_js_has_settings_functions(api_client):
    js = api_client.get("/ui/static/app.js").text
    assert "initSettings" in js
    assert "loadPersonality" in js


def test_css_no_external_resources(api_client):
    css = api_client.get("/ui/static/style.css").text
    assert "https://" not in css
    assert "http://" not in css


@pytest.mark.parametrize("path", ["/ui/settings", "/ui/memory"])
def test_no_real_key_on_new_pages(api_client, path):
    r = api_client.get(path)
    assert "sk-ant-" not in r.text
    assert "ANTHROPIC_API_KEY" not in r.text


def test_version_is_017(api_client):
    assert "0.1.7" in api_client.get("/").json()["version"]


def test_phase_is_8(api_client):
    assert "Phase 8" in api_client.get("/health").json()["phase"]


def test_clear_logs_still_requires_approval(api_client):
    r = api_client.post("/command", json={"command": "clear logs"})
    assert r.json()["requires_approval"] is True


def test_no_ocr_browser_mic_tools_registered():
    """Phase 8 must not introduce OCR / browser / microphone / STT / wake-word tools."""
    from app.core.tool_registry import registry
    names = " ".join(t.definition.name for t in registry.list_tools()).lower()
    for forbidden in ("ocr", "screen_read", "browser", "microphone", "listen",
                      "speech_to_text", "wake_word", "record"):
        assert forbidden not in names


def test_safe_setting_command_no_approval(api_client):
    for cmd in ("set assistant name to JARVIS", "show settings", "what do you remember"):
        body = api_client.post("/command", json={"command": cmd}).json()
        assert body.get("requires_approval", False) is False
