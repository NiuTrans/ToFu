"""Failed concrete invocations retain observed history before terminal delivery."""
from copy import deepcopy
from types import SimpleNamespace
import time

import pytest

from lib.evolution.bridge import terminal_actuals
from lib.evolution.tool_evidence import chunks

pytestmark = pytest.mark.unit


def _spine(monkeypatch, fail, *, early=False, flow=False, complete=True):
    from lib.tasks_pkg.orchestrator import _run as run
    from lib.tasks_pkg.orchestrator import _post_loop as post
    observed = []
    base = [{'role': 'user', 'content': 'Keep the original acceptance.'}]
    task = {'id': 'failed-working-history', '_userId': 123,
            'created_at': time.time() - 7, 'status': 'running',
            'config': {'model': 'glm-5.3'}, 'messages': deepcopy(base),
            'usage': {'total_tokens': 11, 'prompt_tokens': 7, 'completion_tokens': 4},
            '_executionActualsComplete': complete, '_flow_managed': flow}
    evidence = '工具已完成的原始结果\n' * 4000
    mcfg = dict(model='glm-5.3', thinking_enabled=False, thinking_depth=None,
                preset=None, max_tokens=1000, temperature=0, search_mode='off',
                search_enabled=False, fetch_enabled=False, project_path=None,
                project_enabled=False, code_exec_enabled=False, memory_enabled=False)
    for name in ['snapshot_turn_input', 'bind_provider_and_affinity',
                 '_emit_startup_phase', 'setup_project_context',
                 'maybe_run_memory_prefetch', 'inject_context_and_emit_chips',
                 'apply_resume_state', 'stop_prefetches', 'finalize_task_lane']:
        monkeypatch.setattr(run, name, lambda *a, **kw: None)
    monkeypatch.setattr(run, 'make_vu_phase', lambda *a: None)
    monkeypatch.setattr(run, 'log_task_open', lambda *a: 0)
    monkeypatch.setattr(run, 'run_turn_prelude', lambda task, cfg, tid: cfg)
    monkeypatch.setattr(run, 'resolve_and_seed_model_config', lambda *a: mcfg)
    monkeypatch.setattr(run, 'prepare_continue_tool_history',
                        lambda *a, **kw: SimpleNamespace(injected_calls=0))
    monkeypatch.setattr(run, 'prepare_resume_state', lambda *a, **kw: None)
    monkeypatch.setattr(run, 'handle_abort_during_prep', lambda *a, **kw: False)
    monkeypatch.setattr(run, 'start_prefetches', lambda *a, **kw: None)
    monkeypatch.setattr(run, 'assemble_round_tools', lambda *a: ([], False))
    monkeypatch.setattr(run, 'inject_continue_tool_history', lambda *a, **kw: 0)
    monkeypatch.setattr(post, '_maybe_auto_retry_turn', lambda *a: False)
    def terminal(task, *args):
        actuals, complete = terminal_actuals(task)
        observed.append({'messages': deepcopy(task['messages']), 'actuals': actuals,
                         'complete': complete, 'evidence': ''.join(chunks(task)),
                         'finished_at': task.get('finished_at'), 'status': task['status']})
    monkeypatch.setattr(post, 'append_event', terminal)
    monkeypatch.setattr(post, 'persist_task_result', lambda *a: None)
    def broken(request):
        assert request.messages is not task['messages']
        request.messages.extend([
            {'role': 'assistant', 'content': None, 'tool_calls': [
                {'id': 'already-done', 'type': 'function', 'function':
                 {'name': 'read_files', 'arguments': '{"paths":["existing.txt"]}'}}]},
            {'role': 'tool', 'tool_call_id': 'already-done', 'content': evidence}])
        raise fail
    monkeypatch.setattr(run, 'run_root_agent_loop', broken)
    if early:
        def opening(*args): raise fail
        monkeypatch.setattr(run, 'log_task_open', opening)
    policy = SimpleNamespace(is_child=False, kickoff=False)
    return run, policy, task, base, evidence, observed


@pytest.mark.parametrize('failure', [RuntimeError('post-tool fatal'),
                                      ModuleNotFoundError('missing owned runtime module'),
                                      SystemExit('shutdown after observed output')])
@pytest.mark.parametrize('complete', [True, False])
def test_terminal_failure_preserves_working_tool_output_and_accounting(monkeypatch, failure, complete):
    run, policy, task, base, evidence, observed = _spine(monkeypatch, failure, complete=complete)
    if isinstance(failure, Exception):
        run._execute_task_invocation(task, policy=policy)
    else:
        with pytest.raises(SystemExit) as raised:
            run._execute_task_invocation(task, policy=policy)
        assert raised.value is failure
    assert task['status'] == 'error' and task['finishReason'] == 'error'
    assert task['_messages_include_current_run'] is True
    assert task['messages'][0] == base[0]
    assert task['messages'][-1]['content'] == evidence
    assert len(observed) == 1
    assert observed[0]['messages'][-1]['content'] == evidence
    assert evidence.splitlines()[0] in observed[0]['evidence']
    assert observed[0]['actuals']['tokens'] == 11
    assert observed[0]['actuals']['wall_clock_seconds'] >= 7
    assert observed[0]['complete'] is complete
    assert observed[0]['status'] == 'error'
    if isinstance(failure, ImportError):
        assert task['error']['kind'] == 'internal'
        assert 'missing owned runtime module' in task['error']['detail']


def test_early_failure_keeps_original_inputs_without_invented_tool_evidence(monkeypatch):
    failure = ModuleNotFoundError('bootstrap module')
    run, policy, task, base, evidence, observed = _spine(monkeypatch, failure, early=True)
    run._execute_task_invocation(task, policy=policy)
    assert task['messages'] == base
    assert not task.get('_messages_include_current_run')
    assert observed[0]['evidence'] == ''
    assert observed[0]['actuals']['wall_clock_seconds'] >= 7


def test_flow_keeps_partial_history_but_owns_terminal_delivery_and_clock(monkeypatch):
    run, policy, task, base, evidence, observed = _spine(monkeypatch, RuntimeError('flow failure'), flow=True)
    run._execute_task_invocation(task, policy=policy)
    assert task['messages'][-1]['content'] == evidence
    assert observed == []
    assert 'finished_at' not in task
