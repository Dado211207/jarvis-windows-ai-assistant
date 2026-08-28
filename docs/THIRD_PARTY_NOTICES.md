# Third-party notices

Dependencies introduced specifically for Windows installer packaging
(v0.2 packaging pass). This file covers *packaging-only* dependencies —
`requirements.txt`'s existing dependencies (FastAPI, uvicorn, pydantic,
psutil, Pillow, python-dotenv, anthropic, pyttsx3, httpx,
python-multipart) predate this file and are unchanged by it.

This file is a human-readable explanation, not the redistribution
artifact itself — see "Where this ships" below for the actual notice
file bundled with the installed application and installer.

## System tray: pywin32, not pystray

**Decision:** the tray icon (`app/launcher/tray.py`) is implemented
directly on `pywin32` (`win32gui`/`win32con`/`win32api` — raw
`Shell_NotifyIcon`, a hidden message window, a native popup menu), not
on the `pystray` library originally used in this pass's first draft.

### Why pystray was rejected

- **License:** LGPL-3.0 (confirmed via PyPI's classifier and pystray's
  own repository, <https://github.com/moses-palmer/pystray>).
- **The actual question, stated precisely:** LGPLv3 Section 4 ("Combined
  Works") requires that a user of the combined work be able to modify
  the LGPL-covered library and relink/recombine a modified copy with
  the rest of the application. Whether a PyInstaller `--onedir` bundle
  (which does extract each dependency as individual, individually
  replaceable `.pyc`/compiled files into a directory, unlike `--onefile`)
  satisfies that in practice is a genuinely reasonable position to hold
  — but it is a legal compliance conclusion, not an engineering fact,
  and this project has no legal review process to stand behind it.
  Rather than assert compliance without being able to back it, or ship
  the library and hope, the library was removed.
- This is a **replacement**, not a mitigation: no LGPL-licensed code
  ships in JARVIS at all, so the question above no longer needs an
  answer for this project.

### Why pywin32

- **Version pinned:** `312` (see `requirements-windows.txt`), the
  `cp311-win_amd64` wheel matching this project's pinned Python 3.11 —
  uploaded 2026-06-04, i.e. within about two months of this writing.
- **License:** PSF-2.0 (Python Software Foundation License 2.0),
  confirmed via PyPI's classifier and the project's own
  <https://github.com/mhammond/pywin32> (license badge: `PSF-2.0`).
  Permissive, no copyleft, no source-disclosure or relinking obligation
  regardless of how it's bundled. pywin32's own README states it
  "contains a mix of differently licensed code" internally and points
  to the in-source license files as authoritative for individual files;
  every classifier, badge, and community reference found describes the
  aggregate as permissively licensed, with no GPL/LGPL component
  identified anywhere in it. A full file-by-file re-audit of a
  ~5,000-commit C-extension project is beyond what this pass can
  exhaustively re-verify — but that is a categorically lower-risk
  position than pystray's, because *no* permissive-license combination
  requires the relinking/disclosure analysis LGPL does; the compliance
  question that motivated removing pystray simply does not arise here.
- **Transitive dependencies:** none (`Requires-Dist: None` on PyPI) —
  nothing further to verify.
- **Maintenance:** the de facto standard, long-established Windows
  Python extension package; a release shipped within the last two
  months, vastly more current than pystray's ~2-year-old last release.
- **Trade-off, stated honestly:** pywin32 has no cross-platform
  abstraction — the tray code is raw Win32 API calls. That's a genuine
  increase in code complexity and, because pywin32 has no non-Windows
  wheel at all, this code could not even be import-checked in this
  project's Linux development/CI sandbox (unlike pystray, which at
  least imports, if unsafely, on Linux). It is Windows-only, which is
  an exact match for an app that only ever ships a Windows tray icon in
  the first place — not a cross-platform library pulling in
  platform-conditional extras (`python-xlib`, `pyobjc-framework-Quartz`)
  this project never uses. Real verification of the native code path is
  necessarily the manual Windows acceptance test, not automation — see
  the packaging report.

## API key storage: keyring

**Purpose:** stores the Anthropic API key in the Windows credential
store (`app/core/credentials.py`) instead of a plaintext `.env` file —
see `app/config.py::Settings.effective_api_key`.

- **Version pinned:** `25.7.0` (see `requirements-windows.txt`),
  uploaded 2025-11-16 — actively maintained.
- **License:** MIT (confirmed via PyPI classifier). Permissive.
- **Transitive dependency closure, every one individually verified via
  PyPI (not assumed from keyring's own reputation):**

  | Package | Condition | License |
  |---|---|---|
  | `pywin32-ctypes` | `sys_platform == "win32"` (this project's target) | BSD-3-Clause |
  | `jaraco.classes` | unconditional | MIT |
  | `jaraco.functools` | unconditional | MIT |
  | `jaraco.context` | unconditional | MIT |
  | `importlib_metadata` | `python_version < "3.12"` (applies — this project pins 3.11) | Apache-2.0 |
  | `SecretStorage`, `jeepney` | `sys_platform == "linux"` | not applicable — never installed on the Windows build |

  Every entry that actually applies to a Windows install is permissive;
  no copyleft license anywhere in the closure. Note `pywin32-ctypes` is
  a distinct, smaller package from the `pywin32` used by the tray (a
  ctypes-only subset keyring depends on so it doesn't require the full
  `pywin32` install) — both are pinned here and coexist without
  conflict.
- **A real robustness finding, not a hypothetical:** during development,
  calling `keyring.get_password()` in this project's own Linux sandbox
  crashed with a Rust-level `pyo3_runtime.PanicException` (a broken
  `cryptography`/`cffi` native extension backing keyring's Linux
  `SecretService` backend) — and a plain same-thread `try/except
  Exception` around that call did **not** catch it; the interpreter
  just exited. `app/core/credentials.py` isolates every keyring call on
  its own thread with a bounded timeout (the same pattern already used
  for tool execution and STT transcription) specifically because this
  was observed, not assumed — verified empirically that reading the
  result through `concurrent.futures.Future.result()` does catch it
  reliably, restoring the "external call can never crash or hang the
  caller" guarantee. The real target platform's backend
  (`pywin32-ctypes`-based `WinVaultKeyring`) never goes near
  `cryptography`/`SecretStorage`/Rust at all, so this exact failure
  mode is Linux-sandbox-specific — but the isolation stays in place
  regardless, on the same "don't fully trust an external call" basis
  the rest of this codebase already applies elsewhere.

## Native desktop window: pywebview + pythonnet

**Purpose:** the release-candidate packaging pass replaced opening the
dashboard in the user's default browser with a real, dedicated
application window (`app/launcher/webview_window.py`) — title, icon,
resizing, minimize/maximize/close, correct taskbar identity — instead of
another browser tab.

- **Version pinned:** `pywebview==6.2.1`, `pythonnet==3.1.0` (see
  `requirements-windows.txt`) — both verified as the current release at
  the time of writing by installing them directly and inspecting the
  installed package metadata, not assumed from either project's PyPI
  page description.
- **License:** pywebview is BSD-3-Clause (confirmed via `pip show`).
  pythonnet is MIT (confirmed via its `dist-info/METADATA`'s
  `License-Expression: MIT`). Both permissive, no copyleft.
- **Why pythonnet is a dependency at all:** verified directly by reading
  pywebview 6.2.1's own source (`webview/guilib.py`), not assumed from
  its "lightweight wrapper" reputation — on Windows, pywebview has
  exactly one practical backend, `webview/platforms/winforms.py`, and it
  requires `pythonnet` (.NET/CLR interop) to host a WinForms window.
  There is no lighter Windows backend to pick instead (the only
  alternative `guilib.py` offers on Windows is a Qt backend, which would
  trade pythonnet for PyQt/PySide — a heavier and, in PyQt's GPL/
  commercial-dual-license case, worse licensing trade, not a better one).
- **A real finding this pass acted on, not just documented:**
  `winforms.py` picks between the modern WebView2 (Chromium-based)
  control and the legacy `mshtml` (Internet Explorer/Trident) control
  based on a registry probe for the Edge/WebView2 runtime — silently,
  with no signal to the caller either way (confirmed by reading both
  `webview/platforms/winforms.py` and the separate `webview/platforms/
  mshtml.py` fallback module directly). An old Trident-engine window
  would be a real security downgrade for a screen that collects an API
  key, so `webview_window.py` never allows that trade silently: it
  forces `gui="edgechromium"` explicitly in `webview.start()`, and if
  that can't initialise for any reason (WebView2 Runtime genuinely
  absent, pythonnet/.NET missing, or anything else), window creation
  raises and the app falls back to opening the dashboard in the default
  browser instead — see that module's docstring and
  `app/launcher/gui.py::_open_main_window()`.
- **Transitive dependency closure, every one individually verified (not
  assumed from either project's own reputation):**

  | Package | License | Depends on |
  |---|---|---|
  | `bottle` (pywebview) | MIT | — |
  | `proxy_tools` (pywebview) | MIT | — |
  | `typing_extensions` (pywebview) | PSF-2.0 | — |
  | `clr_loader` (pythonnet) | MIT | `cffi` |
  | `cffi` (clr_loader) | MIT (MIT No Attribution variant) | — |

  Every entry is permissive; no copyleft anywhere in the closure.
- **Honesty note, matching this codebase's existing standard for native
  code:** the actual native window — WinForms hosting, WebView2
  rendering, the cross-thread coordination with the pywin32 tray running
  on its own thread (`app/launcher/gui.py::run_windowed()`) — has no
  automated test on this project's Linux dev/CI, the same honest
  limitation already true of the tray's own native message loop (see
  "Why pywin32" above). `tests/test_launcher_webview_window.py` proves
  the actual close-to-tray-vs-quit decision logic and the forced-
  edgechromium/browser-fallback behavior against an injected fake
  `webview` module; the real native window is verified only by the
  manual Windows acceptance test.

## The neural voice: ONNX Runtime, numpy, and the Kokoro model

**Decision:** JARVIS's normal speaking voice is Kokoro 82M running
locally on ONNX Runtime. The old SAPI5 voice is retained only as a last
resort, and Windows' own natural voices sit between them — see
`app/voice/engines.py` for the selection order and
`app/voice/kokoro/assets.py` for the complete manifest, which is kept as
data so this file and the in-app licence page cannot drift apart.

The constraint driving every choice below was the owner's: the complete
distributed dependency chain stays permissive, and no GPL component is
bundled, imported, invoked or downloaded.

- **ONNX Runtime:** MIT. Runs the model on the CPU. Bundled in the
  installer. No GPU is required and none is used — the model is the
  quantized build specifically so it runs on an ordinary desktop CPU.
- **numpy:** BSD-3-Clause. Arrives as an ONNX Runtime dependency and is
  used directly for the sample arrays. Bundled in the installer.
- **Kokoro 82M (ONNX, quantized) and the `bm_*` voice packs:**
  Apache-2.0. *Not* bundled — downloaded on request from a pinned
  repository revision, verified against the SHA-256 digests recorded in
  `app/voice/kokoro/assets.py`, and never fetched without the size,
  source and licence being shown first.
- **CMU Pronouncing Dictionary (derived lexicon):** CMU's own licence,
  reproduced verbatim at `docs/licences/CMUDICT-LICENSE.txt` and shipped
  with the application. Deliberately **not** labelled with an SPDX
  identifier: the text reads like a two-clause BSD licence but is not
  literally one, and the acknowledgement it asks for is carried in
  `app/voice/kokoro/lexicon_source.py` and the About page. The upstream
  data files are pinned by content hash and converted by a script in
  this repository; the `cmudict` *PyPI package* is GPL-3.0-or-later and
  is not installed.

### What was rejected, and why

- **Piper** — GPL-3.0-or-later since its rewrite. Out on licence alone.
- **`kokoro-onnx`** — depends on `espeakng-loader` and
  `phonemizer-fork`, which exist to drive espeak-ng (GPL-3.0).
- **`misaki[en]`** — misaki itself is Apache-2.0 and its English G2P can
  run without an espeak fallback, so the licence is not the objection.
  The `[en]` extra is: as a set it pulls `espeakng-loader` and
  `phonemizer-fork`, so that extra is never installed.
- **pyttsx3's espeak driver** — pyttsx3 is bundled for the last-resort
  tier, but its espeak ctypes bindings are excluded from the build
  (`packaging/jarvis.spec`). They are pyttsx3's own source rather than
  GPL text, but they are a loader for a GPL library, and a Windows-only
  application that can never select that driver has no reason to carry
  one. Enforced by `tests/test_licence_policy.py` against the real
  installed tree.

## Where this ships

`scripts/build-installer.ps1` copies this file into the PyInstaller
onedir output as `THIRD_PARTY_NOTICES.md`, and the Inno Setup installer
(`packaging/jarvis.iss`) installs it alongside `JARVIS.exe` and surfaces
it from the installer's own info page — see those files for the exact
mechanism. Anything JARVIS bundles and redistributes gets its license
disclosed in the actual installed product, not only in this
repository's docs.

## Pre-existing dependency referenced above

- **Pillow:** MIT-CMU (SPDX `license_expression`, confirmed via PyPI).
  Permissive. Already a `requirements.txt` dependency (screenshots) —
  listed here because `app/launcher/tray.py`'s icon handling also uses
  it, not because packaging introduced it.

## Build tools (not bundled — a different category from everything above)

Neither of these ships inside `JARVIS.exe` or the installer; they only
run at build time, on the machine producing the release. Documented
here anyway because "verify the license" was applied to every
dependency this pass touches, build-time or not.

- **PyInstaller** (pinned `6.21.0` — the latest stable release at the
  time of writing, verified via PyPI, not assumed): "GPLv2-or-later
  with a special exception which allows [it] to build and distribute
  non-free programs (including commercial ones)" (PyPI classifier,
  verbatim). That exception is exactly what makes this a non-issue for
  JARVIS's own licensing — the well-established "compiler exception"
  pattern (the same shape as GCC's runtime library exception): programs
  *built with* PyInstaller are not thereby placed under the GPL,
  because PyInstaller's own GPL-covered source is not redistributed as
  part of the built executable in a way that would trigger that
  obligation.
- **Inno Setup** (pinned `6.7.1` — the newest version actually published
  on Chocolatey's community package repository, which is how this
  project's build pipeline installs it; upstream's own latest is 6.7.3,
  confirmed via jrsoftware.org/isdl.php, but Chocolatey lags upstream —
  see `packaging/jarvis.iss`'s own header comment for the verified
  detail and why 6.x was chosen over the newer 7.0.2 regardless): a
  modified zlib/libpng license (permissive, free for any use including
  commercial — verified via jrsoftware.org/files/is/license.txt
  directly, not assumed from the separate, non-binding "please consider
  a commercial license" donation request on the project's info page).
  Not bundled; only its compiler (ISCC.exe) runs at build time.
