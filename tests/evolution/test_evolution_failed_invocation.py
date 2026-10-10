"""Concrete fatal root invocations commit owner-bound failure evidence."""
from copy import deepcopy
import json
import time

import pytest

from tests.evolution.test_evolution_runs import OWNER, runtime as runtime
from tests.tasks_pkg.test_failed_invocation_evidence import _spine

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('complete', [True, False])
def test_root_fatal_preserves_native_capsule_and_cold_settlement(runtime, monkeypatch, complete):
    from lib.tasks_pkg.manager import _persist, _terminal
    from lib.tasks_pkg.orchestrator import _post_loop as post
    from lib.agent_core.execution_session import ExecutionPhase
    from lib.evolution import stage_outcomes
    from lib.storage import get_storage_client
    runs, _, tasks = runtime
    run = runs.submit_run({'objective': 'Retain observed work after a fatal exception.'}, user_id=OWNER)
    actual = tasks[0]
    execution, policy, seed, base, evidence, _ = _spine(
        monkeypatch, ModuleNotFoundError('controlled dependency failure'), complete=complete)
    actual.update(status='running', created_at=time.time()-7,
                  usage=deepcopy(seed['usage']), _executionActualsComplete=complete)
    original_messages = deepcopy(actual['messages'])
    def commit(task, event):
        task['_executionSession'].settle(ExecutionPhase.FAILED, cause='controlled_fatal')
        with monkeypatch.context() as patch:
            patch.setattr(_terminal, 'notify_terminal_listeners',
                          lambda _: (_ for _ in ()).throw(RuntimeError('lost callback after durable commit')))
            assert _persist.persist_task_result(task, _defer_heavy_release=True) is False
    monkeypatch.setattr(post, 'append_event', commit)
    execution._execute_task_invocation(actual, policy=policy)
    row = get_storage_client().query('task_results.replay_get',
        {'key': actual['id'], 'user_id': OWNER, 'include_metadata': True})
    frozen = row['metadata']['terminalContext']['outcome']
    assert row['status'] == frozen['status'] == 'error'
    assert frozen['tool_evidence']['message_count'] == 1
    assert frozen['tool_evidence']['content_chars'] > 30000
    assert frozen['actuals']['wall_clock_seconds'] >= 7
    assert frozen['actuals']['tokens'] == 11 and frozen['actuals_complete'] is complete
    assert stage_outcomes.read_metadata(run['run_id'], actual['id'], user_id=OWNER) is None
    actual['messages'] = original_messages
    monkeypatch.setattr(runs, 'default_task_loader', lambda *a, **kw: None)
    resumed = runs.advance_run(run['run_id'], user_id=OWNER)
    assert resumed['status'] in {'failed', 'needs_attention'}
    assert len(tasks) == 1 and resumed['review'] is None
    outcome = stage_outcomes.read_metadata(run['run_id'], actual['id'], user_id=OWNER)
    assert outcome == frozen
    assert resumed.get('actuals_complete', True) is complete
    page = stage_outcomes.page(run['run_id'], actual['id'], user_id=OWNER, source_kind='tool_evidence')
    content = page['content']
    while page['next_offset'] is not None:
        page = stage_outcomes.page(run['run_id'], actual['id'], user_id=OWNER,
                                  source_kind='tool_evidence', offset=page['next_offset'])
        content += page['content']
    assert json.loads(content)['content'] == evidence
