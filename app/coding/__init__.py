"""Coding Workspace — a scoped, explicit mode in which JARVIS may inspect
and change a project the user has selected.

Nothing here is importable by the ordinary assistant path. Coding tools
live in this package's own registry (app/coding/registry.py) and are
never added to app.core.tool_registry.registry, so no chat message can
reach one. See docs/coding-workspace-architecture.md §2.2, and
tests/test_coding_isolation.py, which asserts it rather than trusting it.
"""
