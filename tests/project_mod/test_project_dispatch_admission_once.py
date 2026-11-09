"""Dispatch authority is validated once per admission, never re-read to build a request.

Executable specification for docs/PROJECT_COORDINATION.md, "One live validation
per tool-batch admission". A bound Worker or Coordinator continuation builds
each round's request from the role its grant fixed when ``bind_account``
validated it, so request building reads no dispatch storage. One tool batch
validates its grant once; every admission gate of its calls (batch admission,
role policy, executor admission, file-handler selection, handler scopes and the
tool intent's pre-check) decides from that validation, and each call's tool
intent stays its live effect fence. Every decision per authority fault (outcome
and stop cause) is the one the former per-gate revalidation made. Counts are
exact calls on the injected storage fakes, never time.
"""
from collections import Counter
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from lib.llm.llm_errors import AbortedError, ToolAdmissionRefused
from lib.project_dispatch import admission
from tests.project_mod.test_coordinator_continuation import (
    activate, base_runtime, context, ready, run, runtime,
)
from tests.project_mod.test_coordinator_workers import _worker_task
from tests.project_mod.test_project_dispatch_runtime import _lease

# Keep the imported fixtures' registered aliases visible to static tooling.
__all__ = ('base_runtime', 'context', 'run', 'runtime')

pytestmark = pytest.mark.unit


def _schema(name):
    return {'type': 'function', 'function': {'name': name, 'description': name,
            'parameters': {'type': 'object', 'properties': {}}}}


def _spy(r, monkeypatch):
    """Count every storage-backed evidence read of the dispatch authority ports."""
    reads = Counter()

    def counted(name, real):
        def call(*args, **kwargs):
            reads[name] += 1
            return real(*args, **kwargs)
        return call
    repository = r.c.repo
    # Dispatch records and Stop watermarks share the injected record client.
    monkeypatch.setattr(repository.client, 'query', counted('record', repository.client.query))
    monkeypatch.setattr(repository.client, 'command', counted('record_write', repository.client.command))
    monkeypatch.setattr(repository.binding_repository, 'read',
                        counted('binding', repository.binding_repository.read))
    monkeypatch.setattr(repository.conversations, 'get', counted('conversation', repository.conversations.get))
    monkeypatch.setattr(repository, 'turn_get', counted('turn', repository.turn_get))
    monkeypatch.setattr(repository, 'attempt_get', counted('attempt', repository.attempt_get))
    return reads


@pytest.fixture
def worker(runtime, monkeypatch, tmp_path):
    """A bound Worker executing in its registered isolated checkout, as in production."""
    from lib.project_mod.config import build_root_policy
    from lib.tasks_pkg.workspace_delivery import workspace_work_id
    from tests.tools.test_tool_settle_all_lanes import _mk_task
    task, row = _worker_task(runtime)
    canonical = task['config']['canonicalProjectPath']
    workspace = (tmp_path / 'worker-checkout').resolve()
    workspace.mkdir()
    for index in range(3):
        (workspace / f'notes-{index}.txt').write_text(f'worker copy {index}\n')
    task['config']['projectPath'] = str(workspace)
    shared = build_root_policy(canonical)
    task['_workspaceRootPolicy'] = {'primaryPath': str(workspace), 'roots': {
        label: {**state, 'path': str(workspace)} for label, state in shared['roots'].items()}}
    project_root = str(Path(canonical).resolve())
    work_id = workspace_work_id(task)
    task['_workspaceDelivery'] = {'repositories': [{
        'canonicalRoot': project_root, 'workspacePath': str(workspace), 'workId': work_id}]}
    task.update({key: value for key, value in _mk_task().items() if key not in task})
    reads = _spy(runtime, monkeypatch)
    registered = {'user_id': 7, 'task_id': work_id, 'project_root': project_root,
                  'state': 'active', 'workspace_path': str(workspace)}

    def find_workspace(root, task_id, user_id):
        reads['workspace'] += 1
        return dict(registered) if (root, task_id, user_id) == (project_root, work_id, 7) else None
    monkeypatch.setattr('lib.integration_state_repository.find_workspace', find_workspace)
    # The task's event lane acknowledges every event, as a registered task's does.
    events = []

    def append_event(task, event):
        events.append(event)
        return True
    for owner in ('lib.tasks_pkg.manager', 'lib.tasks_pkg.tool_dispatch._pipeline',
                  'lib.tasks_pkg.tool_dispatch._heartbeat', 'lib.tasks_pkg.executor._finalize'):
        monkeypatch.setattr(owner + '.append_event', append_event, raising=False)
    return SimpleNamespace(r=runtime, task=task, row=row, workspace=str(workspace),
                           registered=registered, reads=reads, events=events)


@pytest.fixture
def continuation(run, monkeypatch):
    """A bound automatic Coordinator continuation."""
    ready(run)
    row = activate(run)
    return SimpleNamespace(r=run, task=run.task, row=row, reads=_spy(run, monkeypatch))


# Storage reads of three rounds' request building (``build_body.tool_metadata``).
# The former per-round revalidation read 23 per Worker round and 33 per
# Coordinator continuation round.
ROUND_READS = {'worker': Counter(), 'continuation': Counter()}


@pytest.mark.parametrize('role', ['worker', 'continuation'])
def test_request_building_reads_no_dispatch_storage_and_keeps_the_granted_role(
        role, request):
    from lib.tasks_pkg.coordinator_policy import coordinator_scope
    from lib.tasks_pkg.orchestrator._tool_request_metadata import attach_tool_request_metadata
    bound = request.getfixturevalue(role)
    task, row = bound.task, bound.row
    catalog = [_schema('get_project_workers'), _schema('read_files')]
    messages = [{'role': 'user', 'content': 'Continue the delegated work'}]
    bound.reads.clear()
    bodies = []
    for round_num in (1, 2, 3):
        body = {'model': 'model-a', 'messages': messages, 'tools': deepcopy(catalog)}
        attach_tool_request_metadata(task, body, messages, catalog, model='model-a',
                                     round_num=round_num, tid=task['id'])
        bodies.append(body)
    assert +bound.reads == ROUND_READS[role]
    if role == 'continuation':
        # The continuation's grant fixes the Coordinator role and its scope.
        assert coordinator_scope(task) == {
            'project_path': row['request']['projectPath'],
            'binding_revision': row['grant']['bindingRevision'],
            'conversation_id': row['request']['coordinatorId'], 'user_id': row['grant']['owner']}
        assert all(body['_programmatic_tool_calling'] == 'off' and body['_multi_agent_mode'] == 'disabled'
                   for body in bodies)
    else:
        assert coordinator_scope(task) is None
        assert all(body['_multi_agent_mode'] != 'disabled' for body in bodies)


def test_request_building_without_its_grant_carrier_stops_the_task(worker):
    from lib.tasks_pkg.orchestrator._tool_request_metadata import attach_tool_request_metadata
    task = worker.task
    task['config'].pop(admission.KEY)
    with pytest.raises(AbortedError, match='carrier'):
        attach_tool_request_metadata(task, {}, [], [], model='model-a', round_num=1, tid=task['id'])
    assert task['aborted'] is True and task['_abort_reason'] == 'dispatch_authority_lost'


def _round(task):
    """One round's request building and its model dispatch intent, before any provider."""
    from lib.tasks_pkg.orchestrator._tool_request_metadata import attach_tool_request_metadata
    messages = [{'role': 'user', 'content': 'Continue'}]
    attach_tool_request_metadata(task, {}, messages, [], model='model-a', round_num=1, tid=task['id'])
    lease = _lease(task)
    try:
        lease.mark_dispatched()
    except AbortedError:
        lease.settle(unsent=True)
        raise
    lease.settle({'prompt_tokens': 4, 'completion_tokens': 6})


@pytest.mark.parametrize('role', ['worker', 'continuation'])
def test_revocation_between_rounds_refuses_the_next_model_request(role, request):
    bound = request.getfixturevalue(role)
    task, repository = bound.task, bound.r.c.repo
    _round(task)
    sent = deepcopy(repository.read(bound.row['id'])['modelAttempts'])
    assert len(sent) == 1
    repository.revoke(bound.row['id'], bound.row['grant']['revision'])
    with pytest.raises(AbortedError):
        _round(task)
    # Refused before any provider saw the request, naming the revocation.
    assert repository.read(bound.row['id'])['modelAttempts'] == sent
    assert task['aborted'] is True and task['_abort_reason'] == 'dispatch_grant_revoked'


def _calls(names, start=0):
    from tests.tools.test_tool_settle_all_lanes import _mk_tc
    return [_mk_tc(f'call-{start + index}', name, 1,
                   args={'path': f'notes-{(start + index) % 3}.txt'} if name == 'read_files' else {})
            for index, name in enumerate(names)]


def _batch(worker, parsed):
    """Run one model round's tool batch through the real pipeline and executor."""
    from lib.tasks_pkg.tool_dispatch.api import execute_tool_pipeline
    task = worker.task
    task.setdefault('toolRounds', []).extend(item[5] for item in parsed)
    messages = []
    execute_tool_pipeline(task, parsed, task['config'], worker.workspace, True,
                          [_schema('read_files')], messages, [], 0, 'model-a')
    return {message['tool_call_id']: message['content'] for message in messages}


def _ledger(worker):
    return worker.r.c.repo.read(worker.row['id']).get('toolAttempts', [])


# One live validation of this Worker's grant: the dispatch record, both Stop
# watermarks and the delegation source's Stop (record), the binding epoch twice,
# both conversations, the human and delegation source Turns, the delegation
# attempt and the bound executor, and the registered checkout.
VALIDATION = Counter(record=4, binding=2, conversation=2, turn=2, attempt=2, workspace=1)
# Each call's tool intent and its settlement load and write the record once.
LEDGER = Counter(record=2, record_write=2)


def _times(counter, count):
    return Counter({key: value * count for key, value in counter.items()})


# One validation admits the batch and its decisions read the checkout once more;
# each call's tool intent is its live effect fence. The former per-gate
# revalidation read 143 (1 call) and 429 (3 calls).
BATCH_READS = {calls: _times(VALIDATION, 1 + calls) + _times(LEDGER, calls) + Counter(workspace=1)
               for calls in (1, 3)}


@pytest.mark.parametrize('calls', [1, 3])
def test_one_tool_batch_validates_once_and_each_call_fences_its_effect(worker, calls):
    worker.reads.clear()
    results = _batch(worker, _calls(['read_files'] * calls))
    counted = +worker.reads
    assert all(f'worker copy {index}' in results[f'call-{index}'] for index in range(calls))
    assert not worker.task.get('aborted')
    assert sorted(row['phase'] for row in _ledger(worker)) == ['executor_returned'] * calls
    assert counted == BATCH_READS[calls]


def test_a_prefetched_project_read_validates_once(worker):
    """Streaming prefetch admits its call once; the tool's own dispatch scope shares it."""
    import json
    from lib.tasks_pkg.streaming_tool_executor import StreamingToolAccumulator
    task = worker.task
    worker.reads.clear()
    accumulator = StreamingToolAccumulator(task, worker.workspace, round_num=1, project_enabled=True)
    try:
        accumulator.on_tool_call_ready({'id': 'prefetch-0', 'type': 'function', 'function': {
            'name': 'read_files', 'arguments': json.dumps({'path': 'notes-0.txt'})}})
        published = accumulator.inject_into_cache(task)
    finally:
        accumulator.close()
    counted = +worker.reads
    assert published == 1 and 'worker copy 0' in str(task['_tool_result_cache'])
    # The former prefetch read 51: its admission, the role check's two
    # validations and the tool scope's own validation.
    assert counted == VALIDATION + Counter(workspace=1)


def _inject(fault, worker, monkeypatch):
    """Commit one authority change; the decision table below names its verdict."""
    task, row, r = worker.task, worker.row, worker.r
    repository = r.c.repo
    if fault == 'revoked':
        repository.revoke(row['id'], row['grant']['revision'])
    elif fault == 'durable_stop':
        from lib.project_dispatch.repository import record_stop
        record_stop(repository.owner, row['request']['recipientId'], row['grant']['createdAt'],
                    client=repository.client)
    elif fault == 'rebound':
        repository.binding_repository.value['revision'] += 1
    elif fault == 'source_changed':
        r.c.turn['projectionRevision'] += 1
    elif fault == 'settled':
        r.attempts[task['_attemptId']]['status'] = 'completed'
    elif fault == 'superseded':
        # A successor attempt of the same Turn took the immutable association.
        r.attempts[task['_attemptId']]['status'] = 'interrupted'
        r.attempts['successor-attempt'] = {**r.attempts[task['_attemptId']],
                                           'attemptId': 'successor-attempt', 'status': 'running'}
        repository.continue_execution(row['id'], repository.read(row['id'])['execution'],
                                      task['_turnId'], 'successor-attempt')
    elif fault == 'workspace_discarded':
        worker.registered['state'] = 'discarded'
    elif fault == 'carrier_removed':
        task['config'].pop(admission.KEY)
    elif fault == 'cooperative_stop':
        task['aborted'] = True


# Stop cause per fault: the one every live admission gate gave before.
CAUSES = {
    'revoked': 'dispatch_grant_revoked',
    'durable_stop': 'dispatch_grant_revoked',
    'rebound': 'dispatch_authority_lost',
    'source_changed': 'dispatch_authority_lost',
    'settled': 'dispatch_authority_lost',
    'superseded': 'dispatch_authority_lost',
    'workspace_discarded': 'dispatch_authority_lost',
    'carrier_removed': 'dispatch_authority_lost',
}


@pytest.mark.parametrize('fault', list(CAUSES))
def test_authority_change_between_batches_is_refused_at_the_next_batch_admission(
        worker, monkeypatch, fault):
    first = _batch(worker, _calls(['read_files']))
    assert 'worker copy 0' in first['call-0']
    recorded = deepcopy(_ledger(worker))
    _inject(fault, worker, monkeypatch)
    with pytest.raises(ToolAdmissionRefused):
        _batch(worker, _calls(['read_files', 'read_files'], start=1))
    # No call of the refused batch recorded an effect intent or ran its handler.
    assert _ledger(worker) == recorded
    assert worker.task['aborted'] is True and worker.task['_abort_reason'] == CAUSES[fault]


def _refused_batch(worker, parsed):
    """A batch whose authority fails after its admission: the halt is the task's abort.

    The refusal either ends the pipeline (a role or admission gate raised) or
    settles each call as an error; both leave every handler unentered.
    """
    try:
        return _batch(worker, parsed)
    except AbortedError:
        return {}


@pytest.mark.parametrize('fault', list(CAUSES))
def test_authority_change_inside_a_batch_is_refused_before_any_effect(worker, monkeypatch, fault):
    """A change committed after the batch admission reaches every call's effect fence."""
    from lib.tasks_pkg.tool_dispatch import _pipeline as pipeline
    admit = pipeline._admit_dispatch_tool_calls

    def admit_then_change(*args, **kwargs):
        admitted = admit(*args, **kwargs)
        _inject(fault, worker, monkeypatch)
        return admitted
    monkeypatch.setattr(pipeline, '_admit_dispatch_tool_calls', admit_then_change)
    results = _refused_batch(worker, _calls(['read_files', 'read_files']))
    assert not any('worker copy' in str(content) for content in results.values())
    # Every intent the batch recorded settled as never entered.
    assert all(row['phase'] == 'not_entered' for row in _ledger(worker))
    assert worker.task['aborted'] is True and worker.task['_abort_reason'] == CAUSES[fault]


def test_cooperative_stop_inside_a_batch_keeps_its_cancellation(worker, monkeypatch):
    from lib.tasks_pkg.tool_dispatch import _pipeline as pipeline
    admit = pipeline._admit_dispatch_tool_calls

    def admit_then_stop(*args, **kwargs):
        admitted = admit(*args, **kwargs)
        worker.task['aborted'] = True
        return admitted
    monkeypatch.setattr(pipeline, '_admit_dispatch_tool_calls', admit_then_stop)
    results = _refused_batch(worker, _calls(['read_files']))
    assert 'worker copy' not in str(results.get('call-0'))
    assert _ledger(worker) == []
    assert worker.task.get('_abort_reason') is None
