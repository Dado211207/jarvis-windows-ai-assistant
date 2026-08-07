# Third-party notices

Dependencies introduced specifically for Windows installer packaging
(v0.2 packaging pass). This file covers *packaging-only* dependencies —
`requirements.txt`'s existing dependencies (FastAPI, uvicorn, pydantic,
psutil, Pillow, python-dotenv, anthropic, pyttsx3, httpx,
python-multipart) predate this file and are unchanged by it.

## pystray

- **Purpose:** the Windows system tray icon and menu (`app/launcher/tray.py`)
  that replaces a visible console window as JARVIS's running-app control
  surface.
- **Version pinned:** `0.19.5` (see `requirements-windows.txt`).
- **License:** LGPL-3.0. Used here as an unmodified, dynamically-imported
  library dependency — not forked or modified — which is the standard
  basis for LGPL compliance when bundling via PyInstaller (the library
  stays a separate, distinctly-identifiable component inside the frozen
  bundle rather than being statically merged into a single object the
  way C static linking would be). Full LGPL-3.0 text and source:
  <https://github.com/moses-palmer/pystray>.
- **Maintenance:** actively used (500+ GitHub stars at the time of this
  writing), not archived. Its most recent PyPI release (0.19.5) shipped
  2023-09-17 — stated plainly here since "verify maintenance state" was
  an explicit requirement for this pass, not because it's disqualifying:
  a focused, single-purpose tray-icon library having a multi-year gap
  between releases is not unusual for a "done" utility package, but it
  is a real fact worth the repo owner knowing rather than glossing over.
- **Platform footprint:** its own platform-conditional dependencies
  (`python-xlib` on Linux, `pyobjc-framework-Quartz` on macOS) are gated
  by environment markers, so a Windows `pip install` pulls in neither —
  only Pillow (already a dependency) and `six`.
- **Why not an alternative:** pystray is the de facto standard
  cross-platform (Win32/AppKit/GTK/Xorg) Python tray library and the one
  most commonly paired with PyInstaller-packaged desktop apps; no
  actively-maintained alternative with a smaller footprint or more
  permissive license was found to offer equivalent Windows tray-menu
  support (dynamic labels, enabled/disabled items) with as little
  additional surface area.
