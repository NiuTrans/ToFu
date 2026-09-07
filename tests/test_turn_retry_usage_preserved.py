"""tests/test_turn_retry_usage_preserved.py — a whole-turn auto-retry must not
erase the usage the gateway already billed.

THE BUG
=======
``_maybe_auto_retry_turn`` re-runs a settled-but-transiently-failed turn. Before
re-invoking ``run_task`` it reset the per-turn accumulators, including::

    task['usage'] = {}          # <- every token billed so far, erased
    task['toolRounds'] = []

Those tokens were REAL. By the time a turn settles into ``abnormal_stop`` or
``premature_close`` the inner stream-anomaly loop may already have burned up to
16 attempts' worth of thinking tokens, and each one was correctly folded into
``accumulated_usage`` → ``task['usage']``. The outer retry then dropped the
whole figure on the floor and started a fresh ``accumulated_usage = {}``
(``_run.py``). Since the wallet settles from the FINAL ``task['usage']``
(``lib/billing/request_flow.settle_task``), the user paid for up to 4 attempts
and was charged for one.

This is the same failure mode as the FloorRetry accounting bug: a real request
that the gateway billed, hidden from the cost report. ``abnormal_stop`` is in
``_AUTO_RETRY_KINDS``, so this is a routine long-turn outcome, not a rare edge.

THE FIX
=======
Fold the discarded attempt's usage / apiRounds into the SAME carry-forward slots
the continue-checkpoint path already uses (``_checkpointUsage`` /
``_checkpointApiRounds``), which ``_finalize_task`` merges into the terminal
``task['usage']``. Semantically exact: those slots mean "billed before this
run_task invocation", and a discarded attempt is precisely that. Additive, so a
task that is BOTH resumed-from-checkpoint AND auto-retried accumulates both.

Run:
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/test_turn_retry_usage_preserved.py -v
"""
from __future__ import annotations

import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.error_envelope import make_envelope  # noqa: E402

pytestmark = pytest.mark.unit


def _task(*, usage=None, api_rounds=None, err='abnormal_stop', **extra):
    t = {
        'id': 'usagetask1',
        'convId': 'convusage',
        'aborted': False,
        'content': 'partial answer',
        'thinking': 'partial thinking',
        'error': make_envelope(err, detail='x') if err else None,
        'status': 'error',
        'finishReason': 'error',
        'toolRounds': [{'round': 0}],
        'messages': [{'role': 'user', 'content': 'hi'}],
        '_turn_input_messages': [{'role': 'user', 'content': 'hi'}],
        'model': 'yuju-claude-opus-5-evaDaily',
        'events': [],
        'events_lock': threading.Lock(),
        'content_lock': threading.Lock(),
    }
    if usage is not None:
        t['usage'] = usage
    if api_rounds is not None:
        t['apiRounds'] = api_rounds
    t.update(extra)
    return t


@pytest.fixture()
def seam(monkeypatch):
    """_maybe_auto_retry_turn with run_task spied and backoff zeroed."""
    import lib.tasks_pkg.orchestrator._finalize as orch
    import lib.tasks_pkg.orchestrator._ports as orchestrator_ports
    import lib.tasks_pkg.turn_retry as tr

    seen = {'calls': 0, 'usage_at_rerun': None}

    def _spy(task):
        seen['calls'] += 1
        seen['usage_at_rerun'] = dict(task.get('usage') or {})

    monkeypatch.setattr(orchestrator_ports, 'rerun_task', _spy)
    monkeypatch.setattr(tr, 'auto_turn_backoff_seconds', lambda attempt: 0.0)
    return orch, seen


# ── The core defect ─────────────────────────────────────────────────────

def test_discarded_attempt_usage_is_carried_forward(seam):
    """The failed attempt's billed tokens must survive the retry reset.

    16 inner stream retries' worth of thinking tokens were already folded into
    task['usage']; the outer retry must not make them vanish from the bill.
    """
    orch, _ = seam
    billed = {'prompt_tokens': 82843, 'completion_tokens': 4120,
              'cache_read_tokens': 82841, 'reasoning_tokens': 3900}
    task = _task(usage=dict(billed))

    assert orch._maybe_auto_retry_turn(task, cfg={}) is True

    carried = task.get('_checkpointUsage') or {}
    assert carried, (
        'the discarded attempt was billed but nothing was carried forward — '
        'the wallet will settle from the re-run alone')
    for k, v in billed.items():
        assert carried.get(k) == v, (
            'carried %s=%s, expected %s' % (k, carried.get(k), v))


def test_per_round_breakdown_is_carried_forward(seam):
    """apiRounds drives the cost popover's per-round view; keep it too."""
    orch, _ = seam
    rounds = [{'round': 1, 'usage': {'prompt_tokens': 1000}},
              {'round': 2, 'usage': {'prompt_tokens': 2000}}]
    task = _task(usage={'prompt_tokens': 3000}, api_rounds=list(rounds))

    orch._maybe_auto_retry_turn(task, cfg={})

    carried = task.get('_checkpointApiRounds') or []
    assert len(carried) == 2, (
        'discarded per-round breakdown lost: %s' % carried)


def test_running_state_is_still_clean_for_the_rerun(seam):
    """REGRESSION GUARD: carrying the bill forward must NOT re-seed the live
    accumulator — the re-run still starts from a clean slate, otherwise the
    re-run would double-count its own prior attempt."""
    orch, seen = seam
    task = _task(usage={'prompt_tokens': 5000})

    orch._maybe_auto_retry_turn(task, cfg={})

    assert seen['calls'] == 1
    assert seen['usage_at_rerun'] == {}, (
        "run_task must see an EMPTY task['usage']; got %s"
        % seen['usage_at_rerun'])
    assert task['content'] == ''
    assert task['thinking'] == ''
    assert task['toolRounds'] == []


def test_repeated_retries_accumulate_every_attempt(seam):
    """3 attempts → the carry-forward holds the sum of all discarded ones.

    The budget allows up to _AUTO_TURN_RETRY_MAX whole-turn re-runs, so the
    under-billing compounds; each discarded attempt must be added, not
    overwritten.
    """
    orch, _ = seam
    task = _task(usage={'prompt_tokens': 1000})
    orch._maybe_auto_retry_turn(task, cfg={})

    # Second failed attempt: run_task would have repopulated usage.
    task['usage'] = {'prompt_tokens': 2000}
    task['error'] = make_envelope('abnormal_stop', detail='x')
    task['status'] = 'error'
    orch._maybe_auto_retry_turn(task, cfg={})

    carried = task.get('_checkpointUsage') or {}
    assert carried.get('prompt_tokens') == 3000, (
        'expected 1000+2000 carried, got %s' % carried.get('prompt_tokens'))


def test_composes_with_an_existing_continue_checkpoint(seam):
    """A task resumed from a checkpoint AND auto-retried must keep BOTH bills."""
    orch, _ = seam
    task = _task(usage={'prompt_tokens': 500},
                 _checkpointUsage={'prompt_tokens': 7000})

    orch._maybe_auto_retry_turn(task, cfg={})

    carried = task.get('_checkpointUsage') or {}
    assert carried.get('prompt_tokens') == 7500, (
        'checkpoint 7000 + discarded 500 expected, got %s'
        % carried.get('prompt_tokens'))


def test_non_numeric_usage_fields_do_not_break_the_merge(seam):
    """Real usage dicts carry strings/dicts (trace_id, _dispatch); summing must
    skip them rather than raise."""
    orch, _ = seam
    task = _task(usage={'prompt_tokens': 100, 'trace_id': 'abc123',
                        '_dispatch': {'key': 'sankuai_key_1'}})

    assert orch._maybe_auto_retry_turn(task, cfg={}) is True
    carried = task.get('_checkpointUsage') or {}
    assert carried.get('prompt_tokens') == 100


def test_empty_usage_carries_nothing(seam):
    """A turn that failed before billing anything must not invent a carry."""
    orch, _ = seam
    task = _task(usage={})
    orch._maybe_auto_retry_turn(task, cfg={})
    assert not (task.get('_checkpointUsage') or {})


# ── Non-retry paths must not touch the carry-forward ────────────────────

def test_non_retryable_error_leaves_usage_intact(seam):
    """A persistent error finalizes normally — usage stays on task['usage'],
    nothing is moved, so the caller bills it exactly once."""
    orch, seen = seam
    task = _task(usage={'prompt_tokens': 4321}, err='content_filter')

    assert orch._maybe_auto_retry_turn(task, cfg={}) is False
    assert seen['calls'] == 0
    assert task['usage'] == {'prompt_tokens': 4321}
    assert not task.get('_checkpointUsage')


def test_abort_during_backoff_leaves_usage_intact(monkeypatch, seam):
    """User pressed Stop mid-backoff: no re-run, so the bill stays where the
    finalizer will read it."""
    orch, seen = seam
    import lib.tasks_pkg.stream_handler._budget as sh
    import lib.tasks_pkg.turn_retry as tr
    # The seam fixture zeroes the backoff, but `_maybe_auto_retry_turn` only
    # sleeps when backoff > 0 — a zero would skip the sleep and never reach the
    # abort. Restore a positive backoff so the abort path is genuinely driven.
    monkeypatch.setattr(tr, 'auto_turn_backoff_seconds', lambda attempt: 0.01)
    monkeypatch.setattr(sh, '_interruptible_sleep',
                        lambda s, t: t.__setitem__('aborted', True))

    task = _task(usage={'prompt_tokens': 999})
    assert orch._maybe_auto_retry_turn(task, cfg={}) is False
    assert seen['calls'] == 0
    assert task['usage'] == {'prompt_tokens': 999}
    assert not task.get('_checkpointUsage')
