"""Personality / preferences memory — Phase 8.

Explicit, local-only memory of user preferences. JARVIS only writes here when
the user clearly asks ("remember that…", "save preference…") — never silently,
never from ordinary chat, and never for secrets. Entries are categorised, kept
in the SQLite ``preferences`` table, and can be listed, searched, and forgotten.

Tools registered:
  remember_preference  (SAFE)              — save one explicit preference
  list_preferences     (SAFE)              — "what do you remember"
  search_preferences   (SAFE)              — keyword search
  forget_preference    (SAFE)              — delete a single named entry
  clear_preferences    (APPROVAL_REQUIRED) — wipe all personality memory
"""

import re
from typing import List

from app.core.models import PermissionLevel, Preference, ToolCategory, ToolDefinition
from app.core.secret_guard import find_secret
from app.logging_config import get_logger

logger = get_logger("preferences")

MAX_PREF_LEN = 500
VALID_CATEGORIES = (
    "profile", "style", "voice", "ui", "command", "general_preference",
)

# Keyword → category heuristics for auto-classifying an explicit preference.
_CATEGORY_KEYWORDS = [
    ("profile", ("call me", "my name", "name is", "i am ", "i'm ", "refer to me")),
    ("style", ("answer", "response", "reply", "short", "brief", "concise",
               "detailed", "verbose", "diacritic", "tone", "formal", "casual",
               "direct", "language", "english", "serbian")),
    ("voice", ("voice", "tts", "speak", "speech", "read aloud", "rate", "volume")),
    ("ui", ("theme", "dark", "light", "ui ", "dashboard", "compact", "layout",
            "sidebar")),
    ("command", ("command", "alias", "shortcut", "pin", "favourite", "favorite")),
]

# Leading filler words to strip when deriving a short title from the text.
_TITLE_PREFIX = re.compile(
    r"^(that\s+|i\s+(usually\s+|always\s+|generally\s+)?|my\s+|to\s+|please\s+)+",
    re.IGNORECASE,
)


def detect_category(text: str) -> str:
    low = text.lower()
    for category, keywords in _CATEGORY_KEYWORDS:
        if any(kw in low for kw in keywords):
            return category
    return "general_preference"


def _derive_title(text: str) -> str:
    stripped = _TITLE_PREFIX.sub("", text.strip())
    stripped = stripped or text.strip()
    words = stripped.split()
    title = " ".join(words[:8])
    if len(title) > 60:
        title = title[:57].rstrip() + "…"
    return title or "preference"


# --- tool implementations --------------------------------------------------

def remember_preference(text: str) -> dict:
    text = (text or "").strip()
    if not text:
        return {"success": False, "message": "Nothing to remember — please add some text.",
                "data": None}
    if len(text) > MAX_PREF_LEN:
        return {"success": False,
                "message": f"That preference is too long (max {MAX_PREF_LEN} characters).",
                "data": None}

    secret = find_secret(text)
    if secret is not None:
        logger.info("Refused to store a preference containing a secret.")
        return {
            "success": False,
            "message": (
                f"That looks like {secret}. JARVIS never stores secrets, passwords, "
                "or API keys in memory. Nothing was saved."
            ),
            "data": None,
        }

    from db.database import get_db

    category = detect_category(text)
    title = _derive_title(text)
    pref_id = get_db().add_preference(
        title=title, value=text, category=category, source="user", is_sensitive=False
    )
    logger.info("Preference saved (id=%s, category=%s)", pref_id, category)
    return {
        "success": True,
        "message": f"Got it — I'll remember that. (category: {category})",
        "data": {"id": pref_id, "title": title, "category": category},
    }


def _format_preference(p: Preference) -> dict:
    return {
        "id": p.id,
        "title": p.title,
        "value": p.value,
        "category": p.category,
        "created_at": p.created_at,
        "updated_at": p.updated_at,
    }


def list_preferences() -> dict:
    from db.database import get_db

    prefs: List[Preference] = get_db().get_preferences()
    if not prefs:
        return {
            "success": True,
            "message": "I don't have any saved preferences yet. Tell me something with "
                       "'remember that …'.",
            "data": [],
        }
    lines = [f"I remember {len(prefs)} preference(s):"]
    for p in prefs:
        lines.append(f"  • [{p.category}] {p.value}")
    return {
        "success": True,
        "message": "\n".join(lines),
        "data": [_format_preference(p) for p in prefs],
    }


def search_preferences(query: str) -> dict:
    query = (query or "").strip()
    if not query:
        return {"success": False, "message": "Please provide something to search for.",
                "data": None}
    from db.database import get_db

    results = get_db().search_preferences(query)
    if not results:
        return {"success": True, "message": f"No preferences match '{query}'.", "data": []}
    lines = [f"Found {len(results)} matching preference(s):"]
    for p in results:
        lines.append(f"  • [{p.category}] {p.value}")
    return {
        "success": True,
        "message": "\n".join(lines),
        "data": [_format_preference(p) for p in results],
    }


def preview_forget(text: str) -> dict:
    """Resolve which single preference a `forget <text>` refers to.

    Returns a status the router uses to decide whether to create an approval:
      empty     — no text supplied
      none      — nothing matches (benign; nothing to delete)
      ambiguous — several match (ask the user to be more specific)
      single    — exactly one match (gate its deletion behind approval)
    """
    text = (text or "").strip()
    if not text:
        return {"status": "empty",
                "message": "Tell me which preference to forget.", "matches": []}
    from db.database import get_db

    matches = get_db().search_preferences(text)
    if not matches:
        return {"status": "none",
                "message": f"I have no saved preference matching '{text}'.", "matches": []}
    if len(matches) > 1:
        lines = [f"That matches {len(matches)} preferences — please be more specific:"]
        for p in matches:
            lines.append(f"  • {p.value}")
        return {"status": "ambiguous", "message": "\n".join(lines), "matches": matches}
    return {"status": "single", "matches": matches, "preference": matches[0]}


def forget_preference(text: str = None, pref_id: int = None) -> dict:
    """Delete a single preference. Registered APPROVAL_REQUIRED.

    Normal flow: the router resolves the target and stores ``pref_id``; the
    confirm endpoint then calls this via ``execute_approved`` to delete exactly
    that one entry. A ``text`` fallback is kept for direct/legacy callers.
    """
    from db.database import get_db

    db = get_db()

    if pref_id is not None:
        pref = db.get_preference(pref_id)
        if pref is None:
            return {"success": False,
                    "message": "That preference no longer exists — nothing was deleted.",
                    "data": None}
        db.delete_preference(pref_id)
        logger.info("Preference forgotten via approval (id=%s)", pref_id)
        return {"success": True, "message": f"Forgotten: {pref.value}", "data": {"id": pref_id}}

    preview = preview_forget(text)
    status = preview["status"]
    if status in ("empty", "none"):
        return {"success": status == "none", "message": preview["message"], "data": []}
    if status == "ambiguous":
        return {"success": False, "message": preview["message"],
                "data": [_format_preference(p) for p in preview["matches"]]}

    pref = preview["preference"]
    db.delete_preference(pref.id)
    logger.info("Preference forgotten (id=%s)", pref.id)
    return {"success": True, "message": f"Forgotten: {pref.value}", "data": {"id": pref.id}}


def clear_preferences() -> dict:
    """Delete ALL personality memory. Registered APPROVAL_REQUIRED."""
    from db.database import get_db

    count = get_db().clear_preferences()
    noun = "preference" if count == 1 else "preferences"
    logger.info("All preferences cleared: %d rows deleted.", count)
    return {
        "success": True,
        "message": f"Cleared {count} {noun} from personality memory.",
        "data": {"rows_deleted": count},
    }


def register_tools(registry) -> None:
    registry.register(
        ToolDefinition(
            name="remember_preference",
            description="Explicitly save a user preference to personality memory.",
            permission_level=PermissionLevel.SAFE,
            category=ToolCategory.MEMORY,
        ),
        remember_preference,
    )
    registry.register(
        ToolDefinition(
            name="list_preferences",
            description="List everything JARVIS remembers about the user's preferences.",
            permission_level=PermissionLevel.SAFE,
            category=ToolCategory.MEMORY,
        ),
        list_preferences,
    )
    registry.register(
        ToolDefinition(
            name="search_preferences",
            description="Search saved personality/preference memory by keyword.",
            permission_level=PermissionLevel.SAFE,
            category=ToolCategory.MEMORY,
        ),
        search_preferences,
    )
    registry.register(
        ToolDefinition(
            name="forget_preference",
            description="Forget (delete) a single named preference from memory. Requires approval.",
            permission_level=PermissionLevel.APPROVAL_REQUIRED,
            category=ToolCategory.MEMORY,
        ),
        forget_preference,
    )
    registry.register(
        ToolDefinition(
            name="clear_preferences",
            description="Delete all personality memory. Requires approval.",
            permission_level=PermissionLevel.APPROVAL_REQUIRED,
            category=ToolCategory.MEMORY,
        ),
        clear_preferences,
    )
