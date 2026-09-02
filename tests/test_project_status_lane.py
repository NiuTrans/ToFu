"""Black-box contracts for owner-scoped project status snapshots."""

from __future__ import annotations

import inspect
import threading
import time

import pytest

import lib.conversations.project_status as status

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures('chat_sidecar')]
pytest_plugins = ('tests._chat_sidecar',)

OWNER_A = 31
OWNER_B = 47
NORTH_STAR = 'Ship a durable, owner-scoped project status lane.'
EPIC_TITLE = 'Refactor the parser subsystem'


class DispatchSpy:
    def __init__(self, answer='All tracking. No drift.'):
        self.answer = answer
        self.calls: list[list[dict]] = []

    def __call__(self, messages, **_kwargs):
        self.calls.append(messages)
        return self.answer, {}


def _wire_llm(monkeypatch, spy: DispatchSpy):
    import lib.llm_dispatch as dispatch

    monkeypatch.setattr(dispatch, 'dispatch_chat', spy)


def _wire_pillars(
    monkeypatch,
    *,
    board=None,
    charter=None,
    pending=0,
    peers=None,
    feed=None,
    digest=None,
    observed_owners=None,
):
    import lib.conversations.project_board as project_board
    import lib.conversations.project_charter as project_charter
    import lib.conversations.project_feed as project_feed
    import lib.conversations.project_summary as project_summary
    import lib.presence.registry as presence

    board_value = board if board is not None else {
        'tasks': [{
            'title': EPIC_TITLE,
            'status': 'claimed',
            'owner_conv_id': 'conv-a',
            'kind': 'epic',
        }],
        'open': 2,
        'claimed': 1,
        'done': 5,
        'blocked': 0,
    }
    charter_value = charter if charter is not None else {
        'exists': True,
        'version': 8,
        'content': NORTH_STAR,
        'decisions': [{'text': 'Keep storage behind the Sidecar authority.'}],
    }

    def record(label, user_id):
        if observed_owners is not None:
            observed_owners.append((label, user_id))

    def read_board(_path, *, user_id):
        record('board', user_id)
        return board_value

    def read_charter(_path, *, user_id):
        record('charter', user_id)
        return charter_value

    def pending_proposals(_path, *, user_id):
        record('pending', user_id)
        return [{}] * pending

    def snapshot(_path, *, user_id):
        record('presence', user_id)
        return {
            'peers': peers if peers is not None
            else [{'convId': 'conv-a'}, {'convId': 'conv-b'}]
        }

    def read_feed(_path, *, user_id, **_kwargs):
        record('feed', user_id)
        return feed if feed is not None else {'events': []}

    def digest_entries(_path, *, user_id, **_kwargs):
        record('digest', user_id)
        return digest if digest is not None else []

    monkeypatch.setattr(project_board, 'read_board', read_board)
    monkeypatch.setattr(project_charter, 'read_charter', read_charter)
    monkeypatch.setattr(project_charter, 'pending_proposals', pending_proposals)
    monkeypatch.setattr(presence, 'snapshot', snapshot)
    monkeypatch.setattr(project_feed, 'read_project_feed', read_feed)
    monkeypatch.setattr(project_summary, 'project_digest_entries', digest_entries)


def _moved_board(done: int):
    return {
        'tasks': [],
        'open': 1,
        'claimed': 0,
        'done': done,
        'blocked': 0,
    }


def test_collect_pillar_state_propagates_owner_to_every_source(monkeypatch):
    observed = []
    _wire_pillars(monkeypatch, pending=3, observed_owners=observed)

    state = status.collect_pillar_state('/status/collect', user_id=OWNER_A)

    assert state['epicsOpen'] == 2
    assert state['epicsClaimed'] == 1
    assert state['epicsDone'] == 5
    assert state['pendingDecisions'] == 3
    assert state['charterVersion'] == 8
    assert state['northStar'] == NORTH_STAR
    assert state['activePeers'] == 2
    assert state['epicsInFlight'] == [
        {'title': EPIC_TITLE, 'owner': 'conv-a'}
    ]
    assert {label for label, _owner in observed} == {
        'board', 'charter', 'pending', 'presence', 'feed', 'digest'
    }
    assert {owner for _label, owner in observed} == {OWNER_A}


def test_collect_degrades_one_failed_pillar_without_hiding_others(monkeypatch):
    _wire_pillars(monkeypatch)
    import lib.conversations.project_board as project_board

    def fail_board(_path, *, user_id):
        raise RuntimeError(f'board unavailable for {user_id}')

    monkeypatch.setattr(project_board, 'read_board', fail_board)
    state = status.collect_pillar_state('/status/degrade', user_id=OWNER_A)

    assert state['epicsOpen'] == 0
    assert state['charterVersion'] == 8


def test_synthesis_prompt_contains_live_evidence(monkeypatch):
    _wire_pillars(monkeypatch)
    spy = DispatchSpy()
    _wire_llm(monkeypatch, spy)

    pillar_state = status.collect_pillar_state(
        '/status/prompt', user_id=OWNER_A)
    assert status.generate_narrative(pillar_state) == spy.answer
    prompt = spy.calls[0][-1]['content']
    assert NORTH_STAR in prompt
    assert EPIC_TITLE in prompt


def test_staleness_gate_reuses_then_regenerates(monkeypatch):
    project_path = '/status/staleness'
    _wire_pillars(monkeypatch)
    spy = DispatchSpy()
    _wire_llm(monkeypatch, spy)

    first = status.build_status_snapshot(
        project_path, user_id=OWNER_A, trigger='manual')
    unchanged = status.build_status_snapshot(
        project_path, user_id=OWNER_A, trigger='on_open')
    assert first['seq'] == unchanged['seq'] == 0
    assert len(spy.calls) == 1

    _wire_pillars(monkeypatch, board=_moved_board(6))
    moved = status.build_status_snapshot(
        project_path, user_id=OWNER_A, trigger='epic_completed')
    assert moved['seq'] == 1
    assert len(spy.calls) == 2


def test_snapshot_history_is_strictly_owner_isolated(monkeypatch):
    project_path = '/status/owner-isolation'
    _wire_pillars(monkeypatch)
    _wire_llm(monkeypatch, DispatchSpy())

    status.build_status_snapshot(
        project_path, user_id=OWNER_A, trigger='owner-a')
    status.build_status_snapshot(
        project_path, user_id=OWNER_B, trigger='owner-b')

    history_a = status.read_status_history(
        project_path, user_id=OWNER_A)
    history_b = status.read_status_history(
        project_path, user_id=OWNER_B)
    assert [row['trigger'] for row in history_a['snapshots']] == ['owner-a']
    assert [row['trigger'] for row in history_b['snapshots']] == ['owner-b']


def test_history_is_append_only_newest_first_and_bounded(monkeypatch):
    project_path = '/status/history'
    monkeypatch.setattr(status, '_SNAPSHOTS_KEEP', 3)
    _wire_llm(monkeypatch, DispatchSpy())
    for done in range(5):
        _wire_pillars(monkeypatch, board=_moved_board(done))
        status.build_status_snapshot(
            project_path,
            user_id=OWNER_A,
            trigger=f'done-{done}',
        )

    history = status.read_status_history(
        project_path, user_id=OWNER_A, limit=200)
    assert [row['seq'] for row in history['snapshots']] == [4, 3, 2]
    assert [row['trigger'] for row in history['snapshots']] == [
        'done-4', 'done-3', 'done-2'
    ]


def test_status_view_returns_cached_value_and_schedules_owner_scoped_warm(
    monkeypatch,
):
    project_path = '/status/view'
    _wire_pillars(monkeypatch)
    spy = DispatchSpy()
    _wire_llm(monkeypatch, spy)
    status.build_status_snapshot(
        project_path, user_id=OWNER_A, trigger='manual')
    _wire_pillars(monkeypatch, board=_moved_board(9))
    warm_calls = []
    monkeypatch.setattr(
        status,
        'build_status_snapshot',
        lambda path, **kwargs: warm_calls.append((path, kwargs)),
    )

    view = status.get_status_view(
        project_path, user_id=OWNER_A, limit=30)

    assert view['latest']['seq'] == 0
    assert view['refreshing'] is True
    assert len(spy.calls) == 1
    assert warm_calls == [(project_path, {
        'user_id': OWNER_A,
        'trigger': 'on_open',
        'force': False,
        'blocking': False,
    })]


def test_background_lane_is_bounded_coalesced_and_owner_scoped(monkeypatch):
    gate = threading.Event()
    two_active = threading.Event()
    lock = threading.Lock()
    calls = []
    active = 0
    peak = 0

    def blocking(path, *, user_id, trigger, force):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            calls.append((user_id, path, trigger, force))
            if active == status._BACKGROUND_WORKERS:
                two_active.set()
        gate.wait(5)
        with lock:
            active -= 1

    monkeypatch.setattr(status, '_build_status_snapshot_blocking', blocking)
    prefix = f'/status/bounded-{time.time_ns()}'
    try:
        for index in range(6):
            status._schedule_background_snapshot(
                f'{prefix}/{index}',
                user_id=OWNER_A,
                trigger=f'event-{index}',
                force=False,
            )
        repeated = f'{prefix}/repeat'
        status._schedule_background_snapshot(
            repeated, user_id=OWNER_A, trigger='first', force=False)
        for index in range(10):
            status._schedule_background_snapshot(
                repeated,
                user_id=OWNER_A,
                trigger=f'latest-{index}',
                force=index == 9,
            )
        status._schedule_background_snapshot(
            repeated, user_id=OWNER_B, trigger='other-owner', force=False)

        assert two_active.wait(2)
        with lock:
            assert peak == status._BACKGROUND_WORKERS == 2
    finally:
        gate.set()
    assert status._wait_for_background_status(5)
    repeated_a = [call for call in calls if call[:2] == (OWNER_A, repeated)]
    repeated_b = [call for call in calls if call[:2] == (OWNER_B, repeated)]
    assert repeated_a == [(OWNER_A, repeated, 'latest-9', True)]
    assert repeated_b == [(OWNER_B, repeated, 'other-owner', False)]


def test_status_question_is_read_only(monkeypatch):
    project_path = '/status/question'
    _wire_pillars(monkeypatch)
    spy = DispatchSpy('Nothing is blocked.')
    _wire_llm(monkeypatch, spy)

    result = status.answer_status_question(
        project_path, 'What is blocked?', user_id=OWNER_A)

    assert result['ok'] is True
    assert result['answer'] == 'Nothing is blocked.'
    assert 'What is blocked?' in spy.calls[0][-1]['content']
    assert status.read_status_history(
        project_path, user_id=OWNER_A)['snapshots'] == []


def test_status_line_reads_only_the_owner_latest_snapshot(monkeypatch):
    project_path = '/status/headline'
    _wire_pillars(monkeypatch)
    _wire_llm(monkeypatch, DispatchSpy(
        'We are on track. Two epics remain open.'))
    status.build_status_snapshot(
        project_path, user_id=OWNER_A, trigger='manual')

    assert status.status_line(
        project_path, user_id=OWNER_A) == 'We are on track.'
    assert status.status_line(project_path, user_id=OWNER_B) == ''


def test_status_lane_is_not_part_of_prompt_composition():
    import lib.tasks_pkg.context_composer._providers as providers

    source = inspect.getsource(providers)
    for banned in (
        'project_status',
        'build_status_snapshot',
        'status_line',
        'collect_pillar_state',
        'read_status_history',
    ):
        assert banned not in source
