"""Executable contract for the signal-driven Project Brain hard cut."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import re
import sys
import uuid

import pytest


pytest_plugins = ('tests._chat_sidecar',)
pytestmark = [pytest.mark.unit, pytest.mark.usefixtures('chat_sidecar')]

# Non-matching probe path for run_matching_checkers — deliberately not a file.
_AUDIT_SYNTHETIC_REPO_PATHS = {'docs/guide.md', 'lib/auth.py'}
ROOT = Path(__file__).resolve().parents[2]

RETIRED_MODEL_TOOLS = {
    'project_charter_read', 'project_charter_propose',
    'project_board_read', 'project_board_post', 'project_board_claim',
    'project_board_complete', 'project_board_block',
    'project_peer_status', 'project_feed_read', 'project_message',
    'project_intervene', 'integration_status',
}

RETIRED_HTTP_PATHS = {
    '/api/v1/project/board/post', '/api/v1/project/board/complete',
    '/api/v1/project/board/block', '/api/v1/project/board/reopen',
    '/api/v1/project/board/delete', '/api/v1/project/board/answer',
    '/api/v1/project/brain/attention', '/api/v1/project/brain/attention/add',
    '/api/v1/project/charter/commit', '/api/v1/project/charter/pending',
    '/api/v1/project/charter/dismiss',
    '/api/v1/project/charter/decision/update',
    '/api/v1/project/charter/decision/delete',
    '/api/v1/project/charter/delete',
    '/api/v1/project/brain/peers', '/api/v1/project/brain/influence',
    '/api/v1/project/brain/peer-message', '/api/v1/project/brain/peer-abort',
    '/api/v1/project/brain/status/history', '/api/v1/project/brain/status/ask',
}


def _project(prefix: str) -> str:
    return f'/tmp/{prefix}-{uuid.uuid4().hex}'


def _task(task_id: str, conv_id: str, *, query: str = 'Implement request') -> dict:
    return {
        'id': task_id,
        'convId': conv_id,
        '_userId': 1,
        'config': {'projectPath': '/unused'},
        'lastUserQuery': query,
    }


@pytest.fixture
def coordinator_binding(monkeypatch):
    from lib.conversations.project_coordination import ProjectCoordinationRepository

    bindings = {}

    def read(repository, project):
        conversation_id = bindings.get((repository.user_id, project), '')
        return {
            'projectPath': project, 'conversationId': conversation_id,
            'availability': 'available' if conversation_id else 'unbound',
        }

    monkeypatch.setattr(ProjectCoordinationRepository, 'read', read)

    def bind(project, conversation_id, user_id=1):
        bindings[(user_id, project)] = conversation_id

    return bind


def _start_payload(work_id: str, task_id: str, conv_id: str) -> dict:
    return {
        'owner_user_id': 1,
        'project_key': '/tmp/project-brain-replay',
        'work_item': {
            'id': work_id,
            'taskId': task_id,
            'conversationId': conv_id,
            'title': 'Replay-safe work',
            'trigger': 'file_write',
            'status': 'active',
            'changedPaths': [],
            'artifacts': [],
            'resultSummary': '',
            'startedAt': 1,
            'finishedAt': None,
            '_titlePriority': 100,
            '_titleRefined': False,
        },
        'timestamp': 1,
    }


def test_machine_readable_contract_is_valid_and_cataloged():
    from jsonschema.validators import Draft202012Validator

    schema_path = ROOT / 'contracts/project_brain_v1.schema.json'
    schema = json.loads(schema_path.read_text(encoding='utf-8'))
    Draft202012Validator.check_schema(schema)
    catalog = json.loads((ROOT / 'docs/catalog.json').read_text(encoding='utf-8'))
    assert any(row['path'] == 'contracts/project_brain_v1.schema.json'
               and row['guard'] == 'tests/project_mod/test_project_brain_signal_driven.py'
               for row in catalog['contracts'])


def test_command_receipt_and_rebuild_are_idempotent(chat_sidecar):
    del chat_sidecar
    from lib.storage import get_storage_client
    from lib.conversations.project_brain import deterministic_work_id

    task_id = 'task-replay-' + uuid.uuid4().hex
    work_id = deterministic_work_id(task_id)
    payload = _start_payload(work_id, task_id, 'conv-replay')
    payload['project_key'] = _project('replay')
    command_id = 'project-work-start:' + work_id
    client = get_storage_client(write=True)
    first = client.command('project_brain.work.start', payload, command_id)
    second = client.command('project_brain.work.start', payload, command_id)
    assert first == second
    assert first['event']['kind'] == 'work_started'

    rebuilt = client.maintenance('project_brain.rebuild', {
        'owner_user_id': 1, 'project_key': payload['project_key'],
    })
    assert rebuilt['headSequence'] == 1
    assert rebuilt['replayedEvents'] == 1
    assert rebuilt['projection']['workItems'][0]['id'] == work_id

    from lib.storage.errors import StorageError
    for retired_or_reserved_stream in (
            'project_brain', 'project_feed', 'project_status'):
        with pytest.raises(StorageError, match='event.append only accepts'):
            client.command('event.append', {
                'task_id': f'forged-{retired_or_reserved_stream}',
                'sequence': 1,
                'stream_kind': retired_or_reserved_stream,
                'event': {'type': 'forged'},
            }, None)


def test_concurrent_todo_and_file_signals_create_one_immutable_work_item(
        chat_sidecar):
    del chat_sidecar
    from lib.conversations.project_brain import (
        board_projection, deterministic_work_id, note_file_signal,
        note_todo_signal,
    )

    project = _project('signals')
    task = _task('task-' + uuid.uuid4().hex, 'conv-original')
    todos = [{'content': 'Implement the signal cut', 'status': 'in_progress'}]

    with ThreadPoolExecutor(max_workers=2) as executor:
        todo_future = executor.submit(
            note_todo_signal, task, project, todos, accepted=True)
        file_future = executor.submit(
            note_file_signal, task, project, fn_name='write_file',
            fn_args={'path': 'src/project.py'}, tool_content='ok')
        assert todo_future.result() == file_future.result()

    board = board_projection(project, user_id=1)
    assert len(board['active']) == 1
    item = board['active'][0]
    assert item['id'] == deterministic_work_id(task['id'])
    assert item['conversationId'] == 'conv-original'
    assert item['title'] == 'Implement the signal cut'
    assert item['status'] == 'active'
    assert item['changedPaths'] == ['src/project.py']
    assert set(item) == {
        'id', 'taskId', 'conversationId', 'title', 'trigger', 'status',
        'changedPaths', 'artifacts', 'resultSummary', 'startedAt', 'finishedAt',
    }

    # Same project key under another owner has no visible work.
    assert board_projection(project, user_id=2)['active'] == []


def test_isolated_workspace_signal_is_wired_at_physical_worker_start(
        chat_sidecar, monkeypatch):
    del chat_sidecar
    import lib.conversations.project_brain as brain
    from lib.conversations.project_brain import (
        board_projection, deterministic_work_id,
        note_isolated_workspace_signal,
    )
    import lib.integration_control as integration
    from lib.tasks_pkg import spawn

    project = _project('isolated-start')
    task = _task('task-' + uuid.uuid4().hex, 'conv-isolated')
    expected_work_id = deterministic_work_id(task['id'])
    monkeypatch.setattr(
        integration, 'has_active_workspace_for_work',
        lambda path, work_id, *, user_id: (
            path == project and work_id == expected_work_id and user_id == 1),
    )

    assert note_isolated_workspace_signal(task, project) == expected_work_id
    board = board_projection(project, user_id=1)
    assert board['active'][0]['trigger'] == 'isolated_workspace'

    physical_task = _task(
        'task-' + uuid.uuid4().hex, 'conv-physical-start')
    physical_task['config']['projectPath'] = project
    observed: list[tuple[dict, str]] = []
    monkeypatch.setattr(
        brain, 'note_isolated_workspace_signal',
        lambda task, path: observed.append((task, path)) or 'pw_observed',
    )
    assert spawn._mark_worker_started_locked(physical_task, None) is True
    assert observed == [(physical_task, project)]


def test_title_refines_once_and_terminal_states_have_no_block_or_open(
        chat_sidecar):
    del chat_sidecar
    from lib.conversations.project_brain import (
        board_projection, feed_projection, note_file_signal, note_todo_signal,
        settle_work_item,
    )

    project = _project('lifecycle')
    task = _task('task-' + uuid.uuid4().hex, 'conv-life', query='User request title')
    note_file_signal(
        task, project, fn_name='write_file',
        fn_args={'path': 'src/first.py'}, tool_content='ok')
    note_todo_signal(task, project, [
        {'content': 'Higher priority todo', 'status': 'in_progress'},
    ], accepted=True)
    note_todo_signal(task, project, [
        {'content': 'Different later todo', 'status': 'in_progress'},
    ], accepted=True)
    assert board_projection(project, user_id=1)['active'][0]['title'] == (
        'Higher priority todo')

    task['content'] = 'Completed with a file.'

    task.update(status='done', finishReason='stop', streamState='provider_finished')
    assert settle_work_item(task, project) == 'completed'
    board = board_projection(project, user_id=1)
    assert board['active'] == []
    assert board['recentOutcomes'][0]['status'] == 'completed'
    assert all(item['status'] in {'active', 'completed', 'failed', 'cancelled'}
               for item in board['recentOutcomes'])
    feed = feed_projection(project, user_id=1)
    assert [event['kind'] for event in feed['events']] == ['work_result']
    assert not any(event['kind'] in {'started', 'completed', 'blocked'}
                   for event in feed['events'])

    quiet_project = _project('quiet-success')
    quiet = _task('task-' + uuid.uuid4().hex, 'conv-quiet')
    note_todo_signal(quiet, quiet_project, [
        {'content': 'Answer only', 'status': 'in_progress'},
    ], accepted=True)
    quiet.update(status='done', finishReason='stop', streamState='provider_finished')
    assert settle_work_item(quiet, quiet_project) == 'completed'
    assert feed_projection(quiet_project, user_id=1)['events'] == []

    failed_project = _project('failed')
    failed = _task('task-' + uuid.uuid4().hex, 'conv-failed')
    note_todo_signal(failed, failed_project, [
        {'content': 'Failing work', 'status': 'in_progress'},
    ], accepted=True)
    failed['error'] = 'execution failed'
    assert settle_work_item(failed, failed_project) == 'failed'
    assert board_projection(failed_project, user_id=1)[
        'recentOutcomes'][0]['status'] == 'failed'

    cancelled_project = _project('cancelled')
    cancelled = _task('task-' + uuid.uuid4().hex, 'conv-cancelled')
    note_todo_signal(cancelled, cancelled_project, [
        {'content': 'Cancelled work', 'status': 'in_progress'},
    ], accepted=True)
    cancelled['aborted'] = True
    assert settle_work_item(cancelled, cancelled_project) == 'cancelled'


def test_narrative_cursor_is_delta_only_acknowledged_and_paged(
        chat_sidecar, coordinator_binding):
    del chat_sidecar
    from lib.conversations.project_brain import (
        confirm_project_context_delivery, prepare_project_context,
    )
    from lib.storage import get_storage_client

    project = _project('cursor')

    coordinator_binding(project, 'conv-cursor')
    task = {'_userId': 1}
    # First sight initializes at head and never emits a history snapshot.
    assert prepare_project_context(
        project, 'conv-cursor', user_id=1, task=task) == ''
    # A single multilingual row is bounded before storage, never clipped by
    # delivery and then acknowledged past unseen content.
    from lib.conversations.project_brain import add_narrative, feed_projection
    from lib.token_counter import count_text
    multilingual_project = _project('cursor-multilingual')

    coordinator_binding(multilingual_project, 'conv-multilingual')
    multilingual_task = {'_userId': 1}
    assert prepare_project_context(
        multilingual_project, 'conv-multilingual', user_id=1,
        task=multilingual_task) == ''
    add_narrative(
        multilingual_project, kind='decision', text='验' * 1200,
        user_id=1, conversation_id='conv-source')
    stored_text = feed_projection(
        multilingual_project, user_id=1)['events'][0]['text']
    assert len(stored_text.encode('utf-8')) <= 720
    delivered = prepare_project_context(
        multilingual_project, 'conv-multilingual', user_id=1,
        task=multilingual_task)
    assert stored_text in delivered
    assert count_text(delivered, model='') <= 900
    assert confirm_project_context_delivery(multilingual_task) is True
    assert '_projectNarrativeDelivery' not in multilingual_task

    client = get_storage_client(write=True)
    for index in range(14):
        client.command('project_brain.narrative.add', {
            'owner_user_id': 1,
            'project_key': project,
            'kind': 'decision',
            'text': f'narrative-{index:02d}',
            'conversation_id': 'conv-source',
            'timestamp': 100 + index,
        }, f'narrative-{uuid.uuid4().hex}')

    first = prepare_project_context(
        project, 'conv-cursor', user_id=1, task=task)
    first_sequences = [
        int(value) for value in re.findall(r'narrative-(\d+)', first)]
    assert first.startswith('[Project Context]')
    assert len(first_sequences) == 12
    # Simulated request failure: without confirmation the same page replays.
    replay = prepare_project_context(
        project, 'conv-cursor', user_id=1, task=task)
    assert [int(value) for value in re.findall(
        r'narrative-(\d+)', replay)] == first_sequences
    assert confirm_project_context_delivery(task) is True

    second = prepare_project_context(
        project, 'conv-cursor', user_id=1, task=task)
    second_sequences = [
        int(value) for value in re.findall(r'narrative-(\d+)', second)]
    assert len(second_sequences) == 2
    assert first_sequences[-1] < second_sequences[0]
    assert confirm_project_context_delivery(task) is True
    assert prepare_project_context(
        project, 'conv-cursor', user_id=1, task=task) == ''


def test_work_result_narrative_names_worker_todos_and_changed_paths(
        chat_sidecar, monkeypatch, coordinator_binding):
    del chat_sidecar
    from lib.conversations.project_brain import (
        confirm_project_context_delivery, feed_projection, note_file_signal,
        note_todo_signal, prepare_project_context, settle_work_item,
    )

    project = _project('work-result-rich')
    viewer_task = {'_userId': 1}
    own_task = {'_userId': 1}

    coordinator_binding(project, 'conv-viewer')
    # Both cursors initialize at head before the work finishes, so the
    # completed work becomes an unseen narrative page for each of them.
    assert prepare_project_context(
        project, 'conv-viewer', user_id=1, task=viewer_task) == ''
    coordinator_binding(project, 'conv-worker')
    assert prepare_project_context(
        project, 'conv-worker', user_id=1, task=own_task) == ''

    worker = _task('task-' + uuid.uuid4().hex, 'conv-worker')
    note_file_signal(
        worker, project, fn_name='write_file',
        fn_args={'path': 'lib/auth.py'}, tool_content='ok')
    note_todo_signal(worker, project, [
        {'content': '修复重试逻辑', 'status': 'in_progress'},
    ], accepted=True)
    worker['_todos'] = [
        {'content': '修复重试逻辑', 'status': 'completed'},
        {'content': '补充回归用例', 'status': 'completed'},
        {'content': '跑全量测试', 'status': 'completed'},
        {'content': '尚未开始的事', 'status': 'pending'},
    ]
    worker['content'] = '全绿（59 passed）。三件事都处理完了。'

    worker.update(status='done', finishReason='stop', streamState='provider_finished')
    assert settle_work_item(worker, project) == 'completed'

    event = feed_projection(project, user_id=1)['events'][0]
    assert event['kind'] == 'work_result'
    summary_lines = event['text'].splitlines()
    assert summary_lines[0] == '全绿（59 passed）。三件事都处理完了。'
    assert summary_lines[1] == '完成项: 修复重试逻辑; 补充回归用例; 跑全量测试'
    assert len(event['text'].encode('utf-8')) <= 720

    monkeypatch.setattr(
        'lib.conversations.repository.list_conversations',
        lambda *, user_id, ids, include_messages: [
            {'id': 'conv-worker', 'title': '修登录重试'}])
    coordinator_binding(project, 'conv-viewer')
    delivered = prepare_project_context(
        project, 'conv-viewer', user_id=1, task=viewer_task)
    assert re.search(
        r'- 同一项目的其他工作者 会话「修登录重试」\(conv-worker\) '
        r'完成了: 修复重试逻辑; 补充回归用例; 跑全量测试。'
        r'改动文件: lib/auth\.py',
        delivered)
    assert '尚未开始' not in delivered
    # The plain statement replaces the assistant sign-off, field labels and
    # all internal bookkeeping in the model-visible text.
    assert '全绿（59 passed）' not in delivered
    assert '完成项' not in delivered
    assert '[work_result]' not in delivered
    assert not re.search(r'#\d+', delivered)
    assert confirm_project_context_delivery(viewer_task) is True

    coordinator_binding(project, 'conv-worker')
    own = prepare_project_context(
        project, 'conv-worker', user_id=1, task=own_task)
    assert '本会话「修登录重试」(conv-worker) 完成了:' in own
    assert confirm_project_context_delivery(own_task) is True


def test_work_result_summary_budget_and_no_completed_todo_fallback(
        chat_sidecar, coordinator_binding):
    del chat_sidecar
    from lib.conversations.project_brain import (
        feed_projection, note_file_signal, note_todo_signal,
        prepare_project_context, settle_work_item,
    )

    project = _project('work-result-budget')
    task = _task('task-' + uuid.uuid4().hex, 'conv-budget')
    note_file_signal(
        task, project, fn_name='write_file',
        fn_args={'path': 'src/budget.py'}, tool_content='ok')
    note_todo_signal(task, project, [
        {'content': 'Long running work', 'status': 'in_progress'},
    ], accepted=True)
    task['_todos'] = [{'content': 'done item', 'status': 'completed'}]
    task['content'] = '全' * 1000

    task.update(status='done', finishReason='stop', streamState='provider_finished')
    assert settle_work_item(task, project) == 'completed'
    text = feed_projection(project, user_id=1)['events'][0]['text']
    assert len(text.encode('utf-8')) <= 720
    assert '完成项: done item' in text

    plain_project = _project('work-result-plain')

    coordinator_binding(plain_project, 'conv-plain-viewer')
    plain_viewer = {'_userId': 1}
    assert prepare_project_context(
        plain_project, 'conv-plain-viewer', user_id=1,
        task=plain_viewer) == ''
    plain = _task('task-' + uuid.uuid4().hex, 'conv-plain')
    note_file_signal(
        plain, plain_project, fn_name='write_file',
        fn_args={'path': 'src/plain.py'}, tool_content='ok')
    note_todo_signal(plain, plain_project, [
        {'content': 'Only ever in progress', 'status': 'in_progress'},
    ], accepted=True)
    plain['content'] = 'single line outcome'

    plain.update(status='done', finishReason='stop', streamState='provider_finished')
    assert settle_work_item(plain, plain_project) == 'completed'
    plain_text = feed_projection(
        plain_project, user_id=1)['events'][0]['text']
    assert plain_text == 'single line outcome'
    # No completed checklist item: the stored summary is the fallback body.
    delivered = prepare_project_context(
        plain_project, 'conv-plain-viewer', user_id=1, task=plain_viewer)
    assert re.search(
        r'- 同一项目的其他工作者 会话\(conv-plain\) '
        r'完成了: single line outcome。改动文件: src/plain\.py',
        delivered)

    failed_project = _project('work-result-failed')

    coordinator_binding(failed_project, 'conv-failed-viewer')
    failed_viewer = {'_userId': 1}
    assert prepare_project_context(
        failed_project, 'conv-failed-viewer', user_id=1,
        task=failed_viewer) == ''
    failed = _task('task-' + uuid.uuid4().hex, 'conv-failed-worker')
    note_file_signal(
        failed, failed_project, fn_name='write_file',
        fn_args={'path': 'src/fail.py'}, tool_content='ok')
    failed['error'] = 'boom'
    assert settle_work_item(failed, failed_project) == 'failed'
    failed_delivered = prepare_project_context(
        failed_project, 'conv-failed-viewer', user_id=1, task=failed_viewer)
    assert '未能完成: boom' in failed_delivered
    assert '改动文件: src/fail.py' in failed_delivered


def test_watch_human_decisions_reach_existing_conversations_once(
        chat_sidecar, coordinator_binding):
    del chat_sidecar
    from lib.conversations.project_brain import (
        add_watch_item, confirm_project_context_delivery, delete_watch_item,
        feed_projection, prepare_project_context, update_watch_item,
    )

    project = _project('watch-narrative')

    coordinator_binding(project, 'conv-watch-reader')
    task = _task('task-watch-reader', 'conv-watch-reader')
    assert prepare_project_context(
        project, 'conv-watch-reader', user_id=1, task=task) == ''

    item = add_watch_item(
        project, kind='concern', text='Keep migrations reversible.',
        user_id=1, source_conversation_id='conv-owner')
    context = prepare_project_context(
        project, 'conv-watch-reader', user_id=1, task=task)
    assert 'Watch added: Keep migrations reversible.' in context
    assert 'Watch:' in context and 'Keep migrations reversible.' in context
    assert confirm_project_context_delivery(task) is True
    assert prepare_project_context(
        project, 'conv-watch-reader', user_id=1, task=task) == ''

    update_watch_item(
        project, item['id'], user_id=1, status='resolved')
    assert 'Watch resolved: Keep migrations reversible.' in (
        prepare_project_context(
            project, 'conv-watch-reader', user_id=1, task=task))
    assert confirm_project_context_delivery(task) is True
    delete_watch_item(project, item['id'], user_id=1)
    assert 'Watch removed: Keep migrations reversible.' in (
        prepare_project_context(
            project, 'conv-watch-reader', user_id=1, task=task))
    assert [event['kind'] for event in feed_projection(
        project, user_id=1)['events'][:3]] == [
            'watch_deleted', 'watch_updated', 'watch_added']


def test_project_context_is_final_user_meta_and_system_bytes_do_not_change():
    from lib.tasks_pkg.context_composer import ComposeRequest, ContextBlock
    from lib.tasks_pkg.context_composer._render import render_context

    messages = [
        {'role': 'system', 'content': 'byte-stable-system'},
        {'role': 'user', 'content': 'request'},
    ]
    original_system = json.dumps(messages[0], sort_keys=True)
    block = ContextBlock(
        id='project_context', source='project.brain',
        content='[Project Context]\nNew project narrative:\n- #2 [decision] use X',
        authority='project', placement='tail', stability='turn',
        lifecycle='task', priority=10,
    )
    result = render_context(messages, [block], ComposeRequest(model=''))
    assert json.dumps(result.messages[0], sort_keys=True) == original_system
    assert result.messages[-1]['role'] == 'user'
    assert result.messages[-1]['_isMeta'] is True
    body = result.messages[-1]['content'][0]['text']
    assert '[Project Context]' in body
    assert '<system-reminder>' not in body


def test_overlap_advice_is_next_round_only_bounded_and_never_persisted(
        chat_sidecar, monkeypatch):
    del chat_sidecar
    import lib.agent_inbox as agent_inbox
    import lib.conversations.project_brain as brain
    import lib.swarm.integration as swarm_integration
    from lib.tasks_pkg.manager.runtime import chat_task_runtime
    from lib.tasks_pkg.orchestrator._swarm_inbox import drain_and_inject_inbox

    project = _project('overlap')
    left = _task('task-left-' + uuid.uuid4().hex, 'conv-left')
    right = _task('task-right-' + uuid.uuid4().hex, 'conv-right')
    for task in (left, right):
        task['status'] = 'running'
        task['config']['projectPath'] = project
    monkeypatch.setattr(chat_task_runtime, 'snapshot', lambda: [left, right])
    pushes = []
    monkeypatch.setattr(brain, '_push_project_hint',
                        lambda _path, _owner, payload: pushes.append(payload))

    for index in range(25):
        path = f'src/overlap-{index}.py'
        brain.note_file_signal(
            left, project, fn_name='write_file', fn_args={'path': path},
            tool_content='ok')
        brain.note_file_signal(
            right, project, fn_name='write_file', fn_args={'path': path},
            tool_content='ok')

    assert len(left['_projectOverlapAdvisories']) == 20
    assert len(right['_projectOverlapAdvisories']) == 20
    assert len(left['_projectOverlapKeys']) == 20
    assert any(item['type'] == 'path_overlap' for item in pushes)
    # Repeating the same task-pair/path signal cannot expand the bounded set.
    brain.note_file_signal(
        right, project, fn_name='write_file',
        fn_args={'path': 'src/overlap-0.py'}, tool_content='ok')
    assert len(left['_projectOverlapAdvisories']) == 20

    monkeypatch.setattr(agent_inbox, 'drain', lambda *_args, **_kwargs: [])
    monkeypatch.setattr(swarm_integration, 'swarm_key_for',
                        lambda task: task['id'])
    messages = [
        {'role': 'system', 'content': 'stable-prefix'},
        {'role': 'user', 'content': 'request'},
    ]
    drain_and_inject_inbox(
        task=left, messages=messages, round_num=1, tid='left')
    assert messages[0] == {'role': 'system', 'content': 'stable-prefix'}
    assert messages[-1]['role'] == 'user'
    assert '[Project overlap advisory]' in messages[-1]['content']
    assert '_projectOverlapAdvisories' not in left

    right['content'] = 'done'
    brain.settle_work_item(right, project)
    assert '_projectOverlapAdvisories' not in right
    assert brain.feed_projection(project, user_id=1)['events'][0][
        'kind'] == 'work_result'
    assert not any(item['kind'] == 'path_overlap'
                   for item in brain.feed_projection(
                       project, user_id=1)['events'])


def test_checker_versions_gate_charter_and_failure_only_adds_narrative(
        chat_sidecar, tmp_path):
    del chat_sidecar
    from lib.conversations.project_brain import (
        board_projection, charter_projection,
        feed_projection, promote_decision, read_projection, register_checker,
        run_all_enabled_checkers, run_checker, run_matching_checkers,
    )

    project = str(tmp_path.resolve())
    passing = register_checker(project, {
        'checkerId': 'python-pass', 'version': 1, 'label': 'Python pass',
        'argv': [sys.executable, '-c', 'print("passed")'], 'cwd': '.',
        'pathGlobs': ['src/*.py'], 'timeoutMs': 5000, 'enabled': True,
    }, user_id=1)
    result = run_checker(
        project, passing['checkerId'], passing['version'], user_id=1)
    assert result['ok'] is True
    assert 'passed' in result['output']

    decision = promote_decision(
        project, decision_id='decision-one', text='Python checks must pass.',
        checker_id='python-pass', checker_version=1,
        source_conversation_id='conv-source', source_turn_id='turn-source',
        user_id=1,
    )
    assert decision['checkerRef'] == {'id': 'python-pass', 'version': 1}
    assert charter_projection(project, user_id=1)['decisions'] == [decision]
    verified = run_checker(
        project, 'python-pass', 1, user_id=1, reason='manual')
    assert verified['ok'] is True
    assert charter_projection(project, user_id=1)['decisions'][0][
        'latestVerification']['ok'] is True
    matched = run_matching_checkers(
        project, ['src/project.py'], user_id=1, work_id='pw_changed')
    assert [item['checkerRef'] for item in matched] == [
        {'id': 'python-pass', 'version': 1}]
    assert run_matching_checkers(
        project, ['docs/guide.md'], user_id=1, work_id='pw_ignored') == []
    with pytest.raises(ValueError, match='unknown checker version'):
        promote_decision(
            project, decision_id='unchecked', text='Unchecked text',
            checker_id='missing', checker_version=1,
            source_conversation_id='conv-source', source_turn_id='turn-source',
            user_id=1,
        )

    register_checker(project, {
        'checkerId': 'python-fail', 'version': 1, 'label': 'Python fail',
        'argv': [sys.executable, '-c', 'raise SystemExit(3)'], 'cwd': '.',
        'pathGlobs': ['**'], 'timeoutMs': 5000, 'enabled': True,
    }, user_id=1)
    failed = run_checker(project, 'python-fail', 1, user_id=1, work_id='pw_test')
    assert failed['ok'] is False
    assert 'attention' not in read_projection(project, user_id=1)
    assert feed_projection(project, user_id=1)['events'][0]['kind'] == 'checker_failed'
    # Checker failure has no authority to create or revive work state.
    assert board_projection(project, user_id=1)['active'] == []

    register_checker(project, {
        'checkerId': 'version-toggle', 'version': 1, 'label': 'Old enabled',
        'argv': [sys.executable, '-c', 'print("old")'], 'cwd': '.',
        'pathGlobs': ['**'], 'timeoutMs': 5000, 'enabled': True,
    }, user_id=1)
    register_checker(project, {
        'checkerId': 'version-toggle', 'version': 2, 'label': 'New disabled',
        'argv': [sys.executable, '-c', 'print("new")'], 'cwd': '.',
        'pathGlobs': ['**'], 'timeoutMs': 5000, 'enabled': False,
    }, user_id=1)
    automatic = run_all_enabled_checkers(
        project, user_id=1, reason='integration')
    assert not any(result['checkerRef']['id'] == 'version-toggle'
                   for result in automatic)

    register_checker(project, {
        'checkerId': 'python-timeout', 'version': 1, 'label': 'Python timeout',
        'argv': [sys.executable, '-c', 'import time; time.sleep(1)'], 'cwd': '.',
        'pathGlobs': ['**'], 'timeoutMs': 100, 'enabled': True,
    }, user_id=1)
    timed_out = run_checker(
        project, 'python-timeout', 1, user_id=1, reason='manual')
    assert timed_out['ok'] is False and timed_out['timedOut'] is True
    assert len(timed_out['output']) <= 4000


def test_retired_tools_have_zero_schema_and_mutation_routes_are_unregistered():
    from quart import Quart
    from lib.tools.conversation import INTEGRATION_TOOLS
    from routes.api_v1.project import api_v1_project_bp
    from routes.api_v1.project_brain import api_v1_project_brain_bp

    schemas = json.dumps(INTEGRATION_TOOLS, sort_keys=True)
    names = {tool['function']['name'] for tool in INTEGRATION_TOOLS}
    assert names == {'integration_checkpoint', 'integration_submit'}
    assert not any(name in schemas for name in RETIRED_MODEL_TOOLS)

    app = Quart(__name__)
    app.register_blueprint(api_v1_project_bp)
    app.register_blueprint(api_v1_project_brain_bp)
    paths = {rule.rule for rule in app.url_map.iter_rules()}
    assert RETIRED_HTTP_PATHS.isdisjoint(paths)
    assert {
        '/api/v1/project/board', '/api/v1/project/feed',
        '/api/v1/project/charter', '/api/v1/project/brain/status',
        '/api/v1/project/brain/watch',
        '/api/v1/project/brain/checkers',
        '/api/v1/project/brain/checkers/run',
        '/api/v1/project/charter/decision/promote',
    } <= paths


def test_frontend_is_read_only_projection_without_peer_or_legacy_aliases():
    manifest = json.loads((
        ROOT / 'frontend/src/runtime/sections/manifest.json'
    ).read_text(encoding='utf-8'))
    bundle = next(item for item in manifest['lazyBundles']
                  if item['name'] == 'project-brain')
    assert [item['source'] for item in bundle['sections']] == [
        'project-brain.js', 'project-brain-integration.js',
    ]
    source = (ROOT / 'frontend/src/runtime/sections/project-brain.js').read_text(
        encoding='utf-8')
    api = (ROOT / 'frontend/src/runtime/sections/api.js').read_text(
        encoding='utf-8')
    integration = (
        ROOT / 'frontend/src/runtime/sections/project-brain-integration.js'
    ).read_text(encoding='utf-8')
    presence = (ROOT / 'frontend/src/runtime/sections/presence.js').read_text(
        encoding='utf-8')
    html = (ROOT / 'index.html').read_text(encoding='utf-8')
    assert 'data-pb-action="claim"' not in source
    assert 'data-pb-action="block"' not in source
    assert 'data-pb-action="reopen"' not in source
    assert not any(path in api for path in RETIRED_HTTP_PATHS)
    assert 'item.taskId' not in integration
    assert "pushSubscribe('presence'" not in presence
    assert 'peerEpics' not in presence and 'epicsClaimed' not in presence
    assert 'projectBrainTranslateToggle' not in html
    assert 'data-pb-tab="peers"' not in html
    assert 'data-pb-tab="influence"' not in html


def test_restart_recovery_only_finishes_original_work(monkeypatch):
    import lib.conversations.project_brain_startup as startup

    class FakeClient:
        def __init__(self):
            self.commands = []

        def maintenance(self, operation, _payload, **_kwargs):
            assert operation == 'project_brain.recovery.snapshot'
            return {
                'capped': False,
                'projects': [{
                    'ownerUserId': 7,
                    'projectKey': '/project',
                    'workItems': [
                        {'id': 'pw_done', 'taskId': 'task-done', 'status': 'active',
                         'conversationId': 'conv-original'},
                        {'id': 'pw_cancel', 'taskId': 'task-cancel', 'status': 'active',
                         'conversationId': 'conv-original'},
                        {'id': 'pw_lost', 'taskId': 'task-lost', 'status': 'active',
                         'conversationId': 'conv-original'},
                    ],
                }],
            }

        def query(self, operation, payload):
            assert operation == 'task_results.replay_get'
            return {
                'task-done': {'status': 'done', 'metadata': {'finishReason': 'stop'}},
                'task-cancel': {'status': 'aborted'},
                'task-lost': {},
            }[payload['key']]

        def command(self, operation, payload, command_id):
            self.commands.append((operation, payload, command_id))
            return {'ok': True}

    client = FakeClient()
    monkeypatch.setattr(startup, 'get_storage_client', lambda **_kwargs: client)
    assert startup.recover_active_work_items() == 3
    assert [payload['status'] for _, payload, _ in client.commands] == [
        'completed', 'cancelled', 'failed',
    ]
    assert {payload['work_id'] for _, payload, _ in client.commands} == {
        'pw_done', 'pw_cancel', 'pw_lost',
    }
    assert all(operation == 'project_brain.work.finish'
               for operation, _, _ in client.commands)
    assert all('conversation_id' not in payload
               for _, payload, _ in client.commands)


def test_cutover_backup_precedes_migration_and_verification(monkeypatch):
    import lib.conversations.project_brain_startup as startup

    class FakeClient:
        def __init__(self):
            self.calls = []
            self.complete = False

        def query(self, operation, _payload):
            self.calls.append(operation)
            return {'complete': self.complete}

        def health(self, **_kwargs):
            self.calls.append('health')
            return {'backend': 'sqlite'}

        def maintenance(self, operation, **_kwargs):
            self.calls.append(operation)
            assert _kwargs == {'deadline': 4321.0}
            return {'ok': True}

        def command(self, operation, _payload, command_id, **_kwargs):
            self.calls.append(operation)
            assert command_id == 'project-brain-cutover-v1'
            self.complete = True
            return {'verified': True}

    client = FakeClient()
    monkeypatch.setattr(startup, 'get_storage_client', lambda **_kwargs: client)
    monkeypatch.setattr(
        startup, 'storage_backup_timeout_seconds', lambda: 4321)
    result = startup.ensure_project_brain_cutover()
    assert result['complete'] is True
    assert client.calls == [
        'project_brain.cutover.status', 'health', 'system.backup',
        'project_brain.cutover', 'project_brain.cutover.status',
    ]


@pytest.mark.parametrize('case', [
    'ordinary', 'unbound', 'foreign_owner', 'foreign_project',
    'deleted', 'archived', 'read_only', 'moved', 'lookup_failure',
])
def test_noncoordinators_never_acquire_cursor_or_snapshot(monkeypatch, case):
    from types import SimpleNamespace
    import lib.conversations.project_brain as brain
    from lib.conversations.project_coordination import ProjectCoordinationRepository
    from lib.conversations.repository import ConversationRepository

    project = _project('coordinator-gate')
    settings = {'projectPath': project}
    targets = {(1, 'coordinator'): settings}
    monkeypatch.setattr(
        ConversationRepository, 'get',
        lambda self, cid, *, user_id, include_messages: (
            SimpleNamespace(metadata={'settings': targets[(user_id, cid)]})
            if (user_id, cid) in targets else None))
    repository = ProjectCoordinationRepository(1)
    if case != 'unbound':
        repository.bind(project, 'coordinator', expected_revision=0,
                        command_id='bind-' + uuid.uuid4().hex)
    conversation_id, user_id, requested_project = 'coordinator', 1, project
    if case == 'ordinary':
        conversation_id = 'worker'
    elif case == 'foreign_owner':
        user_id = 2
    elif case == 'foreign_project':
        requested_project = project + '-other'
    elif case == 'deleted':
        targets.clear()
    elif case == 'archived':
        settings['archived'] = True
    elif case == 'read_only':
        settings['readOnly'] = True
    elif case == 'moved':
        settings['projectPath'] = project + '-other'
    elif case == 'lookup_failure':
        def failed_read(self, path):
            raise RuntimeError('binding unavailable')
        monkeypatch.setattr(ProjectCoordinationRepository, 'read', failed_read)

    def forbidden(*args, **kwargs):
        pytest.fail('noncoordinator acquired Project Brain storage')

    monkeypatch.setattr(brain, 'get_storage_client', forbidden)
    monkeypatch.setattr(brain, 'read_projection', forbidden)
    task = _task('gate', conversation_id)
    task['_userId'] = user_id
    task['_projectNarrativeDelivery'] = {'deliveryToken': 'uncommitted'}
    assert brain.prepare_project_context(
        requested_project, conversation_id, user_id=user_id, task=task) == ''
    assert '_projectNarrativeDelivery' not in task
    assert brain.confirm_project_context_delivery(task) is False
    assert brain.prepare_project_context(
        requested_project, conversation_id, user_id=user_id) == ''


def test_rebinding_and_task_owner_changes_cannot_ack_pending_page(
        monkeypatch, coordinator_binding):
    import lib.conversations.project_brain as brain
    import lib.agent_core.store as store

    project = _project('rebind')
    task = _task('coordinator-task', 'coordinator')
    coordinator_binding(project, 'coordinator')
    assert brain.prepare_project_context(
        project, 'coordinator', user_id=1, task=task) == ''
    brain.add_narrative(project, kind='note', text='unseen', user_id=1)
    assert 'unseen' in brain.prepare_project_context(
        project, 'coordinator', user_id=1, task=task)

    def forbidden():
        pytest.fail('revoked delivery reached ACK store')

    monkeypatch.setattr(store, 'get_conversation_store', forbidden)
    coordinator_binding(project, 'replacement')
    assert brain.confirm_project_context_delivery(task) is False
    assert '_projectNarrativeDelivery' not in task
    coordinator_binding(project, 'coordinator')
    assert 'unseen' in brain.prepare_project_context(
        project, 'coordinator', user_id=1, task=task)
    task['_userId'] = 2
    assert brain.confirm_project_context_delivery(task) is False
    assert brain.prepare_project_context(
        project, 'coordinator', user_id=1, task=task) == ''
    task['_userId'] = 1
    assert 'unseen' in brain.prepare_project_context(
        project, 'coordinator', user_id=1, task=task)
    task['convId'] = 'worker'
    assert brain.confirm_project_context_delivery(task) is False


def test_coordinator_active_work_excludes_own_conversation(coordinator_binding):
    import lib.conversations.project_brain as brain

    project = _project('active-coordinator')
    coordinator_binding(project, 'coordinator')
    viewer = _task('viewer', 'coordinator')
    assert brain.prepare_project_context(
        project, 'coordinator', user_id=1, task=viewer) == ''
    for cid, title in [('coordinator', 'own active work'), ('worker', 'peer work')]:
        task = _task('active-' + uuid.uuid4().hex, cid, query=title)
        brain.note_todo_signal(task, project, [], accepted=True)
    brain.add_narrative(project, kind='note', text='new note', user_id=1)
    context = brain.prepare_project_context(
        project, 'coordinator', user_id=1, task=viewer)
    assert 'Active work:' in context
    assert 'peer work' in context
    assert 'own active work' not in context
