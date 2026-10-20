"""Slice 24 wire-parity: _stream_acc_settle.py extraction from _run.py."""

import inspect
from types import SimpleNamespace

import lib.tasks_pkg.orchestrator._stream_acc_settle as _stream_acc_settle

class TestStreamAccSettleWireParity:
    def test_module_exists(self):
        assert _stream_acc_settle is not None

    def test_helper_callable(self):
        assert callable(_stream_acc_settle.settle_stream_accumulator)

    def test_signature_accepts_required_kwargs(self):
        sig = inspect.signature(_stream_acc_settle.settle_stream_accumulator)
        params = set(sig.parameters.keys())
        assert {'stream_acc', 'task', 'rs', 'tid'} <= params

    def test_unverified_response_retires_announcements_without_cache_adoption(self):
        from unittest.mock import Mock
        from lib.llm.stream_result import ProviderStreamResult, ProviderStreamState
        message = {'role': 'assistant', 'tool_calls': [{'id': 'unverified'}]}
        result = ProviderStreamResult(message, 'stop', None, ProviderStreamState.PREMATURE_CLOSE)
        accumulator = Mock(announced_tc_map={}, announced_count=1,
                           tool_round_num=7, submitted_count=1)
        state = SimpleNamespace(assistant_msg=message, last_stream_result=result,
                                tool_round_num=0)
        _stream_acc_settle.settle_stream_accumulator(
            accumulator, {'id': 'unverified'}, state, tid='test')
        accumulator.on_provider_attempt_restart.assert_called_once()
        accumulator.reconcile_announced_rounds.assert_called_once_with({})
        accumulator.inject_into_cache.assert_not_called()
        assert state.tool_round_num == 7

    def test_body_reads_back_tool_round_num(self):
        src = inspect.getsource(_stream_acc_settle.settle_stream_accumulator)
        assert "rs.tool_round_num = stream_acc.tool_round_num" in src
        assert "getattr(stream_acc, 'announced_count', 0)" in src

    def test_body_injects_into_cache_when_submitted(self):
        src = inspect.getsource(_stream_acc_settle.settle_stream_accumulator)
        assert "if stream_acc.submitted_count > 0" in src
        assert "inject_into_cache(task)" in src

    def test_step_ordering(self):
        """Reconcile → readback → cache inject (byte-order parity)."""
        src = inspect.getsource(_stream_acc_settle.settle_stream_accumulator)
        i_reconcile = src.index("reconcile_announced_rounds")
        i_readback = src.index("rs.tool_round_num = stream_acc.tool_round_num")
        i_inject = src.index("inject_into_cache")
        assert i_reconcile < i_readback < i_inject

    def test_behavioural_full_settle(self):
        """All three steps fire with a populated accumulator."""
        calls = []

        class FakeAcc:
            announced_tc_map = {'tc1': object()}
            tool_round_num = 7
            submitted_count = 2

            def reconcile_announced_rounds(self, msg):
                calls.append(('reconcile', msg))

            def inject_into_cache(self, task):
                calls.append(('inject', task))
                return 2

        rs = SimpleNamespace(assistant_msg={'role': 'assistant'},
                             tool_round_num=0)
        task = {'id': 'x'}
        _stream_acc_settle.settle_stream_accumulator(
            FakeAcc(), task, rs, tid='deadbeef')
        assert rs.tool_round_num == 7
        assert calls[0][0] == 'reconcile'
        assert calls[1][0] == 'inject'

    def test_behavioural_empty_acc_skips_readback_and_inject(self):
        class FakeAcc:
            announced_tc_map = {}
            tool_round_num = 99
            submitted_count = 0

            def reconcile_announced_rounds(self, msg):
                pass

            def inject_into_cache(self, task):  # pragma: no cover
                raise AssertionError('must not be called')

        rs = SimpleNamespace(assistant_msg={}, tool_round_num=3)
        _stream_acc_settle.settle_stream_accumulator(
            FakeAcc(), {'id': 'x'}, rs, tid='deadbeef')
        assert rs.tool_round_num == 3  # untouched

    def test_docstring_mentions_extraction(self):
        doc = _stream_acc_settle.__doc__ or ""
        assert "Extracted from" in doc
        assert "_run.py" in doc


class TestRunTaskDelegation:
    def test_run_task_delegates_to_helper(self):
        import lib.tasks_pkg.orchestrator._root_agent_loop as _root_agent_loop
        src = inspect.getsource(_root_agent_loop)
        assert "settle_stream_accumulator(" in src

    def test_run_task_no_longer_carries_cluster_inline(self):
        import lib.tasks_pkg.orchestrator._run as _run
        src = inspect.getsource(_run.run_task)
        assert "reconcile_announced_rounds" not in src
        assert "inject_into_cache" not in src

    def test_round_dispatch_closes_prefetch_on_every_exit(self):
        import lib.tasks_pkg.orchestrator._root_agent_loop as _root_agent_loop

        dispatch = inspect.getsource(_root_agent_loop._RootLoopHooks.dispatch)
        assert 'return self._dispatch_open_round(round_num, round_tools)' in dispatch
        src = inspect.getsource(_root_agent_loop._RootLoopHooks._dispatch_open_round)
        assert 'finally:' in src
        assert 'stream_accumulator.close(' in src
        assert src.index('run_llm_call_with_fallback(') \
            < src.index('finally:') \
            < src.index('stream_accumulator.close(')
