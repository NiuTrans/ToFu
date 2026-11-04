"""Idle evidence survives context restoration without counting new human input."""
from copy import deepcopy

import pytest

from tests.model_routing.test_model_continuation_policy import replay_store
from tests.model_routing.test_model_replay_stability import _turn

pytestmark = pytest.mark.unit

NOW_MS = 1_800_000_000_000
OLD_MS = NOW_MS - 6 * 60 * 60 * 1000


def new_input():
    human = _turn('u2', 'human', 'continue', 2)
    pending = _turn('a2', 'assistant', '', 3, status='running')
    human['updatedAt'] = pending['updatedAt'] = NOW_MS
    return [human, pending]


def test_completed_activity_survives_checkpoint_reopen(replay_store, monkeypatch):
    from lib.storage.model_replay_cache import ModelReplayCache
    from lib.tasks_pkg import model_replay as replay
    from lib import turn_lifecycle

    monkeypatch.setattr('lib.tasks_pkg.compaction._idle.time.time', lambda: OLD_MS / 1000)
    task = {'convId': 'c', '_turnId': 'a1', '_userId': 1, 'model': 'model',
            'content': 'done', 'config': {'model': 'model', '_modelReplayBase': []},
            'messages': [{'role': 'assistant', 'content': 'done'}]}
    prepared = replay.prepare_completed_task(task)
    assert prepared['projection']['lastModelActivityAtMs'] == OLD_MS
    prior = _turn('a1', 'assistant', 'done', 1)
    prepared['projection']['turns'][-1] = replay.turn_witness(prior)
    assert replay_store.put(user_id=1, conversation_id='c', lane_id='main',
                            projection=prepared['projection'])
    reopened = ModelReplayCache(root=replay_store.root, max_bytes=replay_store.max_bytes)
    monkeypatch.setattr(replay, 'ModelContinuationStore', lambda: reopened)
    monkeypatch.setattr(turn_lifecycle, 'get_turn',
                        lambda *a, **k: pytest.fail('new checkpoint requires no extra Turn query'))
    config = {'model': 'model'}
    restored, covered = replay.restore_prefix([prior, *new_input()], config,
                                              conversation_id='c', user_id=1)
    assert covered == 1 and restored == task['messages']
    assert config['_contextLastModelActivityAtMs'] == OLD_MS
    assert replay.policy_witness(config) == replay.policy_witness({'model': 'model'})


def test_legacy_checkpoint_recovers_activity_from_owned_turn(replay_store, monkeypatch):
    from lib.tasks_pkg import model_replay as replay
    from lib import turn_lifecycle

    prior = _turn('a1', 'assistant', 'done', 1)
    config = {'model': 'model'}
    replay_store.put(user_id=1, conversation_id='c', lane_id='main', projection={
        'policy': replay.policy_witness(config), 'turns': [replay.turn_witness(prior)],
        'messages': [{'role': 'assistant', 'content': 'done'}],
    })
    reads = []
    def get_turn(conversation_id, turn_id, *, user_id):
        reads.append((conversation_id, turn_id, user_id))
        return {**deepcopy(prior), 'updatedAt': OLD_MS}
    monkeypatch.setattr(turn_lifecycle, 'get_turn', get_turn)
    _, covered = replay.restore_prefix([prior, *new_input()], config,
                                       conversation_id='c', user_id=1)
    assert covered == 1 and reads == [('c', 'a1', 1)]
    assert config['_contextLastModelActivityAtMs'] == OLD_MS


def test_cold_rebuild_uses_assistant_activity_not_new_human_or_target(replay_store):
    from lib.tasks_pkg import model_replay as replay

    prior = {**_turn('a1', 'assistant', 'done', 1), 'updatedAt': OLD_MS}
    config = {'model': 'model', '_contextLastModelActivityAtMs': NOW_MS}
    assert replay.restore_prefix([prior, *new_input()], config,
                                  conversation_id='c', user_id=1) == ([], 0)
    assert config['_contextLastModelActivityAtMs'] == OLD_MS


def test_newer_uncovered_assistant_restarts_clock(replay_store, monkeypatch):
    from lib.tasks_pkg import model_replay as replay
    from lib import turn_lifecycle

    prior = {**_turn('a1', 'assistant', 'done', 1), 'updatedAt': OLD_MS}
    newer = {**_turn('a2', 'assistant', 'more work', 2), 'updatedAt': NOW_MS - 1000}
    pending = _turn('a3', 'assistant', '', 3, status='running')
    config = {'model': 'model'}
    replay_store.put(user_id=1, conversation_id='c', lane_id='main', projection={
        'policy': replay.policy_witness(config), 'turns': [replay.turn_witness(prior)],
        'messages': [{'role': 'assistant', 'content': 'done'}],
    })
    monkeypatch.setattr(turn_lifecycle, 'get_turn',
                        lambda *a, **k: pytest.fail('newer activity makes a legacy query redundant'))
    replay.restore_prefix([prior, newer, pending], config, conversation_id='c', user_id=1)
    assert config['_contextLastModelActivityAtMs'] == NOW_MS - 1000


def test_live_activity_does_not_move_backwards(monkeypatch):
    from lib.tasks_pkg.compaction._idle import record_model_activity

    task = {}
    monkeypatch.setattr('lib.tasks_pkg.compaction._idle.time.time', lambda: NOW_MS / 1000)
    record_model_activity(task)
    assert task['_lastModelActivityAtMs'] == NOW_MS
    monkeypatch.setattr('lib.tasks_pkg.compaction._idle.time.time', lambda: OLD_MS / 1000)
    record_model_activity(task)
    assert task['_lastModelActivityAtMs'] == NOW_MS
