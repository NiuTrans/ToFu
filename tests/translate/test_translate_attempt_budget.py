"""Translation budget arithmetic: an attempt bound that leaves a retry.

Problem this pins: the total budget used to be ``1.5x`` the dispatch timeout,
where that "timeout" was only ever enforced by the NON-streaming transport. For
a 2783-char answer it evaluated to ``1.5 * 60 = 90s`` — identical to the floor —
and the actual streaming attempt had no per-attempt bound at all. One provider
that never sent response headers therefore held the only attempt for 90.7s until
the budget expired, and the retry that would have delivered the translation was
never dispatched (conv mvhiznrwv669ue, turn f85740dd, 2026-10-21).

The invariant these specs enforce is behavioural, not cosmetic: the per-attempt
bound must be strictly smaller than the total budget, and the total budget must
afford at least two full attempts for every text size.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

# Sizes spanning every row of the size table plus both ends of real traffic.
_SIZES = (0, 120, 2783, 3000, 3001, 6000, 6001, 40_000)


@pytest.mark.parametrize('chars', _SIZES)
def test_one_attempt_can_never_consume_the_whole_budget(chars):
    from lib.translate.engine import (
        translation_attempt_timeout_seconds,
        translation_total_deadline_seconds,
    )

    attempt = translation_attempt_timeout_seconds(chars)
    total = translation_total_deadline_seconds(chars)

    assert attempt < total, (
        'a single attempt may not own the entire task budget')
    assert total >= 2 * attempt, (
        'the budget must still afford a full retry after one failed attempt')


@pytest.mark.parametrize('chars', _SIZES)
def test_first_byte_bound_is_not_larger_than_the_attempt(chars):
    from lib.translate.engine import (
        translation_attempt_timeout_seconds,
        translation_first_byte_timeout_seconds,
    )

    assert (translation_first_byte_timeout_seconds(chars)
            <= translation_attempt_timeout_seconds(chars))


@pytest.mark.parametrize('chars', _SIZES)
def test_attempt_bound_stays_inside_the_transport_dispatch_timeout(chars):
    from lib.translate.engine._engine import _translation_chunk_attempt_limits
    from lib.translate.engine import translation_attempt_timeout_seconds

    _mt, dispatch_timeout, _retries = _translation_chunk_attempt_limits(chars)
    assert translation_attempt_timeout_seconds(chars) <= dispatch_timeout


def test_a_silent_provider_is_abandoned_within_the_first_byte_window():
    """The exact regression size: a ~2.8k-char answer, provider never speaks.

    Waiting must be bounded by the first-byte window, NOT by the attempt
    window: 90.7s of pre-header silence is what let the provider consume the
    whole task budget and skip the retry entirely.
    """
    from lib.translate.engine import (
        translation_attempt_timeout_seconds,
        translation_first_byte_timeout_seconds,
        translation_total_deadline_seconds,
    )

    chars = 2783
    first_byte = translation_first_byte_timeout_seconds(chars)
    attempt = translation_attempt_timeout_seconds(chars)

    assert first_byte <= 20.0
    assert first_byte < attempt
    # The first attempt may die at the first-byte bound and still leave a full
    # attempt's worth of budget for the retry that delivers the answer.
    assert translation_total_deadline_seconds(chars) - first_byte >= attempt


def test_a_healthy_slow_generation_is_not_cut_short():
    """Speed must not be bought by truncating legitimate output windows."""
    from lib.translate.engine import translation_attempt_timeout_seconds

    assert translation_attempt_timeout_seconds(40_000) >= 180
    assert translation_attempt_timeout_seconds(120) >= 60


def test_interactive_engine_calls_keep_the_engine_default_total():
    """An unbound caller (no ``overall_deadline``) keeps the 600s default."""
    from lib.translate.engine._engine import _translate_one_chunk
    import inspect

    assert inspect.signature(
        _translate_one_chunk).parameters['overall_deadline'].default is None
