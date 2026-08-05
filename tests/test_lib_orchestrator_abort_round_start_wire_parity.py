# Incident anchor: born in commit d219d8ea — refactor(orchestrator): pt_03f4cdf1 slice 23 — extract abort-at-round...
# (funeral audit pt_c565a36b3e8f42e6, docs/RATCHET_AUDIT.md)
"""Slice 23 wire-parity: _abort_round_start.py extraction from _run.py."""

import inspect
from types import SimpleNamespace

from lib.tasks_pkg.orchestrator import _abort_round_start


class TestAbortRoundStartWireParity:
    def test_module_exists(self):
        assert _abort_round_start is not None

    def test_helper_callable(self):
        assert callable(_abort_round_start.handle_abort_at_round_start)

    def test_signature_accepts_required_kwargs(self):
        sig = inspect.signature(_abort_round_start.handle_abort_at_round_start)
        params = set(sig.parameters.keys())
        assert {'task', 'rs', 'round_num', 'tid'} <= params

    def test_returns_false_when_not_aborted(self):
        rs = SimpleNamespace(abort_phase=None, exit_reason=None, model='m')
        task = {'aborted': False}
        assert _abort_round_start.handle_abort_at_round_start(
            task, rs, round_num=0, tid='deadbeef') is False
        assert rs.abort_phase is None
        assert rs.exit_reason is None

    def test_returns_true_and_stamps_state_when_aborted(self):
        rs = SimpleNamespace(abort_phase=None, exit_reason=None, model='m')
        task = {'aborted': True, '_abort_timestamp': 0, 'content': 'abc'}
        assert _abort_round_start.handle_abort_at_round_start(
            task, rs, round_num=2, tid='deadbeef') is True
        assert rs.abort_phase == 'loop_start_round_2'
        assert rs.exit_reason == 'aborted_at_round_2'

    def test_body_logs_abort_signal_age(self):
        src = inspect.getsource(_abort_round_start.handle_abort_at_round_start)
        assert "_abort_timestamp" in src
        assert "'unknown'" in src
        assert "content so far" in src

    def test_body_does_not_emit_round_end(self):
        """No ROUND_END here — the round never opened, nothing to pair."""
        src = inspect.getsource(_abort_round_start.handle_abort_at_round_start)
        assert "append_event" not in src
        assert "ROUND_END" not in src

    def test_docstring_mentions_extraction(self):
        assert "pt_03f4cdf1" in (_abort_round_start.__doc__ or "")


class TestRunTaskDelegation:
    def test_run_task_delegates_to_helper(self):
        from lib.tasks_pkg.orchestrator import _run
        src = inspect.getsource(_run.run_task)
        assert "handle_abort_at_round_start(" in src

    def test_run_task_no_longer_carries_block_inline(self):
        from lib.tasks_pkg.orchestrator import _run
        src = inspect.getsource(_run.run_task)
        assert "aborted_at_round_" not in src
        assert "loop_start_round_" not in src
