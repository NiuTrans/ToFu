"""Execution identity fences for live and detached swarm projections."""
import copy
import json
from types import SimpleNamespace

import pytest

from lib.swarm.snapshot import (
    filter_snapshot, persist_snapshot_to_conversation,
    reconcile_spawn_round_from_active_session, stamp_round,
)

pytestmark = pytest.mark.unit


def receipt(run, ids=('a',), **extra):
    return {'toolName': 'spawn_agents', 'toolContent': json.dumps({
        'swarm_id': run, 'agents': [{'id': aid} for aid in ids], **extra,
    })}


def snapshot(run, ids=('a',), settled=True):
    return {'swarm_id': run, 'agents': [
        {'id': aid, 'status': 'done' if settled else 'running',
         'preview': run, 'tokens': 1} for aid in ids
    ], 'settled': settled, 'version': 100000 + len(ids) if settled else 0}


@pytest.mark.parametrize('incoming,existing', [('old', 'new'), ('new', 'old')])
def test_stamp_rejects_foreign_run_even_with_equal_or_higher_version(incoming, existing):
    entry = receipt(existing)
    assert stamp_round(entry, snapshot(existing, settled=False))
    before = copy.deepcopy(entry)
    assert not stamp_round(entry, snapshot(incoming))
    assert entry == before


def test_legacy_missing_identity_is_readable_but_not_reassigned():
    legacy = receipt('')
    legacy['_swarmSnapshot'] = {'agents': [{'id': 'a', 'status': 'done'}]}
    before = copy.deepcopy(legacy)
    assert not stamp_round(legacy, snapshot('new'))
    assert legacy == before
    # A legacy snapshot on an identified receipt may be enriched safely.
    identified = receipt('new')
    identified['_swarmSnapshot'] = {'version': 0}
    assert stamp_round(identified, snapshot('new'))


def test_followup_receipt_uses_original_run_not_new_spawning_task():
    entry = receipt('followup-task', ('b',), swarm_run_id='original')
    combined = snapshot('original', ('a', 'b'))
    scoped = filter_snapshot(combined, {'b'})
    assert scoped['swarm_id'] == 'original'
    assert scoped['doneCount'] == 1
    assert stamp_round(entry, scoped)
    assert entry['_swarmSnapshot']['agents'][0]['id'] == 'b'


def test_fast_settlement_rejects_replacement_conversation_session(monkeypatch):
    monkeypatch.setattr('lib.swarm.integration._state._get_session',
                        lambda key: SimpleNamespace(
                            _build_agent_snapshot=lambda: snapshot('new')))
    entry = receipt('old')
    assert not reconcile_spawn_round_from_active_session(
        {'id': 'old', 'convId': 'shared'}, entry)
    assert '_swarmSnapshot' not in entry


def test_detached_persistence_fences_runs_and_keeps_followup_waves(monkeypatch):
    import lib.turn_lifecycle as lifecycle
    import lib.conversations as conversations

    turns = [
        {'turnId': 'old', 'status': 'completed', 'projectionRevision': 3,
         'projection': {'toolRounds': [receipt('old')]}},
        {'turnId': 'new', 'status': 'completed', 'projectionRevision': 7,
         'projection': {'toolRounds': [receipt('new'),
             receipt('followup', ('b',), swarm_run_id='new')]}},
    ]
    writes = []
    monkeypatch.setattr(
        'lib.conversation_sync.repository.SidecarConversationSyncRepository.snapshot',
        lambda *a, **k: {'syncSequence': 10, 'turns': turns})
    monkeypatch.setattr(conversations, 'notify_conv_changed', lambda *a, **k: None)

    def update(conv, turn_id, *, projection, expected_projection_revision, user_id):
        turn = next(t for t in turns if t['turnId'] == turn_id)
        assert expected_projection_revision == turn['projectionRevision']
        assert user_id == 9
        turn['projection'] = projection
        turn['projectionRevision'] += 1
        writes.append(turn_id)
        return {'conversationRevision': 10}

    monkeypatch.setattr(lifecycle, 'update_turn_projection', update)
    assert persist_snapshot_to_conversation('shared', ['a', 'b'],
                                            snapshot('new', ('a', 'b')), user_id=9)
    assert writes == ['new']
    panels = turns[1]['projection']['toolRounds']
    assert [[a['id'] for a in p['_swarmSnapshot']['agents']] for p in panels] == [['a'], ['b']]
    new_before = copy.deepcopy(turns[1])
    assert persist_snapshot_to_conversation('shared', ['a'], snapshot('old'), user_id=9)
    assert writes == ['new', 'old']
    assert turns[1] == new_before
    assert not persist_snapshot_to_conversation('shared', ['a'],
                                                snapshot('old', settled=False), user_id=9)


def test_master_live_stamp_does_not_touch_other_runs(monkeypatch):
    from lib.swarm.master import MasterOrchestrator
    from lib.swarm.protocol import SubTaskSpec
    from lib.tasks_pkg.manager.runtime import chat_task_runtime

    tasks = [{'convId': 'shared', '_userId': 9, 'toolRounds': [receipt(run)]}
             for run in ('old', 'new')]
    def update_matching(*, predicate, updater):
        for task in tasks:
            if predicate(task):
                updater(task)
    monkeypatch.setattr(chat_task_runtime, 'update_matching', update_matching)
    master = MasterOrchestrator(task_id='new', conv_id='shared', user_id=9,
                               specs=[SubTaskSpec(id='a', role='general', objective='work')])
    master._persist_agent_snapshot(cas=False)
    assert '_swarmSnapshot' not in tasks[0]['toolRounds'][0]
    assert tasks[1]['toolRounds'][0]['_swarmSnapshot']['swarm_id'] == 'new'


def test_cas_retry_rechecks_identity_and_never_writes_live_turn(monkeypatch):
    import lib.turn_lifecycle as lifecycle
    import lib.conversations as conversations

    turn = {'turnId': 'turn', 'status': 'completed', 'projectionRevision': 1,
            'projection': {'toolRounds': [receipt('old')]}}
    writes = []
    reads = []
    def read(*args, **kwargs):
        reads.append(kwargs['turn_limit'])
        return {'syncSequence': 10, 'turns': [turn]}
    def conflict(*args, **kwargs):
        writes.append(kwargs['expected_projection_revision'])
        turn['projection'] = {'toolRounds': [receipt('new')]}
        raise lifecycle.LifecycleConflict('projection_conflict', 'raced')
    monkeypatch.setattr(
        'lib.conversation_sync.repository.SidecarConversationSyncRepository.snapshot', read)
    monkeypatch.setattr(lifecycle, 'update_turn_projection', conflict)
    monkeypatch.setattr(conversations, 'notify_conv_changed', lambda *a, **k: None)
    monkeypatch.setattr('time.sleep', lambda delay: None)
    assert not persist_snapshot_to_conversation('shared', ['a'], snapshot('old'), user_id=9)
    assert writes == [1]
    assert reads == [64, 64]
    turn['projection'] = {'toolRounds': [receipt('old')]}
    turn['status'] = next(iter(lifecycle.LIVE_ATTEMPT_STATUSES))
    assert not persist_snapshot_to_conversation('shared', ['a'], snapshot('old'), user_id=9)
    assert writes == [1]
    assert '_swarmSnapshot' not in turn['projection']['toolRounds'][0]
