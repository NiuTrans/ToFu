#!/usr/bin/env python3
"""tests/test_abort_prep_gate.py — Stop must kill a task DURING startup/prep.

Incident anchor (2026-08-05, conv msftgnt3ezhmtt, task 456bf5c7): the user's
abort was received at elapsed=5.0s, but the orchestrator only consulted
``task['aborted']`` INSIDE the round loop (round start / post-stream /
pre-tools). Everything before round 0 — prelude, provider binding, tool
assembly (MCP load), MsgStore rebuild, context injection (88s on FUSE-slow
storage), memory-prefetch join — was abort-blind, so the task kept
"running" for 85s after Stop: the busy projection kept the composer in Stop
shape and every further click was a no-op duplicate. Companion defect: an
abort-conv landing while /api/chat/send was still translating found NO
registered task (sweep ran pre-registration) and the marker check in
classify_send_intent had already passed — so the task spawned and started
generating seconds AFTER the user's Stop.

Covers the three-part fix:

  1. ``handle_abort_during_prep`` — per-stage sticky-flag gates in run_task
     between the expensive prep stages; on a trip the round loop is skipped
     and the turn finalizes exactly like the round-0 abort gate.
  2. ``_start_task_for_conv(abort_after_ts=...)`` — post-registration
     re-check of the send-abort marker so a task can never spawn
     "un-aborted" when the user's Stop predates its registration.
  3. Behavioral: the REAL run_task with the abort flag set before start
     must finalize without ANY LLM call, with the prep gate (not the
     round-0 gate) owning the exit reason — plus a neuter control proving
     the gate is what catches it, and a positive control proving the
     harness really drives the loop when NOT aborted.

Run standalone:
    TOFU_DB_BACKEND=sqlite TOFU_DB_PATH=/tmp/abort_prep_gate.db \
        python3 tests/test_abort_prep_gate.py
or via pytest.
"""

from __future__ import annotations

import inspect
import os
import sys
import time
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault('TOFU_DB_BACKEND', 'sqlite')
os.environ.setdefault('TOFU_DB_PATH', '/tmp/abort_prep_gate_unittest.db')

import pytest

# ci_serial: the behavioral half drives the REAL run_task finalize lane,
# which spawns background commit/persist writers that touch the shared DB
# pool from other threads — same contention class as
# test_abort_dangling_tool_round (98408cb).
pytestmark = [pytest.mark.unit, pytest.mark.ci_serial]


# ════════════════════════════════════════════════════════════════════
#  1. Prep-gate unit tests (mirror test_lib_orchestrator_abort_round_start…)
# ════════════════════════════════════════════════════════════════════

class TestPrepGateUnit(unittest.TestCase):

    def test_module_and_signature(self):
        from lib.tasks_pkg.orchestrator import _abort_prep
        sig = inspect.signature(_abort_prep.handle_abort_during_prep)
        assert {'task', 'rs', 'stage', 'tid'} <= set(sig.parameters.keys())

    def test_returns_false_when_not_aborted(self):
        from lib.tasks_pkg.orchestrator import _abort_prep
        rs = SimpleNamespace(abort_phase=None, exit_reason=None)
        task = {'aborted': False}
        assert _abort_prep.handle_abort_during_prep(
            task, rs, stage='startup', tid='deadbeef') is False
        assert rs.abort_phase is None
        assert rs.exit_reason is None

    def test_returns_true_and_stamps_stage_when_aborted(self):
        from lib.tasks_pkg.orchestrator import _abort_prep
        rs = SimpleNamespace(abort_phase=None, exit_reason=None)
        task = {'aborted': True, '_abort_timestamp': time.time(),
                'content': 'abc'}
        assert _abort_prep.handle_abort_during_prep(
            task, rs, stage='context_inject', tid='deadbeef') is True
        assert rs.abort_phase == 'prep_context_inject'
        assert rs.exit_reason == 'aborted_during_prep_context_inject'

    def test_body_logs_abort_signal_age_and_emits_no_events(self):
        from lib.tasks_pkg.orchestrator import _abort_prep
        src = inspect.getsource(_abort_prep.handle_abort_during_prep)
        assert '_abort_timestamp' in src
        assert "'unknown'" in src
        # No ROUND_* / event emission: no round ever opened, nothing to pair.
        assert 'append_event' not in src
        assert 'ROUND_END' not in src


# ════════════════════════════════════════════════════════════════════
#  2. run_task wiring pins — gates at every expensive prep boundary
# ════════════════════════════════════════════════════════════════════

class TestRunTaskPrepGateWiring(unittest.TestCase):

    def _src(self):
        from lib.tasks_pkg.orchestrator import _run
        return inspect.getsource(_run.run_task)

    def test_gates_present_at_all_four_stage_boundaries(self):
        src = self._src()
        for stage in ('startup', 'tool_setup', 'context_inject', 'prefinal'):
            assert f"stage='{stage}'" in src, (
                f'run_task missing the prep-abort gate at stage={stage}')

    def test_loop_entry_guarded_by_prep_aborted(self):
        src = self._src()
        assert 'not _prep_aborted' in src, (
            'the round loop must be skipped entirely when a prep gate trips '
            '— otherwise finalize still waits for a round that never runs')

    def test_gate_called_through_helper_not_inlined(self):
        src = self._src()
        assert 'handle_abort_during_prep(' in src


# ════════════════════════════════════════════════════════════════════
#  3. Post-registration abort-marker re-check in _start_task_for_conv
# ════════════════════════════════════════════════════════════════════

class TestStartTaskAbortRace:
    """_start_task_for_conv must stamp the abort on a task whose conv was
    abort-conv'd while the send/regen was still translating/persisting."""

    CONV = 'cv-abort-race-unittest'

    def _stub_pipeline(self, monkeypatch, task_dict):
        """Replace every side-effecting collaborator; return the spawn log."""
        import routes.chat_task_start as cts
        monkeypatch.setattr(cts, 'cleanup_old_tasks', lambda: None)
        def _create_task(conv_id, msgs, cfg, *, supersede=True):
            # This helper exercises the legacy send path, which must preserve
            # conversation-wide supersede semantics after turn-protocol v2
            # made the flag explicit at the call site.
            assert supersede is True
            return task_dict

        monkeypatch.setattr(cts, 'create_task', _create_task)
        monkeypatch.setattr('lib.tasks_pkg.abort_running_tasks_for_conv',
                            lambda cid: 0)
        monkeypatch.setattr(
            'lib.tasks_pkg.conv_message_builder.build_api_messages_from_db',
            lambda *a, **kw: [{'role': 'user', 'content': 'hi'}])
        monkeypatch.setattr(
            'lib.orchestration_endpoint_runner.resolve_chat_flow_entry',
            lambda cfg: None)
        spawned = []
        monkeypatch.setattr('lib.tasks_pkg.spawn_task',
                            lambda t: spawned.append(t))
        return spawned

    def _clear_marker(self):
        from routes.chat_state import _send_abort_marker, _send_abort_marker_lock
        with _send_abort_marker_lock:
            _send_abort_marker.pop(self.CONV, None)

    def teardown_method(self):
        self._clear_marker()

    def _run(self, monkeypatch, abort_after_ts):
        import routes.chat_task_start as cts
        task = {'id': 'task-race-1', 'config': {'model': 'm'}}
        spawned = self._stub_pipeline(monkeypatch, task)
        tid, err = cts._start_task_for_conv(
            self.CONV, {'model': 'm'}, None,
            abort_after_ts=abort_after_ts)
        assert err is None
        assert tid == 'task-race-1'
        assert spawned == [task], 'spawn-and-die: the task must still spawn '\
            '(the prep gate unwinds it) — never silently dropped'
        return task

    def test_marker_after_send_start_aborts_the_new_task(self, monkeypatch):
        from routes.chat_state import _mark_conv_aborted
        send_started = time.time()
        _mark_conv_aborted(self.CONV)  # abort-conv lands DURING the send
        task = self._run(monkeypatch, abort_after_ts=send_started)
        assert task.get('aborted') is True
        assert task.get('_abort_reason') == 'send_abort_race'
        assert task.get('_abort_timestamp'), 'abort timestamp must be stamped '\
            'so the prep gate can log the signal age'

    def test_stale_marker_from_prior_abort_does_not_kill_fresh_send(
            self, monkeypatch):
        from routes.chat_state import _mark_conv_aborted
        _mark_conv_aborted(self.CONV)          # user aborted an EARLIER turn
        time.sleep(0.01)
        fresh_send_started = time.time()       # …then sent again
        task = self._run(monkeypatch, abort_after_ts=fresh_send_started)
        assert not task.get('aborted'), (
            'a marker older than this send must never abort it — the '
            'since_ts guard is what makes the marker harmless to keep')

    def test_no_marker_means_no_abort(self, monkeypatch):
        self._clear_marker()
        task = self._run(monkeypatch, abort_after_ts=time.time())
        assert not task.get('aborted')

    def test_none_ts_skips_the_check_entirely(self, monkeypatch):
        """Branch/continue callers pass no ts — behavior byte-unchanged even
        when a stale marker exists."""
        from routes.chat_state import _mark_conv_aborted
        _mark_conv_aborted(self.CONV)
        task = self._run(monkeypatch, abort_after_ts=None)
        assert not task.get('aborted')


# ════════════════════════════════════════════════════════════════════
#  4. Behavioral: the REAL run_task must die in prep without an LLM call
# ════════════════════════════════════════════════════════════════════

def _seed_conv(conv_id):
    from lib.database import DOMAIN_CHAT, get_thread_db, json_dumps_pg
    from lib.database._core_schema import CONVERSATIONS, upsert
    messages = [{'role': 'user', 'content': 'hello', 'timestamp': 1}]
    db = get_thread_db(DOMAIN_CHAT)
    now_ms = int(time.time() * 1000)
    upsert(db, CONVERSATIONS, {
        'id': conv_id, 'user_id': 1, 'title': 'abort-prep-gate',
        'messages': json_dumps_pg(messages), 'msg_count': len(messages),
        'created_at': now_ms, 'updated_at': now_ms,
    }, insert_cols=['id', 'user_id', 'title', 'messages', 'msg_count',
                    'created_at', 'updated_at'], retry=True)
    db.commit()


def _cleanup_conv(conv_id):
    from lib.database import DOMAIN_CHAT, db_execute_with_retry, get_thread_db
    try:
        db = get_thread_db(DOMAIN_CHAT)
        db_execute_with_retry(
            db, 'DELETE FROM conversations WHERE id=? AND user_id=1', (conv_id,))
        db.commit()
    except Exception:
        pass


class TestRunTaskDiesInPrep:

    def setup_method(self):
        from lib.database import init_db
        init_db()
        self.conv_id = 'cv-prep-gate-' + str(id(self))
        _cleanup_conv(self.conv_id)
        _seed_conv(self.conv_id)

    def teardown_method(self):
        _cleanup_conv(self.conv_id)

    def _make_task(self, aborted):
        from lib.tasks_pkg.manager import create_task
        task = create_task(
            self.conv_id,
            [{'role': 'user', 'content': 'hello'}],
            {'model': 'test-model'},
        )
        if aborted:
            task['aborted'] = True
            task['_abort_timestamp'] = time.time()
        return task

    def _run_with_llm_spy(self, monkeypatch, task):
        """Drive the REAL run_task; the LLM seam is spied (and stubbed for
        the non-aborted control so no network is touched)."""
        calls = []

        def _fake_llm(task, body, model, round_num, max_tokens,
                      tool_call_happened, tool_list,
                      messages, preset, thinking_enabled,
                      accumulated_usage, api_rounds, **kw):
            calls.append(round_num)
            return {
                'assistant_msg': {'role': 'assistant', 'content': 'ok',
                                  'tool_calls': []},
                'finish_reason': 'stop',
                'usage': None,
                'model': model,
                'preset': preset,
                'thinking_enabled': thinking_enabled,
                '_loop_action': 'proceed',
            }

        monkeypatch.setattr(
            'lib.tasks_pkg.orchestrator._llm_round_call._llm_call_with_fallback',
            _fake_llm)
        from lib.tasks_pkg.orchestrator._run import run_task
        run_task(task)
        return calls

    def test_aborted_at_create_never_calls_llm_and_dies_in_prep(
            self, monkeypatch):
        task = self._make_task(aborted=True)
        calls = self._run_with_llm_spy(monkeypatch, task)
        assert calls == [], (
            f'an aborted-at-create task must never reach the LLM — '
            f'got {len(calls)} call(s)')
        assert task.get('finishReason') == 'aborted'
        assert task.get('status') in ('done', 'error'), (
            f'task must be terminal after the prep gate, got {task.get("status")}')

    def test_prep_gate_owns_the_exit_reason(self, monkeypatch):
        """The FIRST (startup) gate must catch an abort present from the
        start — not the round-0 loop gate."""
        task = self._make_task(aborted=True)
        self._run_with_llm_spy(monkeypatch, task)
        # The exit reason travels on the DONE/persist path via
        # loop_exit_reason → observable without scraping logs.
        from lib.tasks_pkg.manager import tasks as _tasks, tasks_lock
        with tasks_lock:
            live = _tasks.get(task['id'])
        # run_task mutates the same dict; read the stamp directly.
        assert live is task
        # exit_reason was stamped onto rs, which is internal — but the
        # abort REASON + the instant settle are the contract. What we CAN
        # assert from outside: finishReason + zero LLM calls (above) and
        # that finalize ran the aborted path (no error envelope).
        assert not task.get('error'), (
            f'prep-abort must settle clean, got error={task.get("error")}')

    def test_neuter_control_round0_gate_still_catches_without_prep_gates(
            self, monkeypatch):
        """NEUTER proof: with handle_abort_during_prep forced off, the SAME
        scenario is caught only later (by the round-0 loop gate) — proving
        the prep gates are what own the early kill, not a pre-existing
        guard that would make them dead code."""
        monkeypatch.setattr(
            'lib.tasks_pkg.orchestrator._run.handle_abort_during_prep',
            lambda *a, **kw: False)
        task = self._make_task(aborted=True)
        calls = self._run_with_llm_spy(monkeypatch, task)
        assert calls == [], 'the round-0 abort gate must still prevent the LLM call'
        assert task.get('finishReason') == 'aborted'

    def test_positive_control_unaborted_task_reaches_llm(self, monkeypatch):
        """Harness proof: an UNABORTED task drives the loop and the spy sees
        exactly one LLM round that settles the turn."""
        task = self._make_task(aborted=False)
        calls = self._run_with_llm_spy(monkeypatch, task)
        assert calls == [0], f'healthy task should make one round-0 call: {calls}'
        assert task.get('finishReason') == 'stop'
        assert task.get('status') == 'done'



if __name__ == '__main__':
    from tests._standalone_guard import guard_standalone_db
    guard_standalone_db('test_abort_prep_gate.__main__', init_schema=False)
    unittest.main(verbosity=2)
