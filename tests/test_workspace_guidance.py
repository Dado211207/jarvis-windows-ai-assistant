"""What JARVIS tells someone about finding their Workspace ID.

**Why this file exists.** The first version of the Workspace ID field sent
everyone to the same place: "the **ID** column of Settings → Workspaces in
the Claude Console". For an account that uses additional workspaces that is
correct. For an account that only has the Default Workspace — which is the
owner's — it is a dead end, because Anthropic documents that the Default
Workspace *is not in that table*:

    Default Workspace has a `wrkspc_` ID like any other workspace
    (returned in the `anthropic-workspace-id` response header and accepted
    by Get Workspace), but it doesn't appear in List Workspaces results

    — https://platform.claude.com/docs/en/manage-claude/workspaces

    You can find a workspace's ID in the **ID** column of Settings →
    Workspaces in the Claude Console, or by calling the List Workspaces
    endpoint. List Workspaces omits the Default Workspace; its ID is in the
    `anthropic-workspace-id` response header of any request that runs
    there.

    — https://platform.claude.com/docs/en/manage-claude/authentication

So instructions that name only the table are instructions that cannot be
followed by the person who needs them most. These tests fail if any surface
goes back to naming the table without the exception, if any surface claims
List Workspaces returns the Default Workspace, or if the documented escape
routes disappear.

The two routes, both from Anthropic's own documentation:

  * **Scope the key to one workspace when you create it** — "You can also
    scope the key to a specific workspace, which lets you skip setting a
    workspace ID manually in future requests." Nothing to look up, nothing
    to type here, and it works for the Default Workspace like any other.
  * **Read the ID off a successful response** — the `anthropic-workspace-id`
    response header carries it "including when that workspace is the
    Default Workspace".

This file also guards the first-run copy, which described a two-field screen
after a third field was added and claimed every key is checked before it is
saved when a key that *could not* be checked is deliberately stored.
"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SETUP_HTML = REPO_ROOT / "app" / "ui" / "templates" / "setup.html"
SETTINGS_HTML = REPO_ROOT / "app" / "ui" / "templates" / "settings.html"
INSTALLER_DOC = REPO_ROOT / "docs" / "WINDOWS_INSTALLER.md"
CHECKLIST_DOC = REPO_ROOT / "docs" / "physical-pc-checklist.md"


# ---------------------------------------------------------------------------
# The surfaces that give Workspace ID guidance, and how to find each one.
#
# Extracted by region rather than whole-file so a test cannot be satisfied
# by the word "Default" appearing somewhere else entirely.
# ---------------------------------------------------------------------------

def _element(path: Path, element_id: str) -> str:
    """The text of the <p> carrying *element_id*."""
    html = path.read_text(encoding="utf-8")
    start = html.index(f'id="{element_id}"')
    end = html.index("</p>", start)
    return html[start:end]


def _markdown_section(path: Path, heading: str) -> str:
    """A markdown section, from *heading* to the next heading of the same
    or higher level."""
    text = path.read_text(encoding="utf-8")
    start = text.index(heading)
    level = len(heading) - len(heading.lstrip("#"))
    rest = text[start + len(heading):]
    following = re.search(rf"^#{{1,{level}}} ", rest, re.MULTILINE)
    return heading + (rest[: following.start()] if following else rest)


def _surfaces() -> dict:
    from app.core.ai.workspace import INVALID_MESSAGE

    return {
        "setup.html workspace hint": _element(SETUP_HTML, "setup-workspace-hint"),
        "settings.html workspace hint": _element(SETTINGS_HTML, "settings-workspace-hint"),
        "workspace.py INVALID_MESSAGE": INVALID_MESSAGE,
        "WINDOWS_INSTALLER.md": _markdown_section(INSTALLER_DOC, "### When you need a Workspace ID"),
        "physical-pc-checklist.md": CHECKLIST_DOC.read_text(encoding="utf-8"),
    }


#: Any phrasing that points someone at the Console's Workspaces table.
_TABLE_PHRASES = (
    "id column",
    "id</strong> column",
    "settings → workspaces",
    "settings &rarr; workspaces",
    "list workspaces",
)


def _points_at_the_table(text: str) -> bool:
    lowered = text.lower()
    return any(phrase in lowered for phrase in _TABLE_PHRASES)


# ---------------------------------------------------------------------------
# The blocker: the table is not the whole answer
# ---------------------------------------------------------------------------

def test_no_surface_names_the_workspaces_table_without_the_default_exception():
    """The regression this file was created for.

    Naming the table is fine — it is where a non-default workspace's ID
    genuinely is. Naming it as though it were the complete answer is what
    sent the owner to a page that does not contain the value he needed.
    """
    for name, text in _surfaces().items():
        if not _points_at_the_table(text):
            continue
        assert "default workspace" in text.lower(), (
            f"{name} sends someone to the Workspaces table without saying the "
            f"Default Workspace is not listed there"
        )


def test_no_surface_claims_the_default_workspace_is_in_the_table():
    """The inverse mistake, and a worse one: a confident instruction to
    look somewhere the value provably is not."""
    for name, text in _surfaces().items():
        lowered = text.lower()
        if "default workspace" not in lowered:
            continue
        assert re.search(
            r"(not|isn't|does not|doesn't|omits|never)\b[^.]{0,80}\b"
            r"(listed|there|appear|shown|include)",
            lowered,
        ), f"{name} mentions the Default Workspace but not that it is absent from the table"


def test_the_scoped_key_route_is_offered_everywhere_the_field_is():
    """Anthropic: "You can also scope the key to a specific workspace,
    which lets you skip setting a workspace ID manually in future
    requests." That is the route with nothing to look up, and it works for
    the Default Workspace like any other, so it belongs beside the field
    rather than in a footnote."""
    for name in ("setup.html workspace hint", "settings.html workspace hint",
                 "WINDOWS_INSTALLER.md"):
        lowered = _surfaces()[name].lower()
        assert "scope" in lowered or "scoped" in lowered, f"{name} does not mention scoping the key"
        assert "blank" in lowered or "empty" in lowered, (
            f"{name} does not say a scoped key needs this field left blank"
        )


def test_the_documented_route_to_the_default_workspace_id_is_the_response_header():
    """The only official, reproducible way to obtain it. Anything else
    would be an invented discovery method."""
    doc = _surfaces()["WINDOWS_INSTALLER.md"].lower()
    assert "anthropic-workspace-id" in doc
    assert "response header" in doc


def test_no_surface_tells_the_owner_to_create_another_workspace_to_make_jarvis_work():
    """An additional workspace is a real thing someone might want. It is
    not a workaround for a JARVIS field, and recommending one as the fix
    would be JARVIS asking for a change to the user's Anthropic account to
    suit itself."""
    for name, text in _surfaces().items():
        lowered = text.lower()
        assert not re.search(r"creat\w*\s+(a\s+)?(new\s+|named\s+|additional\s+|second\s+)?workspace", lowered), (
            f"{name} tells the owner to create a workspace"
        )


def test_the_guidance_names_only_documented_anthropic_locations():
    """No invented Console pages. Every place named here appears in
    Anthropic's own Authentication or Workspaces guide."""
    allowed = ("settings → workspaces", "settings &rarr; workspaces", "settings → api keys",
               "settings &rarr; api keys", "claude console", "anthropic-workspace-id")
    for name, text in _surfaces().items():
        lowered = text.lower()
        for invented in ("account settings", "organisation settings", "organization settings",
                         "workspace settings page", "profile page"):
            assert invented not in lowered, f"{name} names {invented!r}, which Anthropic does not document"
        if _points_at_the_table(text):
            assert any(place in lowered for place in allowed), f"{name} names no documented location"


# ---------------------------------------------------------------------------
# First-run copy — it described the screen it used to be
# ---------------------------------------------------------------------------

def test_first_run_no_longer_describes_itself_as_two_questions():
    """A third field was added to this screen. Copy and comments that
    still say two are the smallest kind of dishonesty and the easiest to
    leave behind."""
    html = SETUP_HTML.read_text(encoding="utf-8")
    lowered = html.lower()
    for stale in ("two questions", "two quick things", "both can be changed"):
        assert stale not in lowered, f"setup.html still says {stale!r} on a three-field screen"


def test_the_key_check_module_does_not_still_say_first_run_asks_for_two_things():
    source = (REPO_ROOT / "app" / "core" / "ai" / "key_check.py").read_text(encoding="utf-8")
    assert "exactly two things" not in source.lower()


def test_setup_does_not_claim_every_key_is_checked_before_it_is_saved():
    """It is not true, and it is deliberately not true: a key that could
    not be checked — offline, rate-limited — is stored rather than making
    someone type it again later. The copy has to say which of those two
    things happened."""
    hint = _element(SETUP_HTML, "setup-key-hint").lower()
    assert "checks it works before saving it" not in hint
    # What actually happens, in both directions.
    assert "reject" in hint, "the hint does not say a rejected key is not saved"
    assert any(word in hint for word in ("can't be checked", "cannot be checked", "could not be checked")), \
        "the hint does not say what happens when the check cannot run"


@pytest.mark.parametrize("path", [INSTALLER_DOC])
def test_the_installer_guide_does_not_overclaim_the_verification(path):
    """"JARVIS checks the pair before storing either, so a combination
    that cannot work is never saved" is narrower than it sounds: an
    explicit rejection is never stored, but a pair that could not be
    checked at all is."""
    text = path.read_text(encoding="utf-8").lower()
    assert "so a combination that cannot work is never saved" not in text
    assert "could not be checked" in text or "cannot be checked" in text


# ---------------------------------------------------------------------------
# What the owner is asked to do on a real PC
# ---------------------------------------------------------------------------

def _checklist() -> str:
    import pathlib

    path = pathlib.Path(__file__).resolve().parent.parent / "docs/physical-pc-checklist.md"
    return path.read_text(encoding="utf-8")


def test_the_checklist_states_the_real_offline_contract():
    """A round-3 report suggested asking the owner to save a key while
    offline and check the *previous* one survived. That contradicts the
    product: PROVIDER_TIMEOUT and PROVIDER_UNAVAILABLE are both in
    `key_check._KEY_IS_PROBABLY_FINE`, so the proposed key is stored and
    labelled unconfirmed. The checklist has always said so; this keeps the
    two from drifting apart again."""
    from app.core.ai.key_check import _KEY_IS_PROBABLY_FINE
    from app.core.errors import ErrorCategory

    assert ErrorCategory.PROVIDER_TIMEOUT in _KEY_IS_PROBABLY_FINE
    assert ErrorCategory.PROVIDER_UNAVAILABLE in _KEY_IS_PROBABLY_FINE

    checklist = _checklist().lower()
    assert "it must be stored and reported as *not yet" in checklist, (
        "the offline step no longer states that the new key is kept"
    )
    assert "not a credential manager write failure" in checklist, (
        "the checklist no longer warns against the offline/write-failure confusion"
    )


def test_the_checklist_does_not_ask_the_owner_to_manufacture_a_write_failure():
    """Credential-store and metadata failures are automated-test territory,
    not something to practise on a real machine with a real key."""
    raw = _checklist()
    # The exclusion note quotes both instructions in order to rule them out,
    # so only the *instructions* are searched — blockquote lines, which is
    # where the note lives, are not instructions to anyone.
    instructions = "\n".join(
        line for line in raw.splitlines() if not line.lstrip().startswith(">")
    ).lower()

    for forbidden in (
        "make the settings file read-only",
        "make the preferences file unwritable",
        "simulate a metadata failure",
        "press remove twice",
        "press **remove** twice",
    ):
        assert forbidden not in instructions, (
            f"the checklist asks the owner to perform {forbidden!r}"
        )

    assert "deliberately *not* on this list" in raw, (
        "the checklist no longer records why those two steps are excluded"
    )
