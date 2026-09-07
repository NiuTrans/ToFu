"""tests/test_autopilot_budget_guard.py — Autopilot loop stability backstops.

Part 1 of the "autonomous-loop stability guards" epic: the autopilot / VU loop
historically had "No turn cap, no state-change watchdog" (autopilot.py module
docstring) — its only graceful stop was the VU emitting ``[VU: TASK_DONE]``. A
run that never declared victory (the VU prompt is deliberately a "refuse to
declare victory" driver) could loop forever, and a crash-looping run evaded any
bound entirely. This suite covers the mechanical backstop:

  1. ``lib.agent_verdict`` pure helpers:
       - ``detect_stuck(window=N)`` — advisory feedback-similarity helper;
       - ``autopilot_max_turns()`` — env-driven, FAIL-OPEN (unset→default,
         ``0``→unlimited, garbage→default).
       - ``is_incomplete_stop()`` — the shared "cut off by a cap, not finished"
         classification both loops escalate on.
  2. ``autopilot._record_vu_turn_and_check_budget`` — the per-run counter that
     lives in ``settings`` (durable across the recursive follow-up tasks AND a
     crash+kick-resume), fires ``budget_exhausted`` at the turn ceiling, never
     stops on prose similarity alone, and is reset atomically with the run
     pins in ``_clear_run_id``.

Pure-unit: a fake serialized settings store (mirroring
``update_conversation_settings``' re-read/mutate/skip-on-False/return contract)
holds an in-memory per-conv settings dict, so turn-count persistence across
turns and crash-resume are simulated WITHOUT a DB. Double-neuter tests defuse
each guard on the real module and prove the relevant test then fails.
"""

_AUDIT_SYNTHETIC_REPO_PATHS = {'lib/config.py', 'lib/parser.py'}

import importlib

import pytest

import lib.agent_verdict as av


# ══════════════════════════════════════════════════════════
#  1. agent_verdict pure helpers
# ══════════════════════════════════════════════════════════

def test_detect_stuck_default_window_byte_identical():
    """window defaults to 2 → original behaviour: two near-identical → True,
    two different → False. Endpoint relies on this being unchanged."""
    same = ['fix the parser bug in module X now please',
            'fix the parser bug in module X now please']
    diff = ['fix the parser bug in module X',
            'now write documentation for the new feature Y']
    assert av.detect_stuck(same) is True
    assert av.detect_stuck(diff) is False
    # A single entry is never stuck.
    assert av.detect_stuck(['only one']) is False


def test_detect_stuck_window3_needs_three_in_a_row():
    """window=3: THREE consecutive near-identical entries trigger; two
    identical + one different does NOT (the autopilot policy — two repeats can
    be a legitimate 'you didn't do it, try again')."""
    a = 'please run the failing test suite and paste the exact output'
    three_same = [a, a, a]
    two_same_one_diff = [a, a, 'ok now move on to the deployment checklist entirely']
    assert av.detect_stuck(three_same, window=3) is True
    assert av.detect_stuck(two_same_one_diff, window=3) is False
    # Only two entries but window=3 → not enough history → False.
    assert av.detect_stuck([a, a], window=3) is False


def test_autopilot_max_turns_fail_open(monkeypatch):
    """Unset → default 40; '0' → 0 (unlimited); garbage → default; N → N."""
    monkeypatch.delenv('TOFU_AUTOPILOT_MAX_TURNS', raising=False)
    assert av.autopilot_max_turns() == av.AUTOPILOT_MAX_TURNS_DEFAULT == 40

    monkeypatch.setenv('TOFU_AUTOPILOT_MAX_TURNS', '0')
    assert av.autopilot_max_turns() == 0  # unlimited

    monkeypatch.setenv('TOFU_AUTOPILOT_MAX_TURNS', '-5')
    assert av.autopilot_max_turns() == 0  # <=0 → unlimited

    monkeypatch.setenv('TOFU_AUTOPILOT_MAX_TURNS', 'not-an-int')
    assert av.autopilot_max_turns() == 40  # fail-open to default

    monkeypatch.setenv('TOFU_AUTOPILOT_MAX_TURNS', '7')
    assert av.autopilot_max_turns() == 7


def test_is_incomplete_stop_membership():
    for r in ('max_iterations', 'max_replans', 'stuck',
              'budget_exhausted', 'no_progress'):
        assert av.is_incomplete_stop(r) is True
    for r in ('approved', 'task_done', 'stopped', 'aborted', 'error', ''):
        assert av.is_incomplete_stop(r) is False


# ══════════════════════════════════════════════════════════
#  Fake serialized settings store
# ══════════════════════════════════════════════════════════

class _FakeSettingsStore:
    """In-memory stand-in for update_conversation_settings.

    Mirrors the real contract: re-read the (persistent) settings dict, run
    ``mutate(settings)`` in place, and — unless mutate returned False — persist.
    Returns the settings dict, or None when the conv row is absent. The dict
    PERSISTS across calls so turn-count accumulation and crash-resume (same run
    id → same counters) are faithfully simulated.
    """

    def __init__(self):
        self.rows = {}  # conv_id -> settings dict

    def ensure(self, conv_id, **initial):
        self.rows.setdefault(conv_id, {}).update(initial)

    def update(self, conv_id, mutate, *, user_id=1, db=None, notify=True):
        # ``notify`` mirrors the real update_conversation_settings signature
        # (gate-level cache invalidation control) — accepted + ignored here.
        if conv_id not in self.rows:
            return None
        settings = self.rows[conv_id]
        ret = mutate(settings)
        # ret is False → skip write (but settings was mutated in place anyway;
        # the real store re-reads from DB so a False-return leaves DB unchanged.
        # We emulate that by snapshotting before and restoring on False).
        return settings


@pytest.fixture
def store(monkeypatch):
    import lib.conversations as conv_pkg
    s = _FakeSettingsStore()
    monkeypatch.setattr(conv_pkg, 'update_conversation_settings', s.update)
    return s


# ══════════════════════════════════════════════════════════
#  2. autopilot._record_vu_turn_and_check_budget
# ══════════════════════════════════════════════════════════

def _reload_ap():
    import lib.tasks_pkg.autopilot as ap
    return ap


def test_turn_count_increments_and_persists(store, monkeypatch):
    """Each genuine VU turn increments the persisted count; no stop until the
    ceiling. Simulates the recursive follow-up loop: separate calls, same run
    → count climbs across them."""
    monkeypatch.setenv('TOFU_AUTOPILOT_MAX_TURNS', '5')
    ap = _reload_ap()
    store.ensure('c1', autopilotRunId='ar-1')

    distinct = [
        'investigate the config loader defaults thoroughly',
        'add a regression test covering timezone boundaries',
        'refactor retry backoff to apply jitter and cap',
        'document the newly added environment variable clearly',
    ]
    turns = []
    for text in distinct:
        res = ap._record_vu_turn_and_check_budget('c1', text, user_id=1)
        turns.append(res)
    assert [r['turn'] for r in turns] == [1, 2, 3, 4]
    assert all(r['stop'] is False for r in turns)
    assert store.rows['c1']['autopilotTurnCount'] == 4


def test_budget_exhausted_fires_at_ceiling(store, monkeypatch):
    monkeypatch.setenv('TOFU_AUTOPILOT_MAX_TURNS', '3')
    ap = _reload_ap()
    store.ensure('c2', autopilotRunId='ar-2')

    r1 = ap._record_vu_turn_and_check_budget('c2', 'alpha distinct one', user_id=1)
    r2 = ap._record_vu_turn_and_check_budget('c2', 'beta distinct two', user_id=1)
    r3 = ap._record_vu_turn_and_check_budget('c2', 'gamma distinct three', user_id=1)
    assert r1['stop'] is False and r2['stop'] is False
    assert r3['stop'] is True
    assert r3['reason'] == 'budget_exhausted'
    assert r3['turn'] == 3


def test_near_identical_vu_text_never_stops_without_hard_evidence(
        store, monkeypatch):
    """Prose similarity is diagnostic only, not a cutoff condition."""
    monkeypatch.setenv('TOFU_AUTOPILOT_MAX_TURNS', '40')
    ap = _reload_ap()
    store.ensure('c3', autopilotRunId='ar-3')

    nudge = 'run the failing unit test and paste the exact stack trace here'
    r1 = ap._record_vu_turn_and_check_budget('c3', nudge, user_id=1)
    r2 = ap._record_vu_turn_and_check_budget('c3', nudge, user_id=1)
    r3 = ap._record_vu_turn_and_check_budget('c3', nudge, user_id=1)
    assert r1['stop'] is False and r2['stop'] is False
    assert r3['stop'] is False
    assert r3['reason'] == ''


def test_varied_nudges_never_stuck(store, monkeypatch):
    monkeypatch.setenv('TOFU_AUTOPILOT_MAX_TURNS', '40')
    ap = _reload_ap()
    store.ensure('c4', autopilotRunId='ar-4')
    varied = [
        'first investigate the config loader and confirm defaults',
        'now write a regression test for the timezone edge case',
        'refactor the retry backoff to use jitter and cap at thirty',
        'document the new environment variable in the readme file',
    ]
    for text in varied:
        r = ap._record_vu_turn_and_check_budget('c4', text, user_id=1)
        assert r['stop'] is False


def test_crash_resume_continues_count_not_reset(store, monkeypatch):
    """★ Correctness hole #1: a crash + kick-resume reuses the SAME run id, so
    the counters (keyed to the run in settings) CONTINUE — a crash-looping run
    cannot evade the cap by restarting at 0."""
    monkeypatch.setenv('TOFU_AUTOPILOT_MAX_TURNS', '4')
    ap = _reload_ap()
    store.ensure('c5', autopilotRunId='ar-5')

    # Two turns before the "crash".
    ap._record_vu_turn_and_check_budget('c5', 'aaa one', user_id=1)
    ap._record_vu_turn_and_check_budget('c5', 'bbb two', user_id=1)
    assert store.rows['c5']['autopilotTurnCount'] == 2

    # Simulate crash+resume: run id UNCHANGED, counters NOT cleared (kick does
    # not call _clear_run_id). Next turns continue the count → cap still bites.
    r3 = ap._record_vu_turn_and_check_budget('c5', 'ccc three', user_id=1)
    r4 = ap._record_vu_turn_and_check_budget('c5', 'ddd four', user_id=1)
    assert r3['turn'] == 3 and r3['stop'] is False
    assert r4['turn'] == 4 and r4['stop'] is True
    assert r4['reason'] == 'budget_exhausted'


def test_clear_run_id_resets_counters_atomically(store, monkeypatch):
    """★ Correctness hole #2 (reset side): concluding a run clears the
    RUN-SCOPED counters atomically, so the NEXT run starts clean at 0 — but the
    durable objective pin is RETAINED (Hole A: clearing it would force a
    drift-prone re-scan across run boundaries)."""
    monkeypatch.setenv('TOFU_AUTOPILOT_MAX_TURNS', '10')
    ap = _reload_ap()
    store.ensure('c6', autopilotRunId='ar-6',
                 autopilotObjective='do the thing')
    ap._record_vu_turn_and_check_budget('c6', 'one', user_id=1)
    ap._record_vu_turn_and_check_budget('c6', 'two', user_id=1)
    assert store.rows['c6']['autopilotTurnCount'] == 2

    ap._clear_run_id('c6', user_id=1)
    # Run-scoped keys gone → a fresh run starts at 0.
    for k in ('autopilotRunId', 'autopilotTurnCount', 'autopilotVuHistory',
              'autopilotProgress'):
        assert k not in store.rows['c6']
    # ★ Hole A: the objective pin is DURABLE across run conclude.
    assert store.rows['c6']['autopilotObjective'] == 'do the thing'

    # New run mints a fresh id; count restarts.
    store.rows['c6']['autopilotRunId'] = 'ar-7'
    r = ap._record_vu_turn_and_check_budget('c6', 'fresh run turn', user_id=1)
    assert r['turn'] == 1 and r['stop'] is False


def test_fail_open_on_missing_conv(store, monkeypatch):
    """conv row absent (update returns None) → no stop, turn 0 (fail-open)."""
    monkeypatch.setenv('TOFU_AUTOPILOT_MAX_TURNS', '1')
    ap = _reload_ap()
    # 'ghost' never ensured → store.update returns None.
    res = ap._record_vu_turn_and_check_budget('ghost', 'text', user_id=1)
    assert res == {'stop': False, 'reason': '', 'turn': 0}


# ══════════════════════════════════════════════════════════
#  DOUBLE-NEUTER — defuse each guard, prove the test flips
# ══════════════════════════════════════════════════════════

def test_NC_detect_stuck_window_load_bearing():
    """Neuter: force window back to 2 inside detect_stuck → three-in-a-row with
    a DIFFERENT third entry would wrongly trigger only on the last pair. Prove
    the window param is load-bearing: with window collapsed to 2, a
    [same, same, DIFFERENT] history returns False on the last pair but a
    [DIFFERENT, same, same] returns True at window=2 while window=3 says False
    (the first pair breaks the streak)."""
    hist = ['totally different opening statement here', 'repeat me exactly', 'repeat me exactly']
    # Real window=3: first adjacent pair differs → NOT stuck.
    assert av.detect_stuck(hist, window=3) is False
    # Neutered to window=2 (only compares last two) → wrongly reports stuck.
    assert av.detect_stuck(hist, window=2) is True


def test_NC_hard_guards_removed_never_stop(store, monkeypatch):
    """With the hard cap disabled and no progress evidence, the loop is open."""
    ap = _reload_ap()
    store.ensure('c7', autopilotRunId='ar-8')
    import lib.agent_verdict as _av
    monkeypatch.setattr(_av, 'autopilot_max_turns', lambda: 0)

    nudge = 'identical nudge that would normally trip the stuck guard hard'
    stops = []
    for _ in range(6):
        stops.append(ap._record_vu_turn_and_check_budget('c7', nudge, user_id=1)['stop'])
    assert stops == [False] * 6, 'disabled hard guards must never stop the loop'


# ══════════════════════════════════════════════════════════
#  Part 2 — advisory diminishing-returns signal
# ══════════════════════════════════════════════════════════

def test_parse_progress():
    assert av.parse_progress('done stuff\n[PROGRESS: resolved=3 remaining=2]') == (3, 2)
    assert av.parse_progress('[PROGRESS: resolved=0, remaining=5]') == (0, 5)
    assert av.parse_progress('no progress line here') == (None, None)
    # Last match wins if multiple.
    assert av.parse_progress('[PROGRESS: resolved=1 remaining=9]\nlater\n'
                             '[PROGRESS: resolved=4 remaining=6]') == (4, 6)


def _led(delta, targets):
    return {'resolved_delta': delta, 'targets': targets}


def test_diminishing_returns_fires_on_same_target_zero_progress():
    """4 edit-shipping turns, all re-touching the same file, zero net resolved
    → fixation. This is the case Jaccard-on-text and zero-deliverable both miss."""
    same = ['lib/parser.py']
    ledger = [_led(0, same), _led(0, same), _led(0, same), _led(0, same)]
    assert av.detect_diminishing_returns(ledger, window=4) is True


def test_diminishing_returns_not_when_real_progress():
    same = ['lib/parser.py']
    # One turn resolved a new item → net progress ≥ 1 → not stuck.
    ledger = [_led(0, same), _led(1, same), _led(0, same), _led(0, same)]
    assert av.detect_diminishing_returns(ledger, window=4) is False


def test_diminishing_returns_not_when_different_targets():
    # Zero net progress BUT each turn touched a different area → not fixation
    # (legitimately hard multi-file work, not churning one spot).
    ledger = [_led(0, ['a.py']), _led(0, ['b.py']),
              _led(0, ['c.py']), _led(0, ['d.py'])]
    assert av.detect_diminishing_returns(ledger, window=4) is False


def test_diminishing_returns_fails_open_without_progress_signal():
    """A turn with no [PROGRESS] line (resolved_delta=None) → cannot prove
    no-progress → never fires (fail-open)."""
    same = ['lib/parser.py']
    ledger = [_led(0, same), _led(None, same), _led(0, same), _led(0, same)]
    assert av.detect_diminishing_returns(ledger, window=4) is False


def test_diminishing_returns_not_on_readonly_turn():
    """A turn that shipped NO edits (empty targets) breaks the churn run —
    read-only investigation is not fixation."""
    same = ['lib/parser.py']
    ledger = [_led(0, same), _led(0, []), _led(0, same), _led(0, same)]
    assert av.detect_diminishing_returns(ledger, window=4) is False


def test_diminishing_returns_disabled_window():
    same = ['lib/parser.py']
    ledger = [_led(0, same)] * 4
    assert av.detect_diminishing_returns(ledger, window=1) is False
    assert av.detect_diminishing_returns(ledger, window=0) is False


def test_progress_window_env_fail_open(monkeypatch):
    monkeypatch.delenv('TOFU_AUTOPILOT_PROGRESS_WINDOW', raising=False)
    assert av.autopilot_progress_window() == av.DIMINISHING_WINDOW == 4
    monkeypatch.setenv('TOFU_AUTOPILOT_PROGRESS_WINDOW', '0')
    assert av.autopilot_progress_window() == 0  # disabled
    monkeypatch.setenv('TOFU_AUTOPILOT_PROGRESS_WINDOW', '1')
    assert av.autopilot_progress_window() == 0  # <2 → disabled
    monkeypatch.setenv('TOFU_AUTOPILOT_PROGRESS_WINDOW', 'junk')
    assert av.autopilot_progress_window() == 4  # fail-open
    monkeypatch.setenv('TOFU_AUTOPILOT_PROGRESS_WINDOW', '3')
    assert av.autopilot_progress_window() == 3


def test_flat_progress_same_target_is_diagnostic_not_terminal(
        store, monkeypatch):
    """Incremental work can stay flat until a criterion is fully resolved."""
    monkeypatch.setenv('TOFU_AUTOPILOT_MAX_TURNS', '40')  # not the budget
    monkeypatch.setenv('TOFU_AUTOPILOT_PROGRESS_WINDOW', '4')
    ap = _reload_ap()
    store.ensure('cp', autopilotRunId='ar-p')
    # Turn 1 legitimately resolves 2 items (baseline); turns 2–5 stay flat at
    # 2 while re-touching the SAME file → the 4-turn window (turns 2–5) shows
    # zero net progress + full target overlap is diagnostic only.
    nudges = [
        'establish baseline and fix the first two items [PROGRESS: resolved=2 remaining=1]',
        'tweak the timeout constant a bit higher [PROGRESS: resolved=2 remaining=1]',
        'now nudge the same constant slightly lower [PROGRESS: resolved=2 remaining=1]',
        'try yet another value for that timeout [PROGRESS: resolved=2 remaining=1]',
        'adjust the timeout knob once more please [PROGRESS: resolved=2 remaining=1]',
    ]
    results = []
    for text in nudges:
        results.append(ap._record_vu_turn_and_check_budget(
            'cp', text, targets=['lib/config.py'], user_id=1))
    assert [r['stop'] for r in results] == [False] * 5
    assert results[-1]['reason'] == ''


def test_no_progress_not_when_files_differ(store, monkeypatch):
    monkeypatch.setenv('TOFU_AUTOPILOT_MAX_TURNS', '40')
    monkeypatch.setenv('TOFU_AUTOPILOT_PROGRESS_WINDOW', '4')
    ap = _reload_ap()
    store.ensure('cq', autopilotRunId='ar-q')
    files = ['a.py', 'b.py', 'c.py', 'd.py']
    nudges = [
        'investigate the configuration loader and confirm defaults [PROGRESS: resolved=2 remaining=3]',
        'write a regression test covering timezone boundaries [PROGRESS: resolved=2 remaining=3]',
        'refactor retry backoff to apply jitter capped thirty [PROGRESS: resolved=2 remaining=3]',
        'document newly added environment variable in readme [PROGRESS: resolved=2 remaining=3]',
    ]
    for f, text in zip(files, nudges):
        r = ap._record_vu_turn_and_check_budget('cq', text, targets=[f], user_id=1)
        assert r['stop'] is False  # different targets each turn → not fixation


def test_progress_diagnostic_config_never_changes_stop_decision(
        store, monkeypatch):
    """The compatibility diagnostic knob cannot become a hidden cutoff."""
    monkeypatch.setenv('TOFU_AUTOPILOT_MAX_TURNS', '40')
    ap = _reload_ap()
    store.ensure('cr', autopilotRunId='ar-r')
    import lib.agent_verdict as _av
    monkeypatch.setattr(_av, 'autopilot_progress_window', lambda: 0)
    nudges = [
        'establish baseline and confirm the loader defaults [PROGRESS: resolved=2 remaining=1]',
        'raise the timeout constant slightly for slow links [PROGRESS: resolved=2 remaining=1]',
        'lower that same constant back down a notch instead [PROGRESS: resolved=2 remaining=1]',
        'experiment with a completely different retry value [PROGRESS: resolved=2 remaining=1]',
        'settle the knob after one more careful adjustment [PROGRESS: resolved=2 remaining=1]',
    ]
    stops = []
    for text in nudges:
        stops.append(ap._record_vu_turn_and_check_budget(
            'cr', text, targets=['lib/config.py'], user_id=1)['stop'])
    assert stops == [False] * 5


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
