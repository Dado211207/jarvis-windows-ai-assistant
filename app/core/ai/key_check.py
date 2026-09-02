"""Does this API key actually work?

First run asks for a name, an API key and — only when the key needs one —
a Workspace ID. A key that is saved without being tried is a key whose
first failure happens later, in the middle of a conversation, with no
obvious connection to the thing the user typed on the setup screen. So it
is tried here, once, immediately.

What "tried" means: one real request to the provider, deliberately the
smallest one the API accepts (a single token). It is the only way to
learn anything true — a key's shape says nothing about whether it was
revoked, whether the account has credit, or whether this machine can
reach the internet at all.

The outcomes the owner asked to be told apart, and how each is
recognised:

  * **invalid key** — the provider authenticated us and said no.
  * **missing Workspace ID** — the key is fine, but it is identity-linked
    and Anthropic will not act on it until a workspace is named.
  * **no credit / billing** — the key is fine; the account is not funded.
    See app/core/errors.py for why this one case is allowed to look at
    the response text, and how narrowly.
  * **rate limited** — the key is fine and this is temporary.
  * **network failure** — the provider was never reached at all.

The first two are reasons to refuse the pair: neither can make a request
as entered. The last three say nothing bad about the key, so it is stored
and the situation reported — a user who is rate-limited or offline during
setup should not have to type their key again later. What is *recorded*
about those three is deliberately not "rejected"; see
app/core/providers.py::state_for_verification().

Every failure here also writes one safe row to the Logs page. That is not
decoration: the defect this module was extended for happened on this exact
path, and the owner's Logs page showed nothing at all.

Nothing here logs, echoes or returns any part of the key, the workspace
ID or the provider's response — see ProviderError's contract in
app/core/ai/base.py and app/core/safe_traceback.py.
"""

from dataclasses import dataclass
from typing import Optional

from app.core.ai.base import Message, ProviderConfig, ProviderError
from app.core.errors import ErrorCategory, safe_message
from app.logging_config import get_logger

logger = get_logger("core.ai.key_check")

# One token, one word. Enough to prove the key is accepted and the
# account can be billed for it; small enough that verifying a key costs
# effectively nothing.
VERIFY_MAX_TOKENS = 1
VERIFY_TIMEOUT_SECONDS = 15.0
VERIFY_PROMPT = "Reply with the single word: ok"
VERIFY_SYSTEM = "You are a connectivity check. Reply with one word."

# Categories that say nothing bad about the key itself, so the key is
# still worth storing.
#
# PROVIDER_WORKSPACE_REQUIRED is deliberately absent. That failure means
# the pair as entered cannot make a request, so storing it would leave an
# installation whose Settings page shows a key that has never worked —
# exactly the misleading state this pass exists to remove. It fails
# closed, and the message tells the owner precisely what to add.
_KEY_IS_PROBABLY_FINE = (
    ErrorCategory.PROVIDER_BILLING,
    ErrorCategory.PROVIDER_RATE_LIMIT,
    ErrorCategory.PROVIDER_TIMEOUT,
    ErrorCategory.PROVIDER_UNAVAILABLE,
)


@dataclass
class KeyVerification:
    """The outcome of trying a key once.

    `ok` means the provider answered. `worth_storing` is deliberately a
    separate question: a key that could not be checked because the
    machine is offline is not a rejected key.
    """

    ok: bool
    message: str
    category: Optional[ErrorCategory] = None
    worth_storing: bool = False


def verify_anthropic_key(
    api_key: str,
    workspace_id: str = "",
    provider_factory=None,
) -> KeyVerification:
    """Try *api_key* — together with *workspace_id* — once, and report what
    happened.

    The pair is verified **atomically**, and deliberately: an identity-linked
    key is rejected by Anthropic without the workspace header, so checking
    the key alone would either fail a perfectly good key or store one that
    has never made a successful request. The request made here carries the
    same header every later request will carry, because it is built by the
    same `AnthropicProvider._client()`.

    A blank *workspace_id* is normal and correct for a legacy
    workspace-scoped key, and sends no header at all.

    *provider_factory* is a test seam; production passes nothing and gets
    the real AnthropicProvider. Never raises: a failure to verify is a
    result, not an exception, because this runs on a first-run screen
    where an unhandled error would be the user's first impression of the
    product.
    """
    from app.core.ai.workspace import normalise_workspace_id, validate_workspace_id

    key = (api_key or "").strip()
    if not key:
        return KeyVerification(
            ok=False,
            message="Enter your Anthropic API key to continue.",
            category=None,
            worth_storing=False,
        )

    workspace = normalise_workspace_id(workspace_id)
    shape_problem = validate_workspace_id(workspace)
    if shape_problem is not None:
        # Refused before spending a request: a value that cannot be a
        # workspace ID cannot become one by being sent. It still gets a
        # Logs row, because "every failed key save leaves a trace" is the
        # promise, and a refusal the user does not understand is exactly
        # the kind that sends them looking for one.
        return _failed(
            ErrorCategory.PROVIDER_WORKSPACE_REQUIRED,
            detail=shape_problem,
            message=shape_problem,
        )

    try:
        # Inside the try on purpose: importing the SDK and constructing a
        # client are the first two things that can fail on a machine
        # where the packaged build is incomplete, and a first-run screen
        # must report that rather than raise through it.
        if provider_factory is None:
            from app.core.ai.anthropic_provider import AnthropicProvider

            provider_factory = AnthropicProvider

        provider = provider_factory(
            ProviderConfig(
                api_key=key,
                anthropic_workspace_id=workspace,
                max_tokens=VERIFY_MAX_TOKENS,
                timeout_seconds=VERIFY_TIMEOUT_SECONDS,
            )
        )
        provider.generate([Message(role="user", content=VERIFY_PROMPT)], VERIFY_SYSTEM)
    except ProviderError as exc:
        return _failed(exc.category, exc.cause or exc)
    except Exception as exc:  # noqa: BLE001 — never let a setup screen crash
        return _failed(ErrorCategory.PROVIDER_ERROR, exc)

    logger.info("API key verified against the provider.")
    return KeyVerification(ok=True, message="API key verified.", worth_storing=True)


def _failed(
    category: ErrorCategory,
    exc: Optional[BaseException] = None,
    detail: Optional[str] = None,
    message: Optional[str] = None,
) -> KeyVerification:
    """Record one failed verification and describe it.

    **The Logs row is the point.** The real-PC failure happened on exactly
    this path — someone saving a key in Settings — and the owner's Logs page
    stayed completely empty, so there was no second place to look and no
    way to tell a rejected key from an unfunded account from a missing
    workspace header. Generation failures already wrote a safe row (see
    app/core/ai/events.py); the path that actually broke did not. Every
    failure now comes through here, including the one refused locally
    before a request is made.

    *detail* and *message* are only ever JARVIS's own fixed text — never
    the provider's, and never the value that was rejected.

    An exception, when there is one, is **described, not rendered**:
    Anthropic's 404 for an inaccessible workspace is ``Workspace `<id>`
    not found.``, so `exc_info=exc` here would put a workspace ID in
    jarvis.log. See app/core/safe_traceback.py.
    """
    import uuid

    from app.core.ai.events import record_provider_failure
    from app.core.safe_traceback import describe

    correlation_id = str(uuid.uuid4())
    logger.warning(
        "API key verification failed [correlation_id=%s] category=%s %s",
        correlation_id, category.value,
        describe(exc) if exc is not None else "refused before any request was made",
    )
    record_provider_failure(
        provider="anthropic",
        category=category,
        correlation_id=correlation_id,
        detail=detail,
    )
    return KeyVerification(
        ok=False,
        message=message or safe_message(category),
        category=category,
        worth_storing=category in _KEY_IS_PROBABLY_FINE,
    )
