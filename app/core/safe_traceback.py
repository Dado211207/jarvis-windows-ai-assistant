"""Describe an exception for a log file without rendering its value.

**Why this exists.** `app/core/errors.py::to_safe_error()` is the boundary
that keeps raw exception text out of anything the browser sees, and it
logged the exception in full on the server side so a developer could still
find the cause from a correlation id. That was the right shape until the
Anthropic workspace work, which introduced a failure whose *message
contains the thing the rest of the feature is careful never to write down*:

    If the workspace doesn't exist, or the key's user or service account
    doesn't have access to it, the API returns a 404 not_found_error with
    the message ``Workspace `<id>` not found.``

    — https://platform.claude.com/docs/en/manage-claude/authentication

`logger.error(..., exc_info=exc)` renders `str(exc)`. For the Anthropic
SDK that string is the provider's response body, and an SDK exception can
carry request headers with it. So a single keyword put a workspace ID —
and potentially an `x-api-key` header — into `jarvis.log`, a file that
lives on the user's disk indefinitely and is quoted into bug reports.

**What is kept.** Everything that is ours and none of what is theirs:

    the exception's type name       AnthropicNotFoundError
    the chain of causes' type names ProviderError <- AnthropicNotFoundError
    the traceback's frames          module.function:line, innermost last

A frame locates the failure precisely, which is what the traceback was for.
The exception's rendered value is the only part that carried provider text,
and it is the only part removed.

**Paths are trimmed to a module-ish name.** An absolute Windows path in a
packaged build starts `C:\\Users\\<the owner's name>\\…`, and CLAUDE.md's
process-lifecycle rules already forbid full paths in records that go into a
log file. `app/core/ai/anthropic_provider.py` is as much as anyone needs.

**Never raises.** It runs on a failure path; an exception here would
replace a described failure with an undescribed one.
"""

import traceback

#: Frames deeper than this add noise, not information. The innermost ones
#: are kept: that is where the failure actually happened.
MAX_FRAMES = 12

#: How many trailing path components identify a module without naming a
#: user. "core/ai/anthropic_provider.py" reads as well as the full path.
PATH_COMPONENTS = 3


def _short_path(filename: str) -> str:
    try:
        parts = str(filename).replace("\\", "/").split("/")
    except Exception:  # noqa: BLE001
        return "?"
    return "/".join(parts[-PATH_COMPONENTS:]) if parts else "?"


def exception_chain(exc: BaseException) -> str:
    """Type names only, outermost first: ``ProviderError <- BadRequestError``.

    The chain matters because JARVIS wraps SDK exceptions in its own
    ProviderError; knowing only the outer type would name our own code
    every time.
    """
    names = []
    seen = set()
    current = exc
    while current is not None and id(current) not in seen and len(names) < 5:
        seen.add(id(current))
        names.append(type(current).__name__)
        current = current.__cause__ or current.__context__
    return " <- ".join(names)


def frames(exc: BaseException) -> str:
    """The traceback as ``path:line in function``, innermost last.

    Deliberately assembled from `traceback.extract_tb`'s structured
    fields rather than `format_exception`, which appends the rendered
    exception value — the one thing this module exists to omit. The source
    line is left out too: it is our own code, but a line inside a
    formatting call can echo a value.
    """
    try:
        extracted = traceback.extract_tb(exc.__traceback__)
    except Exception:  # noqa: BLE001
        return "unavailable"
    if not extracted:
        return "none"
    kept = extracted[-MAX_FRAMES:]
    return " | ".join(
        f"{_short_path(frame.filename)}:{frame.lineno} in {frame.name}" for frame in kept
    )


def describe(exc: BaseException) -> str:
    """One line naming the exception and where it came from, with nothing
    in it that the exception itself produced."""
    try:
        return f"types={exception_chain(exc)} frames=[{frames(exc)}]"
    except Exception:  # noqa: BLE001 — a diagnostic must not become the failure
        return f"types={type(exc).__name__} frames=[unavailable]"


# There is deliberately no environment variable that turns the raw
# rendering back on. A switch that writes provider response bodies to disk
# is a switch that will be on somewhere, and the whole point of this module
# is that the guarantee does not depend on how a machine is configured.
