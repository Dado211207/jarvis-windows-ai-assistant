"""Does this API key actually work?

First-run asks for exactly two things, and one of them is an API key. A
key that is saved without being tried is a key whose first failure
happens later, in the middle of a conversation, with no obvious
connection to the thing the user typed on the setup screen. So it is
tried here, once, immediately.

What "tried" means: one real request to the provider, deliberately the
smallest one the API accepts (a single token). It is the only way to
learn anything true — a key's shape says nothing about whether it was
revoked, whether the account has credit, or whether this machine can
reach the internet at all.

The four outcomes the owner asked to be told apart, and how each is
recognised:

  * **invalid key** — the provider authenticated us and said no.
  * **no credit / billing** — the key is fine; the account is not funded.
    See app/core/errors.py for why this one case is allowed to look at
    the response text, and how narrowly.
  * **rate limited** — the key is fine and this is temporary.
  * **network failure** — the provider was never reached at all.

Only the first is a reason to refuse the key. The other three say
nothing bad about it, so it is stored and the situation reported: a user
who is rate-limited or offline during setup should not have to type
their key again later.

Nothing here logs, echoes or returns any part of the key or the
provider's response — see ProviderError's contract in
app/core/ai/base.py.
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


def verify_anthropic_key(api_key: str, provider_factory=None) -> KeyVerification:
    """Try *api_key* once and report what happened.

    *provider_factory* is a test seam; production passes nothing and gets
    the real AnthropicProvider. Never raises: a failure to verify is a
    result, not an exception, because this runs on a first-run screen
    where an unhandled error would be the user's first impression of the
    product.
    """
    key = (api_key or "").strip()
    if not key:
        return KeyVerification(
            ok=False,
            message="Enter your Anthropic API key to continue.",
            category=None,
            worth_storing=False,
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
                max_tokens=VERIFY_MAX_TOKENS,
                timeout_seconds=VERIFY_TIMEOUT_SECONDS,
            )
        )
        provider.generate([Message(role="user", content=VERIFY_PROMPT)], VERIFY_SYSTEM)
    except ProviderError as exc:
        logger.info("API key verification failed: %s", exc.category.value)
        return KeyVerification(
            ok=False,
            message=safe_message(exc.category),
            category=exc.category,
            worth_storing=exc.category in _KEY_IS_PROBABLY_FINE,
        )
    except Exception as exc:  # noqa: BLE001 — never let a setup screen crash
        logger.warning("API key verification raised an unclassified error.", exc_info=exc)
        return KeyVerification(
            ok=False,
            message=safe_message(ErrorCategory.PROVIDER_ERROR),
            category=ErrorCategory.PROVIDER_ERROR,
            worth_storing=False,
        )

    logger.info("API key verified against the provider.")
    return KeyVerification(ok=True, message="API key verified.", worth_storing=True)
