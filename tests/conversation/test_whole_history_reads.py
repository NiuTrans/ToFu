"""Whole-history reads survive a transcript above every single storage response.

Native TofuDB refuses a whole-conversation read once its projections exceed the
response bound (``database_resource_exhausted``; the SQL Sidecar's frame limit
is the same signal). The full conversation read behind the conversation GET,
preview and activity projections then reads the transcript back in message
windows at one revision, and the auto-translate recovery sweep walks Turns one
bounded page at a time across every lane. Other storage failures keep their
classification.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from tests.support.response_bound import BoundedReads
from tests.support.semantic_authorities import semantic_authority as semantic_authority

pytest_plugins = ('tests.support.postgres_cluster',)
# Native runs when TOFUDB_BIN names a source-matched engine, and skips otherwise.
pytestmark = [pytest.mark.unit,
              pytest.mark.parametrize('semantic_authority', ['sqlite', 'native'], indirect=True)]

_LARGE_REPORT_TEXT = 'Milestone evidence. ' * 1600


def _newest_first(rows):
    return sorted(rows, key=lambda row: row['ordinal'], reverse=True)


@pytest.fixture
def oversized(semantic_authority, monkeypatch):
    """A conversation with a branch lane whose whole history exceeds the bound."""
    from lib.conversations.repository import ConversationRepository
    from lib.turn_lifecycle import (
        append_settled_turn,
        create_branch_lane,
        create_turn_pair,
        get_turn,
        list_turns,
    )
    client, owner, _engine = semantic_authority
    monkeypatch.setattr('lib.storage.get_storage_client', client)
    conversation = 'whole-history-oversized'
    create_turn_pair(conversation, command_id='human-request',
                     input_projection={'content': 'Report the milestones'}, config={},
                     user_id=owner, conversation_defaults={'allowCreate': True})
    reports = [
        append_settled_turn(conversation, command_id=f'large-report-{index}', actor='assistant',
                            user_id=owner, kind='coordinator_report.progress',
                            projection={'content': _LARGE_REPORT_TEXT,
                                        'origin': {'initiator': 'brain'}})['turn']
        for index in range(4)
    ]
    parent = reports[0]
    create_branch_lane(conversation, parent['turnId'], title='Side question',
                       expected_projection_revision=parent['projectionRevision'],
                       user_id=owner)
    [branch] = get_turn(conversation, parent['turnId'], user_id=owner)['projection']['_branchLanes']
    append_settled_turn(conversation, command_id='branch-report', actor='assistant',
                        user_id=owner, kind='coordinator_report.progress',
                        lane_id=branch['laneId'],
                        projection={'content': 'Branch evidence for the side question.',
                                    'origin': {'initiator': 'brain'}})
    walk = [
        row
        for lane_id in ('main', branch['laneId'])
        for row in _newest_first(
            list_turns(conversation, user_id=owner, lane_id=lane_id)['turns'])
    ]
    assert {row['laneId'] for row in walk} == {'main', branch['laneId']}
    complete = ConversationRepository().get(conversation, user_id=owner)
    refusals: list[str] = []

    def bounded(**kwargs):
        return BoundedReads(client(**kwargs), refusals)

    monkeypatch.setattr('lib.storage.get_storage_client', bounded)
    monkeypatch.setattr('routes.conversations.get_storage_client', bounded)
    return SimpleNamespace(owner=owner, conversation=conversation, walk=walk,
                           complete=complete, refusals=refusals, client=client)


def test_the_turn_walk_reads_every_lane_newest_first_above_the_bound(oversized):
    from lib.storage.errors import StorageError
    from lib.turn_lifecycle import _LIGHT_PROJECTION_KEYS, iter_turns_newest_first, list_turns

    with pytest.raises(StorageError, match='bounded streamed payload'):
        list_turns(oversized.conversation, user_id=oversized.owner, light=True)
    rows = list(iter_turns_newest_first(
        oversized.conversation, user_id=oversized.owner, light=True))
    assert [row['turnId'] for row in rows] == [row['turnId'] for row in oversized.walk]
    assert all(set(row['projection']) <= set(_LIGHT_PROJECTION_KEYS) for row in rows)


def test_the_translation_sweep_recovers_turns_above_the_bound(oversized, monkeypatch):
    import lib.translate.terminal as terminal

    spawned = []
    monkeypatch.setattr(terminal, '_spawn_whole_turn_translation',
                        lambda **kw: spawned.append(kw['turn_id']) is None)
    count = terminal.sweep_conversation_translations(
        oversized.conversation, user_id=oversized.owner,
        config={'autoTranslate': True, 'uiLang': 'zh'}, max_respawn=100)
    settled_replies = [row['turnId'] for row in oversized.walk
                       if row['actor'] != 'human' and row['status'] == 'completed']
    assert len(settled_replies) == 5
    assert spawned == settled_replies
    assert count == len(spawned)


def test_the_full_conversation_read_returns_every_message_above_the_bound(oversized):
    from routes.conversations import _get_document

    document = asyncio.run(_get_document(oversized.conversation, oversized.owner))
    assert 'conversation.get' in oversized.refusals
    assert document['messages'] == oversized.complete.messages
    for key in ('id', 'rev', 'msg_count'):
        assert document['metadata'][key] == oversized.complete.metadata[key]


def test_a_write_between_windows_fails_the_full_read_as_a_conflict(oversized):
    from lib.conversations.repository import ConversationRepository
    from lib.storage.errors import StorageError
    from lib.turn_lifecycle import append_settled_turn

    windows = []

    class WriteAfterFirstWindow(BoundedReads):
        def query(self, operation, payload, *args, **kwargs):
            result = super().query(operation, payload, *args, **kwargs)
            # The tail probe is the only window read without a cursor.
            if operation == 'conversation.get' and 'before_sequence' in payload:
                windows.append(payload)
                if len(windows) == 1:
                    append_settled_turn(
                        oversized.conversation, command_id='concurrent-report',
                        actor='assistant', user_id=oversized.owner,
                        kind='coordinator_report.progress',
                        projection={'content': 'Late evidence.',
                                    'origin': {'initiator': 'brain'}})
            return result

    repository = ConversationRepository(
        client_factory=lambda **kwargs: WriteAfterFirstWindow(oversized.client(**kwargs)))
    with pytest.raises(StorageError) as conflict:
        repository.get_complete(oversized.conversation, user_id=oversized.owner)
    assert conflict.value.code == 'database_conflict'
    assert len(windows) == 2


def test_other_storage_failures_keep_their_classification(oversized):
    from lib.conversations.repository import ConversationRepository
    from lib.storage.errors import StorageError

    operations = []

    class Restarting:
        def query(self, operation, payload, *args, **kwargs):
            operations.append(operation)
            raise StorageError('database_unavailable', 'authority restarting', retryable=True)

    repository = ConversationRepository(client_factory=lambda **kwargs: Restarting())
    with pytest.raises(StorageError) as failure:
        repository.get_complete(oversized.conversation, user_id=oversized.owner)
    assert failure.value.code == 'database_unavailable'
    assert operations == ['conversation.get']
