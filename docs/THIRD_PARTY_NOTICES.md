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
