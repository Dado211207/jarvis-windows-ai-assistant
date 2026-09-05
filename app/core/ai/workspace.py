"""The Anthropic workspace ID: one place that knows its shape and its header.

**Why this exists.** Anthropic's *Authentication* guide splits API keys into
two kinds. A **workspace key** (which Anthropic now calls legacy) belongs to
the workspace it was made in, and requests using it "can omit the workspace
ID". A **personal key** or **service account key** is *identity-linked*: it
acts as a user or service account, and — quoting the guide — "If your API key
isn't scoped to a workspace, you must specify the workspace ID in the
`anthropic-workspace-id` header for each request."

JARVIS accepted only a key. On a real Windows 11 machine, a freshly created
identity-linked key therefore failed every request with HTTP 400:

    anthropic-workspace-id is required when authenticating with an
    identity-linked API key; send the id of the workspace this request
    acts in.

That is a JARVIS compatibility defect, not a bad key, not billing, and not a
network problem — which is why it gets its own error category rather than
being folded into "the provider returned an error".

**Shape.** Anthropic documents the value by example — `wrkspc_01JwQvzr7rXLA5AGx3HKfFUJ`
— and by prefix. It does **not** document a fixed length, so neither does
this module: pinning one would invent a constraint Anthropic never made and
would reject a valid future ID. What is checked is the documented prefix and
that the remainder is plain alphanumeric.

That last part is not cosmetic. This value is placed verbatim into an HTTP
header, so anything that could carry a newline, a colon or a control
character is refused here rather than handed to the SDK. A local check
cannot tell whether a workspace *exists* — only the API can, and it says so
with a 404 — so this is a shape gate, never a claim of validity.

**Where a person actually finds one.** Anthropic documents two routes and
one exception, and the exception is the whole reason this docstring exists:

    You can find a workspace's ID in the **ID** column of Settings →
    Workspaces in the Claude Console, or by calling the List Workspaces
    endpoint. **List Workspaces omits the Default Workspace; its ID is in
    the `anthropic-workspace-id` response header of any request that runs
    there.**

The first version of this feature named only the table. For an account
that has never created an additional workspace — which is the common case,
and was the owner's — that is a page which does not contain the value being
asked for. The simplest route avoids the question entirely: "You can also
scope the key to a specific workspace, which lets you skip setting a
workspace ID manually in future requests", and the Default Workspace can be
that workspace like any other. Every surface that mentions the table has to
mention the exception too; `tests/test_workspace_guidance.py` enforces it.

**Not a secret, but not public either.** The workspace ID identifies part of
the owner's Anthropic account. It is stored as local account metadata via
`app/core/preferences.py` (never the credential store, which is for secrets),
and it is never returned by an endpoint, written to a log, or included in a
diagnostic — callers learn only whether one is configured. Note that
Anthropic's own 404 for an inaccessible workspace quotes the ID back inside
the response body, which is why `app/core/safe_traceback.py` exists.
"""

import re
from typing import Optional

#: The header Anthropic documents for selecting a workspace.
WORKSPACE_HEADER = "anthropic-workspace-id"

#: The documented prefix. From Anthropic's own example value.
WORKSPACE_ID_PREFIX = "wrkspc_"

#: Prefix plus one-or-more alphanumerics. Deliberately no length bound —
#: Anthropic documents the prefix and an example, not a width.
_WORKSPACE_ID = re.compile(rf"^{WORKSPACE_ID_PREFIX}[A-Za-z0-9]+$")

#: The preference key. Account metadata, not a credential.
PREFERENCE_KEY = "anthropic_workspace_id"

INVALID_MESSAGE = (
    "That doesn't look like a Workspace ID — it starts with "
    f"'{WORKSPACE_ID_PREFIX}'. Leave the field blank if your key was scoped "
    "to a single workspace when you created it. Otherwise, non-default "
    "workspaces are in the ID column of Settings → Workspaces in the Claude "
    "Console; the Default Workspace is not listed there, and Anthropic "
    "returns its ID in the anthropic-workspace-id response header instead."
)


def normalise_workspace_id(value: Optional[str]) -> str:
    """Trim *value* to the form that gets stored and sent. Never raises."""
    return (value or "").strip()


def is_valid_workspace_id(value: Optional[str]) -> bool:
    """Whether *value* is a well-formed workspace ID.

    Blank is **not** valid here — blank means "not configured", which is a
    separate question the caller answers with `normalise_workspace_id`.
    """
    return bool(_WORKSPACE_ID.match(normalise_workspace_id(value)))


def validate_workspace_id(value: Optional[str]) -> Optional[str]:
    """Return the message to show the user, or None when acceptable.

    Blank is acceptable: that is a legacy workspace-scoped key, which must
    keep working exactly as it did before this field existed.
    """
    candidate = normalise_workspace_id(value)
    if not candidate:
        return None
    return None if is_valid_workspace_id(candidate) else INVALID_MESSAGE


def workspace_headers(value: Optional[str]) -> dict:
    """The `default_headers` mapping for a client, or `{}` when unset.

    A malformed value yields `{}` rather than a header: this is the last
    gate before the SDK, and sending a value that failed our own shape check
    would be sending something we have already decided is not a workspace ID.
    """
    candidate = normalise_workspace_id(value)
    if not candidate or not is_valid_workspace_id(candidate):
        return {}
    return {WORKSPACE_HEADER: candidate}
