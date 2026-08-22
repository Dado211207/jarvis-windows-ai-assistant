"""Downloading from the place we said we would download from.

`app/core/safe_fetch.py` exists because httpx's own `follow_redirects`
cannot enforce a source pin: it walks the chain and hands back only the
end of it. Two consequences, both reproduced against this project's own
Ollama installer before this module was written:

  * an intermediate hop to somebody else's host is contacted, with
    headers, before anything notices; and
  * a chain that detours through that host and comes back to an allowed
    one passes a check on the final URL.

Every test here drives the real code through `httpx.MockTransport`, so
what runs is the product's actual redirect loop — its hop limit, its
relative-URL resolution and its ordering of check-then-request.
"""

from unittest.mock import patch

import httpx
import pytest

from app.core import safe_fetch

SOURCES = (
    ("example.com", ""),
    ("shared.example", "/mine/"),
)


def scripted(chain):
    """A transport that answers from *chain*, recording what was asked
    for. Values are ("redirect", location) or ("ok", body)."""
    requested = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        requested.append(url)
        kind, value = chain.get(url, ("missing", None))
        if kind == "redirect":
            return httpx.Response(302, headers={"location": value})
        if kind == "ok":
            return httpx.Response(200, content=value)
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)

    def fake_stream(method, url, **kwargs):
        kwargs.pop("timeout", None)
        client = httpx.Client(
            transport=transport, follow_redirects=kwargs.pop("follow_redirects", False),
        )
        return client.stream(method, url, **kwargs)

    return fake_stream, requested


def fetch(chain, url="https://example.com/file"):
    fake_stream, requested = scripted(chain)
    body = None
    error = None
    with patch("httpx.stream", fake_stream):
        try:
            with safe_fetch.stream(url, SOURCES, timeout=5.0) as response:
                body = response.read()
        except safe_fetch.UntrustedRedirect as exc:
            error = exc
    return body, error, requested


# ---------------------------------------------------------------------------
# is_allowed
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "https://example.com/anything",
    "https://example.com/",
    "https://shared.example/mine/file.bin",
])
def test_allowed_addresses(url):
    assert safe_fetch.is_allowed(url, SOURCES) is True


@pytest.mark.parametrize("url", [
    "http://example.com/file",                 # downgraded to plain http
    "https://evil.example/file",               # wrong host
    "https://sub.example.com/file",            # a subdomain is a different host
    "https://example.com.evil.example/file",   # and so is a suffix trick
    "https://shared.example/somebody-else/f",  # right host, wrong namespace
    "ftp://example.com/file",
    "",
])
def test_refused_addresses(url):
    assert safe_fetch.is_allowed(url, SOURCES) is False


def test_a_path_prefix_is_what_makes_a_shared_host_a_pin():
    """A bare hostname is not a pin when anybody can publish under it —
    the case this exists for is github.com, where "the host is GitHub"
    says nothing about whose release it is."""
    assert safe_fetch.is_allowed("https://shared.example/mine/x", SOURCES) is True
    assert safe_fetch.is_allowed("https://shared.example/theirs/x", SOURCES) is False


# ---------------------------------------------------------------------------
# stream
# ---------------------------------------------------------------------------

def test_a_direct_download_works():
    body, error, requested = fetch({"https://example.com/file": ("ok", b"payload")})

    assert error is None
    assert body == b"payload"
    assert requested == ["https://example.com/file"]


def test_redirects_within_the_allowed_sources_are_followed():
    body, error, requested = fetch({
        "https://example.com/file": ("redirect", "https://shared.example/mine/a"),
        "https://shared.example/mine/a": ("redirect", "https://example.com/final"),
        "https://example.com/final": ("ok", b"payload"),
    })

    assert error is None
    assert body == b"payload"
    assert len(requested) == 3


def test_an_untrusted_hop_is_never_contacted():
    """The whole point: the check happens before the request, not after
    the chain has finished."""
    body, error, requested = fetch({
        "https://example.com/file": ("redirect", "https://evil.example/hop"),
        "https://evil.example/hop": ("ok", b"malware"),
    })

    assert body is None
    assert "does not trust" in error.message
    assert requested == ["https://example.com/file"]


def test_a_detour_that_returns_to_an_allowed_host_is_still_refused():
    """The regression this module replaces. httpx's follow_redirects
    would report the final, allowed URL and say nothing about the middle
    of the chain."""
    body, error, requested = fetch({
        "https://example.com/file": ("redirect", "https://evil.example/hop"),
        "https://evil.example/hop": ("redirect", "https://example.com/final"),
        "https://example.com/final": ("ok", b"payload"),
    })

    assert body is None
    assert error is not None
    assert requested == ["https://example.com/file"]


def test_a_relative_redirect_is_resolved_against_the_current_url():
    body, error, _ = fetch({
        "https://example.com/a/file": ("redirect", "../b/final"),
        "https://example.com/b/final": ("ok", b"payload"),
    }, url="https://example.com/a/file")

    assert error is None
    assert body == b"payload"


def test_a_redirect_with_no_location_is_refused_rather_than_retried():
    def handler_stream(method, url, **kwargs):
        kwargs.pop("timeout", None)
        transport = httpx.MockTransport(lambda request: httpx.Response(302))
        client = httpx.Client(transport=transport, follow_redirects=False)
        return client.stream(method, url, **kwargs)

    with patch("httpx.stream", handler_stream):
        with pytest.raises(safe_fetch.UntrustedRedirect):
            with safe_fetch.stream("https://example.com/file", SOURCES, timeout=5.0):
                pass


def test_an_endless_chain_stops_at_the_hop_limit():
    body, error, requested = fetch({
        "https://example.com/file": ("redirect", "https://example.com/a"),
        "https://example.com/a": ("redirect", "https://example.com/b"),
        "https://example.com/b": ("redirect", "https://example.com/a"),
    })

    assert body is None
    assert "redirected" in error.message
    assert len(requested) <= safe_fetch.MAX_REDIRECTS + 1


def test_the_starting_url_is_checked_too():
    body, error, requested = fetch(
        {"https://evil.example/file": ("ok", b"payload")}, url="https://evil.example/file",
    )

    assert body is None
    assert "not one JARVIS trusts" in error.message
    assert requested == []


def test_an_http_error_is_raised_rather_than_written_out():
    fake_stream, requested = scripted({"https://example.com/file": ("missing", None)})

    with patch("httpx.stream", fake_stream):
        with pytest.raises(httpx.HTTPStatusError):
            with safe_fetch.stream("https://example.com/file", SOURCES, timeout=5.0):
                pass


def test_the_error_message_never_carries_the_url():
    """Redirect targets on a real CDN carry signed access tokens. A
    message a user reads, and a log line, must not repeat one."""
    secret = "https://evil.example/hop?token=super-secret-value"
    _, error, _ = fetch({"https://example.com/file": ("redirect", secret)})

    assert "super-secret-value" not in error.message
    assert "evil.example" not in error.message


# ---------------------------------------------------------------------------
# get
# ---------------------------------------------------------------------------

def test_get_returns_a_whole_response():
    fake_stream, _ = scripted({"https://example.com/api": ("ok", b'{"ok":true}')})

    with patch("httpx.stream", fake_stream):
        response = safe_fetch.get("https://example.com/api", SOURCES, timeout=5.0)

    assert response.json() == {"ok": True}
