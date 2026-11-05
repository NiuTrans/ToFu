"""Executable specification: a provider-measured prompt bound clamps the
entropy floor, so the gate cannot refuse a prompt the provider just accepted.

INCIDENT (conv ``mvf9laurcvekgh``, 2026-10-22)
---------------------------------------------
The gate demanded an L2 summary at
``908,877`` tokens while the provider had *billed* ``509,044`` for the same
conversation. Arithmetic from the audit trail:

    tiktoken 521,132  ×  1.7 floor cap  =  885,924  (+22,953 schema = 908,877)

The entropy floor is capped at ``1.7 ×`` the estimate tier
(``heuristic_floor_max_ratio``), and it is skipped ONLY when the task holds a
learned calibration ratio. Calibration is task-local, memory-only and paired
to the *immediately following* stream, so **round 1 of every new user turn
runs uncalibrated** and re-inflates the same list by ~1.8×. Above
``821,318 / 1.7 ≈ 483k`` real tokens (≈48 % of a 1M window) such a
conversation then demands a summary at the start of every turn.

The provider's own billed input is stronger evidence than any estimate, and
this module already records it on every successful stream
(``note_stream_ground_truth``) purely to learn a *ratio*. The ratio needs a
second round to be usable; the raw measurement does not — it bounds the
prompt the moment it arrives, and appending messages can only be bounded by
counting the appended suffix.

Design constraints
------------------
* DOWN-ONLY and BOUNDED: the anchor can only LOWER an estimate-tier floor.
  It adds the estimated cost of messages appended since the measurement, so
  it can never claim a smaller prompt than what the provider actually
  received.
* Only for estimate tiers. Provider-measured exact tiers are untouched.
* Refuses when it cannot be sound: unknown model, a rewritten (shortened)
  message list, or an expired measurement.
* Failure discipline: never raises, degrades to today's behaviour.
"""
from __future__ import annotations

import pytest

from lib.tasks_pkg.compaction._calibration import (
    note_stream_ground_truth,
    provider_prompt_anchor,
)


# ── the anchor itself ───────────────────────────────────────────────────

@pytest.mark.unit
def test_anchor_reports_measurement_plus_appended_suffix():
    task = {'config': {'model': 'm1'}}
    sent = [{'role': 'user', 'content': 'a'}, {'role': 'assistant', 'content': 'b'}]
    note_stream_ground_truth(
        task, model='m1', prompt_tokens=500_000, message_count=len(sent))

    # Nothing appended: the bound is exactly what the provider measured.
    assert provider_prompt_anchor(task, model='m1', messages=sent) == 500_000

    # Appended content is counted, so the bound tracks real growth.
    grown = sent + [{'role': 'user', 'content': 'x' * 4_000}]
    bound = provider_prompt_anchor(task, model='m1', messages=grown)
    assert bound is not None and bound > 500_000
    assert bound < 500_000 + 5_000


@pytest.mark.unit
def test_anchor_refuses_when_it_cannot_bound_the_prompt():
    sent = [{'role': 'user', 'content': 'a'}, {'role': 'assistant', 'content': 'b'}]
    task = {'config': {'model': 'm1'}}
    note_stream_ground_truth(
        task, model='m1', prompt_tokens=500_000, message_count=len(sent))

    # Different model: the measurement describes another wire encoding.
    assert provider_prompt_anchor(task, model='m2', messages=sent) is None
    # Fewer messages than were sent: the list was rewritten (compaction), so
    # the measurement no longer describes a prefix of it.
    assert provider_prompt_anchor(
        task, model='m1', messages=sent[:1]) is None
    # No measurement at all.
    assert provider_prompt_anchor(
        {'config': {'model': 'm1'}}, model='m1', messages=sent) is None
    # A measurement that predates the message-count fact cannot be positioned.
    legacy = {'config': {'model': 'm1'}, '_lastStreamGroundTruth': {
        'prompt_tokens': 500_000, 'model': 'm1', 'seq': 1}}
    assert provider_prompt_anchor(legacy, model='m1', messages=sent) is None


@pytest.mark.unit
def test_anchor_expires_without_a_fresh_measurement():
    """A stale measurement must not override a later, larger prompt."""
    task = {'config': {'model': 'm1'}}
    sent = [{'role': 'user', 'content': 'a'}]
    note_stream_ground_truth(
        task, model='m1', prompt_tokens=500_000, message_count=len(sent),
        now=1_000.0)
    assert provider_prompt_anchor(
        task, model='m1', messages=sent, now=1_060.0) == 500_000
    assert provider_prompt_anchor(
        task, model='m1', messages=sent, now=1_000.0 + 86_400) is None


# ── gate integration: the incident shape ────────────────────────────────

def _gate(monkeypatch, task, *, tiktoken, heuristic):
    import lib.token_counter as token_counter
    import lib.tasks_pkg.compaction._tokens as token_mod

    monkeypatch.setattr(
        token_counter, 'count_tokens',
        lambda *a, **k: {'tokens': tiktoken, 'method': 'tiktoken'})
    monkeypatch.setattr(token_mod, '_estimate_total_tokens', lambda _m: heuristic)
    measurement = {}
    count, method = token_mod._count_tokens_authoritative(
        [{'role': 'user', 'content': 'hi'}], task, measurement_out=measurement)
    return count, method, measurement


@pytest.mark.unit
def test_gate_floor_yields_to_provider_measurement(monkeypatch):
    """908,877-pattern: tiktoken 521,132 floored to 885,924 must become ~763k."""
    from lib.tasks_pkg.compaction._constants import real_anchor_slack

    task = {'config': {'model': 'm1'}, 'convId': 'anchor-gate'}
    note_stream_ground_truth(
        task, model='m1', prompt_tokens=509_044, message_count=1)

    count, method, measurement = _gate(
        monkeypatch, task, tiktoken=521_132, heuristic=1_400_000)

    # Bounded by the provider's own measurement (plus the ratified growth
    # slack), not by the entropy estimate.
    assert count == int(509_044 * (1 + real_anchor_slack()))
    assert method.endswith('+anchor')
    assert measurement['provider_anchored'] is True
    # And the bound is far below the ceiling that demanded a summary.
    assert count < 821_318


@pytest.mark.unit
def test_gate_floor_is_unchanged_without_a_measurement(monkeypatch):
    """Control: the pre-existing 1.7x cap still governs a fresh task."""
    count, method, measurement = _gate(
        monkeypatch, {'config': {'model': 'm1'}, 'convId': 'anchor-none'},
        tiktoken=521_132, heuristic=1_400_000)

    assert count == int(521_132 * 1.7)
    assert method.endswith('heuristic_floor')
    assert measurement['calibrated'] is False
    assert measurement['provider_anchored'] is False


@pytest.mark.unit
def test_anchor_never_raises_the_floor(monkeypatch):
    """An anchor larger than the estimate must not inflate the gate."""
    task = {'config': {'model': 'm1'}, 'convId': 'anchor-high'}
    note_stream_ground_truth(
        task, model='m1', prompt_tokens=900_000, message_count=1)

    count, method, _ = _gate(
        monkeypatch, task, tiktoken=100_000, heuristic=120_000)
    assert count == 120_000
    assert not method.endswith('+anchor')


@pytest.mark.unit
def test_anchored_gate_stays_below_the_fatal_ceiling(monkeypatch):
    """The fatal summary demand needs ``count > window_threshold``.

    A conversation near half its window may still trigger the PROACTIVE
    (economic) path, whose summary failure degrades to a warning. What must
    never happen is crossing the window safety threshold: only there does a
    local summary failure become task-fatal.
    """
    from lib.tasks_pkg.compaction._tokens import _compaction_trigger_threshold

    task = {'config': {'model': 'm1'}, 'convId': 'anchor-ceiling'}
    note_stream_ground_truth(
        task, model='m1', prompt_tokens=509_044, message_count=1)

    count, method, _ = _gate(
        monkeypatch, task, tiktoken=521_132, heuristic=1_400_000)
    window_threshold = _compaction_trigger_threshold(
        task, context_limit=1_048_576)[1]

    assert method.endswith('+anchor')
    assert count < window_threshold
    # Control: the unanchored floor is what crossed it.
    assert int(521_132 * 1.7) > window_threshold
