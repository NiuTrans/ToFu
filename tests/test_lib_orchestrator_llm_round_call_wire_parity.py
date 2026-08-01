"""Slice 26 wire-parity: _llm_round_call.py extraction from _run.py."""

import inspect

from lib.tasks_pkg.orchestrator import _llm_round_call


class TestLlmRoundCallWireParity:
    def test_module_exists(self):
        assert _llm_round_call is not None

    def test_helper_callable(self):
        assert callable(_llm_round_call.run_llm_call_with_fallback)

    def test_signature_accepts_required_kwargs(self):
        sig = inspect.signature(_llm_round_call.run_llm_call_with_fallback)
        params = set(sig.parameters.keys())
        assert {'task', 'rs', 'body', 'messages', 'tool_list', 'stream_acc',
                'round_num', 'tid', 'max_tokens', 'max_tool_rounds'} <= params

    def test_body_calls_fallback_with_stream_acc_callback(self):
        src = inspect.getsource(_llm_round_call.run_llm_call_with_fallback)
        assert "_llm_call_with_fallback(" in src
        assert "on_tool_call_ready=stream_acc.on_tool_call_ready" in src

    def test_body_writes_back_six_rs_fields(self):
        src = inspect.getsource(_llm_round_call.run_llm_call_with_fallback)
        for field in ('assistant_msg', 'last_finish_reason', 'last_usage',
                      'model', 'preset', 'thinking_enabled'):
            assert f"rs.{field} = llm_result[" in src or \
                   f"rs.{field} = llm_result.get(" in src, field

    def test_body_usage_falls_back_to_previous(self):
        src = inspect.getsource(_llm_round_call.run_llm_call_with_fallback)
        assert "llm_result['usage'] or rs.last_usage" in src

    def test_body_flushes_deferred_inbox(self):
        src = inspect.getsource(_llm_round_call.run_llm_call_with_fallback)
        assert "flush_deferred_peer_and_steer(task, round_num=round_num, tid=tid)" in src

    def test_body_early_surfaces_model_on_task(self):
        src = inspect.getsource(_llm_round_call.run_llm_call_with_fallback)
        assert "task['model'] = rs.model" in src

    def test_body_break_action_stamps_exit_reason(self):
        src = inspect.getsource(_llm_round_call.run_llm_call_with_fallback)
        assert "llm_result['_loop_action'] == 'break'" in src
        assert "rs.exit_reason = llm_result['_loop_exit_reason']" in src
        assert "return 'break'" in src
        assert "return 'proceed'" in src

    def test_body_catches_aborted_error(self):
        src = inspect.getsource(_llm_round_call.run_llm_call_with_fallback)
        assert "isinstance(e, AbortedError)" in src
        assert "rs.exit_reason = 'user_abort'" in src
        assert "User abort caught at round" in src

    def test_body_reraises_non_abort_exceptions(self):
        src = inspect.getsource(_llm_round_call.run_llm_call_with_fallback)
        assert "raise" in src

    def test_docstring_mentions_extraction(self):
        assert "pt_03f4cdf1" in (_llm_round_call.__doc__ or "")


class TestRunTaskDelegation:
    def test_run_task_delegates_to_helper(self):
        from lib.tasks_pkg.orchestrator import _run
        src = inspect.getsource(_run.run_task)
        assert "run_llm_call_with_fallback(" in src

    def test_run_task_no_longer_carries_block_inline(self):
        from lib.tasks_pkg.orchestrator import _run
        src = inspect.getsource(_run.run_task)
        assert "= _llm_call_with_fallback(" not in src
        assert "_loop_action" not in src
        assert "User abort caught at round" not in src
