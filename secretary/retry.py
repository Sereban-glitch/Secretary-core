"""Ограниченные повторы только для временных ошибок транспорта.

- backoff с полным джиттером (full jitter), экспоненциальная база;
- Retry-After уважается, но ограничивается сверху (retry_after_cap);
- постоянные ошибки не повторяются вовсе;
- между попытками и во время паузы проверяется отмена задания.
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Callable

from .errors import (JobCancelled, PermanentTransportError,
                     TransientTransportError)

_CANCEL_CHECK_INTERVAL = 0.2


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 4
    backoff_base: float = 1.5
    backoff_max: float = 30.0
    retry_after_cap: float = 120.0
    jitter: bool = True
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep


def _interruptible_sleep(seconds: float, policy: RetryPolicy,
                          cancelled: Callable[[], bool] | None) -> None:
    deadline = policy.clock() + seconds
    while True:
        if cancelled is not None and cancelled():
            raise JobCancelled()
        remaining = deadline - policy.clock()
        if remaining <= 0:
            return
        policy.sleep(min(_CANCEL_CHECK_INTERVAL, remaining))


def compute_delay(policy: RetryPolicy, attempt: int,
                  retry_after: float | None) -> float:
    """Задержка перед попыткой №attempt (1-индексированная после неудачи)."""
    if retry_after is not None and retry_after > 0:
        return min(retry_after, policy.retry_after_cap)
    cap = min(policy.backoff_base * (2 ** (attempt - 1)), policy.backoff_max)
    if policy.jitter:
        return random.uniform(0.0, cap)
    return cap


def retry_call(fn: Callable[[], dict], policy: RetryPolicy, *,
               cancelled: Callable[[], bool] | None = None,
               on_retry: Callable[[int, Exception, float], None] | None = None) -> dict:
    """Вызывает fn с ограниченными повторами. Возвращает ответ транспорта."""
    last_error: TransientTransportError | None = None
    for attempt in range(1, policy.max_attempts + 1):
        if cancelled is not None and cancelled():
            raise JobCancelled()
        try:
            return fn()
        except PermanentTransportError:
            raise
        except TransientTransportError as exc:
            last_error = exc
            if attempt == policy.max_attempts:
                break
            delay = compute_delay(policy, attempt, exc.retry_after)
            if on_retry is not None:
                on_retry(attempt, exc, delay)
            _interruptible_sleep(delay, policy, cancelled)
    assert last_error is not None
    raise last_error
