"""Tests for ground-truth token calibration of the compaction gate.

The gate counts tokens with estimate tiers whose systematic bias is
proven (tiktoken ~0.66x on Claude traffic; heuristic up to ~10x on
CJK-heavy content). ``_calibration.py`` closes the loop with the real
billed input size from each successful stream: gate N's raw estimate is
paired with stream N's measured usage, folded into a clamped EWMA per
(model, estimator-tier), and applied to subsequent estimate-tier counts.
"""
from __future__ import annotations

import pytest

from lib.tasks_pkg.compaction._calibration import (
    calibrated_estimate,
    calibration_ratio,
    note_stream_ground_truth,
    update_and_stash_sample,
)
from lib.tasks_pkg.compaction._tokens import (
    _count_tokens_authoritative,
    _should_force_compact,
)


# ── pure scaling ────────────────────────────────────────────────────────

@pytest.mark.unit
def test_calibrated_estimate_scales_estimate_tiers_only():
    task = {'_tokenCalibration': {
        'model': 'm1', 'ratios': {'tiktoken': 1.5, 'heuristic': 0.5}}}

    assert calibrated_estimate(
        task, model='m1', method='tiktoken', estimate=100_000) == (150_000, True)
    # heuristic_fallback shares the heuristic channel.
    assert calibrated_estimate(
        task, model='m1', method='heuristic_fallback',
        estimate=100_000) == (50_000, True)
    # Exact tiers are provider-measured — never scaled.
    assert calibrated_estimate(
        task, model='m1', method='usage_cache',
        estimate=100_000) == (100_000, False)
    # Unknown model / unknown tier / absent calibration pass through.
    assert calibrated_estimate(
        task, model='other', method='tiktoken',
        estimate=100_000) == (100_000, False)
    assert calibrated_estimate(
        {'config': {}}, model='m1', method='tiktoken',
        estimate=100_000) == (100_000, False)


# ── gate integration ────────────────────────────────────────────────────

@pytest.mark.unit
def test_gate_applies_learned_ratio_and_skips_heuristic_floor(monkeypatch):
    """A calibrated estimate supersedes estimate-vs-estimate floor clamping."""
    import lib.token_counter as token_counter
    import lib.tasks_pkg.compaction._tokens as token_mod

    monkeypatch.setattr(
        token_counter, 'count_tokens',
        lambda *a, **k: {'tokens': 50_000, 'method': 'tiktoken'})
    # The raw heuristic would otherwise raise the floor to 1.7×50K=85K.
    monkeypatch.setattr(token_mod, '_estimate_total_tokens', lambda _m: 400_000)

    task = {
        'config': {'model': 'm1'},
        'convId': 'cal-gate',
        '_tokenCalibration': {'model': 'm1', 'ratios': {'tiktoken': 2.0}},
    }
    measurement = {}
    count, method = _count_tokens_authoritative(
        [{'role': 'user', 'content': 'hello'}], task,
        measurement_out=measurement)

    assert count == 100_000
    assert method == 'tiktoken+cal'
    assert measurement['raw_estimate_tokens'] == 50_000
    assert measurement['calibrated'] is True

    # Control: no calibration → the floor engages exactly as before.
    measurement2 = {}
    count2, method2 = _count_tokens_authoritative(
        [{'role': 'user', 'content': 'hello'}],
        {'config': {'model': 'm1'}, 'convId': 'cal-gate-control'},
        measurement_out=measurement2)
    assert count2 == 85_000  # floor capped at 1.7× the estimate
    assert method2.endswith('heuristic_floor')
    assert measurement2['calibrated'] is False


@pytest.mark.unit
def test_force_compact_triggers_on_calibrated_count(monkeypatch):
    """Systematic under-count no longer lets an oversized prompt slip past."""
    import lib.token_counter as token_counter
    import lib.tasks_pkg.compaction._tokens as token_mod

    monkeypatch.setattr(
        token_counter, 'count_tokens',
        lambda *a, **k: {'tokens': 100_000, 'method': 'tiktoken'})
    monkeypatch.setattr(token_mod, '_estimate_total_tokens', lambda _m: 10)
    monkeypatch.setattr(token_mod, '_get_context_limit', lambda _t: 1_000_000)

    base_task = {
        'config': {'model': 'm1', 'compaction': {'workingSetTokens': 128_000}},
        'convId': 'cal-trigger',
    }
    messages = [{'role': 'user', 'content': 'hello'}]

    assert _should_force_compact(messages, dict(base_task)) is False

    calibrated = dict(base_task)
    calibrated['_tokenCalibration'] = {
        'model': 'm1', 'ratios': {'tiktoken': 1.6}}
    assert _should_force_compact(messages, calibrated) is True


# ── pairing loop ────────────────────────────────────────────────────────

@pytest.mark.unit
def test_pairing_learns_ratio_then_ewma_smooths():
    task = {'config': {'model': 'm1'}, 'convId': 'cal-pair'}

    # Round N: gate stashes raw estimate; stream reports the real size.
    update_and_stash_sample(task, model='m1', raw_estimate=100_000,
                            method='tiktoken')
    note_stream_ground_truth(task, model='m1', prompt_tokens=150_000)

    # Round N+1: the pair folds into the learned ratio.
    update_and_stash_sample(task, model='m1', raw_estimate=110_000,
                            method='tiktoken')
    assert calibration_ratio(task, model='m1', method='tiktoken') == 1.5

    # Round N+2: a new observation is EWMA-smoothed (0.5 weight).
    note_stream_ground_truth(task, model='m1', prompt_tokens=55_000)
    update_and_stash_sample(task, model='m1', raw_estimate=120_000,
                            method='tiktoken')
    ratio = calibration_ratio(task, model='m1', method='tiktoken')
    assert ratio == pytest.approx(0.5 * 1.5 + 0.5 * 0.5)


@pytest.mark.unit
def test_pairing_clamps_pathological_ratios():
    task = {'config': {'model': 'm1'}, 'convId': 'cal-clamp'}
    update_and_stash_sample(task, model='m1', raw_estimate=100_000,
                            method='tiktoken')
    note_stream_ground_truth(task, model='m1', prompt_tokens=10_000_000)
    update_and_stash_sample(task, model='m1', raw_estimate=100_000,
                            method='tiktoken')
    assert calibration_ratio(task, model='m1', method='tiktoken') == 3.0

    task2 = {'config': {'model': 'm1'}, 'convId': 'cal-clamp2'}
    update_and_stash_sample(task2, model='m1', raw_estimate=1_000_000,
                            method='heuristic')
    note_stream_ground_truth(task2, model='m1', prompt_tokens=1)
    update_and_stash_sample(task2, model='m1', raw_estimate=1_000_000,
                            method='heuristic')
    assert calibration_ratio(task2, model='m1', method='heuristic') == 0.25


@pytest.mark.unit
def test_pairing_rejects_stale_mismatched_or_weak_samples():
    # A gate sample is consumed exactly once: without a NEW stream the
    # same ground truth must never fold into the ratio a second time.
    task = {'config': {'model': 'm1'}, 'convId': 'cal-stale'}
    update_and_stash_sample(task, model='m1', raw_estimate=100_000,
                            method='tiktoken')
    note_stream_ground_truth(task, model='m1', prompt_tokens=150_000)
    update_and_stash_sample(task, model='m1', raw_estimate=100_000,
                            method='tiktoken')
    assert calibration_ratio(task, model='m1', method='tiktoken') == 1.5
    # No new stream arrived — the stale truth is not re-paired.
    update_and_stash_sample(task, model='m1', raw_estimate=100_000,
                            method='tiktoken')
    assert calibration_ratio(task, model='m1', method='tiktoken') == 1.5

    # A failed stream between gates records nothing; pairing is skipped.
    task_fail = {'config': {'model': 'm1'}, 'convId': 'cal-failed-stream'}
    update_and_stash_sample(task_fail, model='m1', raw_estimate=100_000,
                            method='tiktoken')
    update_and_stash_sample(task_fail, model='m1', raw_estimate=100_000,
                            method='tiktoken')
    assert calibration_ratio(task_fail, model='m1', method='tiktoken') is None

    # Model mismatch between sample and truth is dropped.
    task2 = {'config': {'model': 'm1'}, 'convId': 'cal-mismatch'}
    update_and_stash_sample(task2, model='m1', raw_estimate=100_000,
                            method='tiktoken')
    note_stream_ground_truth(task2, model='m2', prompt_tokens=150_000)
    update_and_stash_sample(task2, model='m1', raw_estimate=100_000,
                            method='tiktoken')
    assert calibration_ratio(task2, model='m1', method='tiktoken') is None

    # Tiny samples are noise-dominated; exact-tier samples need no ratio.
    task3 = {'config': {'model': 'm1'}, 'convId': 'cal-weak'}
    update_and_stash_sample(task3, model='m1', raw_estimate=500,
                            method='tiktoken')
    note_stream_ground_truth(task3, model='m1', prompt_tokens=900)
    update_and_stash_sample(task3, model='m1', raw_estimate=500,
                            method='usage_cache')
    note_stream_ground_truth(task3, model='m1', prompt_tokens=900)
    update_and_stash_sample(task3, model='m1', raw_estimate=500,
                            method='tiktoken')
    assert calibration_ratio(task3, model='m1', method='tiktoken') is None


@pytest.mark.unit
def test_ground_truth_ignores_non_positive_or_malformed_usage():
    task = {'config': {'model': 'm1'}, 'convId': 'cal-bad'}
    note_stream_ground_truth(task, model='m1', prompt_tokens=0)
    note_stream_ground_truth(task, model='m1', prompt_tokens='junk')
    assert '_lastStreamGroundTruth' not in task
