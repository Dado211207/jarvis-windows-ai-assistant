"""Tests for app/core/ai/key_check.py and the classification behind it.

The provider is injected, so every outcome is exercised without a real
request — CLAUDE.md forbids one from any test, and the point here is the
mapping from "what went wrong" to "what the user is told", which a live
call could not exercise deterministically anyway.
"""

import pytest

from app.core.ai.base import ProviderError
from app.core.ai.key_check import KeyVerification, verify_anthropic_key
from app.core.errors import ErrorCategory, classify_anthropic_exception, safe_message


class _FakeProvider:
    """Records the config it was handed and fails however the test says."""

    last_config = None

    def __init__(self, config, error=None):
        _FakeProvider.last_config = config
        self._error = error
        self.calls = 0

    def generate(self, messages, system, cancel=None):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return None


def _factory(error=None):
    def _make(config):
        _make.provider = _FakeProvider(config, error=error)
        return _make.provider
    return _make


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------

def test_a_working_key_is_verified_and_worth_storing():
    result = verify_anthropic_key("sk-ant-good", provider_factory=_factory())

    assert result.ok is True
    assert result.worth_storing is True
    assert result.category is None


def test_verification_really_makes_a_request():
    """A key that is only shape-checked is a key whose first failure
    happens later, in a conversation."""
    factory = _factory()
    verify_anthropic_key("sk-ant-good", provider_factory=factory)

    assert factory.provider.calls == 1


def test_verification_costs_one_token():
    """Checking a key should be effectively free."""
    from app.core.ai import key_check

    verify_anthropic_key("sk-ant-good", provider_factory=_factory())

    assert _FakeProvider.last_config.max_tokens == key_check.VERIFY_MAX_TOKENS
    assert _FakeProvider.last_config.max_tokens == 1


def test_the_key_under_test_is_the_one_used():
    """Not the stored one, and not the environment's — otherwise this
    would verify some other key entirely."""
    verify_anthropic_key("  sk-ant-padded  ", provider_factory=_factory())

    assert _FakeProvider.last_config.api_key == "sk-ant-padded"


def test_verification_has_its_own_timeout():
    """A first-run screen must not sit on the chat pipeline's budget."""
    from app.core.ai import key_check

    verify_anthropic_key("sk-ant-good", provider_factory=_factory())

    assert _FakeProvider.last_config.timeout_seconds == key_check.VERIFY_TIMEOUT_SECONDS


# ---------------------------------------------------------------------------
# Blank
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value", ["", "   ", None])
def test_a_blank_key_is_refused_without_a_request(value):
    factory = _factory()
    result = verify_anthropic_key(value, provider_factory=factory)

    assert result.ok is False
    assert result.worth_storing is False
    assert not hasattr(factory, "provider"), "a blank key must not cost a request"


# ---------------------------------------------------------------------------
# The four causes the owner asked to be told apart
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("category,worth_storing", [
    (ErrorCategory.PROVIDER_AUTH, False),
    (ErrorCategory.PROVIDER_BILLING, True),
    (ErrorCategory.PROVIDER_RATE_LIMIT, True),
    (ErrorCategory.PROVIDER_UNAVAILABLE, True),
    (ErrorCategory.PROVIDER_TIMEOUT, True),
])
def test_each_failure_keeps_its_own_cause_and_storage_decision(category, worth_storing):
    result = verify_anthropic_key(
        "sk-ant-x", provider_factory=_factory(ProviderError(category))
    )

    assert result.ok is False
    assert result.category is category
    assert result.worth_storing is worth_storing
    assert result.message == safe_message(category)


def test_only_an_outright_rejection_discards_the_key():
    """Being offline or rate-limited during setup says nothing about the
    key; making someone re-enter it later would punish them for their
    network."""
    rejected = verify_anthropic_key(
        "sk-ant-x", provider_factory=_factory(ProviderError(ErrorCategory.PROVIDER_AUTH))
    )
    offline = verify_anthropic_key(
        "sk-ant-x", provider_factory=_factory(ProviderError(ErrorCategory.PROVIDER_UNAVAILABLE))
    )

    assert rejected.worth_storing is False
    assert offline.worth_storing is True


def test_the_four_messages_are_four_different_sentences():
    messages = {
        safe_message(c) for c in (
            ErrorCategory.PROVIDER_AUTH,
            ErrorCategory.PROVIDER_BILLING,
            ErrorCategory.PROVIDER_RATE_LIMIT,
            ErrorCategory.PROVIDER_UNAVAILABLE,
        )
    }
    assert len(messages) == 4


def test_an_unfunded_account_is_never_described_as_a_bad_key():
    """The specific misdirection this exists to prevent: telling someone
    their key was rejected sends them to generate a new one that will
    fail in exactly the same way."""
    message = safe_message(ErrorCategory.PROVIDER_BILLING).lower()

    assert "credit" in message
    assert "rejected" not in message


# ---------------------------------------------------------------------------
# Nothing escapes, and nothing leaks
# ---------------------------------------------------------------------------

def test_a_provider_that_breaks_its_contract_is_still_handled():
    """This runs on a first-run screen; an unhandled error would be the
    user's first impression of the product."""
    result = verify_anthropic_key(
        "sk-ant-x", provider_factory=_factory(RuntimeError("something unexpected"))
    )

    assert result.ok is False
    assert result.category is ErrorCategory.PROVIDER_ERROR


def test_a_provider_that_cannot_even_be_constructed_is_handled():
    def _explode(config):
        raise ImportError("no anthropic SDK in this build")

    result = verify_anthropic_key("sk-ant-x", provider_factory=_explode)

    assert result.ok is False
    assert isinstance(result, KeyVerification)


def test_no_message_ever_contains_the_key_or_provider_text():
    secret = "sk-ant-super-secret-value"
    outcomes = [verify_anthropic_key(secret, provider_factory=_factory())]
    for category in ErrorCategory:
        outcomes.append(
            verify_anthropic_key(secret, provider_factory=_factory(ProviderError(category)))
        )
    outcomes.append(
        verify_anthropic_key(
            secret,
            provider_factory=_factory(RuntimeError("upstream said: sk-ant-leaked and http://internal")),
        )
    )

    for outcome in outcomes:
        assert secret not in outcome.message
        assert "sk-" not in outcome.message
        assert "http" not in outcome.message


# ---------------------------------------------------------------------------
# Classification, including the one documented message check
# ---------------------------------------------------------------------------

def _exception_named(name, message=""):
    return type(name, (Exception,), {})(message)


@pytest.mark.parametrize("type_name,expected", [
    ("AuthenticationError", ErrorCategory.PROVIDER_AUTH),
    ("RateLimitError", ErrorCategory.PROVIDER_RATE_LIMIT),
    ("APITimeoutError", ErrorCategory.PROVIDER_TIMEOUT),
    ("APIConnectionError", ErrorCategory.PROVIDER_UNAVAILABLE),
    ("InternalServerError", ErrorCategory.PROVIDER_ERROR),
])
def test_classification_from_the_type_name_alone(type_name, expected):
    assert classify_anthropic_exception(_exception_named(type_name)) is expected


def test_an_unfunded_account_is_recognised_from_a_bad_request():
    """Anthropic returns HTTP 400 for an account with no credit, with the
    same exception type as a malformed request. Only the message
    separates them."""
    exc = _exception_named(
        "BadRequestError",
        "Your credit balance is too low to access the Anthropic API.",
    )
    assert classify_anthropic_exception(exc) is ErrorCategory.PROVIDER_BILLING


def test_an_ordinary_bad_request_is_not_called_a_billing_problem():
    exc = _exception_named("BadRequestError", "messages: at least one message is required")
    assert classify_anthropic_exception(exc) is ErrorCategory.PROVIDER_ERROR


def test_a_permission_error_without_a_billing_marker_stays_an_auth_failure():
    assert classify_anthropic_exception(
        _exception_named("PermissionDeniedError", "not allowed")
    ) is ErrorCategory.PROVIDER_AUTH


def test_classification_survives_an_exception_whose_str_raises():
    class _Hostile(Exception):
        def __str__(self):
            raise ValueError("nope")

    assert classify_anthropic_exception(_Hostile()) is ErrorCategory.PROVIDER_ERROR
