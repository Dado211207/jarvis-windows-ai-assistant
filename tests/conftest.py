"""Shared pytest helpers across the test suite."""

SESSION_TOKEN_HEADER = "X-JARVIS-Session-Token"


def prime_session(client):
    """Perform a GET to receive the v0.2 CSRF/mutation session cookie
    (see app/api/session.py), then set the matching header as a default
    on the client so every subsequent mutating REST call and WebSocket
    connect automatically carries it — exactly what the real dashboard's
    own JS does by reading the (deliberately non-HttpOnly) cookie and
    echoing it back. Returns the same client for convenient chaining in
    a fixture's `yield prime_session(client)`.
    """
    client.get("/health")
    token = client.cookies.get("jarvis_session")
    if token:
        client.headers[SESSION_TOKEN_HEADER] = token
    return client
