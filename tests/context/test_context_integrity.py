"""Executable context-integrity contracts: provenance, freshness, live names."""
from __future__ import annotations

import copy

import pytest


pytestmark = pytest.mark.unit
from lib.tasks_pkg.context_composer import ComposeRequest, context_block, render_context
from lib.memory.prefetch._query import (
    _extract_current_user_request, _build_recent_turns_text,
)


def _text(message):
    content = message.get('content', '')
    if isinstance(content, str):
        return content
    return '\n'.join(block.get('text', '') for block in content)


def test_changed_snapshot_has_visible_precedence_and_stable_reuse():
    messages = [{'role': 'user', 'content': 'request'}]
    render_context(messages, [context_block('parallel_execution', 'OLD')], ComposeRequest())
    frozen = copy.deepcopy(messages)
    block = context_block('parallel_execution', 'CURRENT')
    render_context(messages, [block], ComposeRequest())
    assert messages[:len(frozen)] == frozen
    assert 'replaces all earlier `parallel_execution` snapshots' in _text(messages[-1])
    assert 'historical only' in _text(messages[-1])
    after = copy.deepcopy(messages)
    result = render_context(messages, [block], ComposeRequest())
    assert messages == after
    assert result.manifest[0]['reason'] == 'already_present'


def test_budget_cannot_silently_retain_stale_snapshot_instead_of_correction():
    messages = [{'role': 'user', 'content': 'request'}]
    render_context(messages, [context_block('project_rules', 'OLD')], ComposeRequest())
    result = render_context(messages, [context_block('project_rules', 'CURRENT')],
                            ComposeRequest(global_budget_tokens=1))
    assert 'CURRENT' in _text(messages[-1])
    assert result.manifest[0]['injected'] is True


@pytest.mark.parametrize(('block_id', 'reason'), [
    ('project_rules', 'project_empty'),
    ('selected_skills', 'no_selected_skills'),
    ('relevant_memories', 'memory_disabled'),
    ('goal_objective', 'not_a_goal_worker'),
])
def test_successful_empty_snapshot_withdraws_previous_context(block_id, reason):
    messages = [{'role': 'user', 'content': 'request'}]
    render_context(messages, [context_block(block_id, 'OLD')], ComposeRequest())
    block = context_block(block_id, '', reason=reason)
    render_context(messages, [block], ComposeRequest())
    assert 'no longer active' in _text(messages[-1])
    after = copy.deepcopy(messages)
    render_context(messages, [block], ComposeRequest())
    assert messages == after


@pytest.mark.parametrize('flag', ['_isMeta', '_contextComposer', '_isVirtualUser'])
def test_memory_query_ignores_machine_authored_user_messages(flag):
    messages = [{'role': 'user', 'content': 'actual request'},
                {'role': 'user', flag: True, 'content': 'inspect_image helper'}]
    assert _extract_current_user_request(messages) == 'actual request'
    assert 'inspect_image' not in _build_recent_turns_text(messages)


def test_legacy_owned_sections_and_image_hints_do_not_create_memory_matches():
    from lib.memory.prefetch._run import _confidence
    request = 'Please investigate lifecycle visibility.'
    hint = ('[image ref: /api/images/test.png — call inspect_image with '
            'path="/api/images/test.png" to zoom/crop the original]')
    section = ('<!-- tofu-context:parallel_execution:start -->'
               '<system-reminder>inspect_image</system-reminder>'
               '<!-- tofu-context:parallel_execution:end -->')
    query = _extract_current_user_request([{'role': 'user', 'content': hint + request + section}])
    assert query == request
    assert not _confidence(query, {'name': 'inspect_image', 'description': 'image viewer'})[0]
    # An explicitly authored identifier is still valid relevance evidence.
    assert _confidence('inspect_image', {'name': 'inspect_image'})[0]


def test_projection_preserves_authored_text_before_adding_references():
    from lib.tasks_pkg.conv_message_builder._transform import _build_user_message
    message = _build_user_message({'role': 'user', 'content': 'actual request',
                                   'replyQuote': 'inspect_image generated context'})
    assert 'inspect_image' in message['content']
    assert _extract_current_user_request([message]) == 'actual request'


def test_image_only_request_does_not_fall_back_to_previous_topic():
    messages = [{'role': 'user', 'content': 'inspect_image'},
                {'role': 'user', '_authoredUserText': '',
                 'content': [{'type': 'text', 'text': 'generated attachment'}]}]
    assert _extract_current_user_request(messages) == ''


def test_prefetch_empty_result_clears_stale_task_selection(monkeypatch):
    from lib.memory.prefetch import _run
    monkeypatch.setattr(_run, 'PREFETCH_ENABLED', True)
    task = {'_prefetchedMemories': [{'name': 'obsolete'}]}
    assert _run.run_memory_prefetch([{'role': 'user', 'content': ''}], None, task=task) == []
    assert task['_prefetchedMemories'] == []
    assert task['_memoryPrefetch']['reason'] == 'empty_query'


def test_injected_repository_guidance_has_no_retired_executor_names():
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    text = (root / 'AGENTS.md').read_text()
    assert 'functions.exec`' not in text
    assert '@exec:' not in text


@pytest.mark.parametrize('failure', [OSError('disk unavailable'), TimeoutError('busy')])
def test_project_read_failure_is_not_an_authoritative_empty_snapshot(monkeypatch, failure):
    from concurrent.futures import Future
    from lib.tasks_pkg.context_composer import _providers as providers
    import lib.project_mod as project_mod
    future = Future()
    future.set_exception(failure)
    request = ComposeRequest(project_enabled=True, project_path='/project',
                             task={'_prefetch_project': future})
    with pytest.raises(type(failure)):
        providers._project_rules(request)
    def fail_read(*args, **kwargs):
        raise failure
    monkeypatch.setattr(project_mod, 'get_context_for_prompt', fail_read)
    with pytest.raises(type(failure)):
        providers._project_rules(ComposeRequest(project_enabled=True, project_path='/project'))


def test_merged_human_requests_keep_latest_authored_relevance():
    from lib.tasks_pkg.conv_message_builder._dedup import _merge_consecutive_same_role
    messages = [{'role': 'user', 'content': text + ' helper', '_authoredUserText': text}
                for text in ('oldest topic', 'middle topic', 'latest topic')]
    _merge_consecutive_same_role(messages)
    assert len(messages) == 1
    assert _extract_current_user_request(messages) == 'latest topic'
    assert 'oldest topic' in messages[0]['content']


def test_authored_text_is_local_provenance_not_a_provider_field_or_replay_blocker():
    from lib.llm_sanitize._fields import _strip_non_api_fields
    from lib.tasks_pkg.model_replay import _portable_messages
    messages = [{'role': 'user', 'content': 'request with helper',
                 '_authoredUserText': 'request'},
                {'role': 'assistant', 'content': 'done'}]
    portable = _portable_messages(messages)
    assert portable == messages
    assert portable is not messages
    wire = _strip_non_api_fields(messages)
    assert '_authoredUserText' not in wire[0]
    assert wire[0]['content'] == 'request with helper'


def test_disabled_prefetch_replaces_previous_failure_status(monkeypatch):
    from lib.memory.prefetch import _run
    monkeypatch.setattr(_run, 'PREFETCH_ENABLED', False)
    task = {'_prefetchedMemories': [{'name': 'stale'}],
            '_memoryPrefetch': {'phase': 'failed'}}
    assert _run.run_memory_prefetch([], None, task=task) == []
    assert task['_prefetchedMemories'] == []
    assert task['_memoryPrefetch'] == {'phase': 'skipped', 'reason': 'disabled'}


def test_orchestrator_prefetch_records_unavailability_not_successful_empty(monkeypatch):
    import lib.memory.prefetch as prefetch
    from lib.tasks_pkg.orchestrator._memory_prefetch import maybe_run_memory_prefetch
    def fail_selection(*args, **kwargs):
        raise RuntimeError('storage offline')
    monkeypatch.setattr(prefetch, 'run_memory_prefetch', fail_selection)
    task = {'_prefetchedMemories': [{'name': 'stale'}]}
    maybe_run_memory_prefetch(task=task, cfg={}, messages=[], tool_list=[],
                              project_path=None, project_enabled=False,
                              memory_enabled=True, has_real_tools=True,
                              injected_tool_calls=0)
    assert task['_prefetchedMemories'] == []
    assert task['_memoryPrefetch']['phase'] == 'failed'


def test_parallel_guidance_uses_only_live_waiting_semantics():
    from lib.tasks_pkg.context_composer import _providers as providers
    text = providers._swarm_guidance(
        ComposeRequest(has_real_tools=True, tool_names=('spawn_agents', 'poll')), '')
    assert 'await_agents' not in text
    assert 'wait_s' in text and 'handles' in text
    assert 'dashboard, not a wait' in text
