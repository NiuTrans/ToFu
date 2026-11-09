"""get_conversation_status reads any conversation within bounded storage responses.

A long conversation's whole history exceeds the authority's per-response bound
(``tests/support/response_bound.py``) while every single Turn fits. The status
tool must still name one Turn exactly, show the newest Turns that the model can
read inline, and page older Turns through its cursor. Both authorities run.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from tests.support.response_bound import BoundedReads
from tests.support.semantic_authorities import semantic_authority as semantic_authority

pytest_plugins = ('tests.support.postgres_cluster',)
# Native runs when TOFUDB_BIN names a source-matched engine, and skips otherwise.
pytestmark = [pytest.mark.unit,
              pytest.mark.parametrize('semantic_authority', ['sqlite', 'native'], indirect=True)]

# One report fits the bound; four of them exceed any whole-history read.
_LARGE_REPORT = 'Milestone evidence. ' * 1600
_REPORTS = 72


@pytest.fixture
def session(semantic_authority, monkeypatch, tmp_path):
    from lib.conversation_sync.repository import SidecarConversationSyncRepository
    from lib.conversation_sync.service import ConversationSyncService
    from lib.storage.errors import StorageError
    from lib.turn_lifecycle import append_settled_turn, bind_task, create_turn_pair, get_turn
    from lib.router import policy
    from lib.router.watchdog import binding

    client, owner, _engine = semantic_authority
    monkeypatch.setattr('lib.storage.get_storage_client', client)
    monkeypatch.setattr(policy, 'is_router_scope', lambda scope: False)
    monkeypatch.setattr(binding, 'is_watchdog_scope', lambda scope: False)
    conversation = 'managed-session'
    pair = create_turn_pair(conversation, command_id='first-request',
                            input_projection={'content': 'Run the long migration'}, config={},
                            user_id=owner, conversation_defaults={
                                'allowCreate': True, 'title': 'Managed session',
                                'settings': {'projectPath': str(tmp_path)}})
    bind_task(pair['attempt']['attemptId'], 'managed-task', user_id=owner)
    for index in range(_REPORTS):
        large = index % 18 == 5
        append_settled_turn(conversation, command_id=f'report-{index}', actor='assistant',
                            user_id=owner, kind='coordinator_report.progress',
                            projection={'content': _LARGE_REPORT if large else f'Step {index} verified',
                                        'origin': {'initiator': 'brain'}})
    lane = ConversationSyncService(SidecarConversationSyncRepository(client)).snapshot(conversation, owner)['turns']
    assert len(lane) == _REPORTS + 2
    bounded = lambda **kwargs: BoundedReads(client(**kwargs))  # noqa: E731
    monkeypatch.setattr('lib.storage.get_storage_client', bounded)
    monkeypatch.setattr('lib.conversation_sync.repository.SidecarConversationSyncRepository',
                        lambda client_factory=None: SidecarConversationSyncRepository(bounded))
    # The precondition this file is about: no whole-history read fits the bound.
    with pytest.raises(StorageError, match='bounded streamed payload'):
        ConversationSyncService(SidecarConversationSyncRepository(bounded)).snapshot(conversation, owner)
    reply = get_turn(conversation, pair['turn']['turnId'], user_id=owner, include_projection=False)
    scope = {'user_id': owner, 'project_path': str(tmp_path), 'conversation_id': 'coordinator',
             'binding_revision': 1}
    task = {'id': 'coordinator-task', 'convId': 'coordinator', '_userId': owner, 'model': ''}
    return SimpleNamespace(owner=owner, conversation=conversation, reply=reply, scope=scope, task=task,
                           lane=[turn['turnId'] for turn in lane])


def _status(s, **arguments):
    from lib.conversations.session_control import execute_session_tool
    text = execute_session_tool(s.task, s.scope, 'get_conversation_status',
                                {'conversation_id': s.conversation, **arguments})
    return text, json.loads(text)


def _reaches_model_inline(s, text):
    """The pipeline delivers the result itself, never an artifact pointer."""
    from lib.tasks_pkg.compaction.api import budget_tool_result_v2
    view = json.loads(budget_tool_result_v2('get_conversation_status', text,
                                            user_id=s.owner, model=s.task['model']))
    return view.get('status') == 'ok' and not view.get('truncated') and not view.get('artifactRef')


def test_a_turn_is_read_exactly_when_the_whole_history_exceeds_the_response_bound(session):
    s = session
    text, status = _status(s, turn_id=s.reply['turnId'])
    assert 'error' not in status, status
    assert [turn['turnId'] for turn in status['turns']] == [s.reply['turnId']]
    [turn] = status['turns']
    assert turn['currentAttemptId'] == s.reply['currentAttemptId']
    assert turn['projectionRevision'] == s.reply['projectionRevision']
    assert turn['settlement'] == s.reply['settlement']
    assert [attempt['attemptId'] for attempt in status['attempts']] == [s.reply['currentAttemptId']]
    assert status['attempts'][0]['status'] in {'pending', 'running'}
    assert 'config' not in status['attempts'][0]
    assert 'window' not in status
    assert _reaches_model_inline(s, text)


def test_missing_turn_is_a_typed_refusal_not_a_storage_failure(session):
    _, status = _status(session, turn_id='missing-turn')
    assert status['code'] != 'database_resource_exhausted'
    assert 'not found' in status['error'].lower()
    assert status['execution_started'] is False


def test_recent_window_fits_the_model_and_its_cursor_reaches_every_older_turn(session):
    s = session
    shown, pages, before = [], 0, None
    while True:
        arguments = {} if before is None else {'before_ordinal': before}
        text, status = _status(s, **arguments)
        assert 'error' not in status, status
        assert _reaches_model_inline(s, text)
        window = status['window']
        assert window['lane_id'] == 'main' and window['total_turns'] == len(s.lane)
        ids = [turn['turnId'] for turn in status['turns']]
        assert ids, status
        current = {turn['currentAttemptId'] for turn in status['turns'] if turn.get('currentAttemptId')}
        assert {attempt['attemptId'] for attempt in status['attempts']} == current
        if pages == 0:
            assert ids[-1] == s.lane[-1]  # The newest Turn is always in the first window.
        shown = ids + shown
        pages += 1
        before = window['next_before_ordinal']
        if before is None:
            break
    assert pages > 1
    assert shown == s.lane  # Every Turn exactly once, in lane order.
