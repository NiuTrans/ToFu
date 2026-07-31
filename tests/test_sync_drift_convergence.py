#!/usr/bin/env python3
"""Guards for lib/conversations/drift_tracker.py (pt_conv_state_ssot P6 precondition).

WHAT IS BEING PROVEN
--------------------
The P5 probe WARN-logs every client/server inequality. On a conversation that
is actively streaming, the client's 60s-old digest CANNOT equal a live server
read — so the warning fires constantly and says nothing. P6 must delete three
fallback branches, and the only honest justification for deleting one is
evidence that the new channel converges on its own. That requires separating:

  * healthy lag  — client is moving, just sampled late
  * a real fault — server moving while the client is FROZEN

These tests pin that discriminator, the sustained-threshold escalation, the
self-heal record, and the direction classifier. Time is injected everywhere
(``now=``) so nothing sleeps and the suite is deterministic.

Run: python3 tests/test_sync_drift_convergence.py
  or PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/test_sync_drift_convergence.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import pytest
except ImportError:  # standalone run without pytest
    pytest = None

from lib.conversations.drift_tracker import (
    observe_agreement,
    observe_divergence,
    reset,
    sustained_threshold_sec,
    tracked_count,
)

T0 = 1_800_000_000.0  # fixed epoch base; absolute value is irrelevant


def _unit(fn):
    return fn if pytest is None else pytest.mark.unit(fn)


# ── Face 1: a generating conversation must NOT warn ──────────────────────

@_unit
def test_moving_client_never_escalates_even_when_far_behind():
    """THE load-bearing case — the exact shape seen in production.

    Six live conversations reported client revs far below server (e.g. 1 vs
    181) purely because the digest is up to 60s stale. Here BOTH sides advance
    every report and the gap stays wide; that is a tracking client and must
    stay DEBUG no matter how large the gap or how long it lasts.
    """
    reset()
    client, server = 1, 181
    verdict = None
    for i in range(10):
        # Both sides move: server races ahead, client follows at a distance.
        verdict = observe_divergence('cv-live', 'rev', client, server,
                                     now=T0 + i * 60.0)
        client += 3
        server += 12

    assert verdict['client_moved'] is True
    assert verdict['server_moved'] is True
    assert verdict['stalled'] is False, (
        'a client whose value keeps advancing is tracking, not stalled')
    assert verdict['sustained'] is False
    assert verdict['severity'] == 'debug', (
        'a healthy generating conversation must not produce a WARNING — '
        'that is what made the original 246 alerts unactionable')
    assert verdict['age'] >= sustained_threshold_sec(), (
        'sanity: this ran well past the threshold, so only the moving-client '
        'discriminator (not youth) can be suppressing the warning')


# ── Face 2: a frozen client MUST escalate, but only once sustained ───────

@_unit
def test_frozen_client_escalates_only_after_sustained_window():
    """Server advancing while the client is pinned = the real fault shape."""
    reset()
    thresh = sustained_threshold_sec()

    first = observe_divergence('cv-stuck', 'rev', 7, 100, now=T0)
    assert first['stalled'] is False, (
        'one observation cannot establish "frozen" — nothing to compare to yet')

    early = observe_divergence('cv-stuck', 'rev', 7, 140, now=T0 + 30.0)
    assert early['stalled'] is True, 'server moved, client did not'
    assert early['sustained'] is False, 'not yet past the threshold'
    assert early['severity'] == 'debug'

    late = observe_divergence('cv-stuck', 'rev', 7, 205, now=T0 + thresh + 1.0)
    assert late['stalled'] is True
    assert late['sustained'] is True
    assert late['severity'] == 'warning', (
        'a client frozen while the server advances past the threshold is the '
        '"notify frame dropped, never converges" hole — it must be loud')
    assert late['observations'] == 3


@_unit
def test_idle_conversation_with_both_sides_frozen_is_not_a_fault():
    """Neither side moving is a stale-but-quiet conv, not a dropped frame.

    Without the server_moved condition this would look identical to a stalled
    client and would warn forever on every idle conversation.
    """
    reset()
    thresh = sustained_threshold_sec()
    verdict = None
    for i in range(5):
        verdict = observe_divergence('cv-idle', 'rev', 42, 99,
                                     now=T0 + i * (thresh / 2.0))
    assert verdict['server_moved'] is False
    assert verdict['stalled'] is False, (
        'if the server is not advancing, the client is not missing anything')
    assert verdict['severity'] == 'debug'


# ── Face 3: convergence is recorded (the positive evidence P6 needs) ─────

@_unit
def test_agreement_clears_tracking_and_reports_the_resolution():
    reset()
    observe_divergence('cv-heals', 'rev', 5, 60, now=T0)
    observe_divergence('cv-heals', 'rev', 5, 90, now=T0 + 60.0)
    assert tracked_count() == 1

    resolution = observe_agreement('cv-heals', 'rev', now=T0 + 120.0)
    assert resolution is not None
    assert resolution['age'] == 120.0
    assert resolution['observations'] == 2
    assert resolution['was_stalled'] is True, (
        'it WAS stalled before healing — recording that is the evidence that '
        'the channel recovers without the fallback branch')
    assert tracked_count() == 0, 'healed pairs must stop consuming state'

    assert observe_agreement('cv-heals', 'rev', now=T0 + 180.0) is None, (
        'steady-state agreement must be silent, not a repeated event')


@_unit
def test_tracking_is_scoped_per_conv_and_per_kind():
    """rev drift on one conv must not mask task_ids drift on another."""
    reset()
    observe_divergence('cv-a', 'rev', 1, 2, now=T0)
    observe_divergence('cv-a', 'task_ids', [], ['t1'], now=T0)
    observe_divergence('cv-b', 'rev', 1, 2, now=T0)
    assert tracked_count() == 3

    observe_agreement('cv-a', 'rev', now=T0 + 10.0)
    assert tracked_count() == 2, 'clearing one pair must not clear the others'


# ── Face 4: direction — client_ahead is a DIFFERENT defect ───────────────

@_unit
def test_direction_flags_client_ahead_separately():
    """client > server on server-authoritative rev means the server went
    backwards (row restored/rewritten) or a foreign frame was applied. Folding
    it into ordinary lag would hide a genuinely different bug."""
    reset()
    behind = observe_divergence('cv-x', 'rev', 10, 50, now=T0)
    assert behind['direction'] == 'client_behind'

    reset()
    ahead = observe_divergence('cv-y', 'rev', 500, 50, now=T0)
    assert ahead['direction'] == 'client_ahead'

    reset()
    tids = observe_divergence('cv-z', 'task_ids', ['a'], ['b'], now=T0)
    assert tids['direction'] == 'incomparable', (
        'task-id sets have no ordering — they must not be forced into a '
        'numeric direction')


# ── NEUTER: prove each guard is load-bearing ─────────────────────────────

@_unit
def test_NEUTER_dropping_the_moving_client_check_would_warn_on_healthy_convs():
    """Reproduce the pre-fix policy (age alone) and show it misfires.

    If severity were computed from age only — what the probe effectively did
    before — the healthy generating conversation from Face 1 would escalate.
    This is the regression this module exists to prevent.
    """
    reset()
    client, server = 1, 181
    verdict = None
    for i in range(10):
        verdict = observe_divergence('cv-neuter', 'rev', client, server,
                                     now=T0 + i * 60.0)
        client += 3
        server += 12

    naive_sustained = verdict['age'] >= sustained_threshold_sec()
    assert naive_sustained is True, (
        'age-only policy WOULD have fired here (this is the bug)')
    assert verdict['sustained'] is False, (
        'the shipped policy must NOT fire — if this ever equals the naive '
        'verdict, the moving-client discriminator has been removed')


@_unit
def test_NEUTER_dropping_the_server_moved_check_would_warn_on_idle_convs():
    reset()
    thresh = sustained_threshold_sec()
    verdict = None
    for i in range(4):
        verdict = observe_divergence('cv-neuter-idle', 'rev', 42, 99,
                                     now=T0 + i * thresh)

    stalled_without_server_check = (verdict['observations'] >= 2
                                    and not verdict['client_moved'])
    assert stalled_without_server_check is True, (
        'without the server_moved condition an idle conv looks stalled')
    assert verdict['stalled'] is False, (
        'the shipped policy must require the server to be ADVANCING; if this '
        'flips, every idle conversation becomes a false alarm')


if __name__ == '__main__':
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith('test_') and callable(fn):
            try:
                fn()
                print('ok  ', name)
            except AssertionError as e:
                failures += 1
                print('FAIL', name)
                print('     ', e)
    print('ALL PASSED' if not failures else f'{failures} FAILED')
    sys.exit(1 if failures else 0)
