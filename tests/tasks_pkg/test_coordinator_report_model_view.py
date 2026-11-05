"""Coordinator report Turns reach the model only through its own report call.

Production 2026-10-26 (coordinator-1a390889aa954da2b18551d1b4a32625): a report
published through ``report_to_user`` is a canonical assistant Turn ingested
after the still-running reply that called the tool. History projection replayed
each report Turn as one more assistant message after that reply, so every later
request logged "UNEXPECTED pair" (six per request) while build_body folded each
report into the reply's final text: the model read every report twice, once
out of order. The real report producer and the real Turn-native history
builder run here; only storage IO is controlled.
"""
from copy import deepcopy
import json
import logging
from types import SimpleNamespace

import pytest

from lib.conversations.coordinator_reports import CoordinatorReports
from lib.llm.body import build_body

pytestmark = pytest.mark.unit

_OWNER = 7
_MODEL = 'kimi-k3'
_FINAL = 'Both workers finished; waiting for your decision.'
_REPORT = {'kind': 'decision', 'title': 'Choose the rollout window',
           'message': 'Workers verified both options; I recommend tonight.',
           'idempotency_key': 'decision-1'}
_SANITIZE_LOGGER = 'lib.llm_sanitize._messages'


class _Records:
    """CAS records behind the producer's publication intent and unread pointer."""

    def __init__(self):
        self.rows = {}

    def __call__(self, **kwargs):
        return self

    def query(self, operation, payload, **kwargs):
        assert operation == 'record.get'
        return deepcopy(self.rows.get((payload['namespace'], payload['key'])))

    def command(self, operation, payload, command_id):
        key = (payload['namespace'], payload['key'])
        assert self.rows.get(key, {}).get('version', 0) == payload['expected_version']
        if operation == 'record.delete':
            return {'deleted': self.rows.pop(key, None) is not None}
        assert operation == 'record.put'
        self.rows[key] = {'value': deepcopy(payload['value']),
                          'version': payload['expected_version'] + 1}
        return {'version': payload['expected_version'] + 1}


@pytest.fixture
def coordinator(monkeypatch, tmp_path):
    from lib import storage, turn_lifecycle
    from lib.storage.model_replay_cache import ModelReplayCache
    from lib.tasks_pkg import model_replay

    rows = []

    class Turns:
        def query(self, operation, payload, **kwargs):
            assert payload['user_id'] == _OWNER
            if operation == 'turn.get':
                return deepcopy(next(row for row in rows
                                     if row['turnId'] == payload['turn_id']))
            return deepcopy(rows)

    def turn(turn_id, actor, projection, *, kind, status='completed'):
        rows.append({'turnId': turn_id, 'conversationId': 'coordinator',
                     'actor': actor, 'kind': kind, 'laneId': 'main',
                     'ordinal': len(rows) + 1, 'parentTurnId': '',
                     'status': status, 'projection': deepcopy(projection),
                     'createdAt': 1000 + len(rows)})
        return deepcopy(rows[-1])

    def append_turn(conversation_id, *, user_id, command_id, actor, kind, projection):
        assert (conversation_id, user_id) == ('coordinator', _OWNER)
        return {'turn': turn(f'report-{len(rows)}', actor, projection, kind=kind)}

    monkeypatch.setattr(turn_lifecycle, '_turn_client', lambda **kwargs: Turns())
    monkeypatch.setattr(storage, 'get_storage_client', lambda **kwargs: Turns())
    replay = ModelReplayCache(root=tmp_path / 'replay', max_bytes=2_000_000)
    monkeypatch.setattr(model_replay, 'ModelContinuationStore', lambda: replay)
    binding = {'availability': 'available', 'conversationId': 'coordinator', 'revision': 1}
    reports = CoordinatorReports(
        _OWNER, '/project', client=_Records(), append_turn=append_turn,
        bindings=SimpleNamespace(read=lambda path: dict(binding)),
        notify=lambda conversation_id: None)
    scope = {'owner': _OWNER, 'projectPath': '/project',
             'coordinatorId': 'coordinator', 'bindingRevision': 1}
    return SimpleNamespace(rows=rows, turn=turn, reports=reports, scope=scope)


def _config():
    return {'model': _MODEL, 'excludeLast': True}


def _build(target, config, **kwargs):
    from lib import turn_lifecycle
    return turn_lifecycle.build_api_messages(
        'coordinator', target, config, user_id=_OWNER, **kwargs)


def _wire(messages, caplog):
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=_SANITIZE_LOGGER):
        return build_body(_MODEL, deepcopy(messages), max_tokens=256)['messages']


def _settle_reply_that_reports(c, *, reported_by_model):
    """One Coordinator reply exactly as its live tool loop sent it."""
    from lib.tasks_pkg.conv_message_builder._toolcalls import (
        _reconstruct_tool_call_messages,
    )
    from lib.tasks_pkg.model_replay import publish_completed_task

    c.turn('human-1', 'human', {'content': 'Ship the storage fix.'}, kind='input')
    c.turn('reply-1', 'assistant', {}, kind='reply', status='running')
    config = _config()
    live = _build('reply-1', config)
    rounds = []
    if reported_by_model:
        # report_to_user ingests the report Turn while this reply still runs.
        published = c.reports.publish(c.scope, _REPORT)
        rounds.append({
            'toolCallId': 'call-report', 'toolName': 'report_to_user',
            'toolArgs': json.dumps(_REPORT), 'toolContent': json.dumps(published),
            'llmRound': 1, 'roundNum': 1, 'status': 'done',
            'taskId': 'coordinator-task', 'assistantContent': ''})
        live.extend(_reconstruct_tool_call_messages(rounds))
    live.append({'role': 'assistant', 'content': _FINAL})
    c.rows[1].update(status='completed',
                     projection={'content': _FINAL, 'toolRounds': rounds})
    if not reported_by_model:
        # The stall stop publishes after settlement without a model request.
        c.reports.publish(c.scope, _REPORT)
    task = {'_userId': _OWNER, 'convId': 'coordinator', '_turnId': 'reply-1',
            'model': _MODEL, 'config': config, 'messages': live, 'content': _FINAL}
    publish_completed_task(task)
    assert task['_modelReplayPublishStatus'] == 'stored'
    c.turn('human-2', 'human', {'content': 'Go with tonight.'}, kind='input')
    c.turn('reply-2', 'assistant', {}, kind='reply', status='running')
    return live


@pytest.mark.parametrize('reported_by_model', [True, False],
                         ids=['report_to_user', 'platform_stall_stop'])
def test_report_turn_is_not_replayed_as_an_adjacent_assistant_message(
        coordinator, caplog, reported_by_model):
    c = coordinator
    live = _settle_reply_that_reports(c, reported_by_model=reported_by_model)
    report_text = _REPORT['title'] + '\n\n' + _REPORT['message']
    assert [(row['actor'], row['kind']) for row in c.rows] == [
        ('human', 'input'), ('assistant', 'reply'),
        ('assistant', 'coordinator_report.decision'),
        ('human', 'input'), ('assistant', 'reply')]
    assert c.rows[2]['projection']['content'] == report_text

    cold_config = _config()
    cold = _build('reply-2', cold_config, allow_model_replay=False)
    cold_wire = _wire(cold, caplog)
    assert 'UNEXPECTED' not in caplog.text
    restored_config = _config()
    restored = _build('reply-2', restored_config)
    assert restored_config['_modelReplayStatus'] == 'restored'
    restored_wire = _wire(restored, caplog)
    assert 'UNEXPECTED' not in caplog.text

    # The cold rebuild is exactly what the model saw live plus the new request.
    assert cold_wire == restored_wire == _wire(
        live + [{'role': 'user', 'content': 'Go with tonight.'}], caplog)
    assert [m['role'] for m in cold_wire] == (
        ['user', 'assistant', 'tool', 'assistant', 'user'] if reported_by_model
        else ['user', 'assistant', 'user'])
    assert cold_wire[-2]['content'] == _FINAL
    # A model-authored report stays visible once, as its own call arguments.
    assert json.dumps(cold_wire, ensure_ascii=False).count(_REPORT['message']) == (
        1 if reported_by_model else 0)


def test_other_ingested_assistant_turns_still_alarm_the_last_resort_merge(
        coordinator, caplog):
    c = coordinator
    c.turn('human-1', 'human', {'content': 'Ship the storage fix.'}, kind='input')
    c.turn('reply-1', 'assistant', {'content': _FINAL}, kind='reply')
    c.turn('import-1', 'assistant', {'content': 'Imported note.'}, kind='ingested')
    c.turn('human-2', 'human', {'content': 'Go with tonight.'}, kind='input')
    c.turn('reply-2', 'assistant', {}, kind='reply', status='running')

    wire = _wire(_build('reply-2', _config(), allow_model_replay=False), caplog)

    assert 'UNEXPECTED' in caplog.text
    assert wire[1]['content'] == _FINAL + '\n\nImported note.'
