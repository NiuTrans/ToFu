"""Terminal durability precedes visibility and survives task memory loss."""
from copy import deepcopy
import pytest

from lib.agent_core.task_runtime import TaskRuntime
from lib.research.action_terminal import persist_action_terminal, load_action_terminal
from lib.storage.errors import StorageError
from tests.research.test_research_action_observations import MemoryRecords
from tests.research.test_research_workspace_evidence_guard import storage as storage

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('process_state', ['dead', 'alive', 'unknown'])
def test_cold_executor_loss_requires_proven_death_and_never_writes(monkeypatch, process_state):
    from lib.research.action_terminal import load_public_action_task
    from tests.research.test_research_execution_owner import repository
    client = MemoryRecords(); owners = repository(client); owners.claim_initial()
    monkeypatch.setattr('lib.storage.get_storage_client', lambda **kwargs: client)
    monkeypatch.setattr('lib.research.execution_owner.owner_state', lambda value: process_state)
    before = deepcopy(client.rows)
    reads = []; original_query = client.query
    def query(operation, payload):
        reads.append(operation)
        return original_query(operation, payload)
    monkeypatch.setattr(client, 'query', query)
    def no_write(*args): pytest.fail('cold interruption inspection wrote or replayed state')
    monkeypatch.setattr(client, 'command', no_write)
    public = load_public_action_task('research_action_observed', user_id=1)
    if process_state == 'dead':
        assert public['status'] == 'interrupted'
        assert public['finished_at'] is None and public['result'] is None
        assert public['meta']['settlement_recorded'] is False
        assert public['meta']['interruption_reason'] == 'executor_process_lost'
        assert public['replay_allowed'] is False
    else:
        assert public is None
    assert reads == ['record.get'] * 6
    assert load_public_action_task('research_action_observed', user_id=2) is None
    assert client.rows == before


def test_cold_binding_without_executor_is_not_an_interruption(monkeypatch):
    from lib.research.action_terminal import load_public_action_task
    from tests.research.test_research_execution_owner import repository
    client = MemoryRecords(); repository(client)
    monkeypatch.setattr('lib.storage.get_storage_client', lambda **kwargs: client)
    monkeypatch.setattr('lib.research.execution_owner.owner_state', lambda value: 'dead')
    assert load_public_action_task('research_action_observed', user_id=1) is None


def test_actual_local_process_exit_is_visible_without_replaying(monkeypatch):
    import json
    import subprocess
    import sys
    from lib.process_owner import owner_state
    from lib.research.action_terminal import load_public_action_task
    from tests.research.test_research_execution_owner import repository
    completed = subprocess.run([sys.executable, '-c',
        'import json; from lib.process_owner import process_owner; print(json.dumps(process_owner()))'],
        check=True, capture_output=True, text=True)
    exited_owner = json.loads(completed.stdout)
    if exited_owner is None: pytest.skip('local process incarnation evidence unavailable')
    assert owner_state(exited_owner) == 'dead'
    client = MemoryRecords(); owners = repository(client)
    monkeypatch.setattr('lib.research.execution_owner.process_owner', lambda: exited_owner)
    owners.claim_initial()
    monkeypatch.setattr('lib.storage.get_storage_client', lambda **kwargs: client)
    public = load_public_action_task('research_action_observed', user_id=1)
    assert public['status'] == 'interrupted' and public['replay_allowed'] is False


def test_terminal_settlement_takes_precedence_over_dead_executor(monkeypatch):
    from lib.research.action_terminal import load_public_action_task
    from tests.research.test_research_execution_owner import repository
    client = MemoryRecords(); owners = repository(client); owners.claim_initial()
    monkeypatch.setattr('lib.storage.get_storage_client', lambda **kwargs: client)
    monkeypatch.setattr('lib.research.execution_owner.owner_state', lambda value: 'dead')
    runtime = TaskRuntime('terminal-precedence', terminal_persist=persist_action_terminal)
    task = create(runtime, 'research_action_observed')
    runtime.finish(task['id'], result={'delivered': True})
    public = load_public_action_task(task['id'], user_id=1)
    assert public['status'] == 'done' and public['result'] == {'delivered': True}


def test_cold_v4_query_reports_executor_loss_after_store_restart(storage, monkeypatch):
    import asyncio
    from quart import Quart, g, request
    from lib.api_keys import local_admin_context
    from lib.api_v4 import attach_v4_envelope, copy_blueprint_to_v4
    from tests.research.test_research_execution_owner import repository
    import routes.api_v1.tasks as tasks
    supervisor, _ = storage
    owners = repository(supervisor.client); owners.claim_initial()
    supervisor.stop(); supervisor.start()
    monkeypatch.setattr('lib.storage.get_storage_client', lambda **kwargs: supervisor.client)
    monkeypatch.setattr('lib.research.execution_owner.owner_state', lambda value: 'dead')
    monkeypatch.setattr(tasks, '_registries', lambda: {})
    app = Quart(__name__); app.config['TESTING'] = True
    @app.before_request
    async def grant():
        context = local_admin_context()
        context.owner_user_id = int(request.headers.get('X-Test-User', '1'))
        g.auth_ctx = context; g.rate_decision = None
    app.register_blueprint(attach_v4_envelope(copy_blueprint_to_v4(tasks.api_v1_tasks_bp,
        name='interrupted_action_v4')))
    async def query():
        response = await app.test_client().get('/api/v4/tasks/research_action_observed')
        assert response.status_code == 200
        public = (await response.get_json())['data']
        assert public['status'] == 'interrupted' and public['finished_at'] is None
        assert public['result_available'] is False and public['replay_allowed'] is False
        assert public['meta']['settlement_recorded'] is False
        foreign = await app.test_client().get('/api/v4/tasks/research_action_observed',
            headers={'X-Test-User': '2'})
        assert foreign.status_code == 404
    asyncio.run(query())


def create(runtime, task_id='terminal-action'):
    task = runtime.create(user_id=1, task_id=task_id)
    runtime.update_fields(task_id, fields={'task_id': task_id, 'user_id': 1,
        'direction': 'Retained terminal fact', 'lang': 'en', 'action': 'experiment',
        'expected_revision': 3, '_committedWorkspaceRevision': 4})
    runtime.mark_running(task_id)
    return task


def test_terminal_commit_is_before_visible_status_and_failed_commit_retries_same_candidate(monkeypatch):
    client = MemoryRecords(); calls = []; refuse = [True]
    monkeypatch.setattr('lib.storage.get_storage_client', lambda **kwargs: client)
    runtime = None
    def persist(task, candidate):
        assert runtime.get(task['id'])['status'] == 'running'
        assert not any(event.get('type') in {'done', 'error', 'aborted'} for event in task['events'])
        calls.append(deepcopy(candidate))
        if refuse[0]: raise StorageError('database_unavailable', 'injected terminal refusal')
        persist_action_terminal(task, candidate)
    runtime = TaskRuntime('controlled-terminal', terminal_persist=persist)
    task = create(runtime)
    with pytest.raises(StorageError): runtime.finish(task['id'], result={'actual': 'committed'})
    assert task['status'] == 'running' and task['_terminalPersistencePending'] is True
    assert load_action_terminal(task['id'], user_id=1)['found'] is False
    refuse[0] = False
    polled = runtime.poll(task['id'])
    assert polled['status'] == 'done'
    assert calls[0] == calls[1]
    assert task['status'] == 'done' and task['result'] == {'actual': 'committed'}
    assert '_terminalPersistencePending' not in task
    terminal = load_action_terminal(task['id'], user_id=1)['terminal']
    assert terminal['status'] == 'done' and terminal['committed_workspace_revision'] == 4
    assert terminal['replay_allowed'] is False
    assert load_action_terminal(task['id'], user_id=2)['found'] is False


@pytest.mark.parametrize('status', ['done', 'error', 'aborted'])
def test_settlement_fact_survives_selected_store_restart_and_task_eviction(storage, monkeypatch, status):
    supervisor, _ = storage
    monkeypatch.setattr('lib.storage.get_storage_client', lambda **kwargs: supervisor.client)
    runtime = TaskRuntime('terminal-restart', terminal_persist=persist_action_terminal)
    task = create(runtime)
    from lib.paper.agent_usage import PaperAgentUsageMeter
    usage = PaperAgentUsageMeter('research-action', token_budget=320000, dispatch_budget=10).snapshot()
    task['_researchUsageSnapshot'] = deepcopy(usage)
    if status == 'aborted': runtime.abort(task['id'])
    runtime.finish(task['id'], result={'summary': 'terminal result'} if status == 'done' else None,
                   error=RuntimeError('known failure') if status == 'error' else None)
    assert runtime.remove_owned(task['id'], user_id=1)
    supervisor.stop(); supervisor.start()
    terminal = load_action_terminal(task['id'], user_id=1)['terminal']
    assert terminal['status'] == status and terminal['committed_workspace_revision'] == 4
    assert bool(terminal['result_digest']) == (status == 'done')
    assert terminal['usage_snapshot'] == usage


def test_lost_terminal_ack_accepts_exact_readback(monkeypatch):
    client = MemoryRecords(); client.lose_ack = True
    monkeypatch.setattr('lib.storage.get_storage_client', lambda **kwargs: client)
    runtime = TaskRuntime('terminal-lost-ack', terminal_persist=persist_action_terminal)
    task = create(runtime)
    assert runtime.finish(task['id'], result={'known': True})
    assert load_action_terminal(task['id'], user_id=1)['terminal']['status'] == 'done'


def test_terminal_storage_io_does_not_hold_registry_lock_or_allow_competing_finish():
    from concurrent.futures import ThreadPoolExecutor
    import threading
    entered, release = threading.Event(), threading.Event()
    def persist(task, candidate):
        entered.set(); assert release.wait(3)
    runtime = TaskRuntime('terminal-lock', terminal_persist=persist)
    task = create(runtime)
    with ThreadPoolExecutor(max_workers=2) as pool:
        finished = pool.submit(runtime.finish, task['id'], result={'done': True})
        assert entered.wait(2)
        try:
            observed = pool.submit(runtime.get, task['id']).result(timeout=1)
            assert observed['status'] == 'running'
            assert runtime.finish(task['id'], error=RuntimeError('competitor')) is False
            assert runtime.abort(task['id']) is False
        finally:
            release.set()
        assert finished.result(timeout=2) is True
    assert task['status'] == 'done'


def test_terminal_retains_loop_boundary_after_memory_loss(monkeypatch):
    from lib.paper.agent_usage import PaperAgentUsageMeter
    client = MemoryRecords()
    monkeypatch.setattr('lib.storage.get_storage_client', lambda **kwargs: client)
    runtime = TaskRuntime('terminal-loop-boundary', terminal_persist=persist_action_terminal)
    task = create(runtime)
    meter = PaperAgentUsageMeter('research-action', token_budget=320000, dispatch_budget=10)
    for number in range(10):
        meter.tools_for_round([], number)
    snapshot = meter.snapshot()
    task['_researchUsageSnapshot'] = deepcopy(snapshot)
    runtime.finish(task['id'], result={'summary':'bounded synthesis'})
    runtime.remove_owned(task['id'], user_id=1)
    terminal = load_action_terminal(task['id'], user_id=1)['terminal']
    assert terminal['usage_snapshot'] == snapshot
    assert terminal['usage_snapshot']['forced_final_reason'] == 'dispatch_budget'
    assert terminal['replay_allowed'] is False


@pytest.mark.parametrize('mismatch', [False, True])
def test_success_shaped_terminal_ack_requires_exact_published_record(monkeypatch, mismatch):
    client = MemoryRecords()
    monkeypatch.setattr('lib.storage.get_storage_client', lambda **kwargs: client)
    command = client.command
    def false_ack(operation, payload, command_id):
        if mismatch:
            payload = deepcopy(payload)
            payload['value']['expected_revision'] += 1
            command(operation, payload, command_id)
        return {'version': 1}
    monkeypatch.setattr(client, 'command', false_ack)
    runtime = TaskRuntime('terminal-false-ack', terminal_persist=persist_action_terminal)
    task = create(runtime)
    with pytest.raises(StorageError, match='exactly acknowledged'):
        runtime.finish(task['id'], result={'summary':'not acknowledged'})
    assert task['status'] == 'running'
    assert task['_terminalPersistencePending']
    assert not any(event.get('type') == 'done' for event in task['events'])
    if not mismatch:
        monkeypatch.setattr(client, 'command', command)
        runtime.poll(task['id'])
        terminal = load_action_terminal(task['id'], user_id=1)['terminal']
        assert terminal['status'] == 'done' and terminal['expected_revision'] == 3


def test_cold_result_is_exact_and_legacy_digest_only_is_explicit(monkeypatch):
    from lib.research.action_terminal import load_public_action_task, NAMESPACE
    client = MemoryRecords()
    monkeypatch.setattr('lib.storage.get_storage_client', lambda **kwargs: client)
    runtime = TaskRuntime('cold-action-result', terminal_persist=persist_action_terminal)
    task = create(runtime, 'research_action_cold_result')
    result = {'workspace':{'revision':4,'hypothesis':'Original delivered result'}}
    runtime.finish(task['id'], result=result)
    runtime.remove_owned(task['id'], user_id=1)
    retained = load_public_action_task(task['id'], user_id=1)
    assert retained['result'] == result and retained['result_available'] is True
    assert retained['replay_allowed'] is False
    assert load_public_action_task(task['id'], user_id=2) is None
    row = next(row for (namespace,key),row in client.rows.items() if namespace == NAMESPACE)
    del row['value']['result']
    legacy = load_public_action_task(task['id'], user_id=1)
    assert legacy['status'] == 'done' and legacy['result'] is None
    assert legacy['result_available'] is False and legacy['result_digest'] is not None


def test_changed_retained_result_refuses_cold_delivery(monkeypatch):
    from lib.research.action_terminal import NAMESPACE
    client = MemoryRecords()
    monkeypatch.setattr('lib.storage.get_storage_client', lambda **kwargs: client)
    runtime = TaskRuntime('cold-action-drift', terminal_persist=persist_action_terminal)
    task = create(runtime)
    runtime.finish(task['id'], result={'actual':'original'})
    row = next(row for (namespace,key),row in client.rows.items() if namespace == NAMESPACE)
    row['value']['result']['actual'] = 'changed'
    with pytest.raises(StorageError, match='result digest'):
        load_action_terminal(task['id'], user_id=1)


def test_cold_v4_query_restores_result_after_store_restart(storage, monkeypatch):
    import asyncio
    import uuid
    from quart import Quart, g, request
    from lib.api_keys import local_admin_context
    from lib.api_v4 import attach_v4_envelope, copy_blueprint_to_v4
    import routes.api_v1.tasks as tasks
    supervisor, backend = storage
    monkeypatch.setattr('lib.storage.get_storage_client', lambda **kwargs: supervisor.client)
    runtime = TaskRuntime('cold-http-result', terminal_persist=persist_action_terminal)
    task = create(runtime, 'research_action_' + uuid.uuid4().hex[:16])
    delivered = {'workspace':{'revision':4,'hypothesis':'Exact original '+backend},'summary':'Delivered original'}
    runtime.finish(task['id'], result=delivered)
    runtime.remove_owned(task['id'], user_id=1)
    supervisor.stop(); supervisor.start()
    app = Quart(__name__); app.config['TESTING'] = True
    @app.before_request
    async def grant():
        context = local_admin_context()
        context.owner_user_id = int(request.headers.get('X-Test-User','1'))
        g.auth_ctx = context; g.rate_decision = None
    monkeypatch.setattr(tasks, '_registries', lambda: {})
    app.register_blueprint(attach_v4_envelope(copy_blueprint_to_v4(tasks.api_v1_tasks_bp, name='cold_action_v4')))
    async def query():
        response = await app.test_client().get('/api/v4/tasks/' + task['id'])
        assert response.status_code == 200
        body = await response.get_json()
        assert body['data']['result'] == delivered and body['data']['result_available'] is True
        assert body['data']['status'] == 'done'
        foreign = await app.test_client().get('/api/v4/tasks/' + task['id'], headers={'X-Test-User':'2'})
        assert foreign.status_code == 404
        foreign_runtime = TaskRuntime('foreign-cold-collision')
        foreign_runtime.create(user_id=2, task_id=task['id'])
        monkeypatch.setattr(tasks, '_registries', lambda: {'foreign':foreign_runtime})
        try:
            collision = await app.test_client().get('/api/v4/tasks/' + task['id'])
            assert collision.status_code == 404
        finally:
            foreign_runtime.finish(task['id'], result=None)
            foreign_runtime.remove_owned(task['id'], user_id=2)
    asyncio.run(query())


def test_retained_result_growth_is_measured_without_extra_store(monkeypatch, record_property):
    import json
    from lib.research.action_terminal import NAMESPACE
    client = MemoryRecords()
    monkeypatch.setattr('lib.storage.get_storage_client', lambda **kwargs: client)
    runtime = TaskRuntime('cold-result-size', terminal_persist=persist_action_terminal)
    task = create(runtime)
    runtime.finish(task['id'], result={'payload':'漢字' * 1000})
    value = next(row['value'] for (namespace,key),row in client.rows.items() if namespace == NAMESPACE)
    legacy = {key:item for key,item in value.items() if key not in ('result','error','created_at')}
    encode = lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(',',':')).encode()
    growth = len(encode(value)) - len(encode(legacy))
    assert 6000 < growth < 6200
    record_property('terminal_payload_growth_bytes', growth)
    assert len(client.rows) == 1
