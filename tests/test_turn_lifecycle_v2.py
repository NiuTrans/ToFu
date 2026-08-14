from __future__ import annotations

import json

import pytest


pytestmark = pytest.mark.unit


@pytest.fixture()
def turn_db(tmp_path):
    from lib.database import _core as core

    snapshot = core.reset_sqlite_for_tests(str(tmp_path / 'turns-v2.db'))
    db = core._new_sqlite_connection()
    db.execute(
        'INSERT INTO conversations(id,user_id,title,messages,created_at,'
        'updated_at,settings,msg_count,search_text,rev,messages_rows_rev) '
        "VALUES ('conv-v2',1,'v2','[]',1,1,'{}',0,'',0,-1)")
    db.commit()
    db.close()
    try:
        yield
    finally:
        core.restore_db_state(snapshot)


def _create():
    from lib.turn_lifecycle import create_turn_pair
    return create_turn_pair(
        'conv-v2', command_id='command-create',
        input_projection={'content': 'hello'}, config={'model': 'gpt-4o'})


def test_turn_pair_is_atomic_and_command_idempotent(turn_db):
    from lib.turn_lifecycle import (
        claim_attempt_start,
        create_turn_pair,
        list_turns,
        read_events,
    )

    first = _create()
    second = create_turn_pair(
        'conv-v2', command_id='command-create',
        input_projection={'content': 'different retry body'},
        config={'model': 'different'})

    assert first['turn']['turnId'] == second['turn']['turnId']
    assert first['attempt']['attemptId'] == second['attempt']['attemptId']
    assert second['idempotentReplay'] is True
    snapshot = list_turns('conv-v2')
    assert [(t['actor'], t['ordinal']) for t in snapshot['turns']] == [
        ('human', 0), ('assistant', 1)]
    event = read_events(first['attempt']['attemptId'])[0]
    assert (event['conversationId'], event['turnId'], event['attemptId']) == (
        'conv-v2', first['turn']['turnId'], first['attempt']['attemptId'])
    assert claim_attempt_start(first['attempt']['attemptId']) is True
    assert claim_attempt_start(first['attempt']['attemptId']) is False


def test_turn_revision_bumps_preserve_message_row_authority(
        turn_db, monkeypatch):
    """V2 projection revisions must not invalidate an unchanged canonical
    transcript.  The 2026-08-13 regression advanced ``rev`` hundreds of times
    while leaving ``messages_rows_rev`` frozen, preventing the next boot."""
    from lib.database import DOMAIN_CHAT, pooled_db
    from lib.database.messages_rows import assert_rows_authority_ready
    from lib.turn_lifecycle import bind_task, create_turn_pair, record_task_event

    monkeypatch.setenv('TOFU_MESSAGES_ROWS', '1')
    monkeypatch.setenv('TOFU_MESSAGES_ROWS_READ', '1')
    monkeypatch.setenv('TOFU_MESSAGES_ROWS_AUTHORITY', '1')
    with pooled_db(DOMAIN_CHAT) as db:
        db.execute(
            "UPDATE conversations SET messages_rows_rev=rev "
            "WHERE id='conv-v2'")
        db.commit()

    created = create_turn_pair(
        'conv-v2', command_id='authority-command',
        input_projection={'content': 'hello'}, config={'model': 'gpt-4o'})
    attempt_id = created['attempt']['attemptId']
    bind_task(attempt_id, 'authority-task')
    task = {
        '_attemptId': attempt_id, '_turnProtocolV2': True,
        'id': 'authority-task', 'status': 'running',
        'content': 'partial', 'thinking': '', 'toolRounds': [],
        'model': 'gpt-4o', 'config': {'model': 'gpt-4o'},
    }
    assert record_task_event(task, {'type': 'delta', 'content': 'partial'})

    with pooled_db(DOMAIN_CHAT) as db:
        row = db.execute(
            'SELECT rev,messages_rows_rev,msg_count FROM conversations '
            "WHERE id='conv-v2'").fetchone()
        assert row['rev'] == row['messages_rows_rev']
        assert row['msg_count'] == 0
        assert_rows_authority_ready(db)


def test_authority_created_v2_conversation_starts_with_current_empty_rows(
        turn_db, monkeypatch):
    from lib.database import DOMAIN_CHAT, pooled_db
    from lib.database.messages_rows import assert_rows_authority_ready
    from lib.turn_lifecycle import create_turn_pair

    monkeypatch.setenv('TOFU_MESSAGES_ROWS', '1')
    monkeypatch.setenv('TOFU_MESSAGES_ROWS_READ', '1')
    monkeypatch.setenv('TOFU_MESSAGES_ROWS_AUTHORITY', '1')
    # Make the fixture's unrelated seed canonical before exercising the
    # startup-wide preflight below.
    with pooled_db(DOMAIN_CHAT) as db:
        db.execute(
            "UPDATE conversations SET messages_rows_rev=rev "
            "WHERE id='conv-v2'")
        db.commit()

    create_turn_pair(
        'conv-v2-new', command_id='authority-create',
        input_projection={'content': 'new'}, config={},
        conversation_defaults={'allowCreate': True, 'title': 'new'})

    with pooled_db(DOMAIN_CHAT) as db:
        row = db.execute(
            'SELECT rev,messages_rows_rev,msg_count FROM conversations '
            "WHERE id='conv-v2-new'").fetchone()
        assert row['rev'] == row['messages_rows_rev'] == 1
        assert row['msg_count'] == 0
        assert_rows_authority_ready(db)


def test_branch_lane_identity_is_server_issued_and_persisted_on_parent(turn_db):
    from lib.turn_lifecycle import (
        LifecycleConflict,
        create_branch_lane,
        create_turn_pair,
        delete_branch_lane,
        get_turn,
        list_turns,
    )

    created = _create()
    parent = created['submittedTurn']
    lane_result = create_branch_lane(
        'conv-v2', parent['turnId'], title='Investigate',
        anchor_text='hello', expected_projection_revision=parent['projectionRevision'])
    lane = lane_result['lane']
    assert lane['laneId'].startswith('lane_')
    persisted_parent = get_turn('conv-v2', parent['turnId'])
    assert persisted_parent['projection']['_branchLanes'] == [lane]

    branch_pair = create_turn_pair(
        'conv-v2', command_id='branch-command',
        input_projection={'content': 'branch question'}, config={},
        lane_id=lane['laneId'], parent_turn_id=parent['turnId'],
        kind='branch_reply')
    assert branch_pair['submittedTurn']['laneId'] == lane['laneId']
    assert branch_pair['turn']['parentTurnId'] == branch_pair['submittedTurn']['turnId']
    assert len([turn for turn in list_turns('conv-v2')['turns']
                if turn['laneId'] == lane['laneId']]) == 2

    with pytest.raises(LifecycleConflict) as busy:
        delete_branch_lane('conv-v2', parent['turnId'], lane['laneId'])
    assert getattr(busy.value, 'code', None) == 'lane_busy'


def test_projection_cas_and_superseded_attempt_rejection(turn_db):
    from lib.turn_lifecycle import (
        LifecycleConflict,
        bind_task,
        create_attempt,
        get_turn,
        read_events,
        record_task_event,
    )

    created = _create()
    attempt_id = created['attempt']['attemptId']
    turn_id = created['turn']['turnId']
    bind_task(attempt_id, 'task-old')
    old_task = {
        '_attemptId': attempt_id, '_turnProtocolV2': True,
        'id': 'task-old', 'status': 'done', 'finishReason': 'stop',
        'content': 'first answer', 'thinking': '', 'toolRounds': [],
        'model': 'gpt-4o', 'config': {'model': 'gpt-4o'},
    }
    assert record_task_event(old_task, {'type': 'done', 'finishReason': 'stop'})
    settled = get_turn('conv-v2', turn_id)
    with pytest.raises(LifecycleConflict) as stale:
        create_attempt(
            'conv-v2', turn_id, command_id='stale', operation='regenerate',
            expected_projection_revision=settled['projectionRevision'] - 1)
    assert stale.value.code == 'stale_projection'

    regenerated = create_attempt(
        'conv-v2', turn_id, command_id='regenerate-1', operation='regenerate',
        expected_projection_revision=settled['projectionRevision'],
        config={'model': 'gpt-4o'})
    assert regenerated['turn']['turnId'] == turn_id
    assert regenerated['attempt']['attemptId'] != attempt_id
    # Late delta from the superseded executor cannot mutate or enter its stream.
    old_task['status'] = 'running'
    old_task['content'] = 'stale overwrite'
    assert record_task_event(old_task, {'type': 'delta', 'content': 'stale'}) is False
    assert get_turn('conv-v2', turn_id)['projection']['content'] == ''
    assert all(event['type'] != 'projection_updated'
               for event in read_events(attempt_id))


def test_checkpoint_resume_rejects_client_selected_anchor(turn_db):
    from lib.turn_lifecycle import (
        LifecycleConflict,
        bind_task,
        create_attempt,
        get_turn,
        record_task_event,
    )

    created = _create()
    attempt_id = created['attempt']['attemptId']
    turn_id = created['turn']['turnId']
    bind_task(attempt_id, 'task-checkpoint')
    task = {
        '_attemptId': attempt_id, '_turnProtocolV2': True,
        'id': 'task-checkpoint', 'status': 'running',
        'content': 'newer partial', 'thinking': '',
        'toolRounds': [{'status': 'done', 'assistantContent': 'safe prefix'}],
        'model': 'gpt-4o', 'config': {'model': 'gpt-4o'},
    }
    assert record_task_event(task, {'type': 'delta', 'content': 'newer partial'})
    assert record_task_event(
        {**task, 'status': 'error', 'error': 'provider disconnected'},
        {'type': 'error', 'error': 'provider disconnected'},
    )
    settled = get_turn('conv-v2', turn_id)

    with pytest.raises(LifecycleConflict) as conflict:
        create_attempt(
            'conv-v2', turn_id, command_id='bad-anchor',
            operation='checkpoint_resume',
            expected_projection_revision=settled['projectionRevision'],
            resume_anchor={'content': 'client-chosen text'},
        )
    assert conflict.value.code == 'invalid_resume_anchor'

    option = next(item for item in settled['settlement']['resumeOptions']
                  if item['operation'] == 'checkpoint_resume')
    resumed = create_attempt(
        'conv-v2', turn_id, command_id='valid-anchor',
        operation='checkpoint_resume',
        expected_projection_revision=settled['projectionRevision'],
        resume_anchor=option['anchor'],
    )
    assert resumed['turn']['turnId'] == turn_id
    assert resumed['attempt']['resumeAnchor'] == option['anchor']


def test_terminal_transaction_and_restart_recovery_preserve_projection(turn_db):
    from lib.turn_lifecycle import (
        bind_task,
        get_turn,
        read_events,
        record_task_event,
        recover_running_attempts,
    )

    created = _create()
    attempt_id = created['attempt']['attemptId']
    turn_id = created['turn']['turnId']
    bind_task(attempt_id, 'task-running')
    task = {
        '_attemptId': attempt_id, '_turnProtocolV2': True,
        'id': 'task-running', 'status': 'running',
        'content': 'durable partial', 'thinking': 'work',
        'toolRounds': [{'status': 'done', 'assistantContent': 'checkpoint'}],
        'model': 'gpt-4o', 'config': {'model': 'gpt-4o'},
    }
    assert record_task_event(task, {'type': 'delta', 'content': 'durable partial'})
    assert recover_running_attempts() == 1
    turn = get_turn('conv-v2', turn_id)
    assert turn['status'] == 'interrupted'
    assert turn['projection']['content'] == 'durable partial'
    assert turn['settlement']['cause'] == 'server_restart'
    operations = {item['operation'] for item in turn['settlement']['resumeOptions']}
    assert 'checkpoint_resume' in operations
    assert 'regenerate' in operations
    events = read_events(attempt_id)
    assert events[-1]['type'] == 'terminal_settlement'
    assert events[-1]['payload']['projection']['content'] == 'durable partial'
    assert recover_running_attempts() == 0


def test_legacy_startup_recovery_never_dual_writes_v2_projection(turn_db):
    from lib.database import DOMAIN_CHAT, pooled_db
    from lib.tasks_pkg.manager import recover_stale_tasks_on_startup

    with pooled_db(DOMAIN_CHAT) as db:
        db.execute(
            "UPDATE conversations SET messages=? WHERE id='conv-v2'",
            (json.dumps([{'role': 'user', 'content': 'archived'}]),))
        db.execute(
            'INSERT INTO task_results(task_id,conv_id,content,thinking,status,'
            'metadata,created_at) VALUES (?,?,?,?,?,?,?)',
            ('v2-stale-task', 'conv-v2', 'must stay out of archive', '',
             'running', json.dumps({'turnProtocolV2': True,
                                    'attemptId': 'attempt-v2'}), 10))
        db.commit()

    recover_stale_tasks_on_startup(
        prev_shutdown={'verdict': 'clean'}, dispatch=False)
    with pooled_db(DOMAIN_CHAT) as db:
        row = db.execute(
            "SELECT messages FROM conversations WHERE id='conv-v2'").fetchone()
        assert json.loads(row['messages']) == [
            {'role': 'user', 'content': 'archived'}]
        assert db.execute(
            "SELECT status FROM task_results WHERE task_id='v2-stale-task'"
        ).fetchone()['status'] == 'interrupted'


def test_endpoint_visible_roles_are_explicit_idempotent_turns(turn_db):
    from lib.turn_lifecycle import (
        bind_task,
        list_turns,
        read_events,
        record_task_event,
        sync_visible_run_turns,
    )

    created = _create()
    attempt_id = created['attempt']['attemptId']
    root_turn_id = created['turn']['turnId']
    bind_task(attempt_id, 'task-endpoint')
    task = {
        '_turnProtocolV2': True, '_attemptId': attempt_id,
        '_turnId': root_turn_id, 'id': 'task-endpoint', 'convId': 'conv-v2',
        'endpoint_mode': True, 'status': 'running', 'content': 'aggregate',
        'thinking': '', 'toolRounds': [], 'config': {'model': 'gpt-4o'},
    }
    visible = [
        {'role': 'assistant', 'content': 'plan', '_isEndpointPlanner': True},
        {'role': 'assistant', 'content': 'work', '_epIteration': 1},
        {'role': 'user', 'content': 'approved', '_isEndpointReview': True,
         '_epIteration': 1, '_epApproved': True},
    ]
    assert sync_visible_run_turns(task, visible) is None
    first_ids = task['_v2VisibleRunTurnIds'][:]
    assert sync_visible_run_turns(task, visible) is None
    assert task['_v2VisibleRunTurnIds'] == first_ids

    turns = list_turns('conv-v2')['turns']
    generated = [turn for turn in turns if turn['actor'] != 'human']
    assert len(generated) == 3
    assert [turn['actor'] for turn in generated] == [
        'planner', 'assistant', 'critic']
    assert [turn['kind'] for turn in generated] == [
        'endpoint_planner', 'endpoint_worker', 'endpoint_critic']
    assert generated[0]['turnId'] == root_turn_id
    assert generated[1]['parentTurnId'] == root_turn_id
    assert generated[2]['parentTurnId'] == generated[1]['turnId']
    assert generated[1]['status'] == generated[2]['status'] == 'completed'

    # Finishing the outer executor settles the reused first turn without
    # replacing its phase projection with the aggregate task buffer.
    task.update(status='done', finishReason='stop')
    assert record_task_event(task, {'type': 'done', 'finishReason': 'stop'})
    turns = list_turns('conv-v2')['turns']
    root = next(turn for turn in turns if turn['turnId'] == root_turn_id)
    assert root['projection']['content'] == 'plan'
    assert root['status'] == 'completed'
    event_types = [event['type'] for event in read_events(attempt_id)]
    assert event_types.count('terminal_settlement') == 1


def test_autopilot_baton_creates_vu_and_successor_attempt_before_handoff(turn_db):
    from lib.tasks_pkg.autopilot_baton import _append_v2_autopilot_turns
    from lib.turn_lifecycle import (
        bind_task,
        list_turns,
        read_events,
        record_task_event,
    )

    created = _create()
    parent_attempt_id = created['attempt']['attemptId']
    parent_turn_id = created['turn']['turnId']
    bind_task(parent_attempt_id, 'autopilot-parent-task')
    task = {
        '_turnProtocolV2': True, '_attemptId': parent_attempt_id,
        '_turnId': parent_turn_id, '_userId': 1,
        'id': 'autopilot-parent-task', 'convId': 'conv-v2',
        'status': 'running', 'content': 'parent answer', 'thinking': '',
        'toolRounds': [], 'config': {'model': 'gpt-4o', 'autopilot': True},
        'model': 'gpt-4o',
    }
    vu = _append_v2_autopilot_turns(
        task, 'conv-v2', 'legacy-vu-stream-id', 'please continue',
        rounds=[{'status': 'done'}], run_id='run-v2', segments=[])
    assert vu is not None

    turns = list_turns('conv-v2')['turns']
    assert [turn['actor'] for turn in turns] == [
        'human', 'assistant', 'virtual_user', 'assistant']
    vu_turn, successor = turns[-2:]
    assert vu_turn['parentTurnId'] == parent_turn_id
    assert vu_turn['currentAttemptId']
    assert successor['parentTurnId'] == vu_turn['turnId']
    assert successor['currentAttemptId'] == task['_v2NextAttemptId']
    assert successor['status'] == 'pending'

    task.update(status='done', finishReason='stop')
    assert record_task_event(task, {'type': 'done', 'finishReason': 'stop'})
    parent_terminal = read_events(parent_attempt_id)[-1]
    assert parent_terminal['type'] == 'terminal_settlement'
    assert parent_terminal['payload']['settlement']['continuation'] == {
        'turnId': successor['turnId'],
        'attemptId': successor['currentAttemptId'],
    }
    announcement = next(
        event for event in read_events(parent_attempt_id)
        if event['payload'].get('updateKind') == 'related_turns_created')
    assert [turn['turnId'] for turn in announcement['payload']['turns']] == [
        vu_turn['turnId'], successor['turnId']]


def test_migration_is_deterministic_and_conservative_for_unknown_finish():
    from lib.turn_migration import plan_conversation

    messages = [
        {'role': 'user', 'content': 'u', '_msgId': 'duplicate'},
        {'role': 'assistant', 'content': 'partial', '_msgId': 'duplicate'},
        {'role': 'assistant', 'content': 'done', '_msgId': 'legal-id',
         'finishReason': 'stop', 'segments': [{'type': 'content', 'text': 'done'}]},
    ]
    one = plan_conversation('legacy-conv', messages)
    two = plan_conversation('legacy-conv', messages)
    assert [t['turn_id'] for t in one.turns] == [t['turn_id'] for t in two.turns]
    assert one.turns[0]['turn_id'] != 'duplicate'
    assert one.turns[1]['turn_id'] != 'duplicate'
    assert one.turns[1]['status'] == 'interrupted'
    assert one.turns[1]['settlement']['cause'] == 'legacy_unknown'
    assert one.turns[2]['turn_id'] == 'legal-id'
    assert one.turns[2]['status'] == 'completed'
    assert one.turns[2]['projection']['segments'][0]['text'] == 'done'


def test_migration_translates_orchestration_markers_to_explicit_identity():
    from lib.turn_migration import plan_conversation

    messages = [
        {'role': 'assistant', 'content': 'plan', '_isEndpointPlanner': True,
         '_epPlannerIteration': 2, 'finishReason': 'stop'},
        {'role': 'assistant', 'content': 'work', '_epIteration': 2,
         'finishReason': 'stop'},
        {'role': 'user', 'content': 'review', '_isEndpointReview': True,
         '_epIteration': 2, '_epApproved': False, 'finishReason': 'stop'},
        {'role': 'user', 'content': 'continue', '_isVirtualUser': True,
         '_autopilotRunId': 'run-1'},
    ]
    plan = plan_conversation('legacy-modes', messages)
    assert [turn['actor'] for turn in plan.turns] == [
        'planner', 'assistant', 'critic', 'virtual_user']
    assert [turn['kind'] for turn in plan.turns] == [
        'endpoint_planner', 'endpoint_worker', 'endpoint_critic',
        'autopilot_virtual_user']
    assert plan.turns[0]['projection']['orchestration']['iteration'] == 2
    assert plan.turns[2]['projection']['orchestration']['approved'] is False
    for turn in plan.turns:
        assert not any(key.startswith('_isEndpoint')
                       or key in {'_isVirtualUser', '_epIteration'}
                       for key in turn['projection'])


def test_migration_apply_validates_branches_iso_time_and_shared_legacy_task(turn_db):
    from lib.database import DOMAIN_CHAT, pooled_db
    from lib.turn_migration import apply_plans, plan_conversation

    messages = [
        {'role': 'user', 'content': 'request', '_msgId': 'human-1',
         'timestamp': '2026-08-13T12:00:00+00:00'},
        {'role': 'assistant', 'content': 'plan', '_msgId': 'planner-1',
         '_taskId': 'shared-endpoint-task', '_isEndpointPlanner': True,
         'finishReason': 'stop',
         'branches': [{'id': 'lane-branch', 'messages': [
             {'role': 'user', 'content': 'branch input', '_msgId': 'branch-u'},
             {'role': 'assistant', 'content': 'branch answer',
              '_msgId': 'branch-a', 'finishReason': 'stop'},
         ]}]},
        {'role': 'assistant', 'content': 'work', '_msgId': 'worker-1',
         '_taskId': 'shared-endpoint-task', '_epIteration': 1,
         'finishReason': 'stop'},
        {'role': 'user', 'content': 'next', '_msgId': 'vu-1',
         '_isVirtualUser': True},
    ]
    plan = plan_conversation('conv-v2', messages, created_at=123)
    result = apply_plans([plan])
    assert result == {
        'schemaVersion': 2, 'conversations': 1,
        'turns': 6, 'attempts': 4,
    }
    with pooled_db(DOMAIN_CHAT) as db:
        main = db.execute(
            "SELECT turn_id,parent_turn_id,actor FROM conversation_turns "
            "WHERE conversation_id='conv-v2' AND lane_id='main' "
            'ORDER BY ordinal').fetchall()
        assert [row['parent_turn_id'] for row in main] == [
            None, 'human-1', 'planner-1', 'worker-1']
        assert main[-1]['actor'] == 'virtual_user'
        branch = db.execute(
            "SELECT turn_id,parent_turn_id FROM conversation_turns "
            "WHERE conversation_id='conv-v2' AND lane_id='lane-branch' "
            'ORDER BY ordinal').fetchall()
        assert [row['parent_turn_id'] for row in branch] == [
            'planner-1', 'branch-u']
        assert db.execute(
            'SELECT COUNT(*) AS n FROM generation_attempts '
            'WHERE conversation_id=?', ('conv-v2',)).fetchone()['n'] == 4
        assert db.execute(
            "SELECT value FROM schema_meta WHERE key='_turn_schema_version'"
        ).fetchone()['value'] == '2'
    with pytest.raises(RuntimeError, match='already contains writes'):
        apply_plans([plan])
