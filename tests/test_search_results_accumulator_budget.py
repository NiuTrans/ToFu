"""Regression tests for the bounded per-turn search/fetch accumulator.

A long task with dozens of ``web_search``/``fetch_url`` calls used to append
every full result to ``all_search_results_text`` with no cap, and the
orchestrator's no-prose fallback joined the ENTIRE list into one assistant
message (memory + context blowup). The accumulator is now bounded in UTF-8
bytes, the NEWEST results are kept when trimming, and the finalize joins
respect the same bound.
"""

from __future__ import annotations

import pytest

from lib.tasks_pkg.tool_dispatch._pipeline import (
    SEARCH_RESULTS_ACCUM_BYTES,
    _append_search_result_text,
    bounded_search_results_text,
)

pytestmark = pytest.mark.unit


def _big_result(n: int) -> str:
    return ('x' * 10_000) + f'\nRESULT{n}\n' + ('y' * 10_000)


def _utf8_bytes(text: str) -> int:
    return len(text.encode('utf-8'))


def test_accumulator_stays_within_budget_with_many_large_results():
    results: list[str] = []
    for i in range(50):
        _append_search_result_text(results, _big_result(i))

    total = sum(_utf8_bytes(item) for item in results)
    assert total <= SEARCH_RESULTS_ACCUM_BYTES
    # Newest results are kept when trimming: the most recent append survives
    # intact, while the oldest have been dropped.
    assert results[-1] == _big_result(49)
    assert _big_result(0) not in results


def test_fallback_join_stays_within_budget_and_keeps_newest():
    results: list[str] = []
    for i in range(50):
        _append_search_result_text(results, _big_result(i))

    joined = bounded_search_results_text(results)
    assert _utf8_bytes(joined) <= SEARCH_RESULTS_ACCUM_BYTES
    assert 'RESULT49' in joined  # newest kept
    assert 'RESULT0' not in joined  # oldest trimmed


def test_fallback_join_bounds_an_unbounded_input_list():
    """Defense-in-depth: even a list that bypassed the accumulator must not
    re-send an unbounded corpus through the no-prose fallback."""
    huge = [_big_result(i) for i in range(80)]
    joined = bounded_search_results_text(huge)
    assert _utf8_bytes(joined) <= SEARCH_RESULTS_ACCUM_BYTES
    assert 'RESULT79' in joined


def test_sources_footer_join_stays_within_budget():
    huge = [_big_result(i) for i in range(80)]
    joined = bounded_search_results_text(huge, separator='\n')
    assert _utf8_bytes(joined) <= SEARCH_RESULTS_ACCUM_BYTES
    assert 'RESULT79' in joined
