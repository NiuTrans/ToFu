# Incident anchor: born in commit c9b47ecf — refactor(orchestrator): pt_03f4cdf1 slice 25 — extract post-stream an...
# (funeral audit pt_c565a36b3e8f42e6, docs/RATCHET_AUDIT.md)
"""Slice 25 wire-parity: _stream_decision.py extraction from _run.py."""

import inspect

from lib.tasks_pkg.orchestrator import _stream_decision


class TestStreamDecisionWireParity:
    def test_module_exists(self):
        assert _stream_decision is not None

    def test_helper_callable(self):
        assert callable(_stream_decision.apply_stream_decision)

    def test_signature_accepts_required_kwargs(self):
        sig = inspect.signature(_stream_decision.apply_stream_decision)
        params = set(sig.parameters.keys())
        assert {'task', 'rs', 'round_num', 'tid', 'premature_retry_count',
                'messages'} <= params

    def test_body_calls_analyse_stream_result(self):
        src = inspect.getsource(_stream_decision.apply_stream_decision)
        assert "analyse_stream_result(" in src
        assert "usage=rs.last_usage" in src

    def test_body_applies_finish_reason_and_abort_phase(self):
        src = inspect.getsource(_stream_decision.apply_stream_decision)
        assert "rs.last_finish_reason = stream_decision['last_finish_reason']" in src
        assert "rs.abort_phase = stream_decision['abort_detected_phase']" in src

    def test_body_stamps_exit_reason_only_on_break(self):
        src = inspect.getsource(_stream_decision.apply_stream_decision)
        assert "rs.exit_reason = stream_decision['loop_exit_reason']" in src
        assert "'break'" in src

    def test_body_returns_three_actions(self):
        src = inspect.getsource(_stream_decision.apply_stream_decision)
        assert "return 'break', new_count" in src
        assert "return 'continue', new_count" in src
        assert "return 'proceed', new_count" in src

    def test_body_returns_updated_retry_count(self):
        src = inspect.getsource(_stream_decision.apply_stream_decision)
        assert "stream_decision['premature_retry_count']" in src

    def test_docstring_mentions_extraction(self):
        assert "pt_03f4cdf1" in (_stream_decision.__doc__ or "")


class TestRunTaskDelegation:
    def test_run_task_delegates_to_helper(self):
        from lib.tasks_pkg.orchestrator import _run
        src = inspect.getsource(_run.run_task)
        assert "apply_stream_decision(" in src

    def test_run_task_no_longer_carries_block_inline(self):
        from lib.tasks_pkg.orchestrator import _run
        src = inspect.getsource(_run.run_task)
        assert "analyse_stream_result(" not in src
        assert "stream_decision[" not in src

    def test_run_task_handles_break_and_continue(self):
        """The caller must still map the returned action to loop control."""
        from lib.tasks_pkg.orchestrator import _run
        src = inspect.getsource(_run.run_task)
        assert "_stream_action ==" in src
