# Incident anchor: born in commit 51fe025b — refactor(orchestrator): pt_03f4cdf1 slice 19 — extract abort-before-t...
# (funeral audit pt_c565a36b3e8f42e6, docs/RATCHET_AUDIT.md)
"""Slice 19 wire-parity: _abort_before_tools.py extraction from _run.py."""

import inspect
from types import SimpleNamespace

from lib.tasks_pkg.orchestrator import _abort_before_tools


class TestAbortBeforeToolsWireParity:
    def test_module_exists(self):
        assert _abort_before_tools is not None

    def test_helper_callable(self):
        assert callable(_abort_before_tools.handle_abort_before_tools)

    def test_signature_accepts_required_kwargs(self):
        sig = inspect.signature(_abort_before_tools.handle_abort_before_tools)
        params = set(sig.parameters.keys())
        assert {'task', 'rs', 'messages', 'round_num', 'tid'} <= params

    def test_returns_false_when_not_aborted(self):
        rs = SimpleNamespace(abort_phase=None, exit_reason=None)
        task = {'aborted': False}
        msgs = [{'role': 'assistant', 'content': 'x'}]
        assert _abort_before_tools.handle_abort_before_tools(
            task, rs, msgs, round_num=0, tid='deadbeef') is False
        # Untouched state
        assert rs.abort_phase is None
        assert rs.exit_reason is None
        assert len(msgs) == 1

    def test_body_pops_trailing_tool_calls_message(self):
        src = inspect.getsource(_abort_before_tools.handle_abort_before_tools)
        assert "messages.pop()" in src
        assert "orphaned tool_use" in src

    def test_body_reappends_prose_content(self):
        src = inspect.getsource(_abort_before_tools.handle_abort_before_tools)
        assert "{'role': 'assistant', 'content': _popped['content']}" in src

    def test_body_emits_round_end_reason_aborted(self):
        src = inspect.getsource(_abort_before_tools.handle_abort_before_tools)
        assert "reason='aborted'" in src
        assert "EventType.ROUND_END" in src

    def test_body_stamps_abort_phase_and_exit_reason(self):
        src = inspect.getsource(_abort_before_tools.handle_abort_before_tools)
        assert "before_tool_exec_round_" in src
        assert "aborted_before_tools_round_" in src

    def test_docstring_mentions_extraction(self):
        assert "pt_03f4cdf1" in (_abort_before_tools.__doc__ or "")


class TestRunTaskDelegation:
    def test_run_task_delegates_to_helper(self):
        from lib.tasks_pkg.orchestrator import _run
        src = inspect.getsource(_run.run_task)
        assert "handle_abort_before_tools(" in src

    def test_run_task_no_longer_carries_block_inline(self):
        from lib.tasks_pkg.orchestrator import _run
        src = inspect.getsource(_run.run_task)
        assert "aborted_before_tools_round_" not in src
        assert "Removed trailing tool_calls message" not in src
