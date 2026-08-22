"""A secret must never reach the memory database.

The gap this closes: `memory add my key is sk-ant-…` stored the key
verbatim, in plain text, in a SQLite file that lives on the user's disk
until they delete it. `app/core/redaction.py` did not help — it redacts
tool inputs on their way to a log line, the audit trail or a WebSocket
event, and never runs on the memory write path.

Every credential in this file is synthetic. They are shaped like the real
thing so the patterns are exercised honestly, and every one of them
carries an obvious marker (`FAKE`, `NOTREAL`, `EXAMPLE`) so that a
scanner reading this repository — or a person reading this file — can
tell at a glance that nothing here was ever valid.

The strongest assertion in the file is `test_no_rejected_secret_leaves_a
_single_byte_in_the_database`: it opens the database file in binary
afterwards and searches the raw bytes. A row that was inserted and then
deleted would still leave the value in a free page; nothing here is
deleted, because nothing here is ever written.
"""

import re

import pytest
from fastapi.testclient import TestClient

from app.core.secret_guard import SecretRejected, contains_secret, find_secret, refusal_message
from tests.conftest import prime_session

# ---------------------------------------------------------------------------
# Synthetic credentials. Fake, and obviously so.
# ---------------------------------------------------------------------------

ANTHROPIC_KEY = "sk-ant-api03-FAKE0000NOTREAL1111EXAMPLE2222abcdefghijklmnopqrstuvwxyz3333"
OPENAI_KEY = "sk-FAKE00NOTREAL11EXAMPLE22abcdefghijklmnop"
GITHUB_TOKEN = "ghp_FAKE0000NOTREAL1111EXAMPLE2222abcdefgh"
GITHUB_PAT = "github_pat_FAKE0000NOTREAL1111EXAMPLE2222_abcdefghijklmnop"
NETLIFY_TOKEN = "nfp_FAKE0000NOTREAL1111EXAMPLE2222abcdefgh"
AWS_KEY_ID = "AKIAFAKENOTREAL12345"
GOOGLE_KEY = "AIzaFAKE0000NOTREAL1111EXAMPLE2222abcdefg"
SLACK_TOKEN = "xoxb-FAKE0000-NOTREAL1111-EXAMPLE2222abcdefgh"
PRIVATE_KEY_HEADER = "-----BEGIN RSA PRIVATE KEY-----"
JWT = "eyJhbGciOiJFAKE.eyJzdWIiOiJOT1RSRUFM.FAKESIGNATURE"
BEARER = "Bearer FAKE0000NOTREAL1111EXAMPLE2222abcdefghij"

ALL_SECRETS = (
    ANTHROPIC_KEY, OPENAI_KEY, GITHUB_TOKEN, GITHUB_PAT, NETLIFY_TOKEN,
    AWS_KEY_ID, GOOGLE_KEY, SLACK_TOKEN, PRIVATE_KEY_HEADER, JWT, BEARER,
)


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """A real database file, so the byte-level check below has real bytes
    to look at."""
    from db.database import Database
    from db.migrations import create_tables

    db_path = tmp_path / "secret_guard_test.db"
    create_tables(db_path=db_path)
    database = Database(db_path=db_path)
    monkeypatch.setattr("db.database.get_db", lambda: database)
    yield database
    database.close()


@pytest.fixture
def client():
    from app.api.server import app
    with TestClient(app) as test_client:
        yield prime_session(test_client)


@pytest.fixture(autouse=True)
def _privacy_off():
    """Privacy mode also refuses writes, and would mask whether the
    secret guard is the thing doing the refusing."""
    from app.core.privacy import privacy_mode
    privacy_mode.set(False)
    yield
    privacy_mode.set(False)


# ---------------------------------------------------------------------------
# Detection: the shapes that are credentials
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("secret", ALL_SECRETS)
def test_every_credential_shape_is_detected(secret):
    assert find_secret(secret) is not None
    assert contains_secret(secret) is True


@pytest.mark.parametrize("secret", ALL_SECRETS)
def test_a_credential_is_detected_inside_an_ordinary_sentence(secret):
    """People do not paste a bare key; they say something around it."""
    assert find_secret(f"remember that my key is {secret} for the work account") is not None


def test_each_kind_is_named_specifically():
    """The label is what the user reads. "a credential" for everything
    would be true and useless."""
    assert find_secret(ANTHROPIC_KEY) == "an Anthropic API key"
    assert find_secret(GITHUB_TOKEN) == "a GitHub token"
    assert find_secret(PRIVATE_KEY_HEADER) == "a private key block"
    assert find_secret(AWS_KEY_ID) == "an AWS access key id"


@pytest.mark.parametrize("header", [
    "-----BEGIN RSA PRIVATE KEY-----",
    "-----BEGIN PRIVATE KEY-----",
    "-----BEGIN OPENSSH PRIVATE KEY-----",
    "-----BEGIN EC PRIVATE KEY-----",
    "-----begin rsa private key-----",
])
def test_every_private_key_header_variant_is_caught(header):
    assert find_secret(header) is not None


# ---------------------------------------------------------------------------
# Detection: assignments
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "password=hunter2",
    "password = hunter2",
    "password: hunter2",
    "password:hunter2",
    "my password is hunter2",
    "my password was hunter2",
    "the api key is abc123def456",
    "api_key=abc123def456",
    "api-key: abc123def456",
    "API KEY = abc123def456",
    "access_token: abc123def456",
    "auth token is abc123def456",
    "client_secret = abc123def456",
    "refresh_token=abc123def456",
    'password="hunter2"',
    "password='hunter2'",
    "PASSWORD=HUNTER2",
    "  password   =   hunter2  ",
    "passphrase is correcthorsebatterystaple",
    "pwd=hunter2",
])
def test_a_credential_assignment_is_detected(text):
    assert find_secret(text) is not None, f"{text!r} should have been refused"


@pytest.mark.parametrize("text", [
    # A credential noun with nothing assigned to it. These are ordinary
    # sentences and must still be storable — refusing them is what made
    # the ported version annoying.
    "remind me to change my password on Friday",
    "my api key is not working",
    "the password is expired",
    "my token was invalid",
    "the client secret is missing",
    "I need to rotate my access token next month",
    "keep the private key somewhere safe",
    "password managers are worth using",
    "the wifi password is on the router",
    "ask IT about the api key for the staging system",
    # Nothing credential-shaped at all.
    "I prefer dark roast coffee",
    "my dentist appointment is on the 14th",
    "the sky is blue",
    "remember that Sarah's birthday is in March",
    "the meeting is at 3pm in room 2",
    "buy milk, eggs and bread",
    "my favourite key signature is D minor",
    "the keyboard shortcut is ctrl+shift+p",
])
def test_ordinary_sentences_are_still_allowed(text):
    assert find_secret(text) is None, f"{text!r} was refused but contains no secret"


def test_an_empty_value_is_clean():
    assert find_secret("") is None
    assert find_secret(None or "") is None


# ---------------------------------------------------------------------------
# The label never contains the secret
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("secret", ALL_SECRETS)
def test_the_label_never_quotes_what_it_matched(secret):
    """A guard that reports what it caught puts the secret in the API
    response, the event stream and the log — which is the problem it was
    added to prevent."""
    label = find_secret(secret)
    assert label is not None
    assert secret not in label
    assert secret not in refusal_message(label)


@pytest.mark.parametrize("secret", ALL_SECRETS)
def test_the_backstop_exception_never_carries_the_secret(secret):
    with pytest.raises(SecretRejected) as caught:
        raise SecretRejected(find_secret(secret))
    assert secret not in str(caught.value)


# ---------------------------------------------------------------------------
# Every write path refuses
# ---------------------------------------------------------------------------

def test_the_internal_handler_refuses(isolated_db):
    from app.core.memory import add_memory

    result = add_memory(f"my key is {ANTHROPIC_KEY}")

    assert result["success"] is False
    assert result["data"] is None
    assert isolated_db.get_all_memories() == []


def test_the_api_endpoint_refuses(client, isolated_db):
    response = client.post("/memory", json={"content": f"my key is {ANTHROPIC_KEY}"})

    assert response.status_code == 200
    assert response.json()["success"] is False
    assert isolated_db.get_all_memories() == []


def test_the_natural_language_command_refuses(isolated_db):
    """`memory add ...` reaches the tool through the router, which is a
    different door from the endpoint above."""
    from app.core.router import CommandRouter
    from app.core.tool_registry import ToolRegistry

    registry = ToolRegistry()
    from app.core.memory import register_tools
    register_tools(registry)

    response = CommandRouter(registry).route(f"memory add my key is {ANTHROPIC_KEY}")

    assert response.success is False
    assert ANTHROPIC_KEY not in response.message
    assert isolated_db.get_all_memories() == []


def test_the_database_layer_refuses_even_when_called_directly(isolated_db):
    """The backstop. A caller that forgets to check gets an exception
    rather than quietly writing a credential to disk."""
    with pytest.raises(SecretRejected):
        isolated_db.add_memory(content=f"my key is {ANTHROPIC_KEY}")

    assert isolated_db.get_all_memories() == []


def test_a_secret_in_the_tags_is_refused_too(isolated_db):
    """Tags are user-supplied and stored verbatim, so they are not
    trusted for being short."""
    from app.core.memory import add_memory

    result = add_memory("work notes", tags=GITHUB_TOKEN)

    assert result["success"] is False
    assert isolated_db.get_all_memories() == []


def test_an_ordinary_memory_still_saves(isolated_db):
    from app.core.memory import add_memory

    result = add_memory("I prefer dark roast coffee", tags="coffee")

    assert result["success"] is True
    assert [m.content for m in isolated_db.get_all_memories()] == ["I prefer dark roast coffee"]


# ---------------------------------------------------------------------------
# Nothing reaches the disk — checked in bytes, not through the ORM
# ---------------------------------------------------------------------------

def test_no_rejected_secret_leaves_a_single_byte_in_the_database(isolated_db, tmp_path):
    """The assertion that matters.

    A row inserted and then deleted still leaves its bytes in a free
    page, so "the table is empty" is not the same claim as "the secret is
    not in the file". Nothing here is deleted, because nothing here is
    ever written — and this reads the raw file to prove it.
    """
    from app.core.memory import add_memory

    for secret in ALL_SECRETS:
        add_memory(f"remember that my credential is {secret}")
        add_memory("a note", tags=secret)

    add_memory("something ordinary that should be saved")
    isolated_db.close()

    raw = (tmp_path / "secret_guard_test.db").read_bytes()

    for secret in ALL_SECRETS:
        assert secret.encode("utf-8") not in raw, "a rejected secret reached the database file"
        assert secret.encode("utf-16-le") not in raw
    assert b"something ordinary that should be saved" in raw, (
        "the ordinary memory should still have been written"
    )


def test_a_rejected_secret_is_not_stored_redacted_either(isolated_db):
    """Rejection is a refusal, not a rewrite. Storing "my key is
    ***" would leave a memory the user never asked for, saying
    something they did not say."""
    from app.core.memory import add_memory

    add_memory(f"my key is {ANTHROPIC_KEY}")

    assert isolated_db.get_all_memories() == []


# ---------------------------------------------------------------------------
# Nothing reaches a log line, an API response or an event
# ---------------------------------------------------------------------------

def test_the_refusal_is_not_logged_with_the_secret(isolated_db, caplog):
    from app.core.memory import add_memory

    with caplog.at_level("DEBUG"):
        add_memory(f"my key is {ANTHROPIC_KEY}")

    combined = "\n".join(record.getMessage() for record in caplog.records)
    assert ANTHROPIC_KEY not in combined
    _assert_no_recognisable_fragment(combined, ANTHROPIC_KEY)


def test_the_api_response_carries_no_fragment_of_the_secret(client, isolated_db):
    response = client.post("/memory", json={"content": f"my key is {ANTHROPIC_KEY}"})

    body = response.text
    assert ANTHROPIC_KEY not in body
    _assert_no_recognisable_fragment(body, ANTHROPIC_KEY)


def test_the_refusal_message_explains_the_rule_and_the_alternative():
    """A refusal that does not say why, or what to do instead, reads as a
    malfunction."""
    message = refusal_message(find_secret(ANTHROPIC_KEY))

    lowered = message.lower()
    assert "not saved" in lowered or "was not saved" in lowered
    assert "memory" in lowered
    assert "settings" in lowered or "credential manager" in lowered


def _assert_no_recognisable_fragment(haystack: str, secret: str, window: int = 12) -> None:
    """No run of `window` characters from the secret appears in the text.

    A guard that leaks "sk-ant-api03-FAKE0000" instead of the whole key
    has still leaked the identifying part of it. The common prefixes are
    excluded because they are the *pattern*, not the value — the label
    "an Anthropic API key" conveys the same thing on purpose.
    """
    body = secret
    for prefix in ("sk-ant-api03-", "sk-ant-", "sk-", "ghp_", "github_pat_", "nfp_",
                   "AKIA", "AIza", "xoxb-", "Bearer ", "eyJ", "-----BEGIN "):
        if body.startswith(prefix):
            body = body[len(prefix):]
            break
    for start in range(0, max(1, len(body) - window + 1)):
        fragment = body[start:start + window]
        if len(fragment) < window:
            break
        assert fragment not in haystack, f"a {window}-character fragment of the secret leaked"


# ---------------------------------------------------------------------------
# Structure
# ---------------------------------------------------------------------------

def test_the_guard_runs_before_the_privacy_check(isolated_db):
    """Order matters for the message, not the outcome. With privacy mode
    on, both refuse — but "privacy mode is on" would send someone to turn
    it off and try again, and the second attempt would also have to fail.
    """
    from app.core.memory import add_memory
    from app.core.privacy import privacy_mode

    privacy_mode.set(True)
    try:
        result = add_memory(f"my key is {ANTHROPIC_KEY}")
    finally:
        privacy_mode.set(False)

    assert result["success"] is False
    assert "privacy" not in result["message"].lower()


def test_the_database_layer_is_the_only_place_memory_rows_are_created():
    """The backstop is only a backstop if it sits at the single insert.
    A second INSERT elsewhere would bypass it silently."""
    from pathlib import Path

    repo_root = Path(__file__).resolve().parent.parent
    inserts = []
    for path in list((repo_root / "app").rglob("*.py")) + list((repo_root / "db").rglob("*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        if re.search(r"INSERT\s+INTO\s+memories", text, re.IGNORECASE):
            inserts.append(path.relative_to(repo_root).as_posix())

    assert inserts == ["db/database.py"], f"memory rows are inserted from more than one place: {inserts}"
