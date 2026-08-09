"""Tests for app/core/ai/ — the provider abstraction.

No test here makes a real network call: the Anthropic SDK is mocked and
the Ollama provider is driven through an injected httpx client. The
properties under test are the ones the user actually feels — that a
failure says what really went wrong, that a stop really stops, and that
a local model list is never invented.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from app.core.ai import ProviderConfig, get_provider
from app.core.ai.base import CancellationToken, GenerationCancelled, Message, ProviderError
from app.core.errors import ErrorCategory


def _messages(text="hello"):
    return [Message(role="user", content=text)]


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("configured,expected", [
    ("anthropic", "anthropic"),
    ("ollama", "ollama"),
    ("  OLLAMA  ", "ollama"),
    ("not-a-provider", "anthropic"),
    ("", "anthropic"),
])
def test_factory_resolves_provider_names(configured, expected):
    """An unrecognised value must not be able to take chat down — it
    resolves to the historical default, same as selected_provider()."""
    assert get_provider(configured, ProviderConfig()).name == expected


# ---------------------------------------------------------------------------
# Anthropic
# ---------------------------------------------------------------------------

def _anthropic(**kwargs):
    return get_provider("anthropic", ProviderConfig(**kwargs))


def test_anthropic_is_unavailable_without_a_key():
    availability = _anthropic().availability()
    assert availability.ready is False
    assert "Settings" in availability.reason


def test_anthropic_unavailable_reason_never_mentions_a_dotenv_file():
    """A packaged-app user has no repository and no .env to edit."""
    assert ".env" not in _anthropic().availability().reason


def test_anthropic_is_available_with_a_key():
    assert _anthropic(api_key="sk-test").availability().ready is True


def test_anthropic_availability_does_not_claim_the_key_is_valid():
    """Validity costs a request to discover, so it is reported when a
    call fails — as an auth error — not guessed at up front."""
    reason = _anthropic(api_key="sk-test").availability().reason.lower()
    assert "valid" not in reason


def _reply(text):
    block = MagicMock()
    block.text = text
    message = MagicMock()
    message.content = [block]
    return message


def test_anthropic_generate_returns_the_reply():
    with patch("anthropic.Anthropic") as cls:
        cls.return_value.messages.create.return_value = _reply("Sunny.")
        reply = _anthropic(api_key="sk-test").generate(_messages(), "system text")

    assert reply.content == "Sunny."
    assert reply.used_api is True


def test_anthropic_generate_sends_history_and_system_prompt():
    with patch("anthropic.Anthropic") as cls:
        cls.return_value.messages.create.return_value = _reply("ok")
        _anthropic(api_key="sk-test").generate(
            [Message(role="user", content="first"), Message(role="assistant", content="second")],
            "the system prompt",
        )

    kwargs = cls.return_value.messages.create.call_args.kwargs
    assert kwargs["system"] == "the system prompt"
    assert kwargs["messages"] == [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "second"},
    ]


@pytest.mark.parametrize("exception_name,expected", [
    ("AuthenticationError", ErrorCategory.PROVIDER_AUTH),
    ("RateLimitError", ErrorCategory.PROVIDER_RATE_LIMIT),
    ("APITimeoutError", ErrorCategory.PROVIDER_TIMEOUT),
    ("APIConnectionError", ErrorCategory.PROVIDER_UNAVAILABLE),
    ("SomethingElse", ErrorCategory.PROVIDER_ERROR),
])
def test_anthropic_classifies_failures_by_category(exception_name, expected):
    """The whole point of the refactor: these must be distinguishable,
    because "your key is wrong" and "you are being rate limited" call for
    completely different actions from the user."""
    failure = type(exception_name, (Exception,), {})("boom")

    with patch("anthropic.Anthropic") as cls:
        cls.return_value.messages.create.side_effect = failure
        with pytest.raises(ProviderError) as caught:
            _anthropic(api_key="sk-test").generate(_messages(), "system")

    assert caught.value.category == expected


def test_anthropic_provider_error_carries_no_provider_text():
    """ProviderError reaches app/core/errors.py, which is what a client
    sees. Nothing from the SDK's message may travel with it."""
    secret = "Bearer sk-ant-REALSECRET at https://api.anthropic.com/v1/messages"

    with patch("anthropic.Anthropic") as cls:
        cls.return_value.messages.create.side_effect = Exception(secret)
        with pytest.raises(ProviderError) as caught:
            _anthropic(api_key="sk-test").generate(_messages(), "system")

    assert "sk-ant-REALSECRET" not in str(caught.value)
    assert caught.value.detail == ""


def test_anthropic_streams_text_deltas():
    stream_cm = MagicMock()
    stream_cm.__enter__.return_value.text_stream = iter(["Hel", "lo", " there"])

    with patch("anthropic.Anthropic") as cls:
        cls.return_value.messages.stream.return_value = stream_cm
        chunks = list(_anthropic(api_key="sk-test").stream(_messages(), "system"))

    assert "".join(chunks) == "Hello there"


def test_anthropic_stream_stops_when_cancelled():
    """The user pressed Stop: no further chunk may be delivered."""
    token = CancellationToken()

    def _endless():
        while True:
            yield "word "

    stream_cm = MagicMock()
    stream_cm.__enter__.return_value.text_stream = _endless()

    with patch("anthropic.Anthropic") as cls:
        cls.return_value.messages.stream.return_value = stream_cm
        collected = []
        with pytest.raises(GenerationCancelled):
            for chunk in _anthropic(api_key="sk-test").stream(_messages(), "system", cancel=token):
                collected.append(chunk)
                if len(collected) == 3:
                    token.cancel()

    assert len(collected) == 3, "not one chunk more than the user saw before stopping"


def test_cancelling_before_the_call_never_reaches_the_provider():
    token = CancellationToken()
    token.cancel()

    with patch("anthropic.Anthropic") as cls:
        with pytest.raises(GenerationCancelled):
            _anthropic(api_key="sk-test").generate(_messages(), "system", cancel=token)

    cls.return_value.messages.create.assert_not_called()


# ---------------------------------------------------------------------------
# Ollama
# ---------------------------------------------------------------------------

def _tags_response(models):
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {"models": [{"name": name} for name in models]}
    return response


def _ollama(models, **kwargs):
    """An Ollama provider whose detection reports exactly *models*."""
    provider = get_provider("ollama", ProviderConfig(**kwargs))
    client = MagicMock()
    client.get.return_value = _tags_response(models)
    provider._status = lambda: __import__(
        "app.core.providers", fromlist=["ollama_status"]
    ).ollama_status(http_client=client)
    return provider


def test_ollama_is_unavailable_when_nothing_is_running():
    provider = get_provider("ollama", ProviderConfig())
    client = MagicMock()
    client.get.side_effect = OSError("connection refused")
    provider._status = lambda: __import__(
        "app.core.providers", fromlist=["ollama_status"]
    ).ollama_status(http_client=client)

    availability = provider.availability()

    assert availability.ready is False
    assert availability.category == ErrorCategory.PROVIDER_UNAVAILABLE


def test_ollama_uses_the_only_installed_model_when_none_is_configured():
    assert _ollama(["llama3:latest"]).resolved_model() == "llama3:latest"


def test_ollama_reports_a_configured_model_that_is_not_installed():
    """Naming the installed models turns a dead end into one action the
    user can take."""
    availability = _ollama(["llama3:latest"], ollama_model="mistral").availability()

    assert availability.ready is False
    assert "mistral" in availability.reason
    assert "llama3:latest" in availability.reason


def test_ollama_never_offers_a_model_the_instance_did_not_report():
    assert _ollama([]).resolved_model() == ""


def test_ollama_refuses_to_generate_without_a_model():
    with pytest.raises(ProviderError) as caught:
        list(_ollama([]).stream(_messages(), "system"))

    assert caught.value.category == ErrorCategory.PROVIDER_UNAVAILABLE


def test_no_module_anywhere_can_trigger_a_model_download():
    """Downloading gigabytes is the user's decision, made in Ollama.

    Checked across the whole app rather than one file, and over real
    string constants rather than raw text — the modules that promise not
    to call /api/pull necessarily mention it in their docstrings, and a
    substring search cannot tell an explanation apart from a URL.
    """
    import ast
    from pathlib import Path

    app_dir = Path(__file__).resolve().parent.parent / "app"
    offenders = []

    for path in app_dir.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))

        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                first = node.body[0] if node.body else None
                if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) \
                        and isinstance(first.value.value, str):
                    docstrings.add(id(first.value))

        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                    and id(node) not in docstrings and "api/pull" in node.value:
                offenders.append(f"{path.name}:{node.lineno}")

    assert offenders == [], f"a model-download endpoint is referenced in {offenders}"


def test_ollama_streams_chat_deltas():
    provider = _ollama(["llama3:latest"])
    lines = [
        json.dumps({"message": {"role": "assistant", "content": "Hel"}}),
        json.dumps({"message": {"role": "assistant", "content": "lo"}}),
        json.dumps({"done": True}),
    ]

    with patch.object(provider, "_stream_lines", side_effect=lambda payload, cancel: iter(["Hel", "lo"])):
        assert "".join(provider.stream(_messages(), "system")) == "Hello"

    from app.core.ai.ollama_provider import _delta_from_line
    assert "".join(_delta_from_line(line) for line in lines) == "Hello"


@pytest.mark.parametrize("line", [
    "", "   ", "not json at all", "[1,2,3]", json.dumps({"message": "wrong shape"}),
    json.dumps({"message": {"content": 5}}),
])
def test_ollama_malformed_stream_lines_yield_nothing(line):
    """Same rule as /api/tags parsing: a shape we don't recognise
    produces nothing, never a guess."""
    from app.core.ai.ollama_provider import _delta_from_line

    assert _delta_from_line(line) == ""


def test_ollama_system_prompt_is_sent_as_a_system_message():
    provider = _ollama(["llama3:latest"])
    captured = {}

    def _capture(payload, cancel):
        captured.update(payload)
        return iter(())

    with patch.object(provider, "_stream_lines", side_effect=_capture):
        list(provider.stream(_messages("hi"), "the system prompt"))

    assert captured["messages"][0] == {"role": "system", "content": "the system prompt"}
    assert captured["messages"][-1] == {"role": "user", "content": "hi"}
    assert captured["stream"] is True


def test_ollama_transport_failures_are_classified():
    provider = _ollama(["llama3:latest"])

    def _boom(payload, cancel):
        raise type("ConnectError", (Exception,), {})("no route to host")
        yield  # pragma: no cover — generator marker

    with patch.object(provider, "_stream_lines", side_effect=_boom):
        with pytest.raises(ProviderError) as caught:
            list(provider.stream(_messages(), "system"))

    assert caught.value.category == ErrorCategory.PROVIDER_UNAVAILABLE


def test_ollama_stream_is_loopback_only():
    from app.core.ai.ollama_provider import OllamaProvider

    provider = OllamaProvider(ProviderConfig())
    assert provider._base_url().startswith("http://127.0.0.1:")
