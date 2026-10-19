"""Real producer requests consumed by the native single-event frame owner.

Only the storage transport and clock are substituted. The checked-in fixture
is also executed by Rust through admission/compiler/receipt/outbox, so an unused
internal API cannot masquerade as production coverage.
"""
import copy
import json
from pathlib import Path

import pytest

from lib import turn_lifecycle as lifecycle
from lib.identity import PrincipalContext

pytestmark = pytest.mark.unit
FIXTURE = Path(__file__).parents[1] / 'fixtures' / 'execution_frame_producer.json'


def capture_producer(monkeypatch, prefix='before'):
    task = {
        'id': 'execution-frame-task', 'convId': 'execution-frame-conversation',
        '_turnId': 'execution-frame-turn', '_attemptId': 'execution-frame-attempt',
        '_userId': 11, 'status': 'running', 'content': prefix, 'thinking': '',
        'toolRounds': [], 'segments': [], 'model': 'gpt-4o', 'config': {'model': 'gpt-4o'},
        '_principalContext': PrincipalContext.user(
            subject_id='test-user:11', owner_user_id=11).to_payload(),
    }
    event = {'type': 'phase', 'phase': 'working'}
    projection = lifecycle._task_projection(task, {}, event)
    lifecycle._remember_task_projection_state(
        task, attempt_id=task['_attemptId'], user_id=11,
        turn={'conversationId': task['convId'], 'turnId': task['_turnId'], 'actor': 'assistant'},
        # append pending + bind lifecycle + first running event = revision 3
        projection=copy.deepcopy(projection), projection_revision=3)
    calls = []

    class Transport:
        def command(self, operation, payload, command_id, **kwargs):
            calls.append(copy.deepcopy(dict(operation=operation, payload=payload, command_id=command_id)))
            return {'applied': True, 'projection_revision': payload['projection_patch']['targetRevision'],
                    'task_event': payload['task_event']}

    monkeypatch.setattr(lifecycle, '_turn_client', lambda **kwargs: Transport())
    clock = iter([2000, 2001])
    monkeypatch.setattr(lifecycle, '_now_ms', lambda: next(clock))
    for sequence in range(2):
        if sequence:
            task['_fallback_model'] = 'alternate-model'
        assert lifecycle.record_task_event(task, event, task_event={
            'task_id': task['id'], 'sequence': sequence, 'event': event}) == 'carried'
    return {'projection': projection, 'calls': calls}


@pytest.mark.parametrize('prefix', ['before', 's' * (256 * 1024)], ids=['small', 'long'])
def test_real_phase_producer_matches_native_compiler_fixture(monkeypatch, prefix):
    captured = capture_producer(monkeypatch, prefix)
    expected = json.loads(FIXTURE.read_text())
    expected['projection']['content'] = prefix
    expected['projection']['segments'][0]['text'] = prefix
    assert captured == expected
    assert captured['calls'][0]['payload']['projection_patch']['operations'] == []
    assert captured['calls'][1]['payload']['projection_patch']['operations'] == [
        {'op': 'set', 'path': ['fallbackModel'], 'value': 'alternate-model'}]
