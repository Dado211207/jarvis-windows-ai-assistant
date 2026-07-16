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
