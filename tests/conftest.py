"""Global pytest fixtures.

app.api.server now requires a valid X-Jarvis-Token header on every
state-changing request (POST/PUT/PATCH/DELETE) — see app/api/local_guard.py
and app/core/session_token.py. Real callers get the token from the page
JARVIS itself renders; the ~15 existing test files each build their own
`with TestClient(app) as client:` and call `.get/.post/.patch/.delete`
directly, the same way they did before that requirement existed.

Rather than touch every call site, this monkey-patches
`fastapi.testclient.TestClient.request` (the method every convenience
method funnels through) so any request that didn't explicitly set its own
X-Jarvis-Token header gets the *current, real* token for whichever app
instance's lifespan is active — read live from app.core.session_token, not
a fixed/bypass value, so this never weakens what's actually being tested:
a request either has a genuinely valid token or it doesn't. Tests that
specifically want to exercise the missing/invalid/foreign-origin paths
pass their own header explicitly, which this never overrides.
"""

import fastapi.testclient
import pytest

from app.core import session_token

_original_request = fastapi.testclient.TestClient.request


def _patched_request(self, method, url, **kwargs):
    headers = kwargs.get("headers") or {}
    has_token_header = any(str(k).lower() == "x-jarvis-token" for k in dict(headers).keys())
    if not has_token_header:
        headers = dict(headers)
        headers["X-Jarvis-Token"] = session_token.get_token()
        kwargs["headers"] = headers
    return _original_request(self, method, url, **kwargs)


fastapi.testclient.TestClient.request = _patched_request


@pytest.fixture(autouse=True)
def _reset_db_singleton():
    """Guarantee every test starts with db.database's module-level
    ``_db_instance`` unset, so a connection opened by one test can never
    leak into the next.

    Several files (test_onboarding.py, test_onboarding_provider_audit.py,
    test_paths.py, test_permissions.py, ...) run with cwd temporarily
    chdir'd into a pytest tmp_path via monkeypatch, so that JARVIS's plain
    relative default (``settings.jarvis_db_path == "data/jarvis.db"``)
    resolves somewhere disposable instead of the real repo. But
    ``get_db()`` only constructs+connects *lazily*, on whichever call
    happens to touch the DB first — and that can be indirect (e.g.
    ``brain.process()`` logging a command) in a test that never intended
    to exercise the database at all. If that lazy connect happens while
    _db_instance was already None, it opens a schema-less sqlite file
    inside that test's own tmp_path and caches the connection at module
    scope — outliving the tmp_path itself and silently poisoning every
    later test that calls get_db(), with confusing "no such table" errors
    far away from the actual cause.

    Resetting after every test closes that gap: whatever any given test
    connected to, the next one always starts from a clean, unconnected
    singleton and reconnects on its own terms.
    """
    yield
    import db.database as dbmod
    if dbmod._db_instance is not None:
        dbmod._db_instance.close()
    dbmod._db_instance = None
