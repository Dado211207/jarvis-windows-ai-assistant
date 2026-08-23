"""What the browser is allowed to reach, decided in one place.

Browser QA opens a page. That is the entire capability, and the distance
between "opens the preview this task started" and "opens any URL" is one
careless parameter. This module is the only thing that decides which is
which, and it is deliberately small enough to read in full.

**One allowed origin, computed not supplied.** `http://127.0.0.1:<port>`
where the port is the one the owned `PreviewSession` bound. There is no
argument anywhere in this subsystem through which a model, a project
file, a page or a task record can name a host.

**`localhost` is refused, and that is not pedantry.** `localhost` is a
name; a name is resolved; a `hosts` file, a DNS suffix or a resolver on
the machine decides what it resolves to. `127.0.0.1` is an address. The
same reasoning refuses `[::1]`, `127.0.0.2`, `0.0.0.0` and every other
loopback alias: they may in fact reach this machine, but they reach it
by a route this module did not compute, and one of them (`0.0.0.0`) is a
different service entirely.

**A refusal says which kind.** `foreign_host` and `javascript_scheme` are
both blocked, but they mean very different things about the page under
test — one is a link to a website, the other is an attempt to run code
through a navigation. The UI shows the category.

Browser-level enforcement backs this up: `browser_engine.launch_argv`
passes `--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1`, so
even a URL that got past this module could not resolve. Two independent
mechanisms, because a boundary with a single implementation is a boundary
with a single bug.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlsplit

LOOPBACK = "127.0.0.1"

#: Schemes that are never a navigation target, whatever the host.
_DANGEROUS_SCHEMES = {
    "file": "file_scheme",
    "data": "data_scheme",
    "javascript": "javascript_scheme",
    "blob": "blob_scheme",
    "vbscript": "javascript_scheme",
    "about": "about_scheme",
    "chrome": "browser_scheme",
    "chrome-extension": "browser_scheme",
    "devtools": "browser_scheme",
    # Where Chromium parks a tab whose navigation it refused. Its own
    # category because it is not a destination anything chose: it is the
    # visible result of a block, and reporting it as the place the page
    # "went" describes the defence rather than the attempt.
    "chrome-error": "browser_error",
    "view-source": "browser_scheme",
    "ws": "websocket_scheme",
    "wss": "websocket_scheme",
    "ftp": "custom_scheme",
}

#: Names that resolve to this machine but are not the address we computed.
_LOOPBACK_ALIASES = {"localhost", "localhost.localdomain", "ip6-localhost",
                     "::1", "[::1]", "0.0.0.0", "0", "127.1"}


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    category: str = "allowed"
    detail: str = ""

    def as_dict(self) -> dict:
        return {"allowed": self.allowed, "category": self.category,
                "detail": self.detail}


def origin_for(port: int) -> str:
    """The one origin this check may reach."""
    return f"http://{LOOPBACK}:{int(port)}"


def safe_route(route: str) -> Optional[str]:
    """A path this module is willing to append to the owned origin.

    Returns None for anything that is not purely a path — which is the
    only shape a caller may supply. A route is a place inside the user's
    own preview, so `//evil.example`, `http://…`, a backslash (which
    Chromium normalises to `/`, making `\\\\evil.example` a host) and an
    embedded NUL are all rejected rather than repaired. Repairing an
    attack into something that *nearly* works is how boundaries fail.
    """
    if not isinstance(route, str):
        return None
    if len(route) > 512:
        return None
    if "\x00" in route or "\\" in route or "\n" in route or "\r" in route:
        return None
    if "://" in route or route.lstrip().lower().startswith(("javascript:", "data:", "file:")):
        return None
    candidate = route.strip() or "/"
    if not candidate.startswith("/"):
        candidate = "/" + candidate
    if candidate.startswith("//"):          # protocol-relative: a host, not a path
        return None
    return candidate


def classify(url: str, port: Optional[int]) -> Verdict:
    """Is this URL the owned preview, and if not, what is it?"""
    if not url:
        return Verdict(False, "empty", "an empty URL")

    lowered = url.strip()
    # `about:blank` is the target we create before navigating anywhere; it
    # is the one non-http URL this check ever legitimately sees.
    if lowered in ("about:blank", "about:blank#blocked"):
        return Verdict(True, "initial", "the blank starting page")

    try:
        parts = urlsplit(lowered)
    except ValueError:
        return Verdict(False, "unparseable", "a URL that could not be parsed")

    scheme = (parts.scheme or "").lower()
    if scheme in _DANGEROUS_SCHEMES:
        return Verdict(False, _DANGEROUS_SCHEMES[scheme], f"the {scheme}: scheme")
    if scheme not in ("http", "https"):
        return Verdict(False, "custom_scheme",
                       f"the {scheme or 'schemeless'} scheme")
    if scheme == "https":
        # The preview is plain HTTP on loopback. An https URL is by
        # definition somewhere else, even if the host looks familiar.
        return Verdict(False, "foreign_host", "an https origin")

    host = (parts.hostname or "").lower()
    if host in _LOOPBACK_ALIASES:
        return Verdict(False, "loopback_alias",
                       f"'{host}', which is a name or alias rather than the "
                       f"address JARVIS bound")

    if host != LOOPBACK:
        return Verdict(False, _address_kind(host), host[:80] or "no host")

    if port is None:
        return Verdict(False, "wrong_port", "no preview port is known")
    if parts.port != int(port):
        return Verdict(False, "wrong_port",
                       f"port {parts.port} rather than this task's preview port")

    return Verdict(True, "allowed", "")


def _address_kind(host: str) -> str:
    """Name the category of a host that is not ours, for the report."""
    if not host:
        return "foreign_host"
    try:
        address = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return "dns_name"
    if address.is_loopback:
        return "loopback_alias"
    if address.is_private or address.is_link_local:
        return "private_lan"
    return "public_ip"
