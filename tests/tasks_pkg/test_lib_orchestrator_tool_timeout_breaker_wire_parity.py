"""Slice 21 wire-parity: _tool_timeout_breaker.py extraction from _run.py."""

import inspect
from types import SimpleNamespace

import pytest

import lib.tasks_pkg.orchestrator._tool_timeout_breaker as _tool_timeout_breaker
pytestmark = pytest.mark.unit


class TestToolTimeoutBreakerWireParity:
    def test_module_exists(self):
        assert _tool_timeout_breaker is not None

    def test_helper_callable(self):
        assert callable(_tool_timeout_breaker.handle_tool_timeout_circuit_breaker)

    def test_signature_accepts_required_kwargs(self):
        sig = inspect.signature(
            _tool_timeout_breaker.handle_tool_timeout_circuit_breaker)
        params = set(sig.parameters.keys())
        assert {'task', 'rs', 'round_num', 'tid', 'tool_timed_out',
                'max_consecutive_tool_timeouts'} <= params

    def test_success_resets_counter_and_returns_false(self):
        rs = SimpleNamespace(consecutive_tool_timeouts=2, exit_reason=None,
                             model='m')
        task = {'convId': 'c'}
        assert _tool_timeout_breaker.handle_tool_timeout_circuit_breaker(
            task, rs, round_num=0, tid='deadbeef',
            tool_timed_out=False, max_consecutive_tool_timeouts=3) is False
        assert rs.consecutive_tool_timeouts == 0

    def test_timeout_increments_counter(self):
        rs = SimpleNamespace(consecutive_tool_timeouts=0, exit_reason=None,
                             model='m')
        task = {'convId': 'c'}
        assert _tool_timeout_breaker.handle_tool_timeout_circuit_breaker(
            task, rs, round_num=0, tid='deadbeef',
            tool_timed_out=True, max_consecutive_tool_timeouts=3) is False
        assert rs.consecutive_tool_timeouts == 1

    def test_body_force_stops_at_ceiling(self):
        src = inspect.getsource(
            _tool_timeout_breaker.handle_tool_timeout_circuit_breaker)
        assert "FORCE STOP" in src
        assert "tool_timeout" in src
        assert "context='tool-loop'" in src

    def test_body_emits_round_end_reason_tool_timeout(self):
        src = inspect.getsource(
            _tool_timeout_breaker.handle_tool_timeout_circuit_breaker)
        assert "reason='tool_timeout'" in src
        assert "EventType.ROUND_END" in src

    def test_body_sets_exit_reason_format(self):
        src = inspect.getsource(
            _tool_timeout_breaker.handle_tool_timeout_circuit_breaker)
        assert "consecutive_tool_timeouts_" in src

class TestRunTaskDelegation:
    def test_run_task_delegates_to_helper(self):
        import lib.tasks_pkg.orchestrator._root_agent_loop as _root_agent_loop
        src = inspect.getsource(_root_agent_loop)
        assert "handle_tool_timeout_circuit_breaker(" in src

    def test_run_task_no_longer_carries_block_inline(self):
        import lib.tasks_pkg.orchestrator._run as _run
        src = inspect.getsource(_run.run_task)
        assert "consecutive_tool_timeouts_" not in src
        assert "FORCE STOP" not in src
