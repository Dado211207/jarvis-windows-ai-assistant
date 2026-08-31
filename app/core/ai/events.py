"""One safe row on the Logs page when an AI provider request fails.

**Why this exists.** On a real Windows 11 machine every Anthropic request
was being rejected with HTTP 400, and the owner's Logs page showed nothing
at all. The only thing on screen was "The AI provider returned an error."
There was no way to tell a rejected key from an unfunded account from a
missing workspace header without attaching a debugger, which is not a
thing the owner of a desktop application should have to do.

**What a row is allowed to contain.** Five things, and they are chosen so
that the row is useful to the person holding the machine and useless to
anyone who steals the database:

    category    "ai_provider" — what kind of event this is
    provider    "anthropic" / "ollama" — which one failed
    status      the ErrorCategory name, e.g. provider_workspace_required
    reference   the correlation id already minted for the server-side log
    timestamp   added by SQLite

**What a row must never contain**, and why each is named rather than
implied: the API key, the workspace ID, an authorization header, the
provider's raw response body, the user's message, a username, or a local
path. None of them is needed to know what went wrong, and every one of
them is a thing that outlives the moment in a file on disk. The safe
sentence written here is JARVIS's own fixed text for the category (see
app/core/errors.py::safe_message), never the provider's.

The reference id is the join: the full exception, with its stack, is in
the server log under the same id. A support conversation can quote the
reference without quoting anything sensitive.

**Never raises.** A diagnostic that can take down the request it is
describing is worse than no diagnostic — the caller is already on a
failure path.
"""

from typing import Optional

from app.core.errors import ErrorCategory, safe_message
from app.logging_config import get_logger

logger = get_logger("core.ai.events")

#: The `command` column value. One category, so the Logs page can group
#: these without parsing anything.
EVENT_CATEGORY = "ai_provider"


def record_provider_failure(
    provider: str,
    category: ErrorCategory,
    correlation_id: str,
    detail: Optional[str] = None,
) -> None:
    """Write one safe row describing a provider failure.

    *detail* is only ever JARVIS's own text — a provider's credential-free
    note about, say, which local models are installed. It is never the
    provider's raw message, and it is redacted again at the database
    boundary regardless.
    """
    try:
        from db.database import get_db

        message = detail or safe_message(category)
        get_db().log_action(
            command=EVENT_CATEGORY,
            tool_name=str(provider or "unknown"),
            status=category.value,
            message=f"{message} (reference {correlation_id})",
        )
    except Exception:  # noqa: BLE001 — a diagnostic must not break the failure path
        logger.warning("Could not record the provider failure event.", exc_info=True)
