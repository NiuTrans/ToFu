"""Fresh dynamic reads may elide only an unchanged, still-visible result.

The contract is deliberately stricter than ordinary result caching: mutable
control-plane tools always execute, the prior body is never duplicated in
task state, and compaction/removal of that body forces the next full delivery.
"""

from __future__ import annotations

import threading

import pytest

pytestmark = pytest.mark.unit

_MUTABLE_OBSERVERS = (
    'get_conversation', 'list_artifacts',
    'list_conversations', 'motion_video_check', 'motion_video_env_check',
    'motion_video_probe', 'motion_video_storyboard_check',
    'local_serve_list', 'schedule_list', 'search_memories',
)


def _task(**overrides):
    task = {
        'id': 'unchanged-projection-task',
        'convId': 'unchanged-projection-conv',
        'status': 'running',
        'aborted': False,
        'model': 'test-model',
        '_userId': 1,
        'events': [],
        'events_lock': threading.Lock(),
        '_dispatch_heartbeat': 0.0,
        '_t_last_event': 0.0,
        '_attended': False,
    }
    task.update(overrides)
    return task


def _tool_call(call_id: str, tool_name: str, sequence: int, arguments=None):
    from lib.tasks_pkg.tool_display import _build_tool_round_entry

    arguments = dict(arguments or {})
    _number, round_entry, _event = _build_tool_round_entry(
        tool_name, arguments, call_id, '{}', sequence, False)
    call = {
        'id': call_id,
        'type': 'function',
        'function': {'name': tool_name, 'arguments': '{}'},
    }
    return (
        call, tool_name, call_id, arguments, round_entry['roundNum'],
        round_entry, None,
    )


def _run(task, messages, parsed_call, *, internal_result_sink=None):
    from lib.tasks_pkg.tool_dispatch.api import execute_tool_pipeline

    execute_tool_pipeline(
        task,
        [parsed_call],
        cfg={'autoApply': True},
        project_path='/project',
        project_enabled=True,
        tool_list=[],
        messages=messages,
        all_search_results_text=[],
        round_num=sum(1 for message in messages
                      if message.get('role') == 'tool'),
        model='test-model',
        internal_result_sink=internal_result_sink,
    )


def test_dynamic_observers_execute_fresh_and_compact_only_unchanged(
        monkeypatch):
    import lib.tasks_pkg.tool_dispatch._heartbeat as heartbeat
    import lib.tasks_pkg.tool_dispatch._pipeline as pipeline

    board_v1 = 'board-v1\n' + ('claimed epic details; ' * 20)
    board_v2 = 'board-v2\n' + ('completed epic details; ' * 20)
    produced = iter((board_v1, board_v1, board_v2))
    executions = []

    def fake_execute(task, tc, tool_name, call_id, arguments, rn, round_entry,
                     cfg, project_path, project_enabled, all_tools=None):
        content = next(produced)
        executions.append((tool_name, content))
        return call_id, content, False

    monkeypatch.setattr(heartbeat, '_execute_tool_one', fake_execute)
    monkeypatch.setattr(pipeline, '_execute_tool_one', fake_execute)
    monkeypatch.setattr(pipeline, 'append_event', lambda *_args: None)

    task = _task()
    messages = []
    parsed_calls = []
    for sequence in range(1, 4):
        parsed_call = _tool_call(
            f'conversations-{sequence}', 'list_conversations', sequence)
        parsed_calls.append(parsed_call)
        _run(
            task, messages, parsed_call)

    tool_messages = [m for m in messages if m.get('role') == 'tool']
    assert executions == [
        ('list_conversations', board_v1),
        ('list_conversations', board_v1),
        ('list_conversations', board_v2),
    ], 'an unchanged receipt must never skip the live observer execution'
    assert tool_messages[0]['content'] == board_v1
    assert '[Unchanged:' in tool_messages[1]['content']
    assert 'board-v1' not in tool_messages[1]['content']
    assert tool_messages[2]['content'] == board_v2, (
        'a changed observation must replace the remembered identity and return '
        'its full model projection')
    assert task['_unchanged_tool_result_receipts']
    assert all('board-v' not in repr(value)
               for value in task['_unchanged_tool_result_receipts'].values()), (
        'unchanged tracking may retain hashes and references, never a second '
        'copy of the result body')
    second_round = parsed_calls[1][5]
    assert second_round['compactionLayer'] == 'unchanged'
    assert second_round['toolTokens'] < second_round['rawToolTokens']


@pytest.mark.parametrize('tool_name', _MUTABLE_OBSERVERS)
def test_mutable_observer_ignores_even_a_preexisting_stale_cache_entry(
        monkeypatch, tool_name):
    """A restored task from older code may still carry the stale tuple."""
    import lib.tasks_pkg.tool_dispatch._heartbeat as heartbeat
    import lib.tasks_pkg.tool_dispatch._pipeline as pipeline
    from lib.tasks_pkg.tool_dispatch._flags import _make_cache_key

    executions = []

    def fake_execute(task, tc, name, call_id, arguments, rn, round_entry,
                     cfg, project_path, project_enabled, all_tools=None):
        executions.append(name)
        return call_id, f'fresh version {len(executions)} for {name}', False

    monkeypatch.setattr(heartbeat, '_execute_tool_one', fake_execute)
    monkeypatch.setattr(pipeline, '_execute_tool_one', fake_execute)
    monkeypatch.setattr(pipeline, 'append_event', lambda *_args: None)

    task = _task(
        _tool_result_cache={
            _make_cache_key(tool_name, {}): (
                'STALE TASK-LIFETIME SNAPSHOT', False, 'dedup', None,
                None, None),
        },
        _tool_result_cache_capacity=16,
    )
    messages = []
    _run(task, messages, _tool_call('fresh-1', tool_name, 1))
    _run(task, messages, _tool_call('fresh-2', tool_name, 2))

    assert executions == [tool_name, tool_name]
    assert [message['content'] for message in messages] == [
        f'fresh version 1 for {tool_name}',
        f'fresh version 2 for {tool_name}',
    ]


def test_compacted_prior_projection_forces_next_full_delivery():
    from lib.tasks_pkg.tool_dispatch._unchanged import (
        maybe_project_unchanged_result,
        remember_full_result,
    )

    task = _task(_tool_result_cache={}, _tool_result_cache_capacity=16)
    arguments = {'conv_id': ''}
    snapshot = 'peer snapshot\n' + ('active peer details; ' * 20)
    first = maybe_project_unchanged_result(
        task,
        tool_name='list_conversations',
        arguments=arguments,
        tool_content=snapshot,
        messages=[],
        enabled=True,
    )
    remember_full_result(
        task,
        tool_name='list_conversations',
        arguments=arguments,
        tool_call_id='peer-1',
        projection=first,
        final_model_content=snapshot,
        result_evidence={'evidenceId': 'ev-peer'},
        enabled=True,
    )
    messages = [
        {'role': 'tool', 'tool_call_id': 'peer-1',
         'content': snapshot},
    ]

    unchanged = maybe_project_unchanged_result(
        task,
        tool_name='list_conversations',
        arguments=arguments,
        tool_content=snapshot,
        messages=messages,
        enabled=True,
    )
    assert unchanged.compacted is True
    assert 'peer-1' in unchanged.model_content

    messages[0]['content'] = (
        '[list_conversations result compacted — was 433 chars]')
    restored = maybe_project_unchanged_result(
        task,
        tool_name='list_conversations',
        arguments=arguments,
        tool_content=snapshot,
        messages=messages,
        enabled=True,
    )
    assert restored.compacted is False
    assert restored.model_content == snapshot


def test_receipt_never_replaces_a_shorter_original_result():
    from lib.tasks_pkg.tool_dispatch._unchanged import (
        maybe_project_unchanged_result,
        remember_full_result,
    )

    task = _task(_tool_result_cache={}, _tool_result_cache_capacity=16)
    first = maybe_project_unchanged_result(
        task, tool_name='list_conversations', arguments={},
        tool_content='No active peers.', messages=[], enabled=True)
    remember_full_result(
        task, tool_name='list_conversations', arguments={},
        tool_call_id='peer-short', projection=first,
        final_model_content='No active peers.', result_evidence=None,
        enabled=True)

    repeated = maybe_project_unchanged_result(
        task, tool_name='list_conversations', arguments={},
        tool_content='No active peers.',
        messages=[{'role': 'tool', 'tool_call_id': 'peer-short',
                   'content': 'No active peers.'}],
        enabled=True)

    assert repeated.compacted is False
    assert repeated.model_content == 'No active peers.'


def test_prior_paid_projection_not_raw_body_controls_the_size_gate():
    """An L0 summary can be far smaller than the producer's raw result."""
    from lib.tasks_pkg.tool_dispatch._unchanged import (
        maybe_project_unchanged_result,
        remember_full_result,
    )

    task = _task(_tool_result_cache={}, _tool_result_cache_capacity=16)
    raw = 'large agent body ' * 200
    paid_projection = '{"status":"partial","summary":"tiny"}'
    first = maybe_project_unchanged_result(
        task, tool_name='get_agent_result', arguments={'agent_id': 'a1'},
        tool_content=raw, messages=[], enabled=True)
    remember_full_result(
        task, tool_name='get_agent_result', arguments={'agent_id': 'a1'},
        tool_call_id='agent-large', projection=first,
        final_model_content=paid_projection, result_evidence=None,
        enabled=True, final_model_tokens=9)

    repeated = maybe_project_unchanged_result(
        task, tool_name='get_agent_result', arguments={'agent_id': 'a1'},
        tool_content=raw,
        messages=[{'role': 'tool', 'tool_call_id': 'agent-large',
                   'content': paid_projection}],
        enabled=True)

    assert repeated.compacted is False
    assert repeated.model_content == raw


def test_program_sink_keeps_full_fresh_result_when_model_gets_receipt(
        monkeypatch):
    import lib.tasks_pkg.tool_dispatch._heartbeat as heartbeat
    import lib.tasks_pkg.tool_dispatch._pipeline as pipeline

    agent_body = 'terminal agent body\n' + ('grounded finding; ' * 30)

    def fake_execute(task, tc, tool_name, call_id, arguments, rn, round_entry,
                     cfg, project_path, project_enabled, all_tools=None):
        return call_id, agent_body, False

    monkeypatch.setattr(heartbeat, '_execute_tool_one', fake_execute)
    monkeypatch.setattr(pipeline, '_execute_tool_one', fake_execute)
    monkeypatch.setattr(pipeline, 'append_event', lambda *_args: None)

    class Sink:
        captured = []

        def capture(self, call_id, content):
            self.captured.append((call_id, content))

    task = _task()
    messages = []
    sink = Sink()
    _run(task, messages, _tool_call('agent-1', 'get_agent_result', 1),
         internal_result_sink=sink)
    _run(task, messages, _tool_call('agent-2', 'get_agent_result', 2),
         internal_result_sink=sink)

    assert sink.captured == [
        ('agent-1', agent_body),
        ('agent-2', agent_body),
    ]
    assert messages[0]['content'] == agent_body
    assert '[Unchanged:' in messages[1]['content']


def test_digest_receipts_share_the_existing_bounded_cache_budget():
    from lib.tasks_pkg.tool_dispatch._unchanged import (
        maybe_project_unchanged_result,
        remember_full_result,
    )

    task = _task(_tool_result_cache={}, _tool_result_cache_capacity=16)
    for index in range(40):
        arguments = {'conv_id': f'peer-{index}'}
        projection = maybe_project_unchanged_result(
            task,
            tool_name='list_conversations',
            arguments=arguments,
            tool_content=f'snapshot-{index}',
            messages=[],
            enabled=True,
        )
        remember_full_result(
            task,
            tool_name='list_conversations',
            arguments=arguments,
            tool_call_id=f'call-{index}',
            projection=projection,
            final_model_content=f'snapshot-{index}',
            result_evidence=None,
            enabled=True,
        )

    assert len(task['_unchanged_tool_result_receipts']) == 16
    assert task['_unchanged_tool_result_receipt_evictions'] == 24
    assert task['_tool_result_cache'] == {}, (
        'digest receipts must not consume or evict full result-cache slots')


def test_registry_separates_fresh_observers_from_execution_reuse():
    from lib.tasks_pkg.tool_dispatch._flags import (
        _task_idempotent_tools,
        _task_partitions,
    )
    from lib.tasks_pkg.tool_dispatch._unchanged import (
        compact_unchanged_tool_names,
    )

    _writes, execution_reuse = _task_partitions({})
    retry_safe = _task_idempotent_tools({})
    compact_unchanged = compact_unchanged_tool_names()

    dynamic = {
        'list_conversations', 'get_conversation',
        'motion_video_env_check', 'motion_video_storyboard_check',
        'motion_video_check', 'motion_video_probe',
        'search_memories', 'schedule_list', 'list_artifacts',
        'local_serve_list',
    }
    assert dynamic.isdisjoint(execution_reuse)
    assert dynamic <= retry_safe
    assert dynamic <= compact_unchanged
    assert 'get_agent_result' in compact_unchanged

    stable_reuse = {
        'web_search', 'fetch_url', 'read_files', 'grep_search', 'find_files',
        'load_skill', 'read_skill_resource', 'read_tool_artifact',
        'search_tool_artifact', 'search_tools',
    }
    assert stable_reuse <= execution_reuse, (
        'fresh-observer policy must not disable expensive stable evidence '
        'reuse or file reads protected by FreshGate')
