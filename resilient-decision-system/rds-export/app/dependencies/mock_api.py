"""
app/dependencies/mock_api.py
─────────────────────────────────────────────────────────────────────────────
Simulated external service dependencies + retry infrastructure.

Contains:
  1. RetryConfig          — dataclass configuring backoff parameters.
  2. with_retry()         — decorator that wraps any callable with
                            exponential-backoff retry logic.
  3. ExternalAPIError     — typed exception for all dependency failures.
  4. call_credit_bureau() — mock credit score API (20% failure rate).
  5. call_background_check() — mock background check API (15% failure).
  6. DEPENDENCY_REGISTRY  — maps service_id → callable; the orchestrator
                            resolves callables from this dict, not hardcoded
                            if/elif chains.

Design decisions:
  • Retry logic lives in a decorator, NOT in the orchestrator. This keeps
    each layer focused on one responsibility.
  • `with_retry` is synchronous. For async, swap `time.sleep` for
    `asyncio.sleep` and decorate with `async def`. The interface stays
    identical to the orchestrator.
  • jitter is added to the backoff to prevent the "thundering herd"
    problem when many workers retry the same service simultaneously.
  • The mock failure rate is injected from the YAML mock_config, not
    hardcoded here, so tests can override it easily.
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import functools
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════
#  Exceptions
# ══════════════════════════════════════════════════════════════════════════

class ExternalAPIError(Exception):
    """
    Raised when an external dependency call fails (after all retries).

    Attributes:
        service_id      : the service identifier from the YAML config
        status_code     : HTTP-like status code (500, 503, etc.)
        attempts        : total attempts made before giving up
        last_error      : the error message from the final attempt
    """
    def __init__(
        self,
        service_id:  str,
        status_code: int,
        attempts:    int,
        last_error:  str,
    ):
        self.service_id  = service_id
        self.status_code = status_code
        self.attempts    = attempts
        self.last_error  = last_error
        super().__init__(
            f"External dependency '{service_id}' failed after {attempts} attempt(s). "
            f"Last error: {last_error} (HTTP {status_code})"
        )


class TransientAPIError(Exception):
    """
    Raised on a single attempt failure — the retry decorator catches this
    and schedules the next attempt. When all attempts are exhausted, the
    decorator re-raises as ExternalAPIError.
    """
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message     = message
        super().__init__(f"HTTP {status_code}: {message}")


# ══════════════════════════════════════════════════════════════════════════
#  Retry Configuration
# ══════════════════════════════════════════════════════════════════════════

@dataclass
class RetryConfig:
    """
    Parameterises the exponential backoff retry strategy.

    Fields:
        max_attempts          : total attempts (1 = no retry)
        base_delay_ms         : initial wait before 2nd attempt (milliseconds)
        max_delay_ms          : upper cap on the computed delay
        backoff_strategy      : "exponential" | "linear" | "constant"
        jitter                : if True, adds random ±25% to each delay
        retryable_status_codes: only retry on these HTTP codes
    """
    max_attempts:           int       = 3
    base_delay_ms:          float     = 200.0
    max_delay_ms:           float     = 5000.0
    backoff_strategy:       str       = "exponential"
    jitter:                 bool      = True
    retryable_status_codes: List[int] = field(
        default_factory=lambda: [500, 502, 503, 504]
    )

    @classmethod
    def from_yaml(cls, retry_policy: Dict[str, Any]) -> "RetryConfig":
        """Build a RetryConfig from the YAML retry_policy dict."""
        return cls(
            max_attempts=retry_policy.get("max_attempts", 3),
            base_delay_ms=retry_policy.get("base_delay_ms", 200),
            max_delay_ms=retry_policy.get("max_delay_ms", 5000),
            backoff_strategy=retry_policy.get("backoff_strategy", "exponential"),
            retryable_status_codes=retry_policy.get("retryable_http_codes", [500, 502, 503, 504]),
        )

    def compute_delay(self, attempt_number: int) -> float:
        """
        Compute the sleep duration (in seconds) before the next attempt.

        attempt_number is 0-indexed (0 = before 2nd attempt).

        Exponential: base * 2^attempt    (200ms, 400ms, 800ms, …)
        Linear:      base * attempt      (200ms, 400ms, 600ms, …)
        Constant:    base                (200ms, 200ms, 200ms, …)
        """
        if self.backoff_strategy == "exponential":
            delay_ms = self.base_delay_ms * (2 ** attempt_number)
        elif self.backoff_strategy == "linear":
            delay_ms = self.base_delay_ms * (attempt_number + 1)
        else:  # constant
            delay_ms = self.base_delay_ms

        delay_ms = min(delay_ms, self.max_delay_ms)

        if self.jitter:
            # Add ±25% jitter to spread load across workers
            jitter_factor = random.uniform(0.75, 1.25)
            delay_ms *= jitter_factor

        return delay_ms / 1000.0  # convert to seconds


# ══════════════════════════════════════════════════════════════════════════
#  Retry Decorator
# ══════════════════════════════════════════════════════════════════════════

def with_retry(
    retry_config: RetryConfig,
    service_id:   str,
    on_attempt_callback: Optional[Callable[[int, Optional[Exception]], None]] = None,
):
    """
    Decorator factory that wraps a callable with exponential-backoff retry.

    Usage:
        config = RetryConfig(max_attempts=3, base_delay_ms=200)

        @with_retry(config, service_id="credit_bureau")
        def call_api(payload):
            ...

    The wrapped function must raise `TransientAPIError` on retryable failures.
    Any other exception propagates immediately (no retry on programming errors).

    Parameters:
        retry_config         : RetryConfig instance
        service_id           : used for logging and ExternalAPIError
        on_attempt_callback  : optional hook called before each retry;
                               receives (attempt_number, last_exception).
                               Useful for writing audit log entries mid-retry.
    """
    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs) -> Any:
            last_exception: Optional[TransientAPIError] = None

            for attempt in range(retry_config.max_attempts):
                if attempt > 0 and on_attempt_callback:
                    on_attempt_callback(attempt, last_exception)

                try:
                    logger.info(
                        "[%s] Attempt %d/%d",
                        service_id, attempt + 1, retry_config.max_attempts,
                    )
                    result = fn(*args, **kwargs)
                    if attempt > 0:
                        logger.info(
                            "[%s] Succeeded on attempt %d after %d failure(s).",
                            service_id, attempt + 1, attempt,
                        )
                    return result

                except TransientAPIError as exc:
                    last_exception = exc
                    is_retryable = exc.status_code in retry_config.retryable_status_codes
                    is_last      = attempt == retry_config.max_attempts - 1

                    logger.warning(
                        "[%s] Attempt %d failed: %s (retryable=%s, last_attempt=%s)",
                        service_id, attempt + 1, exc, is_retryable, is_last,
                    )

                    if not is_retryable or is_last:
                        raise ExternalAPIError(
                            service_id=service_id,
                            status_code=exc.status_code,
                            attempts=attempt + 1,
                            last_error=exc.message,
                        ) from exc

                    delay = retry_config.compute_delay(attempt)
                    logger.info(
                        "[%s] Waiting %.3fs before attempt %d…",
                        service_id, delay, attempt + 2,
                    )
                    time.sleep(delay)

            # Should be unreachable, but be explicit
            raise ExternalAPIError(
                service_id=service_id,
                status_code=last_exception.status_code if last_exception else 500,
                attempts=retry_config.max_attempts,
                last_error=str(last_exception),
            )

        return wrapper
    return decorator


# ══════════════════════════════════════════════════════════════════════════
#  Mock External Services
# ══════════════════════════════════════════════════════════════════════════

def _build_mock_caller(
    service_id:    str,
    mock_config:   Dict[str, Any],
    retry_policy:  Dict[str, Any],
    attempt_log:   Optional[List[Dict[str, Any]]] = None,
) -> Callable:
    """
    Factory that builds a mock external-API caller configured from YAML.

    This is called by the orchestrator at runtime, not at module import,
    so the retry callback can capture the right execution context.
    """
    retry_cfg    = RetryConfig.from_yaml(retry_policy)
    failure_rate = mock_config.get("failure_rate", 0.20)
    latency_ms   = mock_config.get("latency_ms", [50, 150])

    def attempt_callback(attempt_num: int, last_exc: Optional[Exception]) -> None:
        if attempt_log is not None:
            attempt_log.append({
                "attempt":    attempt_num,
                "error":      str(last_exc) if last_exc else None,
                "timestamp":  time.time(),
            })

    @with_retry(retry_cfg, service_id=service_id, on_attempt_callback=attempt_callback)
    def _call(**payload: Any) -> Dict[str, Any]:
        # Simulate network latency
        latency_s = random.uniform(latency_ms[0], latency_ms[1]) / 1000.0
        time.sleep(latency_s)

        # Simulate random failure
        if random.random() < failure_rate:
            raise TransientAPIError(
                status_code=500,
                message=f"Internal server error from {service_id} (simulated)",
            )

        return _generate_mock_response(service_id, mock_config, payload)

    return _call


def _generate_mock_response(
    service_id:  str,
    mock_config: Dict[str, Any],
    payload:     Dict[str, Any],
) -> Dict[str, Any]:
    """
    Generate a plausible mock response based on the mock_config.
    The response field names come from the YAML config, not hardcoded here.
    """
    if service_id == "credit_bureau_api":
        score_range = mock_config.get("response_range", [300, 850])
        credit_score = random.randint(score_range[0], score_range[1])
        return {
            "credit_score":    credit_score,
            "bureau":          "MockEquifax",
            "pull_timestamp":  time.time(),
            "factors":         _credit_score_factors(credit_score),
        }

    if service_id == "background_check_api":
        response_values = mock_config.get("response_values", ["clear", "flagged", "pending"])
        # Weight: 70% clear, 20% flagged, 10% pending
        weights = [0.70, 0.20, 0.10]
        check_status = random.choices(response_values, weights=weights[:len(response_values)])[0]
        return {
            "check_status": check_status,
            "provider":     "MockClearcheck",
            "completed_at": time.time(),
        }

    # Generic fallback — return the configured response field with a random value
    response_field = mock_config.get("response_field", "result")
    return {response_field: random.randint(100, 999)}


def _credit_score_factors(score: int) -> List[str]:
    """Generate human-readable credit factors based on score range."""
    if score >= 750:
        return ["Low credit utilization", "Long credit history", "No delinquencies"]
    elif score >= 650:
        return ["Moderate credit utilization", "Some recent inquiries"]
    elif score >= 580:
        return ["High credit utilization", "Late payments in last 24 months"]
    else:
        return ["Collections present", "Multiple delinquencies", "High debt load"]


# ══════════════════════════════════════════════════════════════════════════
#  Public API — used by the orchestrator
# ══════════════════════════════════════════════════════════════════════════

def call_external_dependency(
    service_id:   str,
    mock_config:  Dict[str, Any],
    retry_policy: Dict[str, Any],
    input_payload: Dict[str, Any],
    attempt_log:  Optional[List[Dict[str, Any]]] = None,
) -> Tuple[Dict[str, Any], int]:
    """
    Primary entry point for the orchestrator.

    Builds the appropriate mock caller from config and executes it.
    Returns (response_dict, total_attempts).

    Raises ExternalAPIError if all retries are exhausted.

    Parameters:
        service_id    : e.g. "credit_bureau_api"
        mock_config   : the `mock_config` sub-dict from the YAML
        retry_policy  : the `retry_policy` sub-dict from the YAML
        input_payload : the original client payload (for context-aware mocks)
        attempt_log   : mutable list; each retry attempt is appended here
                        so the orchestrator can write individual audit entries
    """
    if attempt_log is None:
        attempt_log = []

    caller = _build_mock_caller(service_id, mock_config, retry_policy, attempt_log)

    start_time = time.perf_counter()
    response   = caller(**input_payload)
    elapsed_ms = (time.perf_counter() - start_time) * 1000

    total_attempts = len(attempt_log) + 1  # +1 for the successful attempt
    logger.info(
        "[%s] Completed in %.1fms after %d attempt(s). Response: %s",
        service_id, elapsed_ms, total_attempts, response,
    )
    return response, total_attempts
