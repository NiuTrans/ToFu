"""tests/test_scheduler_predicate_promotion.py — scheduler predicate paradigm.

Covers the three-tier condition evaluator shared by BOTH schedulers (Timer
Watcher + proactive `agent`):

  • Layer 1 — the PURE primitive (`evaluate_predicate` / `reconcile_and_decide`
    / `derive_condition_kind`): no DB, no LLM. The reconciliation/promotion
    decision matrix is exercised cell by cell.
  • Layer 2 — the TIMER consumer (`poll_timer`): pure-`code` tier does ZERO LLM
    calls; `hybrid` reconciles + auto-promotes after the streak; a single
    disagreement resets the streak (LOAD-BEARING neuter proves the gate bites);
    a promoted predicate that later goes ambiguous never false-fires and, on a
    sustained ambiguity run, DEMOTES back to hybrid.
  • Layer 3 — the PROACTIVE consumer symmetry (`evaluate_condition_predicate` /
    `apply_reconcile_poll` + `record_poll` audit columns).
  • Default tier: no predicate params → condition_kind='llm'.

Storage-process-free: consumers receive a lightweight semantic-client fake, so
the tests assert the public storage contract instead of SQL implementation
details.
"""

from __future__ import annotations

import os
import sys
from unittest import mock

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import lib.scheduler._shared as shared
import lib.scheduler.timer as timer_mod
from lib.scheduler._shared import (
    PredicateResult,
    derive_condition_kind,
    evaluate_predicate,
    reconcile_and_decide,
)

pytestmark = pytest.mark.unit


# ═══════════════════════════════════════════════════════════════════════════
#  Layer 1 — pure primitive
# ═══════════════════════════════════════════════════════════════════════════

class TestEvaluatePredicate:
    def test_exit_zero_is_ready(self):
        r = evaluate_predicate('true')
        assert r.matched is True
        assert r.errored is False
        assert r.exit_code == 0

    def test_exit_one_is_not_ready(self):
        # grep-style "no match" — a confident False, NOT ambiguous.
        r = evaluate_predicate('false')
        assert r.matched is False
        assert r.errored is False
        assert r.exit_code == 1

    def test_ambiguous_exit_code_is_unknown_not_ready(self):
        # exit 2 (e.g. grep error) → ambiguous → NEVER ready.
        r = evaluate_predicate('sh -c "exit 2"')
        assert r.matched is None
        assert r.errored is True
        assert r.exit_code == 2

    def test_command_not_found_is_unknown(self):
        # 127 spawn-ish failure surfaced via the shell → ambiguous.
        r = evaluate_predicate('this_command_does_not_exist_xyz')
        assert r.matched is None
        assert r.errored is True

    def test_regex_match_over_stdout(self):
        r = evaluate_predicate('echo TRAINING DONE', regex=r'DONE')
        assert r.matched is True
        assert r.errored is False

    def test_regex_no_match(self):
        r = evaluate_predicate('echo still running', regex=r'DONE')
        assert r.matched is False
        assert r.errored is False

    def test_regex_wins_over_exit_code(self):
        # Command exits 0 but regex does not match → not ready (regex decides).
        r = evaluate_predicate('echo nope', regex=r'DONE')
        assert r.matched is False

    def test_empty_command_is_unknown_not_errored(self):
        r = evaluate_predicate('')
        assert r.matched is None
        assert r.errored is False

    def test_invalid_regex_is_ambiguous(self):
        r = evaluate_predicate('echo hi', regex=r'(')
        assert r.matched is None
        assert r.errored is True


class TestDeriveConditionKind:
    def test_both_is_hybrid(self):
        assert derive_condition_kind('is it done?', 'grep -q DONE f') == 'hybrid'

    def test_command_only_is_code(self):
        assert derive_condition_kind('', 'grep -q DONE f') == 'code'

    def test_instruction_only_is_llm(self):
        assert derive_condition_kind('is it done?', '') == 'llm'

    def test_neither_is_llm(self):
        assert derive_condition_kind('', '') == 'llm'


class TestReconcileMatrix:
    THR = 3

    def _m(self, v):
        return PredicateResult(matched=v, output='', exit_code=0 if v else 1)

    # ── code tier ──
    def test_code_matched_true_ready(self):
        o = reconcile_and_decide('code', self._m(True), None, False, 0,
                                 promotion_threshold=self.THR)
        assert o.authoritative_ready is True
        assert o.tier == 'code'
        assert o.new_kind == 'code'
        assert o.predicate_matched == 1

    def test_code_matched_false_not_ready(self):
        o = reconcile_and_decide('code', self._m(False), None, False, 0,
                                 promotion_threshold=self.THR)
        assert o.authoritative_ready is False
        assert o.predicate_matched == 0

    def test_code_ambiguous_never_ready_and_counts_fallback(self):
        amb = PredicateResult(matched=None, errored=True, error_note='exit 127')
        o = reconcile_and_decide('code', amb, None, False, current_streak=0,
                                 fallback_streak=0, fallback_threshold=self.THR)
        assert o.authoritative_ready is False       # never a false trigger
        assert o.fallback_to_llm is True
        assert o.demoted is False                   # not yet at threshold
        assert o.new_fallback_streak == 1
        assert o.new_kind == 'code'

    def test_code_sustained_ambiguity_demotes_to_hybrid(self):
        amb = PredicateResult(matched=None, errored=True, error_note='exit 127')
        o = reconcile_and_decide('code', amb, None, False, current_streak=0,
                                 fallback_streak=self.THR - 1,
                                 fallback_threshold=self.THR)
        assert o.demoted is True
        assert o.new_kind == 'hybrid'
        assert o.authoritative_ready is False

    # ── llm tier ──
    def test_llm_tier_passthrough(self):
        o = reconcile_and_decide('llm', None, True, True, 0)
        assert o.authoritative_ready is True
        assert o.tier == 'llm'
        assert o.new_kind == 'llm'
        assert o.predicate_matched == -1
        assert o.llm_agreed == -1

    # ── hybrid tier ──
    def test_hybrid_agree_increments_streak(self):
        o = reconcile_and_decide('hybrid', self._m(True), True, True,
                                 current_streak=0, promotion_threshold=self.THR)
        assert o.authoritative_ready is True        # LLM authoritative
        assert o.llm_agreed == 1
        assert o.new_streak == 1
        assert o.promoted is False
        assert o.new_kind == 'hybrid'

    def test_hybrid_promotes_at_threshold(self):
        o = reconcile_and_decide('hybrid', self._m(True), True, True,
                                 current_streak=self.THR - 1,
                                 promotion_threshold=self.THR)
        assert o.promoted is True
        assert o.new_kind == 'code'
        assert o.new_streak == self.THR

    def test_hybrid_disagreement_resets_streak(self):
        # LLM says ready, predicate says not — disagreement → streak to 0.
        o = reconcile_and_decide('hybrid', self._m(False), True, True,
                                 current_streak=2, promotion_threshold=self.THR)
        assert o.llm_agreed == 0
        assert o.new_streak == 0
        assert o.promoted is False
        assert o.authoritative_ready is True         # LLM still wins this poll

    def test_hybrid_llm_authoritative_when_predicate_disagrees_false(self):
        # LLM says NOT ready, predicate says ready → LLM wins (no trigger),
        # disagreement resets streak.
        o = reconcile_and_decide('hybrid', self._m(True), False, True,
                                 current_streak=2, promotion_threshold=self.THR)
        assert o.authoritative_ready is False
        assert o.llm_agreed == 0
        assert o.new_streak == 0

    def test_hybrid_unparsed_llm_resets_streak_no_promote(self):
        o = reconcile_and_decide('hybrid', self._m(True), None, False,
                                 current_streak=self.THR - 1,
                                 promotion_threshold=self.THR)
        assert o.promoted is False
        assert o.new_streak == 0
        assert o.authoritative_ready is False

    def test_hybrid_ambiguous_predicate_resets_streak_no_promote(self):
        amb = PredicateResult(matched=None, errored=True, error_note='exit 2')
        o = reconcile_and_decide('hybrid', amb, True, True,
                                 current_streak=self.THR - 1,
                                 promotion_threshold=self.THR)
        assert o.promoted is False
        assert o.new_streak == 0
        assert o.authoritative_ready is True         # LLM still decides this poll


# ═══════════════════════════════════════════════════════════════════════════
#  Layer 2 — TIMER consumer (poll_timer tiers)
# ═══════════════════════════════════════════════════════════════════════════

class _FakeStorageClient:
    """Capture semantic storage calls made by scheduler consumers."""

    def __init__(self, poll_rows=None, task_rows=None):
        self.commands = []
        self.poll_rows = list(poll_rows or [])
        self.task_rows = list(task_rows or [])

    def query(self, operation, payload):
        if operation in {'timer.poll.log', 'scheduler.poll.log'}:
            return list(self.poll_rows)
        assert operation == 'scheduler.task.list_all'
        return list(self.task_rows)

    def command(self, operation, payload, command_id):
        self.commands.append((operation, dict(payload), command_id))
        if operation == 'scheduler.task.claim_due':
            return {'claimed': True}
        return {'changed': True, 'inserted': True}


def _timer_row(**over):
    row = {
        'id': 'tmr_pred', 'user_id': 1, 'status': 'active',
        'check_instruction': '', 'check_command': '',
        'condition_kind': 'llm', 'condition_command': '',
        'condition_regex': '', 'promotion_streak': 0,
        'tools_config': '{}', 'poll_count': 0,
    }
    row.update(over)
    return row


@pytest.fixture
def _no_llm(monkeypatch):
    """Fail loudly if smart_chat is called — proves ZERO-LLM claims."""
    def _boom(*a, **k):
        raise AssertionError('smart_chat must NOT be called on the code tier')
    import lib.llm_dispatch as _ld
    monkeypatch.setattr(_ld, 'smart_chat', _boom, raising=True)


@pytest.fixture
def _capture_timer_storage(monkeypatch):
    """Route timer reconciliation through a shared semantic client."""
    client = _FakeStorageClient()
    import lib.scheduler.timer._poll as _poll

    monkeypatch.setattr(
        _poll, '_timer_client', lambda *, write=False: client, raising=True)
    return client


def test_timer_code_tier_zero_llm_ready(
        monkeypatch, _no_llm, _capture_timer_storage, tmp_path):
    """Pure `code` timer: predicate matched → ready, NO LLM call."""
    f = tmp_path / 'train.log'
    f.write_text('epoch 3 DONE\n')
    row = _timer_row(condition_kind='code',
                     condition_command=f'grep -q DONE {f}')
    monkeypatch.setattr(
        timer_mod, '_get_timer_row', lambda tid, **_kwargs: row)

    (ready, reason, tokens, skipped, parse_error, cmd_output,
     model, tool_trace, raw) = timer_mod.poll_timer('tmr_pred', user_id=1)

    assert ready is True
    assert tokens == 0
    assert model == 'predicate'
    assert skipped is False
    assert parse_error is False


def test_timer_code_tier_not_ready(
        monkeypatch, _no_llm, _capture_timer_storage, tmp_path):
    f = tmp_path / 'train.log'
    f.write_text('epoch 3 running\n')
    row = _timer_row(condition_kind='code',
                     condition_command=f'grep -q DONE {f}')
    monkeypatch.setattr(
        timer_mod, '_get_timer_row', lambda tid, **_kwargs: row)

    ready, *_rest = timer_mod.poll_timer('tmr_pred', user_id=1)
    assert ready is False


def test_timer_code_tier_ambiguous_never_fires(
        monkeypatch, _no_llm, _capture_timer_storage):
    """A broken predicate (exit 127) must NOT trigger and must NOT raise."""
    row = _timer_row(condition_kind='code',
                     condition_command='this_cmd_does_not_exist_xyz')
    monkeypatch.setattr(
        timer_mod, '_get_timer_row', lambda tid, **_kwargs: row)

    ready, reason, tokens, skipped, parse_error, *_ = timer_mod.poll_timer(
        'tmr_pred', user_id=1)
    assert ready is False           # never a false-positive trigger
    assert tokens == 0


def test_timer_code_tier_demotes_after_sustained_ambiguity(monkeypatch, _no_llm, tmp_path):
    """After fallback_threshold ambiguous polls, condition_kind flips to hybrid."""
    # Ledger already holds (threshold-1) trailing ambiguous code polls.
    thr = shared.fallback_streak_threshold()
    prior = [{'tier': 'code', 'predicate_matched': -1} for _ in range(thr - 1)]
    client = _FakeStorageClient(poll_rows=prior)
    import lib.scheduler.timer._poll as _poll
    monkeypatch.setattr(
        _poll, '_timer_client', lambda *, write=False: client, raising=True)

    row = _timer_row(condition_kind='code',
                     condition_command='this_cmd_does_not_exist_xyz')
    monkeypatch.setattr(
        timer_mod, '_get_timer_row', lambda tid, **_kwargs: row)

    ready, *_ = timer_mod.poll_timer('tmr_pred', user_id=1)
    assert ready is False
    updates = [payload for operation, payload, _ in client.commands
               if operation == 'timer.update']
    assert any(payload.get('condition_kind') == 'hybrid' for payload in updates), (
        'sustained ambiguity must demote the promoted predicate back to hybrid')


def _install_scripted_llm(monkeypatch, ready_value, reason='v'):
    import json as _json
    payload = _json.dumps({'ready': ready_value, 'reason': reason})

    def _fake(messages, **kwargs):
        return payload, {'total_tokens': 4, '_dispatch': {'model': 'cheap-x'}}
    import lib.llm_dispatch as _ld
    monkeypatch.setattr(_ld, 'smart_chat', _fake, raising=True)


def test_timer_hybrid_promotes_after_consecutive_agreement(monkeypatch, tmp_path):
    """Hybrid: predicate agrees with the LLM `threshold` times → promoted to code."""
    thr = shared.promotion_streak_threshold()
    f = tmp_path / 'train.log'
    f.write_text('DONE\n')
    client = _FakeStorageClient()
    import lib.scheduler.timer._poll as _poll
    monkeypatch.setattr(
        _poll, '_timer_client', lambda *, write=False: client, raising=True)
    # Predicate matches (grep DONE exit 0); LLM also says ready.
    _install_scripted_llm(monkeypatch, ready_value=True)
    monkeypatch.setattr(timer_mod, '_build_poll_tools', lambda cfg: None)

    # Final poll that crosses the threshold: stored streak == thr-1.
    row = _timer_row(condition_kind='hybrid', promotion_streak=thr - 1,
                     condition_command=f'grep -q DONE {f}',
                     check_instruction='done?')
    monkeypatch.setattr(
        timer_mod, '_get_timer_row', lambda tid, **_kwargs: row)

    ready, *_ = timer_mod.poll_timer('tmr_pred', user_id=1)
    assert ready is True
    updates = [payload for operation, payload, _ in client.commands
               if operation == 'timer.update']
    assert any(payload.get('condition_kind') == 'code' for payload in updates), (
        'threshold agreement must promote to code')


def test_NEUTER_timer_hybrid_disagreement_blocks_promotion(monkeypatch, tmp_path):
    """LOAD-BEARING: at the very poll that would promote, make the predicate
    DISAGREE with the LLM. The streak must reset and promotion must NOT happen.

    This proves the consecutive-agreement gate is load-bearing: flip the
    predicate result and the promotion that the previous test asserted
    disappears."""
    thr = shared.promotion_streak_threshold()
    f = tmp_path / 'train.log'
    f.write_text('still running\n')          # predicate: grep DONE → exit 1 (False)
    client = _FakeStorageClient()
    import lib.scheduler.timer._poll as _poll
    monkeypatch.setattr(
        _poll, '_timer_client', lambda *, write=False: client, raising=True)
    _install_scripted_llm(monkeypatch, ready_value=True)   # LLM says ready
    monkeypatch.setattr(timer_mod, '_build_poll_tools', lambda cfg: None)

    row = _timer_row(condition_kind='hybrid', promotion_streak=thr - 1,
                     condition_command=f'grep -q DONE {f}',
                     check_instruction='done?')
    monkeypatch.setattr(
        timer_mod, '_get_timer_row', lambda tid, **_kwargs: row)

    ready, *_ = timer_mod.poll_timer('tmr_pred', user_id=1)
    assert ready is True                      # LLM still authoritative this poll
    updates = [payload for operation, payload, _ in client.commands
               if operation == 'timer.update']
    assert not any(payload.get('condition_kind') == 'code' for payload in updates), (
        'a disagreement at the threshold poll MUST NOT promote (gate is load-bearing)')
    assert any(payload.get('promotion_streak') == 0 for payload in updates), (
        'disagreement must reset the streak')


# ═══════════════════════════════════════════════════════════════════════════
#  Layer 3 — PROACTIVE consumer symmetry
# ═══════════════════════════════════════════════════════════════════════════

class TestProactiveReconcile:
    def _task(self, **over):
        t = {'id': 'task_abc123', 'user_id': 1,
             'condition_kind': 'hybrid',
             'condition_command': 'true', 'condition_regex': '',
             'promotion_streak': 0}
        t.update(over)
        return t

    def test_evaluate_condition_predicate(self):
        from lib.scheduler.proactive import evaluate_condition_predicate
        r = evaluate_condition_predicate(self._task(condition_command='true'))
        assert r.matched is True

    def test_apply_reconcile_promotes_and_persists(self, monkeypatch):
        import lib.scheduler.proactive as proactive
        thr = shared.promotion_streak_threshold()
        client = _FakeStorageClient()
        monkeypatch.setattr(
            proactive, '_scheduler_client',
            lambda *, write=False: client, raising=True)
        audits = []
        monkeypatch.setattr(proactive, 'audit_log',
                            lambda ev, **kw: audits.append((ev, kw)))

        task = self._task(promotion_streak=thr - 1)
        pred = PredicateResult(matched=True, exit_code=0)
        outcome = proactive.apply_reconcile_poll(task, pred, llm_ready=True,
                                                 llm_available=True)
        assert outcome.promoted is True
        updates = [payload for operation, payload, _ in client.commands
                   if operation == 'scheduler.task.update']
        assert any(payload.get('condition_kind') == 'code' for payload in updates)
        assert any(ev == 'proactive_predicate_promoted' for ev, _ in audits)

    def test_record_poll_persists_audit_columns(self, monkeypatch):
        import lib.scheduler.proactive as proactive
        client = _FakeStorageClient()
        monkeypatch.setattr(
            proactive, '_scheduler_client',
            lambda *, write=False: client, raising=True)
        proactive.record_poll('task_abc123', 'act', 'r', 'cheap', 0, 'snap',
                              tier='llm', predicate_matched=1, llm_agreed=1,
                              user_id=1)
        appends = [payload for operation, payload, _ in client.commands
                   if operation == 'scheduler.poll.append']
        assert len(appends) == 1
        assert appends[0]['tier'] == 'llm'
        assert appends[0]['predicate_matched'] == 1
        assert appends[0]['llm_agreed'] == 1


# ═══════════════════════════════════════════════════════════════════════════
#  Default LLM tier
# ═══════════════════════════════════════════════════════════════════════════

def test_llm_tier_unchanged_when_no_predicate(monkeypatch):
    """No predicate params selects the LLM tier; predicates never run."""
    def _boom(*a, **k):
        raise AssertionError('evaluate_predicate must not run on the llm tier')
    monkeypatch.setattr(timer_mod, '_build_poll_tools', lambda cfg: None)
    monkeypatch.setattr('lib.scheduler._shared.evaluate_predicate', _boom, raising=True)
    _install_scripted_llm(monkeypatch, ready_value=False, reason='waiting')

    row = _timer_row(condition_kind='llm', check_instruction='done?')
    monkeypatch.setattr(
        timer_mod, '_get_timer_row', lambda tid, **_kwargs: row)

    ready, reason, tokens, skipped, parse_error, *_ = timer_mod.poll_timer(
        'tmr_pred', user_id=1)
    assert ready is False
    assert parse_error is False


def test_derive_kind_used_by_create_timer_defaults_llm():
    """A bare timer (no predicate) derives condition_kind='llm'."""
    assert derive_condition_kind('is it done', '') == 'llm'


# ═══════════════════════════════════════════════════════════════════════════
#  Regression — sweep robustness (2026-07-22 bug-sweep)
# ═══════════════════════════════════════════════════════════════════════════

def test_malformed_once_schedule_does_not_wedge_sweep(monkeypatch):
    """A single row with a malformed ``once:`` timestamp must be skipped, NOT
    abort the whole due-task sweep (which would silently starve every other
    due task). Before the fix, ``datetime.fromisoformat`` on the bad row raised
    unguarded and escaped ``_check_and_run_due_tasks``."""
    from lib.scheduler.manager import ScheduledTaskManager

    mgr = ScheduledTaskManager.__new__(ScheduledTaskManager)
    from lib.identity import PrincipalContext
    mgr._process_principal = PrincipalContext.system(
        subject_id='scheduler-predicate-test',
        owner_user_id=1,
        scopes={'scheduler:run'},
    )
    rows = [
        {'id': 'bad', 'user_id': 1,
         'schedule': 'once:not-a-timestamp', 'run_count': 0,
         'task_type': 'command', 'last_run': None},
        {'id': 'good', 'user_id': 1,
         'schedule': 'once:2000-01-01T00:00:00', 'run_count': 0,
         'task_type': 'command', 'last_run': None},
    ]
    client = _FakeStorageClient(task_rows=rows)
    import lib.scheduler.manager as manager_module
    monkeypatch.setattr(
        manager_module, '_scheduler_client',
        lambda *, write=False: client, raising=True)
    ran = []
    mgr._run_and_record = lambda task: ran.append(task['id'])
    mgr.toggle_task = lambda tid, **_kwargs: None

    # Must not raise, and the 'good' due row must still run despite the bad one.
    mgr._check_and_run_due_tasks()
    assert 'good' in ran, 'the valid due task must still run after a malformed once: row'


def test_NEUTER_malformed_once_would_wedge_without_guard(monkeypatch):
    """LOAD-BEARING: prove the guard is what saves the sweep. Feed ONLY the
    malformed row and confirm the guarded sweep swallows it (no exception).
    Removing the try/except in manager.py makes datetime.fromisoformat raise
    here and this test goes red."""
    from lib.scheduler.manager import ScheduledTaskManager

    mgr = ScheduledTaskManager.__new__(ScheduledTaskManager)
    from lib.identity import PrincipalContext
    mgr._process_principal = PrincipalContext.system(
        subject_id='scheduler-predicate-test',
        owner_user_id=1,
        scopes={'scheduler:run'},
    )
    rows = [{'id': 'bad', 'user_id': 1,
             'schedule': 'once:garbage', 'run_count': 0,
             'task_type': 'command', 'last_run': None}]
    client = _FakeStorageClient(task_rows=rows)
    import lib.scheduler.manager as manager_module
    monkeypatch.setattr(
        manager_module, '_scheduler_client',
        lambda *, write=False: client, raising=True)
    mgr._run_and_record = lambda task: None
    mgr.toggle_task = lambda tid, **_kwargs: None

    mgr._check_and_run_due_tasks()  # must NOT raise


def test_ownerless_scheduler_runs_durable_rows_without_project_brain_work(
        monkeypatch):
    from lib.identity import PrincipalContext
    from lib.scheduler.manager import ScheduledTaskManager
    import lib.scheduler.manager as manager_module

    mgr = ScheduledTaskManager.__new__(ScheduledTaskManager)
    mgr._process_principal = PrincipalContext.system(
        subject_id='distributed-scheduler-test',
        scopes={'scheduler:run'},
    )
    client = _FakeStorageClient(task_rows=[{
        'id': 'owner-23-durable-task',
        'user_id': 23,
        'schedule': 'once:2000-01-01T00:00:00',
        'run_count': 0,
        'task_type': 'command',
        'last_run': None,
    }])
    monkeypatch.setattr(
        manager_module, '_scheduler_client',
        lambda *, write=False: client, raising=True)
    ran = []
    mgr._run_and_record = lambda task: ran.append(
        (task['id'], task['user_id']))
    mgr.toggle_task = lambda *_args, **_kwargs: None

    mgr._check_and_run_due_tasks()

    assert ran == [('owner-23-durable-task', 23)]


def test_poll_decision_survives_none_content(monkeypatch):
    """poll_decision must not crash inside its own parse-error handler when the
    LLM returns content=None. parse_json_decision raises AttributeError on
    None (caught), then the handler formats ``(content or '')[:100]`` — before
    the fix this was ``content[:100]`` → TypeError escaping poll_decision."""
    import lib.scheduler.proactive as proactive

    def _fake_smart_chat(messages, **kwargs):
        return None, {'total_tokens': 0}
    import lib.llm_dispatch as _ld
    monkeypatch.setattr(_ld, 'smart_chat', _fake_smart_chat, raising=True)
    monkeypatch.setattr(proactive, 'gather_system_status', lambda task: 'STATUS')

    task = {'id': 'task_none01', 'command': 'do it'}
    should_act, reason, tokens = proactive.poll_decision(task)
    assert should_act is False
    assert reason.startswith('Parse error:')


if __name__ == '__main__':
    pytest.main([__file__, '-v', '-s'])
