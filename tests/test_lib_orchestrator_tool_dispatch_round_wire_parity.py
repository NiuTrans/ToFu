# Incident anchor: born in commit 63c1e4a6 — refactor(orchestrator): pt_03f4cdf1 slice 22 — extract per-round tool...
# (funeral audit pt_c565a36b3e8f42e6, docs/RATCHET_AUDIT.md)
"""Slice 22 wire-parity: _tool_dispatch_round.py extraction from _run.py."""

import inspect

from lib.tasks_pkg.orchestrator import _tool_dispatch_round


class TestToolDispatchRoundWireParity:
    def test_module_exists(self):
        assert _tool_dispatch_round is not None

    def test_helper_callable(self):
        assert callable(_tool_dispatch_round.run_tool_dispatch)

    def test_signature_accepts_required_kwargs(self):
        sig = inspect.signature(_tool_dispatch_round.run_tool_dispatch)
        params = set(sig.parameters.keys())
        assert {'task', 'rs', 'messages', 'all_search_results_text',
                'round_num', 'tid', 'cfg', 'project_path',
                'project_enabled', 'tool_list', 'announced_tc_map'} <= params

    def test_body_parses_with_early_announced(self):
        src = inspect.getsource(_tool_dispatch_round.run_tool_dispatch)
        assert "parse_tool_calls(" in src
        assert "early_announced=announced_tc_map" in src

    def test_body_sanitizes_malformed_args(self):
        src = inspect.getsource(_tool_dispatch_round.run_tool_dispatch)
        assert "sanitize_malformed_tool_call_args(" in src

    def test_body_emits_exec_phase(self):
        src = inspect.getsource(_tool_dispatch_round.run_tool_dispatch)
        assert "emit_tool_exec_phase(task, parsed_tcs)" in src

    def test_body_refreshes_reaper_heartbeat(self):
        src = inspect.getsource(_tool_dispatch_round.run_tool_dispatch)
        assert "task['_dispatch_heartbeat'] = time.time()" in src

    def test_body_executes_pipeline_and_returns_timeout_flag(self):
        src = inspect.getsource(_tool_dispatch_round.run_tool_dispatch)
        assert "execute_tool_pipeline(" in src
        assert "return _tool_timed_out" in src

    def test_body_pops_compact_messages_ref(self):
        src = inspect.getsource(_tool_dispatch_round.run_tool_dispatch)
        assert "task.pop('_compact_messages', None)" in src

    def test_phase_ordering(self):
        """Parse → sanitize → emit → execute (byte-order parity)."""
        src = inspect.getsource(_tool_dispatch_round.run_tool_dispatch)
        i_parse = src.index("parse_tool_calls(")
        i_sanitize = src.index("sanitize_malformed_tool_call_args(")
        i_emit = src.index("emit_tool_exec_phase(")
        i_exec = src.index("execute_tool_pipeline(")
        assert i_parse < i_sanitize < i_emit < i_exec

    def test_docstring_mentions_extraction(self):
        assert "pt_03f4cdf1" in (_tool_dispatch_round.__doc__ or "")


class TestRunTaskDelegation:
    def test_run_task_delegates_to_helper(self):
        from lib.tasks_pkg.orchestrator import _run
        src = inspect.getsource(_run.run_task)
        assert "run_tool_dispatch(" in src

    def test_run_task_no_longer_carries_cluster_inline(self):
        from lib.tasks_pkg.orchestrator import _run
        src = inspect.getsource(_run.run_task)
        assert "parse_tool_calls(" not in src
        assert "execute_tool_pipeline(" not in src
        assert "emit_tool_exec_phase(" not in src
        assert "_dispatch_heartbeat" not in src
