"""Retry wrapper around `litellm.acompletion`.

Fibonacci backoff with a 5-second multiplier, capped at 60 seconds between
attempts: 5s, 5s, 10s, 15s, 25s, 40s, 60s, 60s, ...

Retries indefinitely on transient errors (rate limits, timeouts, connection
issues, 5xx). Permanent errors (bad request, auth, not found, content policy,
unsupported params) are reraised on the first attempt — retrying them only
burns time on a request that will never succeed.

Every failure (retryable or not) logs a structured summary of the request
(model, message roles/lengths, content snippets) so a bad request can be
diagnosed from the logs without re-running.
"""

import json
import logging
from typing import Any

from tenacity import (
    RetryCallState,
    before_sleep_log,
    retry,
    retry_if_exception,
)

logger = logging.getLogger(__name__)

_MESSAGE_SNIPPET_CHARS = 500

_NON_RETRYABLE_NAMES = (
    "BadRequestError",
    "InvalidRequestError",
    "AuthenticationError",
    "PermissionDeniedError",
    "NotFoundError",
    "UnsupportedParamsError",
    "ContentPolicyViolationError",
    "UnprocessableEntityError",
)

_non_retryable_cache: tuple[type, ...] | None = None


def _non_retryable_types() -> tuple[type, ...]:
    global _non_retryable_cache
    if _non_retryable_cache is None:
        import litellm

        types: list[type] = []
        for name in _NON_RETRYABLE_NAMES:
            cls = getattr(litellm, name, None)
            if isinstance(cls, type):
                types.append(cls)
        _non_retryable_cache = tuple(types)
    return _non_retryable_cache


def _fibonacci_backoff(retry_state: RetryCallState) -> float:
    n = retry_state.attempt_number
    if n <= 0:
        return 0.0
    a, b = 1, 1
    for _ in range(n - 1):
        a, b = b, a + b
    return float(min(a * 5, 60))


def _summarize_request(kwargs: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "model": kwargs.get("model", "?"),
    }
    messages = kwargs.get("messages") or []
    msg_summaries: list[dict[str, Any]] = []
    for m in messages:
        if not isinstance(m, dict):
            msg_summaries.append({"repr": repr(m)[:_MESSAGE_SNIPPET_CHARS]})
            continue
        content = m.get("content", "")
        if isinstance(content, str):
            length = len(content)
            snippet = content[:_MESSAGE_SNIPPET_CHARS]
            if length > _MESSAGE_SNIPPET_CHARS:
                snippet += f"...[+{length - _MESSAGE_SNIPPET_CHARS} chars]"
        else:
            length = -1
            snippet = repr(content)[:_MESSAGE_SNIPPET_CHARS]
        msg_summaries.append(
            {"role": m.get("role", "?"), "length": length, "snippet": snippet}
        )
    summary["messages"] = msg_summaries
    extra_keys = [
        k for k in kwargs if k not in ("model", "messages")
    ]
    if extra_keys:
        summary["other_kwargs"] = {
            k: (
                repr(kwargs[k])[:_MESSAGE_SNIPPET_CHARS]
                if not isinstance(kwargs[k], (str, int, float, bool, type(None)))
                else kwargs[k]
            )
            for k in extra_keys
        }
    return summary


def _should_retry(exc: BaseException) -> bool:
    if isinstance(exc, _non_retryable_types()):
        return False
    return True


@retry(
    wait=_fibonacci_backoff,
    retry=retry_if_exception(_should_retry),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)
async def _acompletion(**kwargs: Any) -> Any:
    import litellm

    try:
        return await litellm.acompletion(**kwargs)
    except Exception as exc:
        retryable = _should_retry(exc)
        logger.warning(
            "litellm.acompletion raised %s (%s%s): %s\nrequest=%s",
            type(exc).__name__,
            "retryable" if retryable else "permanent",
            "" if retryable else "; will not retry",
            exc,
            json.dumps(_summarize_request(kwargs), default=str),
        )
        raise


async def acompletion_with_retry(**kwargs: Any) -> Any:
    return await _acompletion(**kwargs)
