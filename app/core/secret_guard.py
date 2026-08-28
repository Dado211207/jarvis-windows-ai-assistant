"""Refusing to write a secret into long-term memory.

JARVIS remembers what it is asked to remember, in plaintext, in a SQLite
file that lives on the user's disk until they delete it. That is the
right shape for "remember that I prefer dark roast" and the wrong shape
for "remember that my key is sk-ant-…". Somebody typing the second thing
is not making a considered decision about credential storage; they are
answering the assistant conversationally, and the assistant should say
no.

**Detection happens before persistence, never after.** The value must not
reach the database at all, so a later purge is unnecessary — there is
nothing to purge. `app/core/redaction.py` is a different mechanism for a
different problem (tool inputs on their way to a log line, the audit
trail or a WebSocket event) and is kept as defence in depth, not as the
answer to this one.

**The detected value is never echoed back.** `find_secret()` returns a
label — "an Anthropic API key" — and never the matched text. A guard that
quotes what it caught puts the secret in the API response, the event
stream and the log, which is the problem it was added to prevent.

Ported from PR #13's `secret_guard.py`, with one deliberate change:
that version rejected any sentence merely *containing* a credential
noun, so "remind me to change my password on Friday" could not be saved.
The trade-off there was stated and defensible, but it makes the feature
annoying in the common case where no secret is present. This version
splits the decision:

* **A credential-shaped string is always refused.** These patterns match
  things that are not English — a real key, a private-key header, a
  bearer token. False positives are near zero.
* **A credential noun followed by a value is refused.** "my password is
  hunter2", "api_key = abc123", "token: ghs_…". The noun alone is not
  enough; there has to be something being assigned.
* **A bare mention is allowed.** "remind me to change my password" is
  stored, because there is no secret in it.

The residual risk of the third rule is real and worth stating: a value
that looks exactly like an ordinary word ("my password is banana") is
caught by rule two, but a sentence that conveys a credential without any
assignment structure at all would not be. That is judged the better
error to make than refusing every sentence that mentions a password.
"""

import re
from typing import Optional

# ---------------------------------------------------------------------------
# Tier 1 — strings that are credentials by shape.
#
# Each of these matches something no ordinary sentence contains. Order
# only decides which label is reported when a value matches more than
# one.
# ---------------------------------------------------------------------------

_SECRET_PATTERNS = (
    ("an Anthropic API key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{6,}")),
    # Current OpenAI project/service-account keys contain a named prefix
    # and URL-safe separators. Keep this separate from the narrower legacy
    # pattern so ordinary prose such as "sk-proj plan" is not masked.
    (
        "an OpenAI API key",
        re.compile(r"\bsk-(?:proj|svcacct)-[A-Za-z0-9_\-]{16,}"),
    ),
    ("an OpenAI-style API key", re.compile(r"\bsk-[A-Za-z0-9]{20,}")),
    # ElevenLabs keys are `sk_` followed by a long token. Distinct from
    # OpenAI's `sk-` (underscore, not hyphen), so the two never collide.
    #
    # Matched as alphanumeric rather than strictly hex, which is what the
    # current format uses: `sk_` plus 32 characters is already a
    # distinctive enough shape that a false positive is close to
    # unimaginable, and being slightly wider means a format change does
    # not silently stop protecting anyone.
    #
    # The older bare 32-character hex form is deliberately NOT matched:
    # it is indistinguishable from any MD5 sum, and a rule that refused
    # every checksum in a memory is a rule people learn to work around.
    ("an ElevenLabs API key", re.compile(r"\bsk_[A-Za-z0-9]{32,}")),
    ("a GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}")),
    ("a GitHub fine-grained token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}")),
    ("a Netlify token", re.compile(r"\bnfp_[A-Za-z0-9]{20,}")),
    ("a GitLab token", re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,}")),
    ("a Hugging Face token", re.compile(r"\bhf_[A-Za-z0-9]{20,}")),
    ("an npm token", re.compile(r"\bnpm_[A-Za-z0-9]{20,}")),
    ("a PyPI token", re.compile(r"\bpypi-[A-Za-z0-9_\-]{30,}")),
    ("a Stripe secret key", re.compile(r"\b[rs]k_(?:live|test)_[A-Za-z0-9]{16,}")),
    (
        "a SendGrid API key",
        re.compile(r"\bSG\.[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{16,}"),
    ),
    ("an AWS access key id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("a Google API key", re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}")),
    ("a Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}")),
    ("a private key block", re.compile(r"-----BEGIN\s+[A-Z0-9 ]*PRIVATE KEY-----", re.IGNORECASE)),
    ("a JSON Web Token", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.")),
    ("a bearer token", re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{20,}", re.IGNORECASE)),
)

# ---------------------------------------------------------------------------
# Tier 2 — a credential noun with something assigned to it.
# ---------------------------------------------------------------------------

# Written as one alternation so "api key", "api-key" and "api_key" are all
# the same noun. The separator class covers a space, a hyphen and an
# underscore because people type all three.
_SEP = r"[ _\-]?"
_CREDENTIAL_NOUN = (
    r"(?:"
    rf"passwords?|passwd|passphrases?|pwd|"
    rf"api{_SEP}keys?|secret{_SEP}keys?|private{_SEP}keys?|"
    rf"access{_SEP}tokens?|auth{_SEP}tokens?|bearer{_SEP}tokens?|refresh{_SEP}tokens?|"
    rf"client{_SEP}secrets?|"
    rf"credentials?|secrets?|tokens?|api{_SEP}secrets?"
    r")"
)

# The connector between the noun and the thing being assigned. Covers
# both code ("key=abc") and speech ("my key is abc").
_CONNECTOR = r"(?:\s*[:=]\s*|\s+(?:is|are|was|were|will\s+be|shall\s+be)\s+)"

# Words that follow a credential noun in ordinary sentences and are
# plainly not values. Without this, "my api key is not working" reads as
# an assignment of the value "not".
_NOT_A_VALUE = frozenset(
    """
    a an the my your our their his her its this that these those
    not no none never nothing nowhere
    missing invalid expired wrong incorrect correct valid
    set unset empty blank gone lost stored saved changed updated rotated
    working broken failing failed fine ok okay good bad safe secure
    required needed necessary optional important urgent ready
    there here somewhere anywhere different same new old
    in on at for from with without about
    """.split()
)

_ASSIGNMENT = re.compile(
    rf"\b{_CREDENTIAL_NOUN}\b{_CONNECTOR}[\"'`]?(?P<value>[^\s\"'`]+)",
    re.IGNORECASE,
)


def _looks_like_a_value(candidate: str) -> bool:
    """Whether the text after a credential noun is plausibly the secret.

    Deliberately generous: anything that is not an obvious sentence word
    counts. Someone writing "my password is hunter2" is assigning a
    value; someone writing "my password is expired" is not. Getting this
    slightly wrong in the rejecting direction costs a rephrase; getting
    it wrong the other way stores a credential forever.
    """
    stripped = candidate.strip(".,;:!?)]}\"'`")
    if not stripped:
        return False
    return stripped.lower() not in _NOT_A_VALUE


def find_secret(text: str) -> Optional[str]:
    """A label for the kind of secret found, or None if the text is clean.

    **Never returns the matched text.** The caller puts this label in a
    message the user reads, and that message travels through the API
    response, the event stream and the log.
    """
    if not text:
        return None

    for label, pattern in _SECRET_PATTERNS:
        if pattern.search(text):
            return label

    for match in _ASSIGNMENT.finditer(text):
        if _looks_like_a_value(match.group("value")):
            return "a password, token or other credential"

    return None


def contains_secret(text: str) -> bool:
    """Convenience boolean wrapper around find_secret()."""
    return find_secret(text) is not None


def refusal_message(label: str) -> str:
    """What the user reads. Explains the rule and what to do instead —
    and names only the *kind* of secret, never the value."""
    return (
        f"That looks like {label}, so it was not saved. JARVIS never stores "
        "passwords, API keys or tokens in memory, where they would sit in "
        "plain text. API keys belong in the relevant Settings or Voice "
        "provider control, which puts them in the Windows Credential Manager."
    )


class SecretRejected(Exception):
    """Raised by the database layer when a write carries a credential.

    A backstop, not the user-facing path: `app/core/memory.py` checks
    first and returns a readable refusal. This exists so that a future
    caller which forgets to check cannot quietly persist a secret — the
    guarantee is enforced at the only place rows are actually inserted.

    Carries the label, never the value, for the same reason find_secret()
    does not return what it matched: an exception message ends up in
    logs and error responses.
    """

    def __init__(self, label: str) -> None:
        super().__init__(f"Refused to store {label} in the database.")
        self.label = label
