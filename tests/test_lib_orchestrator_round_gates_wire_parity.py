"""Slice 17 wire-parity: _round_gates.py extraction from _run.py.

Failing-first: these tests FAIL before the extraction module exists
(ImportError / attribute-missing / body-mismatch) and PASS after.
"""

import inspect
import pytest

import lib.tasks_pkg.orchestrator._round_gates as _round_gates
pytestmark = pytest.mark.unit


class TestRoundGatesWireParity:
    """Wire-parity guards for _round_gates.check_round_gates."""

    def test_module_exists(self):
        assert _round_gates is not None

    def test_check_round_gates_callable(self):
        assert callable(_round_gates.check_round_gates)

    def test_signature_accepts_required_kwargs(self):
        sig = inspect.signature(_round_gates.check_round_gates)
        params = set(sig.parameters.keys())
        assert {'task', 'rs', 'round_num', 'tid', 'cfg'} <= params
        assert 'max_tool_rounds' not in params

    def test_returns_bool(self):
        sig = inspect.signature(_round_gates.check_round_gates)
        assert sig.return_annotation is bool or sig.return_annotation == 'bool'

    def test_budget_gate_emits_round_end_with_reason_budget(self):
        """On budget exceed, the helper must emit ROUND_END(reason='budget')."""
        src = inspect.getsource(_round_gates.check_round_gates)
        assert "reason='budget'" in src
        assert "EventType.ROUND_END" in src

    def test_budget_gate_sets_error_envelope(self):
        """Budget gate must stamp task['error'] with budget_exceeded."""
        src = inspect.getsource(_round_gates.check_round_gates)
        assert "task['error']" in src
        assert "budget_exceeded" in src

    def test_tool_round_gate_is_absent(self):
        src = inspect.getsource(_round_gates.check_round_gates)
        assert 'max_tool_rounds' not in src
        assert 'tool_rounds_exhausted' not in src

    def test_per_round_diagnostic_logged(self):
        """The per-round INFO diagnostic must be present."""
        src = inspect.getsource(_round_gates.check_round_gates)
        assert "proceeding to tool execution" in src
        assert "logger.info" in src

    def test_returns_false_when_no_gate_fires(self):
        """Helper returns False when both gates pass."""
        src = inspect.getsource(_round_gates.check_round_gates)
        assert "return False" in src

    def test_returns_true_when_budget_fires(self):
        """Helper returns True when budget gate fires."""
        src = inspect.getsource(_round_gates.check_round_gates)
        assert "return True" in src

    def test_run_task_delegates_to_helper(self):
        """The root adapter calls the helper; the runner owns control flow."""
        import lib.tasks_pkg.orchestrator._root_agent_loop as _root_agent_loop
        src = inspect.getsource(_root_agent_loop)
        assert "check_round_gates(" in src

    def test_run_task_no_longer_carries_gates_inline(self):
        """The inline gate bodies must be gone from run_task."""
        import lib.tasks_pkg.orchestrator._run as _run
        src = inspect.getsource(_run.run_task)
        assert "budget_exceeded_round_" not in src
        assert "tool_rounds_exhausted_" not in src
        assert "proceeding to tool execution" not in src
