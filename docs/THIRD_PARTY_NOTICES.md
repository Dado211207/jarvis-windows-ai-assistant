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

### Where this ships

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
  it, not because packaging introduced it. pywin32 itself has zero
  transitive dependencies (`Requires-Dist: None`), so there is nothing
  else in the Windows packaging closure to disclose.
