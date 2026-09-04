"""Shared-key contention admission, backoff and recovery accounting."""

import math
import time
from lib.log import get_logger

logger = get_logger('lib.llm_dispatch.api')


_DEFAULT_429_RETRY_DELAY_S = 0.3


def _shared_contention_retry_delay(dispatcher, slot, is_contention,
                                   log_prefix) -> float:
    """Arm project admission, retaining legacy delay for old dispatchers."""
    if not is_contention:
        return _DEFAULT_429_RETRY_DELAY_S
    note_contention = getattr(dispatcher, 'note_shared_contention', None)
    if not callable(note_contention):
        return _DEFAULT_429_RETRY_DELAY_S
    try:
        retry_delay = float(note_contention(slot))
        # Current dispatchers apply the delay before the next network request,
        # where every task sees the same family gate. Older/fake dispatchers
        # have no admission hook, so preserve their post-failure delay.
        if callable(getattr(
                dispatcher, 'reserve_shared_contention_probe', None)):
            return 0.0
        if math.isfinite(retry_delay) and retry_delay >= 0:
            return max(_DEFAULT_429_RETRY_DELAY_S, retry_delay)
    except Exception as exc:
        logger.debug('%s note_shared_contention failed: %s', log_prefix, exc)
    return _DEFAULT_429_RETRY_DELAY_S


def _shared_contention_admission_decision(dispatcher, slot) -> tuple[float, bool]:
    """Return a validated ``(delay_s, admitted)`` family-gate decision."""
    reserve_probe = getattr(
        dispatcher, 'reserve_shared_contention_probe', None)
    if not callable(reserve_probe):
        return 0.0, True
    decision = reserve_probe(slot)
    delay_s = float(getattr(decision, 'delay_s'))
    admitted = bool(getattr(decision, 'admitted'))
    if not math.isfinite(delay_s) or delay_s < 0:
        raise ValueError(f'invalid shared-contention delay: {delay_s!r}')
    return delay_s, admitted


def _shared_contention_immediate_admission(
        dispatcher, slot, log_prefix) -> float | None:
    """Return 0 when reserved now, a positive defer delay, or None to wait.

    ``None`` preserves compatibility with old/fake dispatchers and turns a
    coordinator defect into the ordinary waitable path. A positive result is
    read-only: the family probe clock is not advanced when optional work
    yields.
    """
    reserve_now = getattr(
        dispatcher, 'reserve_shared_contention_probe_now', None)
    if not callable(reserve_now):
        return None
    try:
        decision = reserve_now(slot)
        delay_s = float(getattr(decision, 'delay_s'))
        admitted = bool(getattr(decision, 'admitted'))
        if not math.isfinite(delay_s) or delay_s < 0:
            raise ValueError(
                f'invalid immediate shared-contention delay: {delay_s!r}')
        if admitted:
            if delay_s > 0:
                raise ValueError(
                    'immediate shared-contention admission cannot wait')
            return 0.0
        if delay_s <= 0:
            raise ValueError(
                'deferred shared-contention admission needs a positive delay')
        return delay_s
    except Exception as exc:
        logger.debug('%s immediate shared-contention admission failed: %s',
                     log_prefix, exc)
        return None


def _admit_or_defer_shared_contention(
        dispatcher, slot, sleep_fn, log_prefix, *, defer: bool) -> None:
    """Use ordinary wait admission or yield optional work before transport."""
    if defer:
        delay_s = _shared_contention_immediate_admission(
            dispatcher, slot, log_prefix)
        if delay_s is not None:
            if delay_s > 0:
                from lib.llm_dispatch._api_errors import (
                    DispatchSharedContentionDeferred,
                )
                raise DispatchSharedContentionDeferred(
                    retry_after_s=delay_s)
            return
    _wait_for_shared_contention_admission(
        dispatcher, slot, sleep_fn, log_prefix)


def _wait_for_shared_contention_admission(
        dispatcher, slot, sleep_fn, log_prefix) -> None:
    """Synchronously wait for one pre-request family probe reservation."""
    while True:
        try:
            delay_s, admitted = _shared_contention_admission_decision(
                dispatcher, slot)
        except Exception as exc:
            # Fail closed to the historical 0.3s retry rather than turning a
            # coordinator defect into an unbounded upstream request herd.
            logger.debug('%s shared-contention admission failed: %s',
                         log_prefix, exc)
            sleep_fn(_DEFAULT_429_RETRY_DELAY_S)
            return
        if delay_s > 0:
            sleep_fn(delay_s)
        if admitted:
            return


async def _wait_for_shared_contention_admission_async(
        dispatcher, slot, *, state, abort_check, async_sleep_fn,
        log_prefix) -> None:
    """Asynchronously wait for one pre-request family probe reservation."""
    while True:
        try:
            delay_s, admitted = _shared_contention_admission_decision(
                dispatcher, slot)
        except Exception as exc:
            logger.debug('%s shared-contention admission failed: %s',
                         log_prefix, exc)
            delay_s, admitted = _DEFAULT_429_RETRY_DELAY_S, True
        if delay_s > 0:
            wait_started = time.monotonic()
            await async_sleep_fn(delay_s, abort_check)
            state.record_queue_wait(wait_started)
        if admitted:
            return


async def _admit_or_defer_shared_contention_async(
        dispatcher, slot, *, state, abort_check, async_sleep_fn,
        log_prefix, defer: bool) -> None:
    """Async counterpart of :func:`_admit_or_defer_shared_contention`."""
    if defer:
        delay_s = _shared_contention_immediate_admission(
            dispatcher, slot, log_prefix)
        if delay_s is not None:
            if delay_s > 0:
                from lib.llm_dispatch._api_errors import (
                    DispatchSharedContentionDeferred,
                )
                raise DispatchSharedContentionDeferred(
                    retry_after_s=delay_s)
            return
    await _wait_for_shared_contention_admission_async(
        dispatcher,
        slot,
        state=state,
        abort_check=abort_check,
        async_sleep_fn=async_sleep_fn,
        log_prefix=log_prefix,
    )


def _note_shared_contention_recovered(dispatcher, slot, log_prefix) -> None:
    """Best-effort signal that a provider/model project admitted traffic."""
    note_success = getattr(dispatcher, 'note_shared_success', None)
    if not callable(note_success):
        return
    try:
        note_success(slot)
    except Exception as exc:
        logger.debug('%s note_shared_success failed: %s', log_prefix, exc)
