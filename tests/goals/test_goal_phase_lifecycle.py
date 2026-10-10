"""Synthetic Goal phases: durable verdicts and fail-closed admission."""
from copy import deepcopy

import pytest

from lib.storage import StorageError, StorageSupervisor
from lib.orchestration.chat_flow_adapter import FlowEventAdapter
from lib.orchestration.chat_live_turn import begin_live_flow_turn
from lib.orchestration.visible_identity import visible_phase_identity
from lib.turn_lifecycle import create_turn_pair, get_turn, sync_visible_run_turns

pytestmark = pytest.mark.unit


@pytest.fixture
def goal(tmp_path, monkeypatch):
    monkeypatch.setenv('TOFU_STORAGE_SQLITE_READ_POOL', '2')
    monkeypatch.setenv('TOFU_STORAGE_RPC_CAPACITY', '8')
    supervisor = StorageSupervisor(project_root=tmp_path, backend='sqlite', startup_timeout=60)
    supervisor.start()
    client = supervisor.client
    monkeypatch.setattr('lib.storage.get_storage_client', lambda write=False: client)
    try:
        created = create_turn_pair(
            'phase-spec', command_id='phase-spec-create', user_id=1,
            input_projection={'content': 'Synthetic task'}, config={},
            conversation_defaults={'allowCreate': True, 'title': 'Phase spec',
                                   'createdAt': 1, 'settings': {}})
        task = {'id': 'phase-spec-task', '_userId': 1, 'convId': 'phase-spec',
                '_turnId': created['turn']['turnId'],
                '_attemptId': created['attempt']['attemptId'],
                'config': {}, 'content': 'old buffer', 'thinking': 'old reasoning'}
        client.command('turn.attempt.bind', {'attempt_id': task['_attemptId'],
                       'user_id': 1, 'task_id': task['id']}, 'phase-spec-bind')
        task['_flow_turns'] = [{'role': 'assistant', 'content': 'First phase',
                                'finishReason': 'stop'}]
        sync_visible_run_turns(task, task['_flow_turns'])
        yield task, client
    finally:
        supervisor.stop()


PHASE = {'type': 'flow_iteration', 'flowProjection': 'autopilot', 'iteration': 1,
         'phase': 'reviewing', 'turnRole': 'virtual_user', 'emits': 'user'}


@pytest.mark.parametrize('status,finish,outcome', [
    ('cancelled', 'aborted', 'interrupted'), ('aborted', 'aborted', 'interrupted'),
    ('stalled', 'incomplete', 'truncated'), ('failed', 'error', 'failed'),
    ('completed', 'stop', 'completed'),
])
@pytest.mark.parametrize('explicit_finish', [False, True])
def test_child_terminal_evidence_is_durable(goal, monkeypatch, status, finish, outcome,
                                           explicit_finish):
    task, client = goal
    begin_live_flow_turn(task, PHASE)
    child_id = task['_flowLiveTurn']['turnId']
    canonical = {'content': 'Full verification history', 'thinking': 'Full reasoning',
                 'toolRounds': [], 'segments': [
                     {'type': 'text', 'blockId': 'verification-text',
                      'text': 'Full verification history'}]}
    client.command('turn.event.record', {
        'attempt_id': task['_flowLiveTurn']['attemptId'], 'user_id': 1,
        'flow_root_attempt_id': task['_attemptId'], 'projection': canonical,
    }, 'phase-spec-live-history')
    if explicit_finish:
        canonical['finishReason'] = finish
    def forbidden_preview(*args):
        raise AssertionError('Canonical history must not inspect tool previews')
    monkeypatch.setattr('lib.orchestration.chat_flow_adapter.project_flow_tool_rounds',
                        forbidden_preview)
    monkeypatch.setattr('lib.orchestration.chat_flow_adapter.project_flow_modified_files',
                        forbidden_preview)
    adapter = FlowEventAdapter(projection='autopilot')
    adapter.on_event({'type': 'step_complete', 'role': 'virtual_user', 'emits': 'user',
                      'status': status, 'preview': 'wrong preview', 'output': 'wrong output',
                      'thinking': 'wrong reasoning', 'outputProjection': canonical})
    message = adapter.messages[0]
    assert message['finishReason'] == finish
    assert message['content'] == canonical['content']
    assert message['thinking'] == canonical['thinking']
    assert message['segments'] == canonical['segments']
    task['_flow_turns'].append(message)
    sync_visible_run_turns(task, task['_flow_turns'])
    settled = get_turn(task['convId'], child_id, user_id=1)
    assert settled['status'] == outcome
    assert settled['settlement']['providerFinishReason'] == finish
    assert settled['projection']['content'] == canonical['content']
    assert settled['projection']['segments'][0]['blockId'] == 'verification-text'
    snapshot = client.query('turn.sync.snapshot', {'conversation_id': task['convId'], 'user_id': 1})
    reloaded = next(row for row in snapshot['turns'] if row['turnId'] == child_id)
    assert reloaded['status'] == outcome
    assert reloaded['settlement'] == settled['settlement']
    assert reloaded['projection'] == settled['projection']
    # A repeated phase receipt is an idempotent ACK for every terminal verdict,
    # not just completed phases. Normalization must not manufacture a conflict.
    client.command('turn.visible.sync', {
        'conversation_id': task['convId'], 'user_id': 1,
        'attempt_id': task['_attemptId'], 'root_turn_id': task['_turnId'],
        'messages': [message], 'start_index': 1,
    }, 'phase-spec-terminal-replay')
    assert get_turn(task['convId'], child_id, user_id=1) == settled


@pytest.mark.parametrize('failure', ['before_commit', 'lost_ack', 'fenced'])
def test_admission_failure_preserves_carrier_and_blocks_dispatch(goal, monkeypatch, failure):
    task, client = goal
    carrier = {'turnId': task['_turnId'], 'attemptId': task['_attemptId'],
               'projectionState': {'old': 'baseline'}}
    task['_flowLiveTurn'] = carrier
    before = deepcopy(task)
    dispatched = []
    class FaultClient:
        def command(self, operation, payload, key):
            if failure == 'lost_ack':
                client.command(operation, payload, key)
            if failure == 'fenced':
                return None
            raise StorageError('database_unavailable', 'injected admission failure')
    monkeypatch.setattr('lib.storage.get_storage_client', lambda write=False: FaultClient())
    with pytest.raises(StorageError):
        begin_live_flow_turn(task, PHASE)
        dispatched.append('provider/tool dispatch')
    assert task == before
    assert task['_flowLiveTurn'] is carrier
    assert dispatched == []
    monkeypatch.setattr('lib.storage.get_storage_client', lambda write=False: client)
    begin_live_flow_turn(task, PHASE)
    expected_id, expected_attempt = visible_phase_identity(task['_turnId'], task['_attemptId'], 1)
    assert task['_flowLiveTurn'] == {'turnId': expected_id, 'attemptId': expected_attempt}
    snapshot = client.query('turn.sync.snapshot', {'conversation_id': task['convId'], 'user_id': 1})
    assert len([row for row in snapshot['turns'] if row['turnId'] == expected_id]) == 1
    assert get_turn(task['convId'], expected_id, user_id=1)['status'] == 'running'


def test_terminal_root_cannot_admit_first_provider_dispatch(goal):
    task, client = goal
    task['_flow_turns'] = []
    client.command('turn.event.record', {
        'attempt_id': task['_attemptId'], 'user_id': 1, 'terminal': True,
        'status': 'interrupted', 'projection': {'content': 'Stopped'},
        'settlement': {'outcome': 'interrupted', 'cause': 'user_abort', 'resumeOptions': []},
    }, 'phase-spec-stop')
    before = deepcopy(task)
    with pytest.raises(StorageError):
        begin_live_flow_turn(task, PHASE)
    assert task == before
