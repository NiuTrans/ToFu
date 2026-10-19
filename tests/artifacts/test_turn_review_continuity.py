"""Accepted continuation scope preserves owner judgments, never execution grants."""
import copy
import time
from types import SimpleNamespace

import pytest

from lib.acceptance import continuity, turns
from lib.conversation_sync.command_service import ConversationTurnCommandService
from lib.conversation_sync.execution_identity import REVIEW_SOURCE_CONFIG_KEY as KEY
from lib.turn_lifecycle import LifecycleNotFound
from tests.artifacts.test_turn_review import (
    subject as subject, storage as storage, turn_service as turn_service,
    PRINCIPAL, configure, decide, finish, read)

pytestmark = pytest.mark.unit


def test_continuation_and_retry_share_current_requirements_but_not_answer_acceptance(subject, storage, monkeypatch, tmp_path):
    finish(subject); configure(subject); decide(subject); decide(subject, index=1)
    source = continuity.capture_review_source('turn-review', subject.turn_id,
        subject.task['_attemptId'], subject.task['id'], principal=PRINCIPAL)
    created = subject.service.create_turn_pair('turn-review', command_id='continuation',
        input_projection={'content': 'Continue'}, config={KEY: source}, user_id=1)
    attempt = created['attempt']['attemptId']
    subject.service.bind_task(attempt, 'continued-task', user_id=1)
    task = {**subject.task, 'id': 'continued-task', '_turnId': created['turn']['turnId'],
            '_attemptId': attempt, 'status': 'running', 'config': {KEY: source}}
    continued = SimpleNamespace(turn_id=task['_turnId'], task=task,
        artifact=subject.artifact, service=subject.service)
    inherited = read(continued)
    assert [row['state'] for row in inherited['criteria']] == ['stale', 'pass']
    assert inherited['status'] != 'accepted'
    configure(subject, criteria=[{'id': 'answer', 'kind': 'requirement', 'text': 'Use east'},
                                {'id': 'report', 'kind': 'requirement', 'text': 'Report totals are correct'}])
    from collections import Counter
    import json
    calls, elapsed = Counter(), []
    query = storage.client.query
    def measured(operation, *args, **kwargs):
        calls[operation] += 1
        return query(operation, *args, **kwargs)
    with monkeypatch.context() as measure:
        measure.setattr(storage.client, 'query', measured)
        for _ in range(3):
            start = time.perf_counter()
            requirements = turns.read_execution_requirements('turn-review', continued.turn_id,
                attempt, task['id'], principal=PRINCIPAL)
            elapsed.append((time.perf_counter() - start) * 1000)
    assert calls == {'turn.attempt.get': 6, 'turn.get': 6, 'record.get': 3}
    (tmp_path / 'review-continuity-measurement.json').write_text(json.dumps({
        'reads': 3, 'elapsed_ms': elapsed, 'queries': dict(calls),
        'source_json_bytes': len(json.dumps(source, separators=(',', ':')).encode()),
    }, indent=2))
    assert requirements['criteria'][0]['text'] == 'Use east'
    assert continuity.capture_review_source('turn-review', continued.turn_id,
        attempt, task['id'], principal=PRINCIPAL) == source
    finish(continued)
    assert decide(continued)['status'] == 'accepted'

    command = ConversationTurnCommandService(build_user_message=lambda *a, **k: {},
        was_aborted_after=lambda *a, **k: False, start_task=lambda *a, **k: None)
    monkeypatch.setattr(command, '_start_accepted_attempt', lambda *a, **k: None)
    current = subject.service.get_turn('turn-review', continued.turn_id, user_id=1)
    outcome = command.create_attempt('turn-review', continued.turn_id, 1, {
        'commandId': 'retry-continuation', 'operation': 'regenerate',
        'expectedProjectionRevision': current['projectionRevision'],
        'config': {KEY: {**source, 'turn': 'forged'}, 'model': 'gpt-4o'}},
        request_started_at=time.time(), principal=PRINCIPAL)
    retried = subject.service.get_attempt(outcome.value['attempt']['attemptId'],
        user_id=1, include_execution_config=True)
    assert retried['config'][KEY] == source
    assert KEY not in outcome.value['attempt'].get('config', {})
    assert [row['state'] for row in read(continued)['criteria']] == ['stale', 'pass']
    storage.stop(); storage.start()
    assert turns.read_requirements('turn-review', continued.turn_id, principal=PRINCIPAL) == requirements


@pytest.fixture
def lineage(monkeypatch):
    origin = dict(turnId='original', conversationId='conversation', currentAttemptId='origin-attempt',
                  actor='assistant', laneId='main')
    current = {**origin, 'turnId': 'continued', 'currentAttemptId': 'current-attempt'}
    source = dict(version=1, owner=1, tenant='', conversation='conversation',
                  turn='original', attempt='origin-attempt', task='origin-task')
    attempts = {
        'origin-attempt': dict(attemptId='origin-attempt', turnId='original', conversationId='conversation',
                               taskId='origin-task', config={}),
        'current-attempt': dict(attemptId='current-attempt', turnId='continued', conversationId='conversation',
                                taskId='current-task', config={KEY: source}),
    }
    def get_turn(conversation_id, turn_id, *, user_id):
        assert conversation_id == 'conversation' and user_id == 1
        if turn_id != 'original': raise LifecycleNotFound('Unavailable')
        return copy.deepcopy(origin)
    def get_attempt(attempt_id, *, user_id, include_execution_config):
        assert user_id == 1 and include_execution_config is True
        return copy.deepcopy(attempts[attempt_id])
    monkeypatch.setattr(continuity, 'get_turn', get_turn)
    monkeypatch.setattr(continuity, 'get_attempt', get_attempt)
    return SimpleNamespace(origin=origin, current=current, source=source, attempts=attempts)


@pytest.mark.parametrize('field,value', [
    ('owner', 2), ('owner', True), ('tenant', 'other'), ('conversation', 'other'),
    ('turn', 'continued'), ('attempt', 'superseded'), ('task', 'other'),
    ('version', True), ('extra', 'unrecognized'),
])
def test_invalid_scope_never_falls_back_to_empty_requirements(lineage, field, value):
    lineage.source[field] = value
    with pytest.raises(LifecycleNotFound):
        continuity.review_source(lineage.current, PRINCIPAL)


@pytest.mark.parametrize('change', ['origin_retry', 'nested', 'current_tenant', 'origin_tenant', 'foreign_attempt'])
def test_stale_or_nested_scope_is_refused(lineage, change):
    if change == 'origin_retry': lineage.origin['currentAttemptId'] = 'new-attempt'
    if change == 'nested': lineage.attempts['origin-attempt']['config'][KEY] = lineage.source
    if change == 'current_tenant': lineage.attempts['current-attempt']['config']['_turnPrincipalTenantId'] = 'other'
    if change == 'origin_tenant': lineage.attempts['origin-attempt']['config']['_turnPrincipalTenantId'] = 'other'
    if change == 'foreign_attempt': lineage.attempts['current-attempt']['turnId'] = 'foreign'
    with pytest.raises(LifecycleNotFound):
        continuity.review_source(lineage.current, PRINCIPAL)


def test_flat_source_lookup_is_bounded_independent_of_continuation_count(lineage, monkeypatch):
    calls = []
    for name in ('get_turn', 'get_attempt'):
        original = getattr(continuity, name)
        def record(*args, _name=name, _original=original, **kwargs):
            calls.append(_name)
            return _original(*args, **kwargs)
        monkeypatch.setattr(continuity, name, record)
    assert continuity.review_source(lineage.current, PRINCIPAL) == lineage.source
    assert calls == ['get_attempt', 'get_turn', 'get_attempt']
