"""Startup evidence is content-free and does not alter task authority."""
import json
import threading
import time
import uuid
from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from lib import observability
from lib.tasks_pkg import spawn

pytest_plugins = ('tests._chat_sidecar',)
pytestmark = pytest.mark.unit


def test_startup_anchors_pair_identity_and_use_monotonic_duration(monkeypatch):
    records = []
    monkeypatch.setattr(observability.logger, 'info', lambda fmt, value: records.append(json.loads(value)))
    clock = iter([10.0, 10.125])
    wall = iter([100.0, 90.0])  # Wall-clock adjustment cannot change duration.
    monkeypatch.setattr(observability.time, 'monotonic', lambda: next(clock))
    monkeypatch.setattr(observability.time, 'time', lambda: next(wall))
    observe = Mock()
    monkeypatch.setattr(observability._STORE, 'observe', observe)
    with observability.measure_task_startup('workspace_binding', identity={
            'task_id': 'task', 'conversation_id': 'conv', 'attempt_id': 'attempt',
            'turn_id': 'turn', 'user_id': 7, 'config': 'private-body',
            'trace_id': 'not-admitted'}):
        assert len(records) == 1
        assert records[0]['boundary'] == 'start'
    assert records[1]['durationMs'] == 125.0
    assert records[1]['outcome'] == 'success'
    assert records[1]['observedAtMs'] == 90000
    assert records[0]['attempt_id'] == records[1]['attempt_id'] == 'attempt'
    assert 'private-body' not in json.dumps(records)
    assert 'trace_id' not in records[0]
    assert observe.call_args.kwargs == {'stage': 'workspace_binding', 'outcome': 'success'}


def test_timing_sink_failure_preserves_base_exception(monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError('sink unavailable')
    monkeypatch.setattr(observability.logger, 'info', fail)
    monkeypatch.setattr(observability._STORE, 'observe', fail)
    failure = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt) as caught:
        with observability.measure_task_startup('context', identity={'task_id': 'task'}):
            raise failure
    assert caught.value is failure


def test_slow_workspace_admission_has_phase_before_binding_and_still_fails_closed(monkeypatch):
    order = []
    task = {'id': 'startup-task', 'convId': 'startup-conv', 'status': 'pending',
            'config': {'projectPath': '/explicit-project'}}
    monkeypatch.setattr('lib.project_dispatch.admission.validate_task', lambda task: None)
    monkeypatch.setattr('lib.agent_core.events.emit_phase',
        lambda task, phase, **fields: order.append(('phase', fields['detailKey'])))
    failure = RuntimeError('workspace admission refused')
    def bind(task):
        assert task['status'] == 'running'
        assert order == [('phase', 'stream.phase.workerStarting')]
        order.append(('binding', 'entered'))
        raise failure
    monkeypatch.setattr('lib.tasks_pkg.workspace_delivery.bind_worker_workspace', bind)
    started = threading.Event()
    with pytest.raises(RuntimeError) as caught:
        spawn._mark_worker_started(task, started)
    assert caught.value is failure
    assert not started.is_set()  # Never grant the runner access before binding.
    assert order[-1] == ('binding', 'entered')


def test_workspace_phase_retires_only_after_binding_succeeds(monkeypatch):
    phases = []
    task = {'id': 'workspace-ready', 'status': 'pending',
            'config': {'projectPath': '/explicit-project'}}
    monkeypatch.setattr('lib.project_dispatch.admission.validate_task', lambda task: None)
    monkeypatch.setattr('lib.agent_core.events.emit_phase',
        lambda task, phase, **fields: phases.append(fields['detailKey']))
    started = threading.Event()

    def bind(task):
        assert phases == ['stream.phase.workerStarting']

        from lib.tasks_pkg.workspace_delivery import _emit_workspace_preparing
        _emit_workspace_preparing(task)
        _emit_workspace_preparing(task)  # One notice even for multiple roots.
        assert not started.is_set()

    monkeypatch.setattr('lib.tasks_pkg.workspace_delivery.bind_worker_workspace', bind)
    monkeypatch.setattr('lib.conversations.project_brain.note_isolated_workspace_signal',
                        lambda *args: None)
    assert spawn._mark_worker_started(task, started)
    assert started.is_set()
    assert phases == ['stream.phase.workerStarting', 'stream.phase.workspacePreparing',
                      'stream.phase.workerStarting']


def test_no_workspace_keeps_ordinary_worker_start_phase(monkeypatch):
    phases = []
    task = {'id': 'no-workspace', 'status': 'pending', 'config': {}}
    monkeypatch.setattr('lib.project_dispatch.admission.validate_task', lambda task: None)
    monkeypatch.setattr('lib.agent_core.events.emit_phase',
        lambda task, phase, **fields: phases.append(fields['detailKey']))
    monkeypatch.setattr('lib.tasks_pkg.workspace_delivery.bind_worker_workspace',
                        Mock(side_effect=AssertionError('No workspace should be bound')))
    assert spawn._mark_worker_started(task)
    assert phases == ['stream.phase.workerStarting']


@pytest.mark.parametrize('stage', [
    'input_build', 'turn_persist', 'executor_start', 'acknowledgement',
    'input_translation', 'startup_config', 'startup_resume', 'startup_project',
    'startup_tools', 'startup_memory', 'startup_context', 'startup_history',
    'memory_list', 'memory_rank', 'memory_hydrate',


    'registration_route', 'registration_mint', 'registration_checkpoint',
])
def test_command_and_worker_startup_stages_keep_their_names(monkeypatch, stage):
    records = []
    monkeypatch.setattr(observability.logger, 'info',
                        lambda fmt, value: records.append(json.loads(value)))
    with observability.measure_task_startup(stage, identity={'task_id': 'task'}):
        pass
    assert [record['stage'] for record in records] == [stage, stage]


_IDENTITY_KEYS = ('conversation_id', 'user_id', 'turn_id', 'attempt_id', 'task_id')


def _startup_records(monkeypatch):
    records = []

    def capture(fmt, *args):
        if fmt == '[TaskStartupTiming] %s':
            records.append(json.loads(args[0]))
    monkeypatch.setattr(observability.logger, 'info', capture)
    return records


def _identity(record):
    return {key: record[key] for key in _IDENTITY_KEYS if key in record}


def test_identity_created_inside_a_stage_joins_its_completion_not_its_start(monkeypatch):
    records = _startup_records(monkeypatch)
    with observability.measure_task_startup('turn_persist', identity={
            'conversation_id': 'conv', 'user_id': 7}) as bind_identity:
        bind_identity({'turn_id': 'turn', 'attempt_id': 'attempt', 'config': 'private-body'})
        with observability.measure_task_startup('context'):
            pass
    command = {'conversation_id': 'conv', 'user_id': '7'}
    turn = {**command, 'turn_id': 'turn', 'attempt_id': 'attempt'}
    assert [(r['stage'], r['boundary'], _identity(r)) for r in records] == [
        ('turn_persist', 'start', command),
        ('context', 'start', turn), ('context', 'complete', turn),
        ('turn_persist', 'complete', turn)]
    assert 'private-body' not in json.dumps(records)


def test_blank_or_missing_identity_is_omitted_never_invented(monkeypatch):
    records = _startup_records(monkeypatch)
    with observability.measure_task_startup('startup_config', identity={
            'task_id': 'child-task', 'conversation_id': 'conv', 'turn_id': '',
            'attempt_id': '  ', 'user_id': None}) as bind_identity:
        bind_identity({'turn_id': '', 'attempt_id': None})
    assert [_identity(record) for record in records] == [
        {'conversation_id': 'conv', 'task_id': 'child-task'}] * 2
    assert 'unknown' not in json.dumps(records)


def test_a_stage_without_identity_stays_silent_after_late_identity(monkeypatch):
    records = _startup_records(monkeypatch)
    with observability.measure_task_startup('context') as bind_identity:
        bind_identity({'turn_id': 'turn'})
    assert records == []  # A completion without its start would break pairing.


def _command_service(monkeypatch, persist):
    from lib.conversation_sync import command_service as commands
    monkeypatch.setattr(commands, '_capture_task_principal_tenant',
                        lambda config, *_args: dict(config))
    monkeypatch.setattr(commands, 'create_turn_pair', persist)
    monkeypatch.setattr(commands, 'get_turn', lambda *_a, **_k: {'turnId': 'turn-output'})
    monkeypatch.setattr(commands, 'get_attempt', lambda *_a, **_k: {'attemptId': 'attempt-1'})
    monkeypatch.setattr(commands, 'get_conversation_revision', lambda *_a, **_k: 3)
    return commands.ConversationTurnCommandService(
        build_user_message=lambda *_args: {'role': 'user', 'content': 'hello'},
        was_aborted_after=lambda *_args: False,
        start_task=lambda *_args: ('', None))


def _create_turn(service):
    return service.create_turn('conv', 7, {
        'commandId': 'command', 'message': {'text': 'hello'}, 'config': {}},
        request_started_at=1)


def test_turn_command_anchors_carry_the_turn_from_its_persist_completion(monkeypatch):
    records = _startup_records(monkeypatch)
    service = _command_service(monkeypatch, lambda *_a, **_k: {
        '_needsStart': False, 'turn': {'turnId': 'turn-output'},
        'attempt': {'attemptId': 'attempt-1'}})
    _create_turn(service)
    # Input building and the persist start precede the Turn: no identity is
    # invented for them; a log join pairs them through the persist completion.
    command = {'conversation_id': 'conv', 'user_id': '7'}
    turn = {**command, 'turn_id': 'turn-output', 'attempt_id': 'attempt-1'}
    assert [(r['stage'], r['boundary'], _identity(r)) for r in records] == [
        ('input_build', 'start', command), ('input_build', 'complete', command),
        ('turn_persist', 'start', command), ('turn_persist', 'complete', turn),
        ('executor_start', 'start', turn), ('executor_start', 'complete', turn),
        ('acknowledgement', 'start', turn), ('acknowledgement', 'complete', turn)]


@pytest.mark.parametrize('failure', [
    RuntimeError('storage unavailable'),
    # The conflict names the live Turn that blocked this command, not a Turn
    # this command created.
    'lane_busy',
])
def test_a_failed_turn_persist_names_no_turn(monkeypatch, failure):
    from lib.conversation_sync.command_service import LifecycleConflict
    if failure == 'lane_busy':
        failure = LifecycleConflict('lane_busy', 'busy', {
            'turnId': 'live-turn', 'currentAttemptId': 'live-attempt'})
    records = _startup_records(monkeypatch)

    def persist(*_args, **_kwargs):
        raise failure
    with pytest.raises(type(failure)) as caught:
        _create_turn(_command_service(monkeypatch, persist))
    assert caught.value is failure
    completions = [r for r in records
                   if r['stage'] == 'turn_persist' and r['boundary'] == 'complete']
    assert [(r['outcome'], _identity(r)) for r in completions] == [
        ('error', {'conversation_id': 'conv', 'user_id': '7'})]


def test_memory_prefetch_splits_startup_memory_into_nested_stages(monkeypatch):
    from lib.memory.prefetch import run_memory_prefetch

    records = []
    monkeypatch.setattr(observability.logger, 'info',
                        lambda fmt, value: records.append(json.loads(value)))
    summary = {
        'id': 'parser-state', 'name': 'parser-state-rollback',
        'description': 'ParserState rollback rule in lib/parser/state.py',
        'tags': ['parser'], 'enabled': True, 'eligible': True,
        'is_package': False, 'scope': 'project',
    }
    monkeypatch.setattr('lib.memory.storage.get_eligible_memories',
                        lambda *args, **kwargs: [dict(summary)])
    monkeypatch.setattr('lib.memory.storage.load_eligible_memories',
                        lambda *args, **kwargs: [dict(summary, body='rule')])

    with observability.measure_task_startup(
            'startup_memory', identity={'task_id': 'task', 'turn_id': 'turn'}):
        selected = run_memory_prefetch(
            [{'role': 'user', 'content': 'fix ParserState in lib/parser/state.py'}],
            '/project', task={'id': 'task'})

    assert [row['id'] for row in selected] == ['parser-state']
    assert [(record['stage'], record['boundary']) for record in records] == [
        ('startup_memory', 'start'),
        ('memory_list', 'start'), ('memory_list', 'complete'),
        ('memory_rank', 'start'), ('memory_rank', 'complete'),
        ('memory_hydrate', 'start'), ('memory_hydrate', 'complete'),
        ('startup_memory', 'complete'),
    ]
    # Sub-stages inherit the enclosing stage's identity; IDs stay log-only.
    assert {(record['task_id'], record['turn_id']) for record in records} == {
        ('task', 'turn')}
    # Without an early listing the whole listing runs on the critical path.
    listed = records[2]
    assert listed['listing'] == 'inline'
    assert 0 <= listed['listingMs'] <= listed['durationMs']


def test_memory_list_is_the_wait_for_an_early_listing_and_reports_its_time(
        monkeypatch):
    from lib.memory.prefetch import PrefetchCorpusListing, run_memory_prefetch

    records = _startup_records(monkeypatch)
    summary = {
        'id': 'parser-state', 'name': 'parser-state-rollback',
        'description': 'ParserState rollback rule in lib/parser/state.py',
        'tags': ['parser'], 'enabled': True, 'eligible': True,
        'is_package': False, 'scope': 'project',
    }
    listing_started, finish_listing = threading.Event(), threading.Event()

    def listing_in_progress(*args, **kwargs):
        listing_started.set()
        assert finish_listing.wait(5)
        return [dict(summary)]

    monkeypatch.setattr('lib.memory.storage.get_eligible_memories',
                        listing_in_progress)
    monkeypatch.setattr('lib.memory.storage.load_eligible_memories',
                        lambda *args, **kwargs: [dict(summary, body='rule')])

    def submit(fn):
        future = Future()
        threading.Thread(target=lambda: future.set_result(fn()),
                         daemon=True).start()
        return future

    early = PrefetchCorpusListing('/project', [], submit=submit)
    assert listing_started.wait(5)
    time.sleep(0.2)  # earlier startup work the listing overlaps
    threading.Timer(0.05, finish_listing.set).start()
    with observability.measure_task_startup(
            'startup_memory', identity={'task_id': 'task'}):
        selected = run_memory_prefetch(
            [{'role': 'user', 'content': 'fix ParserState in lib/parser/state.py'}],
            '/project', task={'id': 'task'}, corpus_listing=early)

    assert [row['id'] for row in selected] == ['parser-state']
    listed = next(record for record in records
                  if record['stage'] == 'memory_list'
                  and record['boundary'] == 'complete')
    assert listed['listing'] == 'early'
    # durationMs is only the wait; the overlapped share was hidden.
    assert listed['listingMs'] - listed['durationMs'] >= 150


def test_startup_completion_details_are_closed_and_content_free(monkeypatch):
    records = _startup_records(monkeypatch)
    details = {}
    with observability.measure_task_startup(
            'memory_list', identity={'task_id': 'task'}, details=details):
        details.update(listing='early', listingMs=12.34567,
                       memoryName='private-memory-name', rows=['body'])
    start, complete = records
    assert 'listing' not in start and 'listingMs' not in start
    assert (complete['listing'], complete['listingMs']) == ('early', 12.346)
    assert 'memoryName' not in complete and 'rows' not in complete
    assert 'private-memory-name' not in json.dumps(records)


@pytest.mark.usefixtures('chat_sidecar')
def test_registration_times_route_mint_and_birth_checkpoint(monkeypatch):
    """``registration`` is split into its route, mint and checkpoint stages."""
    import lib.model_routing as routing
    from lib.storage import get_storage_client
    from lib.tasks_pkg.manager import create_task, discard_task
    from tests._seed import delete_conversation, seed_conversation

    conv_id = f'registration-stages-{uuid.uuid4().hex[:10]}'
    seed_conversation(conv_id, messages=[
        {'role': 'user', 'content': 'hi', 'timestamp': 1},
        {'role': 'assistant', 'content': 'hello', 'timestamp': 2},
    ], title='registration stages')
    stages = []
    real_observe = observability._STORE.observe

    def observe(name, value, buckets, **labels):
        if name == 'tofu_task_startup_stage_seconds':
            stages.append(labels['stage'])
        return real_observe(name, value, buckets, **labels)

    def mint(*args, observe_stage, **kwargs):
        with observe_stage('route'):
            pass
        with observe_stage('mint'):
            pass
        return SimpleNamespace(pin_id='registration-stage-pin',
                               candidates=[SimpleNamespace(offering={})])

    monkeypatch.setattr(observability._STORE, 'observe', observe)
    monkeypatch.setattr(routing, 'mint_routed_slot_group', mint)
    monkeypatch.setattr(routing, 'dispose_routed_slot_group', lambda group: True)
    ref = {'creator_id': 'creator', 'model_id': 'fixture-model'}
    task = create_task(conv_id, [{'role': 'user', 'content': 'hi'}],
                       {'modelRef': ref, 'model': 'fixture-model'},
                       user_id=1, supersede=False)
    try:
        assert stages == ['registration_route', 'registration_mint',
                          'registration_checkpoint']
    finally:
        discard_task(task['id'], conv_id)
        get_storage_client(write=True).command(
            'record.delete', {'namespace': 'task_results', 'key': task['id']},
            f'test-delete-task-result:{task["id"]}')
        delete_conversation(conv_id)
