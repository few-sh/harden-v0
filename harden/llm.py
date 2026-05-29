"""Retry wrapper around `litellm.acompletion`.

Fibonacci backoff with a 5-second multiplier, capped at 60 seconds between
attempts: 5s, 5s, 10s, 15s, 25s, 40s, 60s, 60s, ... Retries indefinitely;
callers that need a ceiling should impose it outside this wrapper.
"""

import logging
from typing import Any

from tenacity import RetryCallState, before_sleep_log, retry

logger = logging.getLogger(__name__)


def _fibonacci_backoff(retry_state: RetryCallState) -> float:
    n = retry_state.attempt_number
    if n <= 0:
        return 0.0
    a, b = 1, 1
    for _ in range(n - 1):
        a, b = b, a + b
    return float(min(a * 5, 60))


@retry(
    wait=_fibonacci_backoff,
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)
async def acompletion_with_retry(**kwargs: Any) -> Any:
    import litellm

    return await litellm.acompletion(**kwargs)
