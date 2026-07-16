"""Secret detection for Phase 8 settings & personality memory.

JARVIS must never store secrets. Before any user-supplied value is written to
the settings store or the preferences (personality memory) table, it is scanned
here. If a known secret pattern or an explicit credential assignment is found,
the save is rejected with a friendly message — the value is never persisted.

This is a defensive local guard, not a cryptographic classifier: it errs toward
rejecting obvious tokens (API keys, GitHub/Netlify/AWS/Google keys, private key
blocks) and `password: value` style assignments.
"""

import re
from typing import Optional

# (label, compiled pattern) — order matters only for which label is reported first.
_SECRET_PATTERNS = [
    ("an Anthropic API key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{6,}")),
    ("an OpenAI-style API key", re.compile(r"\bsk-[A-Za-z0-9]{20,}")),
    ("a GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}")),
    ("a GitHub fine-grained token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}")),
    ("a Netlify token", re.compile(r"\bnfp_[A-Za-z0-9]{20,}")),
    ("an AWS access key id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("a Google API key", re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}")),
    ("a Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}")),
    ("a private key block", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")),
    ("a bearer token", re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{20,}")),
]

# Explicit "credential = value" / "secret: value" style assignments.
_CREDENTIAL_ASSIGNMENT = re.compile(
    r"\b(secret|token|key)\b\s*[:=]\s*\S+",
    re.IGNORECASE,
)

# Strong credential nouns. JARVIS never stores these in settings or memory, so
# their mere presence rejects the save. We deliberately err toward rejecting a
# harmless phrase over silently persisting a real password or token: the user
# can simply rephrase, but a leaked secret cannot be un-stored.
_CREDENTIAL_KEYWORDS = re.compile(
    r"\b("
    r"passwords?|passwd|passphrases?|pwd|"
    r"api[_\-\s]?keys?|secret[_\-\s]?keys?|"
    r"access[_\-\s]?tokens?|auth[_\-\s]?tokens?|"
    r"private[_\-\s]?keys?|client[_\-\s]?secrets?"
    r")\b",
    re.IGNORECASE,
)


def find_secret(text: str) -> Optional[str]:
    """Return a human-readable label of the detected secret, or None if clean."""
    if not text:
        return None
    for label, pattern in _SECRET_PATTERNS:
        if pattern.search(text):
            return label
    if _CREDENTIAL_KEYWORDS.search(text) or _CREDENTIAL_ASSIGNMENT.search(text):
        return "a stored credential (password/token/secret)"
    return None


def contains_secret(text: str) -> bool:
    """Convenience boolean wrapper around find_secret()."""
    return find_secret(text) is not None
