"""Live AI generations, so one can be stopped.

A streaming answer is the only thing in JARVIS that runs long enough for
"stop" to be a meaningful button. That needs a handle on the work while
it is in flight, which is what this registry is: id → CancellationToken.

In-memory and per-process on purpose, matching
app/core/pending_actions.py and app/core/privacy.py. A generation cannot
outlive the process that is streaming it, so persisting these would only
create rows describing work that provably no longer exists.

Entries are removed when the generation ends, whether it finished, was
stopped, or failed — a registry that only grew would be a slow leak in a
process meant to stay running all day.

Stopping is *cooperative*: it sets a token that the provider loop checks
between chunks (app/core/ai/base.py). It does not kill a thread. What a
user is promised is that output stops, nothing further is persisted, and
the upstream connection is closed at the next chunk boundary — which is
what actually happens, and is worth stating precisely rather than
implying a hard abort that no HTTP client offers.
"""

import threading
import uuid
from typing import Dict, Optional

from app.core.ai.base import CancellationToken
from app.logging_config import get_logger

logger = get_logger("core.generation")


class GenerationRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tokens: Dict[str, CancellationToken] = {}

    def start(self) -> tuple:
        """Register a new generation. Returns (generation_id, token)."""
        generation_id = str(uuid.uuid4())
        token = CancellationToken()
        with self._lock:
            self._tokens[generation_id] = token
        return generation_id, token

    def finish(self, generation_id: str) -> None:
        """Idempotent: finishing an unknown or already-finished id is a
        no-op, because a generation can end from either side (the stream
        completing, or the client disconnecting) and both call this."""
        with self._lock:
            self._tokens.pop(generation_id, None)

    def stop(self, generation_id: str) -> bool:
        """Cancel one generation. False when the id is unknown — usually
        because it already finished, which the caller should report as
        "nothing to stop", not as an error."""
        with self._lock:
            token = self._tokens.get(generation_id)
        if token is None:
            return False
        token.cancel()
        logger.info("Generation %s stopped by the user.", generation_id)
        return True

    def stop_all(self) -> int:
        """Cancel everything in flight. Used by the UI's Stop button when
        it has no id to hand (a page reloaded mid-stream), and on
        shutdown so a closing process does not sit waiting on a provider."""
        with self._lock:
            tokens = list(self._tokens.items())
        for generation_id, token in tokens:
            token.cancel()
            logger.info("Generation %s stopped.", generation_id)
        return len(tokens)

    def active_count(self) -> int:
        with self._lock:
            return len(self._tokens)

    def token(self, generation_id: str) -> Optional[CancellationToken]:
        with self._lock:
            return self._tokens.get(generation_id)


# Module-level singleton, matching this codebase's existing pattern
# (registry, pending_store, event_bus, runtime, privacy_mode).
generations = GenerationRegistry()
