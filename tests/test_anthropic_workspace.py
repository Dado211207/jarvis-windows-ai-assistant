"""Anthropic identity-linked keys: the `anthropic-workspace-id` contract.

**The failure these exist for.** On a real Windows 11 machine, a freshly
created Anthropic key — 108 characters, correct prefix — was rejected on
every request, including direct HTTPS calls made outside JARVIS:

    HTTP 400 invalid_request_error
    "anthropic-workspace-id is required when authenticating with an
     identity-linked API key; send the id of the workspace this request
     acts in."

Anthropic's *Authentication* guide explains it: a **workspace key** (which
Anthropic now calls legacy) is scoped to one workspace and "can omit the
workspace ID", while a **personal** or **service account** key is
identity-linked and "you must specify the workspace ID in the
`anthropic-workspace-id` header for each request". JARVIS stored only a
key, so identity-linked keys could not work at all.

Two further defects were exposed by the same session: the error was
flattened into "The AI provider returned an error", and the dashboard
reported Claude as available because *a* credential existed, while the
provider was rejecting it.

No test here reaches the network, and none contains a real key or a real
workspace ID — CLAUDE.md forbids both.
"""

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from app.core.ai.base import Message, ProviderConfig, ProviderError
from app.core.ai.workspace import (
    PREFERENCE_KEY,
    WORKSPACE_HEADER,
    normalise_workspace_id,
    validate_workspace_id,
    workspace_headers,
)
from app.core.errors import ErrorCategory, classify_anthropic_exception, safe_message


def _applied():
    """A credential-store write the backend confirmed."""
    from app.core import credentials
    return credentials.MutationResult(credentials.MUTATION_APPLIED)


def _refused():
    """A write the backend refused: provably nothing changed."""
    from app.core import credentials
    return credentials.MutationResult(credentials.MUTATION_UNCHANGED, "backend_refused")


@pytest.fixture(autouse=True)
def _readable_credential_store():
    """Make the OS credential store answer, and answer "empty".

    Every test below is about a *route*, and the route now declines to write
    to a credential store it could not read first — deliberately, because a
    write it cannot undo can destroy a working key (see
    app/core/ai/credential_pair.py). On Linux CI there is no reachable
    backend at all, so without this the routes would be exercising that
    refusal instead of the behaviour under test. Tests that need a previous
    key patch the snapshot themselves; an inner patch wins over this one.
    """
    from unittest.mock import patch as _patch

    with _patch("app.core.credentials.stored_api_key_snapshot", return_value=(True, "")):
        yield

# Shaped like Anthropic's own documented example, invented here. Not a
# real workspace.
FAKE_WORKSPACE = "wrkspc_01EXAMPLEEXAMPLEEXAMPLE"
FAKE_KEY = "sk-ant-not-a-real-key"

#: Reproduced verbatim from the real machine, and identical to the
#: example in Anthropic's documentation.
OBSERVED_400 = (
    "anthropic-workspace-id is required when authenticating with an "
    "identity-linked API key; send the id of the workspace this request acts in."
)


class BadRequestError(Exception):
    """Type name matches the SDK's, which is what classification reads."""


@contextmanager
def _captured_client():
    """Capture the kwargs `AnthropicProvider._client()` builds a client with."""
    fake = MagicMock()
    with patch("anthropic.Anthropic", return_value=fake) as ctor:
        yield ctor, fake


def _provider(workspace=""):
    from app.core.ai.anthropic_provider import AnthropicProvider

    return AnthropicProvider(
        ProviderConfig(api_key=FAKE_KEY, anthropic_workspace_id=workspace, max_tokens=1)
    )


# ---------------------------------------------------------------------------
# 1 + 2. The header is sent when configured, and absent when it is not
# ---------------------------------------------------------------------------

def test_the_client_is_given_the_workspace_header_when_one_is_configured():
    with _captured_client() as (ctor, _fake):
        _provider(FAKE_WORKSPACE)._client()

    headers = ctor.call_args.kwargs.get("default_headers")
    assert headers == {WORKSPACE_HEADER: FAKE_WORKSPACE}


def test_a_legacy_workspace_scoped_key_sends_no_header_at_all():
    """Anthropic says a single-workspace key can omit the ID. Sending an
    empty or absent header instead of none is a different request, and a
    working installation must not start making one."""
    with _captured_client() as (ctor, _fake):
        _provider("")._client()

    assert "default_headers" not in ctor.call_args.kwargs


def test_a_malformed_workspace_id_contributes_no_header():
    """The last gate before the SDK. A value we have already decided is
    not a workspace ID is not sent as though it were one."""
    assert workspace_headers("nonsense") == {}
    assert workspace_headers("") == {}


def test_a_workspace_id_can_never_smuggle_a_second_header():
    """It is interpolated into an HTTP header, so CR/LF and colons are
    refused by shape rather than trusted."""
    for hostile in (
        "wrkspc_a\r\nX-Evil: 1",
        "wrkspc_a\nX-Evil: 1",
        "wrkspc_a: b",
        "wrkspc_a b",
        "wrkspc_a\x00",
    ):
        assert workspace_headers(hostile) == {}, hostile
        assert validate_workspace_id(hostile) is not None


# ---------------------------------------------------------------------------
# 3. Verification, generation and streaming share one construction path
# ---------------------------------------------------------------------------

def test_generate_and_stream_both_build_their_client_through_client():
    """Adding the header on one path and not the other would produce a key
    that verifies and then fails in conversation, or the reverse."""
    import inspect

    from app.core.ai.anthropic_provider import AnthropicProvider

    for method in (AnthropicProvider.generate, AnthropicProvider.stream):
        source = inspect.getsource(method)
        assert "self._client()" in source, f"{method.__name__} builds its own client"

    # And exactly one place constructs one.
    module = inspect.getsource(AnthropicProvider)
    assert module.count("anthropic.Anthropic(") == 1


def test_key_verification_goes_through_the_same_provider_and_header():
    """Verification must make the request the product will make."""
    from app.core.ai.key_check import verify_anthropic_key

    with _captured_client() as (ctor, fake):
        fake.messages.create.return_value = MagicMock(content=[MagicMock(text="ok")])
        result = verify_anthropic_key(FAKE_KEY, FAKE_WORKSPACE)

    assert result.ok is True
    assert ctor.call_args.kwargs.get("default_headers") == {WORKSPACE_HEADER: FAKE_WORKSPACE}


# ---------------------------------------------------------------------------
# 4 + 5. The observed 400 becomes a specific, safe, actionable message
# ---------------------------------------------------------------------------

def test_the_observed_http_400_maps_to_the_workspace_required_category():
    assert classify_anthropic_exception(BadRequestError(OBSERVED_400)) is (
        ErrorCategory.PROVIDER_WORKSPACE_REQUIRED
    )


def test_the_invalid_workspace_id_400_maps_there_too():
    """Anthropic's other documented workspace 400. Same user action."""
    exc = BadRequestError("anthropic-workspace-id header must be a valid workspace ID.")
    assert classify_anthropic_exception(exc) is ErrorCategory.PROVIDER_WORKSPACE_REQUIRED


def test_the_user_facing_message_names_the_workspace_id_and_where_to_get_it():
    message = safe_message(ErrorCategory.PROVIDER_WORKSPACE_REQUIRED)
    assert "Workspace ID" in message
    assert "Claude Console" in message
    # The defect: this used to be the generic sentence, which told the
    # owner nothing they could act on.
    assert message != safe_message(ErrorCategory.PROVIDER_ERROR)


def test_the_raw_provider_message_is_never_the_message_the_user_sees():
    """Matching is not echoing. The provider's sentence classifies the
    failure; JARVIS's own fixed sentence is what is rendered."""
    message = safe_message(ErrorCategory.PROVIDER_WORKSPACE_REQUIRED)
    assert OBSERVED_400 not in message
    assert "identity-linked" not in message


def test_an_unrelated_bad_request_is_not_reclassified_as_a_workspace_problem():
    """The marker is the header name and nothing else."""
    exc = BadRequestError("max_tokens: must be greater than 0")
    assert classify_anthropic_exception(exc) is ErrorCategory.PROVIDER_ERROR


# ---------------------------------------------------------------------------
# 7. Local shape validation — documented prefix, no invented length
# ---------------------------------------------------------------------------

def test_a_blank_workspace_id_is_accepted_because_legacy_keys_need_none():
    assert validate_workspace_id("") is None
    assert validate_workspace_id(None) is None
    assert validate_workspace_id("   ") is None


def test_a_workspace_id_must_carry_the_documented_prefix():
    for bad in ("01EXAMPLE", "workspace_01EXAMPLE", "wrkspc", "wrkspc_", "sk-ant-x"):
        assert validate_workspace_id(bad) is not None, bad


def test_no_length_is_invented_beyond_what_anthropic_documents():
    """Anthropic documents a prefix and an example, never a width. Pinning
    one would reject a valid future ID for a rule nobody published."""
    assert validate_workspace_id("wrkspc_a") is None
    assert validate_workspace_id("wrkspc_" + "a" * 200) is None


def test_surrounding_whitespace_is_trimmed_not_rejected():
    """Pasted from a console table, this is the common case."""
    assert validate_workspace_id(f"  {FAKE_WORKSPACE}  ") is None
    assert normalise_workspace_id(f"  {FAKE_WORKSPACE}  ") == FAKE_WORKSPACE


# ---------------------------------------------------------------------------
# 8 + 9. Verified atomically; a refusal stores nothing
# ---------------------------------------------------------------------------

def test_a_malformed_workspace_id_is_refused_without_spending_a_request():
    from app.core.ai.key_check import verify_anthropic_key

    with _captured_client() as (ctor, _fake):
        result = verify_anthropic_key(FAKE_KEY, "not-a-workspace")

    assert result.ok is False
    assert result.worth_storing is False
    assert result.category is ErrorCategory.PROVIDER_WORKSPACE_REQUIRED
    ctor.assert_not_called()


def test_a_workspace_required_failure_is_never_worth_storing():
    """Fails closed. Storing the pair would leave an installation whose
    Settings page shows a key that has never worked — the misleading
    state this whole pass exists to remove."""
    from app.core.ai.key_check import _KEY_IS_PROBABLY_FINE

    assert ErrorCategory.PROVIDER_WORKSPACE_REQUIRED not in _KEY_IS_PROBABLY_FINE


def test_the_key_and_workspace_are_verified_as_one_pair():
    from app.core.ai.key_check import verify_anthropic_key

    with patch("app.core.ai.anthropic_provider.AnthropicProvider") as factory:
        factory.return_value.generate.return_value = MagicMock()
        verify_anthropic_key(FAKE_KEY, FAKE_WORKSPACE, provider_factory=factory)

    config = factory.call_args.args[0]
    assert config.api_key == FAKE_KEY
    assert config.anthropic_workspace_id == FAKE_WORKSPACE


# ---------------------------------------------------------------------------
# The endpoints, the pages, and what they are allowed to say
# ---------------------------------------------------------------------------

@pytest.fixture
def api_client():
    from fastapi.testclient import TestClient

    from app.api.server import app as jarvis_app
    from tests.conftest import prime_session

    with TestClient(jarvis_app, raise_server_exceptions=True) as client:
        yield prime_session(client)


@contextmanager
def _verification(ok=True, category=None, worth_storing=True, message="API key verified."):
    from app.core.ai.key_check import KeyVerification

    result = KeyVerification(ok=ok, message=message, category=category, worth_storing=worth_storing)
    with patch("app.core.ai.key_check.verify_anthropic_key", return_value=result) as mock:
        yield mock


def _stored(key=""):
    return patch("app.core.credentials.get_stored_api_key", return_value=key)


# --- 6. Setup and Settings accept an optional workspace ID -----------------

def test_the_endpoint_accepts_a_workspace_id_alongside_the_key(api_client):
    with _verification() as verify, patch("app.core.credentials.set_stored_api_key_detailed", return_value=_applied()):
        r = api_client.post(
            "/settings/api-key",
            json={"api_key": FAKE_KEY, "workspace_id": FAKE_WORKSPACE},
        )
    assert r.status_code == 200
    verify.assert_called_once_with(FAKE_KEY, FAKE_WORKSPACE)


def test_the_workspace_id_is_optional_so_legacy_callers_still_work(api_client):
    """Requirement 14: a body without the field at all is a legacy key."""
    with _verification() as verify, patch("app.core.credentials.set_stored_api_key_detailed", return_value=_applied()):
        r = api_client.post("/settings/api-key", json={"api_key": FAKE_KEY})
    assert r.status_code == 200
    verify.assert_called_once_with(FAKE_KEY, "")


@pytest.mark.parametrize("page,field", [
    ("setup.html", "setup-workspace-input"),
    ("settings.html", "settings-workspace-input"),
])
def test_both_pages_offer_the_optional_workspace_field(page, field):
    from pathlib import Path

    html = (Path(__file__).resolve().parent.parent / "app" / "ui" / "templates" / page).read_text(encoding="utf-8")
    assert field in html, f"{page} has no workspace input"
    assert "wrkspc_" in html, f"{page} does not show the documented prefix"
    assert "Claude Console" in html, f"{page} does not say where to find it"
    assert "optional" in html.lower(), f"{page} does not say the field is optional"


# --- 9 + 10. Storage is consistent, and removal clears it -------------------

def test_a_refused_key_stores_neither_the_key_nor_any_workspace_metadata(api_client):
    from app.core.ai.workspace import PREFERENCE_KEY as WS
    from app.core.preferences import get as get_preference
    from app.core.providers import VERIFICATION_PREFERENCE

    with _verification(
        ok=False, worth_storing=False,
        category=ErrorCategory.PROVIDER_WORKSPACE_REQUIRED,
        message=safe_message(ErrorCategory.PROVIDER_WORKSPACE_REQUIRED),
    ), patch("app.core.credentials.set_stored_api_key_detailed", return_value=_applied()) as store:
        r = api_client.post(
            "/settings/api-key",
            json={"api_key": FAKE_KEY, "workspace_id": FAKE_WORKSPACE},
        )

    assert r.json()["stored"] is False
    store.assert_not_called()
    assert not (get_preference(WS) or "")
    assert not (get_preference(VERIFICATION_PREFERENCE) or "")


def test_a_verified_pair_records_the_workspace_and_the_verified_state(api_client):
    from app.core.preferences import get as get_preference
    from app.core.providers import CREDENTIAL_VERIFIED, VERIFICATION_PREFERENCE

    with _verification(), patch("app.core.credentials.set_stored_api_key_detailed", return_value=_applied()):
        api_client.post(
            "/settings/api-key",
            json={"api_key": FAKE_KEY, "workspace_id": FAKE_WORKSPACE},
        )

    assert get_preference(PREFERENCE_KEY) == FAKE_WORKSPACE
    assert get_preference(VERIFICATION_PREFERENCE) == CREDENTIAL_VERIFIED


def test_removing_the_key_clears_the_workspace_and_the_verification_state(api_client):
    """They describe the credential. Leaving them behind would give the
    next key the workspace of the one before it, and go on reporting a
    verification that belonged to something that no longer exists."""
    from app.core.preferences import get as get_preference
    from app.core.providers import VERIFICATION_PREFERENCE

    with _verification(), patch("app.core.credentials.set_stored_api_key_detailed", return_value=_applied()):
        api_client.post(
            "/settings/api-key",
            json={"api_key": FAKE_KEY, "workspace_id": FAKE_WORKSPACE},
        )
    assert get_preference(PREFERENCE_KEY) == FAKE_WORKSPACE

    with patch("app.core.credentials.clear_stored_api_key_detailed", return_value=_applied()):
        r = api_client.post("/settings/api-key/remove", json={})

    assert r.json()["success"] is True
    assert not (get_preference(PREFERENCE_KEY) or "")
    assert not (get_preference(VERIFICATION_PREFERENCE) or "")


def test_a_failed_credential_removal_leaves_the_metadata_alone(api_client):
    """Clearing the workspace of a key that is still stored would leave a
    working identity-linked credential that can no longer authenticate."""
    from app.core.preferences import get as get_preference

    with _verification(), patch("app.core.credentials.set_stored_api_key_detailed", return_value=_applied()):
        api_client.post(
            "/settings/api-key",
            json={"api_key": FAKE_KEY, "workspace_id": FAKE_WORKSPACE},
        )

    with patch("app.core.credentials.clear_stored_api_key_detailed", return_value=_refused()):
        r = api_client.post("/settings/api-key/remove", json={})

    assert r.json()["success"] is False
    assert get_preference(PREFERENCE_KEY) == FAKE_WORKSPACE


# --- 11. Upgrade and uninstall ---------------------------------------------

def test_an_upgraded_install_with_a_pre_existing_key_reads_as_unverified():
    """Requirement 11, upgrade half. A key stored by an older build has no
    verification record. "Never checked here" is the honest answer — not
    "verified", which would reinstate the defect, and not "failed", which
    would libel a key that may be perfectly good."""
    from app.config import settings
    from app.core import providers

    with patch.object(type(settings), "has_anthropic_key", property(lambda self: True)):
        assert providers.anthropic_credential_state() == providers.CREDENTIAL_UNVERIFIED


def test_the_workspace_id_lives_in_the_data_directory_a_purge_removes():
    """Requirement 11, uninstall half. `/DELETEDATA=yes` removes the data
    directory; an ordinary uninstall keeps it, exactly as it keeps the
    credential the workspace ID belongs to. Both follow from storing it in
    preferences, so this pins that it is stored there and nowhere else."""
    from app.core import preferences

    assert PREFERENCE_KEY in preferences.STORABLE_KEYS

    # And it is never written to the credential store, which is for secrets.
    from app.core import credentials

    owned = {c.username for c in credentials.OWNED_CREDENTIALS}
    assert PREFERENCE_KEY not in owned


# --- 12. Nothing leaks the key or the workspace ID -------------------------

def test_no_endpoint_returns_the_workspace_id_itself(api_client):
    from app.core.preferences import store as store_preference

    store_preference(PREFERENCE_KEY, FAKE_WORKSPACE)
    for path in ("/settings/api-key-status", "/providers", "/health", "/diagnostics"):
        r = api_client.get(path)
        if r.status_code != 200:
            continue
        assert FAKE_WORKSPACE not in r.text, f"{path} echoed the workspace ID"


def test_the_status_endpoint_reports_only_whether_a_workspace_is_set(api_client):
    from app.core.preferences import store as store_preference

    store_preference(PREFERENCE_KEY, FAKE_WORKSPACE)
    body = api_client.get("/settings/api-key-status").json()

    assert body["workspace_configured"] is True
    assert FAKE_WORKSPACE not in str(body)


def test_the_safe_log_event_carries_a_reference_but_no_secret():
    from app.core.ai.events import EVENT_CATEGORY, record_provider_failure

    with patch("db.database.get_db") as get_db:
        record_provider_failure(
            provider="anthropic",
            category=ErrorCategory.PROVIDER_WORKSPACE_REQUIRED,
            correlation_id="corr-1234",
        )

    kwargs = get_db.return_value.log_action.call_args.kwargs
    assert kwargs["command"] == EVENT_CATEGORY
    assert kwargs["tool_name"] == "anthropic"
    assert kwargs["status"] == ErrorCategory.PROVIDER_WORKSPACE_REQUIRED.value
    assert "corr-1234" in kwargs["message"]
    blob = " ".join(str(v) for v in kwargs.values())
    for forbidden in (FAKE_KEY, FAKE_WORKSPACE, OBSERVED_400, "x-api-key", "Authorization"):
        assert forbidden not in blob, forbidden


def test_recording_a_failure_event_never_breaks_the_failure_path():
    """It runs on a path that is already going wrong."""
    from app.core.ai.events import record_provider_failure

    with patch("db.database.get_db", side_effect=RuntimeError("database is gone")):
        record_provider_failure("anthropic", ErrorCategory.PROVIDER_ERROR, "corr-x")


# --- 13. Status cannot overclaim -------------------------------------------

def test_a_merely_present_credential_is_never_reported_as_live(api_client):
    """The exact dashboard defect: a stored key the provider was rejecting
    read as "Claude AI live"."""
    from app.config import settings
    from app.core import providers

    with patch.object(type(settings), "has_anthropic_key", property(lambda self: True)):
        status = providers.anthropic_status()

    assert status.available is False
    assert "is available" not in status.detail


def test_a_rejected_credential_says_so_and_points_at_the_workspace_id():
    from app.config import settings
    from app.core import providers
    from app.core.preferences import store as store_preference

    store_preference(providers.VERIFICATION_PREFERENCE, providers.CREDENTIAL_FAILED)
    with patch.object(type(settings), "has_anthropic_key", property(lambda self: True)):
        status = providers.anthropic_status()

    assert status.available is False
    assert "Workspace ID" in status.detail


def test_no_status_detail_ever_contains_the_key_or_the_workspace_id():
    from app.config import settings
    from app.core import providers
    from app.core.preferences import store as store_preference

    store_preference(PREFERENCE_KEY, FAKE_WORKSPACE)
    with patch.object(type(settings), "effective_api_key", property(lambda self: FAKE_KEY)), \
         patch.object(type(settings), "has_anthropic_key", property(lambda self: True)):
        for state in providers._CREDENTIAL_DETAIL.values():
            assert FAKE_KEY not in state
            assert FAKE_WORKSPACE not in state


# --- 14. Legacy compatibility ----------------------------------------------

def test_a_legacy_key_with_no_workspace_still_generates_normally():
    """The whole point of the optional field: nothing about a
    workspace-scoped key changes."""
    with _captured_client() as (ctor, fake):
        fake.messages.create.return_value = MagicMock(content=[MagicMock(text="hello")])
        reply = _provider("").generate([Message(role="user", content="hi")], "system")

    assert reply.content == "hello"
    assert "default_headers" not in ctor.call_args.kwargs


def test_the_provider_config_defaults_to_no_workspace():
    assert ProviderConfig().anthropic_workspace_id == ""
