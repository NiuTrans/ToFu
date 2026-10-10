"""Slice 20 wire-parity: _round_checkpoint.py extraction from _run.py."""

import inspect
from types import SimpleNamespace

import pytest

import lib.tasks_pkg.orchestrator._round_checkpoint as _round_checkpoint
pytestmark = pytest.mark.unit


class TestRoundCheckpointWireParity:
    def test_module_exists(self):
        assert _round_checkpoint is not None

    def test_helper_callable(self):
        assert callable(_round_checkpoint.run_round_checkpoint_and_close)

    def test_signature_accepts_required_kwargs(self):
        sig = inspect.signature(
            _round_checkpoint.run_round_checkpoint_and_close)
        params = set(sig.parameters.keys())
        assert {'task', 'rs', 'round_num', 'tid'} <= params

    def test_sampled_round_checkpoint_is_throttled_and_does_not_require_a_durability_fence(self, monkeypatch):
        calls = []
        monkeypatch.setattr(_round_checkpoint.time, 'time', lambda: 20)
        monkeypatch.setattr(_round_checkpoint, 'checkpoint_task_partial',
            lambda task, **kw: calls.append(('checkpoint', kw)))
        monkeypatch.setattr(_round_checkpoint, 'append_event',
            lambda task, event: calls.append(('event', event)))
        state = SimpleNamespace(last_checkpoint_ts=0)
        _round_checkpoint.run_round_checkpoint_and_close({}, state, round_num=2, tid='test')
        assert calls[0] == ('checkpoint', {'force': True, 'wait_for_durability': False})
        assert calls[1][1]['type'] == 'round_end'
        assert state.last_checkpoint_ts == 20
        calls.clear()
        _round_checkpoint.run_round_checkpoint_and_close({}, state, round_num=3, tid='test')
        assert len(calls) == 1 and calls[0][0] == 'event'

    def test_checkpoint_failure_is_non_fatal(self):
        src = inspect.getsource(
            _round_checkpoint.run_round_checkpoint_and_close)
        assert "except Exception" in src
        assert "non-fatal" in src

    def test_body_emits_round_end_reason_tools(self):
        src = inspect.getsource(
            _round_checkpoint.run_round_checkpoint_and_close)
        assert "reason='tools'" in src
        assert "EventType.ROUND_END" in src

    def test_step_ordering_checkpoint_before_round_end(self):
        src = inspect.getsource(
            _round_checkpoint.run_round_checkpoint_and_close)
        i_checkpoint = src.index("checkpoint_task_partial")
        i_round_end = src.index("EventType.ROUND_END")
        assert i_checkpoint < i_round_end

class TestRunTaskDelegation:
    def test_run_task_delegates_to_helper(self):
        import lib.tasks_pkg.orchestrator._root_agent_loop as _root_agent_loop
        src = inspect.getsource(_root_agent_loop)
        assert "run_round_checkpoint_and_close(" in src

    def test_run_task_no_longer_carries_block_inline(self):
        import lib.tasks_pkg.orchestrator._run as _run
        src = inspect.getsource(_run.run_task)
        assert "checkpoint_task_partial(task, force=True)" not in src
        assert "roundNum=round_num, reason='tools')" not in src
