# Browser QA: how the packaged application opens a real browser

## The defect this replaces

Coding Workspace shipped a browser check that worked in a source
checkout and reported `available: false` on every machine a user
actually had. It needed Playwright — a test dependency the packaged
Windows build does not carry — so the installed product could inspect
HTTP status and parse HTML with a regular expression, and nothing more.

That is not a browser check with a gap in it. A 200 is compatible with a
blank page, a React error boundary, a missing bundle and a script that
threw before rendering anything, and telling those apart is the entire
reason the feature exists.

So the requirement was: **the installed JARVIS performs real browser
checks out of the box, with no manual installation of development
packages, no first-use download, and no external network request.**

---

## The four options

### 1. Bundle Playwright and its own Chromium

Measured on this machine, Playwright 1.62.0:

| Component | Size |
|---|---|
| `playwright` Python package | 13.4 MB |
| bundled driver | 137.3 MB |
| — of which a private Node runtime | 123.7 MB |
| **package total** | **142.5 MB** |
| a Chromium build, additionally | ~150 MB |

The JARVIS installer is 101,093,765 bytes. This option roughly triples
it, and 124 MB of the increase is a second JavaScript runtime whose only
job is to relay messages to a browser.

### 2. Bundle Playwright, drive installed Edge

Removes the ~150 MB browser but keeps the 137.3 MB driver, including the
Node runtime. Still more than doubles the installer, for a component
that is a transport.

### 3. Drive the WebView2 control the desktop window already hosts

JARVIS's window is WebView2. Navigating it to the user's preview would
navigate *JARVIS away*, so this needs a second, hidden WebView2 host.
WebView2 has no supported headless mode, no supported way to attach a
CDP client to a control the host application owns without opening a
debugging port on the host itself, and the host process is the one
rendering the user's own session. Rejected on containment, not on size:
the browser inspecting an untrusted page must not be the same process
that is showing the user their assistant.

### 4. Drive the already-installed Chromium runtime over CDP — **chosen**

Every candidate engine on Windows is Chromium and speaks the Chrome
DevTools Protocol:

* **Microsoft Edge**, present on every supported Windows 10 and 11
  installation.
* **`msedgewebview2.exe`** from the WebView2 Runtime, which JARVIS
  already requires and the installer already offers to install — so even
  an Edge-stripped enterprise image has one.
* **Chromium**, on Linux, for CI only.

CDP is a JSON protocol over a WebSocket. `websockets` is already a
dependency of this project through `uvicorn[standard]`, which
`requirements.txt` has always declared:

```
uvicorn -> ["websockets>=13.0; extra == 'standard'"]
websockets: present, version 17.0.1
```

**Zero new dependencies. Zero installer growth. No download, ever.**

---

## The comparison, against the thirteen criteria

| | 1. Playwright + own Chromium | 2. Playwright + installed Edge | 3. Hosted WebView2 control | 4. **Installed Chromium over CDP** |
|---|---|---|---|---|
| Works from a frozen `.exe` | Yes, with driver unpacking | Yes, with driver unpacking | Untested/unsupported headless | **Yes — a subprocess and a socket** |
| Separate download needed | Browser, at build time | No | No | **No** |
| Installed-size impact | +~290 MB | +142 MB | ~0 | **0 bytes** |
| Startup impact | Node driver process per run | Node driver process per run | Shares the UI process | **Browser process per run, nothing resident** |
| Licence | Apache-2.0 + Chromium BSD | Apache-2.0 | Microsoft EULA | **None added** |
| Security / update model | We ship and must patch a browser | We ship a driver | Windows Update | **Windows Update patches Edge and WebView2** |
| Process cleanup | Playwright's own | Playwright's own | Cannot kill the UI process | **`process_tree` identities, same as everything else** |
| Console / network interception | Yes | Yes | Partial | **Yes — `Runtime`, `Log`, `Network` domains** |
| Screenshot | Yes | Yes | Yes | **Yes — `Page.captureScreenshot`** |
| Accessibility inspection | Via axe (a Node package) | Via axe | Limited | **A fixed nine-rule structural check, named in the UI** |
| Windows 10 / 11 | Yes | Yes | Yes | **Yes — both engines ship with the OS** |
| Restrictable to `127.0.0.1` | Application-level | Application-level | Hard | **Browser-level: `--host-resolver-rules`** |
| Depends on developer tools | Yes at build time | Yes | No | **No** |

Option 4 wins every row that the brief marked preferred, and the one row
where it is weaker — accessibility — it is weaker *honestly*: see below.

---

## The spike, run before the choice was made

Against a local Chromium and a fixture page carrying deliberate defects:

```
browser CDP: OK
  http status      : 200
  title / lang     : 'Corner Shop' / 'en'
  h1 count         : 2
  console errors   : 4
  page exceptions  : 1
  failed requests  : 1  4xx/5xx: 3
  broken images    : 1
  overflow @320    : True
  reduced motion   : True
  screenshot bytes : 4412
  a11y nodes       : 12
browser exited: True
```

Every check the brief requires, from a protocol we already had the
transport for.

---

## What was added, and what it cost

**No package was added.** `requirements.txt` is unchanged by this work.
The transport is `websockets` 17.0.1 (BSD-3-Clause), already installed
as a transitive dependency of `uvicorn[standard]`, already covered by
the packaged licence-policy test, already inside the installer.

The new code is five modules under `app/coding/`:

| Module | What it is |
|---|---|
| `browser_engine.py` | Finds Edge / WebView2 / Chromium; builds the command line |
| `cdp.py` | ~300 lines of DevTools protocol client, and nothing more |
| `browser_origin.py` | The only place that decides what may be opened |
| `browser_probe.py` | The JavaScript evaluated inside the page |
| `browser_findings.py` | The seven outcomes and what each carries |
| `browser_qa.py` | Orchestration, ownership, and process cleanup |

---

## The security boundary

Two independent mechanisms, because a boundary with one implementation
is a boundary with one bug.

**Application level** — `browser_origin.py` computes the single allowed
origin from the owned `PreviewSession`'s port. There is no argument
anywhere in this subsystem through which a model, a project file, a page
or a task record can name a host. `localhost`, `[::1]`, `127.0.0.2` and
`0.0.0.0` are all refused: they may in fact reach this machine, but they
reach it by a route this module did not compute.

**Browser level** — the command line carries:

```
--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1
--proxy-server=http://127.0.0.1:0
--proxy-bypass-list=127.0.0.1
```

Every hostname except the owned preview fails to resolve *inside
Chromium*, and anything that got past that would be sent to a proxy
that does not exist. A redirect, a subresource or a script cannot leave
the machine even if the application-level check were wrong.

Plus, on every run: a fresh temporary profile deleted afterwards, no
extensions, no sync, no background networking, no password store,
`Page.setDownloadBehavior: deny`, all thirteen permissions denied,
`--block-new-web-contents`, and dialogs dismissed by the protocol pump
rather than by the caller — because a page that calls `alert()` blocks
the renderer, and a client waiting for its own command reply before
answering the dialog never returns.

`tests/coding_browser_fixtures.py` serves thirteen pages that attempt
external redirects (server, meta and script), external image, script,
font and `fetch`, a popup, a self-starting download, `file://`
navigation, `localhost` and `[::1]` aliases, twenty thousand console
errors, an endless navigation loop, a 60,000-pixel page, and
credential-shaped console output. Each is asserted to be blocked,
bounded or safely reported.

---

## The accessibility check is nine rules, and says so

`browser_probe.ACCESSIBILITY_RULES` is a fixed list — language, title,
one `<h1>`, heading order, image alt, control name, button name, frame
title, duplicate id — and the UI renders that list beside the count.

It is deliberately **not** described as an axe-core audit. axe-core is a
Node package the packaged build does not carry, and reporting "0
accessibility violations" from a nine-rule structural check while
implying a full audit would be the same species of dishonesty as
reporting zero console errors for a page nothing opened.

JARVIS's *own* interface is still audited with axe, in the Playwright
suite that runs in CI. That is a different check of a different thing.

---

## Where this deliberately stops

This is not a browser-automation library and must not become one. There
is no click, no type, no form fill, no multi-page flow, no cookie
handling, no authentication, and no way to pass a URL. `cdp.py` does
exactly what `browser_qa.py` needs: attach, enable four domains,
navigate, collect, evaluate, screenshot, close.

General web browsing, and browsing the user's own logged-in sessions,
remain out of scope — see `docs/desktop-capability-roadmap.md`.
