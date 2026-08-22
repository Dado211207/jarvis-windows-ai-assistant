"""Downloading from the place we said we would download from.

JARVIS fetches four things over the network that it did not write: the
Ollama installer, the speech-recognition model, the neural voice's
assets, and audio from ElevenLabs. Each one is preceded by a screen
naming the source — "from huggingface.co", "from ollama.com" — and that
sentence is only true if the code enforces it.

`httpx`'s own `follow_redirects=True` does not enforce it. It walks the
whole chain and hands back where it ended up, so:

  * an intermediate hop to somebody else's host is contacted, with
    headers, before anything notices; and
  * a chain that detours through that host and returns to an allowed one
    passes a check on the final URL.

Both were reproduced against this project's own Ollama installer before
this module existed. So redirects are followed by hand here, one at a
time, with every destination checked *before* a request is sent to it.

**This is not the only protection and is not meant to be.** The Ollama
installer's Authenticode signature is verified before it is run; the
speech model and the voice assets are checked against published SHA-256
digests before they are installed. This is the layer that keeps a
request from reaching a host the user was never told about — a privacy
property as much as an integrity one.

**Used by the Ollama installer and deliberately not by the two Hugging
Face downloads.** Those fetch model data, never code; nothing is
executed, and every file is checked against a SHA-256 before it is
installed (`app/voice/kokoro/assets.py` holds its digests in this
repository; `app/voice/model_installer.py` documents where its come
from and which small files are size-checked instead). Hugging Face's
LFS layer redirects to CDN hostnames it changes without notice, so a
pin here could not be verified against the live service from any
environment this project builds in — and an allowlist that gets widened
each time a download breaks is not a control, it is a ritual. The path
where a wrong file would be *run* is the one that is pinned.
"""

from contextlib import contextmanager
from typing import Iterable, Iterator, Sequence, Tuple
from urllib.parse import urlparse

import httpx

from app.logging_config import get_logger

logger = get_logger("core.safe_fetch")

# A chain longer than this is a loop or somebody playing games. Real
# ones (ollama.com to a GitHub release to GitHub's asset CDN,
# huggingface.co to its CDN) take two or three hops.
MAX_REDIRECTS = 6

# (host, path_prefix). An empty prefix means the whole host is allowed.
# A prefix matters for a host that is a shared publishing namespace:
# "github.com" on its own is not a pin, it is "somewhere on a site with
# millions of publishers".
Source = Tuple[str, str]


class UntrustedRedirect(Exception):
    """A hop the download policy does not allow. Carries a message the
    user can read; never the URL, which may carry a signed token."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def is_allowed(url: str, sources: Iterable[Source]) -> bool:
    """Whether a request may be sent to *url* at all.

    HTTPS only — a redirect that downgrades to http would put the file
    on the wire in the clear, where anything between here and there
    could replace it.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https":
        return False
    host = (parsed.hostname or "").lower()
    return any(
        host == allowed_host and parsed.path.startswith(prefix)
        for allowed_host, prefix in sources
    )


@contextmanager
def stream(
    url: str,
    sources: Sequence[Source],
    timeout: float,
    max_redirects: int = MAX_REDIRECTS,
) -> Iterator[httpx.Response]:
    """Stream *url*, following redirects only to hosts in *sources*.

    Yields the final, non-redirect response with `raise_for_status()`
    already called. Raises `UntrustedRedirect` if any hop — including
    the first URL — is not allowed, or if the chain is too long.
    """
    if not is_allowed(url, sources):
        raise UntrustedRedirect(
            "That download address is not one JARVIS trusts, so nothing was fetched."
        )

    current = url
    for _ in range(max_redirects + 1):
        with httpx.stream("GET", current, timeout=timeout, follow_redirects=False) as response:
            if not response.is_redirect:
                response.raise_for_status()
                yield response
                return

            location = response.headers.get("location", "")
            nxt = str(response.url.join(location)) if location else ""
            if not nxt or not is_allowed(nxt, sources):
                logger.warning(
                    "Refusing a redirect to an address outside the allowed sources for %s.",
                    urlparse(url).hostname or "the download",
                )
                raise UntrustedRedirect(
                    "The download was redirected to an address JARVIS does not trust, "
                    "so nothing was saved."
                )
            current = nxt

    raise UntrustedRedirect("The download kept being redirected, so JARVIS stopped.")


def get(url: str, sources: Sequence[Source], timeout: float) -> httpx.Response:
    """A whole small response — for an API call rather than a file.

    Same policy as `stream()`, read fully into memory, so a caller that
    wants JSON does not have to open a stream to get it.
    """
    with stream(url, sources, timeout) as response:
        response.read()
        return response
