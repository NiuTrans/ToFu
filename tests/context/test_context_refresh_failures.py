"""Fault-path specifications for authored relevance and complete snapshots."""
from __future__ import annotations

import builtins
import copy
import os

import pytest

from lib.tasks_pkg.context_composer import ComposeRequest, context_block, render_context
from lib.tasks_pkg.context_composer import _providers as providers

pytestmark = pytest.mark.unit


@pytest.fixture
def isolated_providers(monkeypatch):
    """Keep actual project/memory acquisition, without unrelated user stores."""
    monkeypatch.setattr(providers.system_prompt_base, 'build_static_prompt', lambda **kw: '')
    for name in ('_profile_block', '_memory_guidance', '_swarm_guidance',
                 '_programmatic_guidance'):
        monkeypatch.setattr(providers, name, lambda *a, **kw: '')
    monkeypatch.setattr(providers, '_evolution_guidance', lambda *a: ('', 'disabled'))
    monkeypatch.setattr(providers, '_project_blocks', lambda *a: [])
    monkeypatch.setattr(providers, '_skill_context', lambda *a: {
        'index': '', 'selection': {'content': '', 'receipts': []}})


def _collect(block_id, messages, request):
    return next(block for block in providers.collect_context_blocks(messages, request)
                if block.id == block_id)


def _text(message):
    content = message['content']
    return content if isinstance(content, str) else '\n'.join(
        block.get('text', '') for block in content if isinstance(block, dict))


def _seed(block_id, text):
    messages = [{'role': 'user', 'content': 'Please continue the current task.'}]
    render_context(messages, [context_block(block_id, text)], ComposeRequest())
    return messages


@pytest.mark.parametrize('with_reference', [False, True])
def test_projection_hook_prefetch_never_reintroduces_reference_or_unredacted_text(
        monkeypatch, with_reference):
    from lib.memory.prefetch._query import _extract_current_user_request
    from lib.memory.prefetch._run import _confidence
    from lib.tasks_pkg import tool_hooks
    from lib.tasks_pkg.conv_message_builder._transform import _build_user_message
    from lib.tasks_pkg.manager._registry import create_task
    from lib.tasks_pkg.manager.runtime import chat_task_runtime

    calls = []

    def redact(prompt, task):
        calls.append(prompt)
        return prompt.replace('secret', '[redacted]')

    monkeypatch.setattr(tool_hooks, '_user_prompt_hooks', [redact])
    original = {'role': 'user', 'content': 'Please investigate secret.'}
    if with_reference:
        original['replyQuote'] = 'inspect_image'
    projected = _build_user_message(original)
    task = create_task(None, [projected], {}, user_id=1, transient=True)
    try:
        assert len(calls) == 1
        assert 'secret' not in projected['content']
        assert 'secret' not in projected.get('_authoredUserText', '')
        query = _extract_current_user_request(task['messages'])
        assert not _confidence(query, {'name': 'inspect_image'})[0]
        if with_reference:
            assert 'inspect_image' in projected['content']  # remains reference evidence
            assert query == ''  # opaque hooks cannot recover authored provenance
        else:
            assert query == 'Please investigate [redacted].'
    finally:
        chat_task_runtime.discard(task['id'])


@pytest.mark.parametrize('status', [
    None,
    {'phase': 'failed', 'reason': 'selection_failed'},
    {'phase': 'skipped', 'reason': 'gate_disabled'},
    {'phase': 'skipped', 'reason': 'not_run'},
])
def test_missing_failed_or_skipped_selection_retains_last_snapshot(isolated_providers, status):
    messages = _seed('relevant_memories', 'LAST COMPLETE MEMORY')
    before = copy.deepcopy(messages)
    task = {'_prefetchedMemories': []}
    if status is not None:
        task['_memoryPrefetch'] = status
    request = ComposeRequest(memory_enabled=True, task=task)
    block = _collect('relevant_memories', messages, request)
    result = render_context(messages, [block], request)
    assert messages == before
    assert result.manifest[0]['reason'] == 'retained_after:provider_unavailable'


def test_continuation_gate_does_not_claim_a_fresh_empty_selection(isolated_providers):
    from lib.tasks_pkg.orchestrator._memory_prefetch import maybe_run_memory_prefetch

    messages = _seed('relevant_memories', 'LAST COMPLETE MEMORY')
    before = copy.deepcopy(messages)
    task = {'_prefetchedMemories': [{'name': 'previous match'}]}
    maybe_run_memory_prefetch(
        task=task, cfg={}, messages=messages, tool_list=[], project_path=None,
        project_enabled=False, memory_enabled=True, has_real_tools=True,
        injected_tool_calls=1)
    request = ComposeRequest(memory_enabled=True, task=task)
    result = render_context(messages, [_collect('relevant_memories', messages, request)], request)
    assert messages == before
    assert task['_memoryPrefetch'] == {'phase': 'skipped', 'reason': 'gate_disabled'}
    assert result.manifest[0]['reason'] == 'retained_after:provider_unavailable'


@pytest.mark.parametrize('status', [
    {'phase': 'done', 'selected': 0},
    {'phase': 'skipped', 'reason': 'no_memories'},
    {'phase': 'skipped', 'reason': 'empty_query'},
    {'phase': 'skipped', 'reason': 'disabled'},
    {'phase': 'skipped', 'reason': 'memory_disabled'},
])
def test_empty_selection_is_silent_but_explicit_disable_withdraws_old_snapshot(
        isolated_providers, status):
    messages = _seed('relevant_memories', 'OLD MEMORY')
    before = copy.deepcopy(messages)
    request = ComposeRequest(memory_enabled=True, task={'_memoryPrefetch': status})
    result = render_context(messages, [_collect('relevant_memories', messages, request)], request)
    if status.get('reason') in ('disabled', 'memory_disabled'):
        assert 'no longer active' in _text(messages[-1])
        assert 'retracted:' in result.manifest[0]['reason']
    else:
        assert messages == before
        assert result.manifest[0]['reason'] == 'no_high_confidence_matches'
        assert not result.manifest[0]['injected']
        assert result.manifest[0]['appendedTokens'] == 0


@pytest.mark.parametrize('fault', ['read', 'stat'])
def test_file_level_rule_failure_retains_complete_snapshot_then_recovers(
        isolated_providers, monkeypatch, tmp_path, fault):
    from lib.project_mod import indexer

    monkeypatch.setattr(indexer, '_roots', {})
    claude = tmp_path / 'CLAUDE.md'
    agents = tmp_path / 'AGENTS.md'
    claude.write_text('CLAUDE RULE', encoding='utf-8')
    agents.write_text('COMPLETE AGENT RULE', encoding='utf-8')
    request = ComposeRequest(project_enabled=True, project_path=str(tmp_path), task={})
    initial = providers._project_rules(request)
    assert 'CLAUDE RULE' in initial and 'COMPLETE AGENT RULE' in initial
    messages = _seed('project_rules', initial)
    before = copy.deepcopy(messages)
    real_open, real_stat, real_descriptor_open = builtins.open, os.stat, os.open

    def fail_open(path, *args, **kwargs):
        if os.fspath(path) == str(agents):
            raise PermissionError('simulated AGENTS read failure')
        return real_open(path, *args, **kwargs)


    def fail_descriptor_open(path, *args, **kwargs):
        # Guidance uses no-follow descriptors on POSIX; inject at acquisition.
        if os.fspath(path) == agents.name and kwargs.get('dir_fd') is not None:
            raise PermissionError('simulated AGENTS descriptor read failure')
        return real_descriptor_open(path, *args, **kwargs)
    def fail_stat(path, *args, **kwargs):
        if isinstance(path, (str, os.PathLike)) and os.fspath(path) == str(agents):
            raise PermissionError('simulated AGENTS stat failure')
        return real_stat(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        if fault == 'read' and os.name == 'posix':
            patch.setattr(os, 'open', fail_descriptor_open)
        patch.setattr(builtins if fault == 'read' else os,
                      'open' if fault == 'read' else 'stat',
                      fail_open if fault == 'read' else fail_stat)
        block = _collect('project_rules', messages, request)
        result = render_context(messages, [block], request)
    assert messages == before
    assert result.manifest[0]['reason'] == 'retained_after:provider_unavailable'
    assert next(row for row in request.task['_contextProviderTimings']
                if row['provider'] == 'project_rules')['status'] == 'error'

    agents.write_text('RECOVERED AGENT RULE', encoding='utf-8')
    render_context(messages, [_collect('project_rules', messages, request)], request)
    assert 'RECOVERED AGENT RULE' in _text(messages[-1])
    assert 'replaces all earlier `project_rules` snapshots' in _text(messages[-1])


def test_verified_rule_file_removal_can_replace_prior_snapshot(
        isolated_providers, monkeypatch, tmp_path):
    from lib.project_mod import indexer

    monkeypatch.setattr(indexer, '_roots', {})
    agents = tmp_path / 'AGENTS.md'
    agents.write_text('REMOVED RULE', encoding='utf-8')
    request = ComposeRequest(project_enabled=True, project_path=str(tmp_path), task={})
    messages = _seed('project_rules', providers._project_rules(request))
    agents.unlink()
    render_context(messages, [_collect('project_rules', messages, request)], request)
    assert 'REMOVED RULE' not in _text(messages[-1])
    assert 'replaces all earlier `project_rules` snapshots' in _text(messages[-1])


def test_multi_root_context_defers_tool_usage_to_live_schemas(monkeypatch, tmp_path):
    from lib.project_mod import indexer

    primary = tmp_path / 'primary'
    reference = tmp_path / 'reference'
    primary.mkdir()
    reference.mkdir()
    monkeypatch.setattr(indexer, '_roots', {
        'primary': {'path': str(primary), 'access': 'rw'},
        'reference': {'path': str(reference), 'access': 'ro'},
    })
    text = indexer.get_context_for_prompt(base_path=str(primary))
    assert 'MULTI-ROOT WORKSPACE' in text
    assert 'READ-ONLY' in text and 'REFUSED' in text
    assert 'live tool schema' in text
    assert str(reference / 'src/main.py') in text
    for retired in ('apply_diff', 'insert_content', 'read_files(['):
        assert retired not in text


@pytest.mark.parametrize('block_id', ['parallel_execution', 'programmatic_execution'])
def test_transient_capability_absence_retains_rather_than_retracts(
        isolated_providers, block_id):
    """F1: a capability/wire-catalog blip is not an authoritative withdrawal."""
    messages = _seed(block_id, 'PRIOR CAPABILITY GUIDANCE')
    before = copy.deepcopy(messages)
    block = context_block(block_id, '', reason='capability_unavailable')
    result = render_context(messages, [block], ComposeRequest())
    assert messages == before
    assert result.manifest[0]['reason'] == 'retained_after:capability_unavailable'
    assert 'no longer active' not in _text(messages[-1]) if messages else True


def test_discovery_unavailable_retains_rather_than_retracts(isolated_providers):
    """F1: discovery_unavailable is transient and must not retract a snapshot."""
    messages = _seed('parallel_execution', 'PRIOR SWARM GUIDANCE')
    before = copy.deepcopy(messages)
    block = context_block('parallel_execution', '', reason='discovery_unavailable')
    result = render_context(messages, [block], ComposeRequest())
    assert messages == before
    assert result.manifest[0]['reason'] == 'retained_after:discovery_unavailable'


def test_stat_permission_error_retains_complete_project_rules(
        isolated_providers, monkeypatch, tmp_path):
    """F2: a stat PermissionError is not verified absence of a rule file."""
    from lib.project_mod import indexer

    monkeypatch.setattr(indexer, '_roots', {})
    agents = tmp_path / 'AGENTS.md'
    agents.write_text('COMPLETE AGENT RULE', encoding='utf-8')
    request = ComposeRequest(project_enabled=True, project_path=str(tmp_path), task={})
    initial = providers._project_rules(request)
    assert 'COMPLETE AGENT RULE' in initial
    messages = _seed('project_rules', initial)
    before = copy.deepcopy(messages)
    real_stat = os.stat

    def fail_stat(path, *args, **kwargs):
        if isinstance(path, (str, os.PathLike)) and os.fspath(path) == str(agents):
            raise PermissionError('simulated AGENTS stat failure')
        return real_stat(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os, 'stat', fail_stat)
        block = _collect('project_rules', messages, request)
        result = render_context(messages, [block], request)
    assert messages == before
    assert result.manifest[0]['reason'] == 'retained_after:provider_unavailable'
    assert next(row for row in request.task['_contextProviderTimings']
                if row['provider'] == 'project_rules')['status'] == 'error'


def test_hook_blanked_authored_provenance_does_not_withdraw_memory_snapshot(
        isolated_providers, monkeypatch):
    """F3: an opaque-hook-blanked query is unavailable, not an authoritative empty."""
    from lib.memory.prefetch._run import run_memory_prefetch

    # Latest authored turn has _authoredUserText blanked (hook discarded it).
    hooked = {'role': 'user', 'content': 'rewritten', '_authoredUserText': ''}
    messages = [{'role': 'user', 'content': 'earlier real question'}]
    messages = _seed('relevant_memories', 'LAST COMPLETE MEMORY')
    messages.append(hooked)
    before = copy.deepcopy(messages)
    task = {}
    monkeypatch.setattr(
        'lib.memory.prefetch._run.PREFETCH_ENABLED', True)
    run_memory_prefetch(messages, None, task=task)
    assert task['_memoryPrefetch'] == {'phase': 'failed', 'reason': 'query_unavailable'}
    request = ComposeRequest(memory_enabled=True, task=task)
    block = _collect('relevant_memories', messages, request)
    result = render_context(messages, [block], request)
    assert messages == before
    assert result.manifest[0]['reason'] == 'retained_after:provider_unavailable'
