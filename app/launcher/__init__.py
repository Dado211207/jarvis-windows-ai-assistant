"""Packaged-app launcher — single-instance guard, background server
lifecycle, and (see app/launcher/tray.py) the system tray control that
replaces a visible console window for the installed Windows app.

Nothing in this package changes how JARVIS runs from source: `python -m
app.main` and `python -m app.api.server` are untouched. This package is
only reached via run_jarvis.py's packaged entry point.
"""
