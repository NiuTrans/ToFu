"""Detached snapshots read origins or bounded pages, never full history."""

import copy
import json

import pytest

from lib.swarm.snapshot import persist_snapshot_to_conversation

pytestmark = pytest.mark.unit


def turn(turn_id, agent_id, *, status='completed'):
    return {'turnId': turn_id, 'status': status, 'projectionRevision': 3,
            'projection': {'toolRounds': [{
                'toolName': 'spawn_agents', 'toolContent': json.dumps({
                    'swarm_id': 'run', 'agents': [{'id': agent_id}],
                }),
            }]}}


def snapshot(*agent_ids):
    return {'swarm_id': 'run', 'version': 100001, 'settled': True,
            'agents': [{'id': value, 'status': 'done'} for value in agent_ids]}


def install_writes(monkeypatch):
    writes = []
    def write(conv_id, turn_id, **kwargs):
        assert conv_id == 'conv'
        assert kwargs['user_id'] == 9
        writes.append((turn_id, kwargs['projection']))
        return {'conversationRevision': 10}
    monkeypatch.setattr('lib.turn_lifecycle.update_turn_projection', write)
    monkeypatch.setattr('lib.conversations.notify_conv_changed', lambda *a, **k: None)
    return writes


def test_origins_skip_history_even_for_detached_branch_waves(monkeypatch):
    rows = {'branch-a': turn('branch-a', 'a'), 'branch-b': turn('branch-b', 'b')}
    reads = []
    def read(conv_id, turn_id, *, user_id):
        assert conv_id == 'conv' and user_id == 9
        reads.append(turn_id)
        return copy.deepcopy(rows[turn_id])
    monkeypatch.setattr('lib.turn_lifecycle.get_turn', read)
    def forbidden(*a, **k):
        pytest.fail('known origins must not request conversation history')
    monkeypatch.setattr(
        'lib.conversation_sync.repository.SidecarConversationSyncRepository.snapshot', forbidden)
    writes = install_writes(monkeypatch)
    assert persist_snapshot_to_conversation(
        'conv', ['a', 'b'], snapshot('a', 'b'), user_id='9',
        turn_ids=('branch-a', 'branch-b', 'branch-a'))
    assert reads == ['branch-a', 'branch-b']
    assert {value[0] for value in writes} == set(rows)
    assert '_swarmSnapshot' not in rows['branch-a']['projection']['toolRounds'][0]


def test_unknown_origins_page_to_older_wave_and_stop_after_all_matches(monkeypatch):
    calls = []
    class Client:
        def query(self, operation, payload):
            assert payload['user_id'] == 9
            assert payload['conversation_id'] == 'conv'
            calls.append((operation, payload))
            if operation == 'turn.sync.snapshot':
                assert payload['turn_limit'] == 64
                return {'syncSequence': 10, 'turns': [turn('recent', 'a')],
                        'turnWindow': {'laneId': 'main', 'hasMore': True,
                                       'nextBeforeOrdinal': 100}}
            assert operation == 'turn.sync.page'
            assert payload['sync_sequence'] == 10
            assert payload['before_ordinal'] == 100
            assert payload['limit'] == 64
            return {'syncSequence': 10, 'turns': [turn('older', 'b')],
                    'laneId': 'main', 'hasMore': True, 'nextBeforeOrdinal': 50}
    client = Client()
    monkeypatch.setattr(
        'lib.conversation_sync.repository.SidecarConversationSyncRepository._client',
        lambda self: client)
    writes = install_writes(monkeypatch)
    assert persist_snapshot_to_conversation(
        'conv', ['a', 'b'], snapshot('a', 'b'), user_id=9)
    assert len(calls) == 2
    assert {value[0] for value in writes} == {'recent', 'older'}


def test_stale_page_restarts_read_before_any_cas_write(monkeypatch):
    attempts = []
    def initial(*a, **k):
        attempts.append(1)
        return {'syncSequence': 10, 'turns': [turn('recent', 'a')],
                'turnWindow': {'laneId': 'main', 'hasMore': True,
                               'nextBeforeOrdinal': 100}}
    def older(*a, **k):
        if len(attempts) == 1:
            return {'stale': True, 'syncSequence': 11}
        return {'syncSequence': 10, 'turns': [turn('older', 'b')],
                'laneId': 'main', 'hasMore': False, 'nextBeforeOrdinal': None}
    repo = 'lib.conversation_sync.repository.SidecarConversationSyncRepository'
    monkeypatch.setattr(repo + '.snapshot', initial)
    monkeypatch.setattr(repo + '.turn_page', older)
    monkeypatch.setattr('time.sleep', lambda delay: None)
    writes = install_writes(monkeypatch)
    assert persist_snapshot_to_conversation(
        'conv', ['a', 'b'], snapshot('a', 'b'), user_id=9)
    assert len(attempts) == 2
    assert len(writes) == 2


def test_history_cursor_wedge_is_bounded_and_never_writes(monkeypatch):
    repo = 'lib.conversation_sync.repository.SidecarConversationSyncRepository'
    monkeypatch.setattr(repo + '.snapshot', lambda *a, **k: {
        'syncSequence': 10, 'turns': [],
        'turnWindow': {'laneId': 'main', 'hasMore': True, 'nextBeforeOrdinal': 100}})
    reads = []
    def stuck(*a, **k):
        reads.append(1)
        return {'turns': [], 'hasMore': True, 'nextBeforeOrdinal': 100}
    monkeypatch.setattr(repo + '.turn_page', stuck)
    writes = install_writes(monkeypatch)
    assert not persist_snapshot_to_conversation('conv', ['a'], snapshot('a'), user_id=9)
    assert len(reads) == 1
    assert not writes


def test_master_keeps_first_and_followup_origins_without_live_registry(monkeypatch):
    from lib.swarm.master import MasterOrchestrator
    from lib.swarm.protocol import SubTaskSpec
    master = MasterOrchestrator(
        task_id='run', conv_id='conv', user_id=9,
        specs=[SubTaskSpec(id='a', role='general', objective='work')],
        parent_task={'_turnId': 'branch-a'},
    )
    master.register_followup_specs(
        [SubTaskSpec(id='b', role='general', objective='more')],
        parent_turn_id='branch-b')
    calls = []
    monkeypatch.setattr('lib.swarm.snapshot.persist_snapshot_to_conversation',
                        lambda *a, **k: calls.append(k) or True)
    master._persist_agent_snapshot(force=True)
    assert calls == [{'user_id': 9, 'turn_ids': ('branch-a', 'branch-b')}]
