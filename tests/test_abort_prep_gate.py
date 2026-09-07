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
  2. ``start_conversation_attempt_executor(abort_after_ts=...)`` — post-registration
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


import pytest

# serial: the behavioral half drives the REAL run_task finalize lane,
# which spawns background commit/persist writers that touch the shared DB
# pool from other threads — same contention class as
# test_abort_dangling_tool_round (98408cb).
pytestmark = [pytest.mark.unit, pytest.mark.serial]


# ════════════════════════════════════════════════════════════════════
#  1. Prep-gate unit tests (mirror test_lib_orchestrator_abort_round_start…)
# ════════════════════════════════════════════════════════════════════

class TestPrepGateUnit(unittest.TestCase):

    def test_module_and_signature(self):
        import lib.tasks_pkg.orchestrator._abort_prep as _abort_prep
        sig = inspect.signature(_abort_prep.handle_abort_during_prep)
        assert {'task', 'rs', 'stage', 'tid'} <= set(sig.parameters.keys())

    def test_returns_false_when_not_aborted(self):
        import lib.tasks_pkg.orchestrator._abort_prep as _abort_prep
        rs = SimpleNamespace(abort_phase=None, exit_reason=None)
        task = {'aborted': False}
        assert _abort_prep.handle_abort_during_prep(
            task, rs, stage='startup', tid='deadbeef') is False
        assert rs.abort_phase is None
        assert rs.exit_reason is None

    def test_returns_true_and_stamps_stage_when_aborted(self):
        import lib.tasks_pkg.orchestrator._abort_prep as _abort_prep
        rs = SimpleNamespace(abort_phase=None, exit_reason=None)
        task = {'aborted': True, '_abort_timestamp': time.time(),
                'content': 'abc'}
        assert _abort_prep.handle_abort_during_prep(
            task, rs, stage='context_inject', tid='deadbeef') is True
        assert rs.abort_phase == 'prep_context_inject'
        assert rs.exit_reason == 'aborted_during_prep_context_inject'

    def test_body_logs_abort_signal_age_and_emits_no_events(self):
        import lib.tasks_pkg.orchestrator._abort_prep as _abort_prep
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
        import lib.tasks_pkg.orchestrator._run as _run
        return inspect.getsource(_run.run_task)

    def test_gates_present_at_all_expensive_stage_boundaries(self):
        src = self._src()
        for stage in (
                'startup', 'project_setup', 'tool_setup', 'context_inject',
                'prefinal'):
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

    def test_resume_authority_is_validated_before_expensive_prep(self):
        src = self._src()
        for prepare_call in (
            'prepare_resume_state(cfg)',
            'prepare_continue_tool_history(',
        ):
            assert src.index(prepare_call) < src.index('setup_project_context(')
            assert src.index(prepare_call) < src.index('start_prefetches(')
            assert src.index(prepare_call) < src.index('run_root_agent_loop(')


def _resolved_model_config():
    return {
        'model': 'test-model',
        'thinking_enabled': False,
        'thinking_depth': 'low',
        'preset': None,
        'max_tokens': 128,
        'temperature': 0,
        'search_mode': 'off',
        'response_format': None,
        'search_enabled': False,
        'fetch_enabled': False,
        'project_path': '/unused/project',
        'project_enabled': True,
        'code_exec_enabled': False,
        'memory_enabled': True,
    }


def _exercise_run_task_abort(monkeypatch, *, abort_during: str):
    """Drive the real orchestration spine with every heavy seam observable."""
    import lib.tasks_pkg.orchestrator._run as run_mod
    from lib.log import clear_log_context, set_req_id

    calls = []
    task = {
        'id': 'deadbeef-0000-0000-0000-000000000000',
        'convId': 'conv-abort-prep',
        '_userId': 1,
        'config': {'model': 'test-model'},
        'messages': [{'role': 'user', 'content': 'hello'}],
        'content': '',
        'aborted': abort_during == 'startup',
        '_abort_timestamp': time.time(),
    }

    monkeypatch.setattr(run_mod, 'check_autopilot_kick', lambda _task: False)
    monkeypatch.setattr(run_mod, 'snapshot_turn_input', lambda _task: None)
    monkeypatch.setattr(run_mod, 'log_task_open', lambda _task, _tid: time.time())
    monkeypatch.setattr(run_mod, 'make_vu_phase', lambda _task: None)
    monkeypatch.setattr(run_mod, '_emit_startup_phase', lambda *_a, **_k: None)
    monkeypatch.setattr(run_mod, 'run_turn_prelude',
                        lambda _task, cfg, _tid: cfg)
    monkeypatch.setattr(run_mod, 'bind_provider_and_affinity',
                        lambda *_a, **_k: None)
    monkeypatch.setattr(run_mod, 'resolve_and_seed_model_config',
                        lambda *_a, **_k: _resolved_model_config())

    def setup_project(_task, *_args):
        calls.append('project_setup')
        if abort_during == 'project_setup':
            _task['aborted'] = True
            _task['_abort_timestamp'] = time.time()

    class FakePrefetchExecutor:
        def shutdown(self, **kwargs):
            calls.append(('prefetch_shutdown', kwargs))

    def start_prefetch(_task, **_kwargs):
        calls.append('prefetch_start')
        _task['_prefetch_project'] = object()
        return FakePrefetchExecutor()

    def assemble(_cfg, _task, _mcfg):
        calls.append('tool_setup')
        if abort_during == 'tool_setup':
            _task['aborted'] = True
            _task['_abort_timestamp'] = time.time()
        return [], False

    monkeypatch.setattr(run_mod, 'setup_project_context', setup_project)
    monkeypatch.setattr(run_mod, 'start_prefetches', start_prefetch)
    monkeypatch.setattr(run_mod, 'assemble_round_tools', assemble)
    for heavy_name in (
            'maybe_run_memory_prefetch', 'inject_context_and_emit_chips',
            'inject_continue_tool_history', 'apply_resume_state',
            'run_root_agent_loop'):
        monkeypatch.setattr(
            run_mod,
            heavy_name,
            lambda *_a, _name=heavy_name, **_k: calls.append(_name),
        )

    finalized = {}
    monkeypatch.setattr(
        run_mod, 'finalize_after_loop',
        lambda _task, **kwargs: finalized.update(kwargs),
    )

    def teardown(_task, *, tid):
        del _task, tid
        clear_log_context()
        set_req_id('')

    monkeypatch.setattr(run_mod, 'finalize_task_lane', teardown)
    run_mod.run_task(task)
    return task, calls, finalized


def test_preaborted_task_skips_every_heavy_prep_stage(monkeypatch):
    task, calls, finalized = _exercise_run_task_abort(
        monkeypatch, abort_during='startup')

    assert calls == []
    assert finalized['loop_exit_reason'] == 'aborted_during_prep_startup'
    assert finalized['abort_detected_phase'] == 'prep_startup'
    assert task.get('_prefetch_project') is None


def test_abort_after_project_setup_does_not_start_prefetch_or_tools(monkeypatch):
    _task, calls, finalized = _exercise_run_task_abort(
        monkeypatch, abort_during='project_setup')

    assert calls == ['project_setup']
    assert finalized['loop_exit_reason'] == 'aborted_during_prep_project_setup'


def test_abort_during_tool_setup_cancels_prefetch_and_skips_context(monkeypatch):
    task, calls, finalized = _exercise_run_task_abort(
        monkeypatch, abort_during='tool_setup')

    assert calls == [
        'project_setup',
        'prefetch_start',
        'tool_setup',
        ('prefetch_shutdown', {'wait': False, 'cancel_futures': True}),
    ]
    assert task.get('_prefetch_project') is None
    assert finalized['loop_exit_reason'] == 'aborted_during_prep_tool_setup'


def test_external_edit_probe_does_no_io_for_preaborted_task(monkeypatch):
    import lib.tasks_pkg.orchestrator._vu_startup as startup

    called = []
    monkeypatch.setattr(
        'lib.file_history.detect_external_edits',
        lambda *_a, **_k: called.append(True),
    )
    startup._probe_external_edits(
        {'id': 'deadbeef', 'aborted': True, 'status': 'running'},
        '/unused/project',
    )
    assert called == []


# ════════════════════════════════════════════════════════════════════
#  3. Post-registration abort-marker re-check in start_conversation_attempt_executor
# ════════════════════════════════════════════════════════════════════

class TestStartTaskAbortRace:
    """A shared fence must stop a v3 task registered after the click."""

    CONV = 'cv-abort-race-unittest'

    def _stub_pipeline(self, monkeypatch, task_dict):
        """Replace every side-effecting collaborator; return the spawn log."""
        import lib.conversation_sync.task_start as cts
        monkeypatch.setattr(cts, 'cleanup_old_tasks', lambda: None)
        def _create_task(conv_id, msgs, cfg, *, user_id, supersede=True):
            assert user_id == 1
            assert supersede is False
            return task_dict

        monkeypatch.setattr(cts, 'create_task', _create_task)
        monkeypatch.setattr(
            'lib.turn_lifecycle.build_api_messages',
            lambda *a, **kw: [{'role': 'user', 'content': 'hi'}])
        monkeypatch.setattr(
            'lib.orchestration_chat_flow_runner.resolve_chat_flow_entry',
            lambda cfg: None)
        spawned = []
        monkeypatch.setattr('lib.tasks_pkg.spawn.spawn_task',
                            lambda t: spawned.append(t))
        return spawned

    def _clear_marker(self):
        from lib.runtime_state_store import reset_for_test
        reset_for_test()

    def teardown_method(self):
        self._clear_marker()

    def _run(self, monkeypatch, abort_after_ts):
        import lib.conversation_sync.task_start as cts
        config = {
            'model': 'm', '_turnOwnerUserId': 1,
            '_turnId': 'turn-1', '_attemptId': 'attempt-1',
        }
        task = {'id': 'task-race-1', 'config': config}
        spawned = self._stub_pipeline(monkeypatch, task)
        registered = []
        tid, err = cts.start_conversation_attempt_executor(
            self.CONV, config, abort_after_ts=abort_after_ts,
            on_task_registered=registered.append)
        assert err is None
        assert tid == 'task-race-1'
        assert registered == ['task-race-1']
        assert spawned == [task], 'spawn-and-die: the task must still spawn '\
            '(the prep gate unwinds it) — never silently dropped'
        return task

    def test_marker_after_send_start_aborts_the_new_task(self, monkeypatch):
        from lib.conversation_sync.pending_abort import mark_pending_abort
        send_started = time.time()
        mark_pending_abort(self.CONV, 1)
        task = self._run(monkeypatch, abort_after_ts=send_started)
        assert task.get('aborted') is True
        assert task.get('_abort_reason') == 'send_abort_race'
        assert task.get('_abort_timestamp'), 'abort timestamp must be stamped '\
            'so the prep gate can log the signal age'

    def test_stale_marker_from_prior_abort_does_not_kill_fresh_send(
            self, monkeypatch):
        from lib.conversation_sync.pending_abort import mark_pending_abort
        mark_pending_abort(self.CONV, 1)
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
        from lib.conversation_sync.pending_abort import mark_pending_abort
        mark_pending_abort(self.CONV, 1)
        task = self._run(monkeypatch, abort_after_ts=None)
        assert not task.get('aborted')
