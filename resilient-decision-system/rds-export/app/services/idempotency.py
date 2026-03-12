"""
app/services/idempotency.py
─────────────────────────────────────────────────────────────────────────────
Redis-backed idempotency service.

Falls back to a thread-safe in-memory dict if Redis is unavailable,
so the system keeps working in development without a Redis instance.

The contract:
  • `check(request_id)` → existing execution_id or None
  • `register(request_id, execution_id)` → sets the key with TTL
  • `clear(request_id)` → removes the key (used in tests)
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# Default TTL: 24 hours (seconds)
DEFAULT_TTL_SECONDS = 86_400


class IdempotencyService:
    """
    Wraps Redis with a graceful in-memory fallback.

    Redis key format:  idempotency:<request_id>
    Redis value:       execution_id (string)
    """

    def __init__(self, redis_url: Optional[str] = None, ttl_seconds: int = DEFAULT_TTL_SECONDS):
        self._ttl    = ttl_seconds
        self._redis  = None
        self._lock   = threading.Lock()
        # in-memory fallback: request_id → (execution_id, expires_at)
        self._memory: Dict[str, Tuple[str, float]] = {}

        if redis_url:
            try:
                import redis  # type: ignore
                self._redis = redis.Redis.from_url(redis_url, decode_responses=True)
                self._redis.ping()
                logger.info("IdempotencyService connected to Redis at %s", redis_url)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Could not connect to Redis (%s). Using in-memory fallback. "
                    "This is NOT suitable for multi-process deployments.", exc
                )
                self._redis = None

    def _redis_key(self, request_id: str) -> str:
        return f"idempotency:{request_id}"

    def check(self, request_id: str) -> Optional[str]:
        """
        Return the execution_id if this request_id was already processed,
        or None if it's a new request.
        """
        if self._redis:
            return self._redis.get(self._redis_key(request_id))

        with self._lock:
            entry = self._memory.get(request_id)
            if entry:
                execution_id, expires_at = entry
                if time.monotonic() < expires_at:
                    return execution_id
                # Expired — clean up
                del self._memory[request_id]
        return None

    def register(self, request_id: str, execution_id: str) -> None:
        """
        Store the mapping request_id → execution_id with a TTL.
        Should be called AFTER the execution row is created, so a
        crash between check() and register() results in a re-execution
        on the next attempt (safe because the execution is idempotent on
        the DB side via the unique `request_id` column).
        """
        if self._redis:
            self._redis.set(self._redis_key(request_id), execution_id, ex=self._ttl)
            return

        with self._lock:
            self._memory[request_id] = (execution_id, time.monotonic() + self._ttl)

    def clear(self, request_id: str) -> None:
        """Remove a key (used in tests to reset idempotency state)."""
        if self._redis:
            self._redis.delete(self._redis_key(request_id))
            return
        with self._lock:
            self._memory.pop(request_id, None)
