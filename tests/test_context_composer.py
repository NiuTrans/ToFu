"""Canonical Context Composer ordering, lifecycle, and observability."""

from __future__ import annotations

import copy
import threading
import time
from concurrent.futures import Future

import pytest

from lib.tasks_pkg.context_composer import (
    ComposeRequest,
    ContextBlock,
    append_context_blocks,
    render_context,
)
from lib.tasks_pkg.context_composer import _providers as providers
from lib.tasks_pkg.context_composer import _provider_executor as provider_executor


pytestmark = pytest.mark.unit


def _block(block_id, content, *, authority='ambient', placement='tail',
           priority=10, dedupe_key=''):
    return ContextBlock(
        id=block_id, source=f'test.{block_id}', content=content,
        authority=authority, placement=placement, stability='turn',
        lifecycle='task', priority=priority, dedupe_key=dedupe_key,
    )


def _texts(message):
    content = message.get('content')
    if isinstance(content, str):
        return content
    return '\n'.join(block.get('text', '') for block in content or []
                     if isinstance(block, dict))


def test_authority_order_is_deterministic_and_higher_authority_is_closer():
    messages = [
        {'role': 'system', 'content': 'operator system'},
        {'role': 'user', 'content': 'request'},
    ]
    blocks = [
        _block('ambient', 'ambient body', authority='ambient',
               placement='system'),
        _block('platform', 'platform body', authority='platform',
               placement='system'),
        _block('workflow', 'workflow body', authority='workflow',
               placement='system'),
    ]
    result = render_context(messages, blocks, ComposeRequest(model='m'))
    system = _texts(result.messages[0])
    assert system.index('operator system') < system.index('ambient body')
    assert system.index('ambient body') < system.index('workflow body')
    assert system.index('workflow body') < system.index('platform body')
    assert [row['id'] for row in result.manifest if row['injected']] == [
        'ambient', 'workflow', 'platform']


def test_head_tail_placement_and_single_reminder_wrapper():
    messages = [{'role': 'user', 'content': 'real request'}]
    blocks = [
        _block('rules', '<system-reminder>\nRULES\n</system-reminder>',
               authority='project', placement='head'),
        _block('evidence', 'EVIDENCE', authority='evidence', placement='tail'),
    ]
    result = render_context(messages, blocks, ComposeRequest())
    assert result.messages[0]['_contextComposer'] is True
    assert result.messages[-1]['_contextComposer'] is True
    assert _texts(result.messages[0]).count('<system-reminder>') == 1
    assert 'RULES' in _texts(result.messages[0])
    assert 'EVIDENCE' in _texts(result.messages[-1])


def test_dedupe_manifest_explains_suppression_and_rerender_is_idempotent():
    original = [{'role': 'user', 'content': 'request'}]
    messages = copy.deepcopy(original)
    blocks = [
        _block('winner', 'ONE', dedupe_key='same'),
        _block('loser', 'TWO', priority=20, dedupe_key='same'),
    ]
    first = render_context(messages, blocks, ComposeRequest())
    loser = next(row for row in first.manifest if row['id'] == 'loser')
    assert loser['injected'] is False
    assert loser['reason'] == 'duplicate_of:winner'

    second = render_context(messages, blocks, ComposeRequest())
    joined = '\n'.join(_texts(message) for message in second.messages)
    assert joined.count('tofu-context:winner:start') == 1
    assert 'TWO' not in joined


def test_round_append_preserves_stable_prefix_and_extends_task_manifest():
    task = {'_contextManifest': [{'id': 'stable', 'injected': True}]}
    messages = [
        {'role': 'system', 'content': 'stable system'},
        {'role': 'user', 'content': 'stable request'},
    ]
    prefix = copy.deepcopy(messages)
    append_context_blocks(
        messages,
        [_block('attachment', 'TODO state', placement='tail')],
        ComposeRequest(task=task),
    )
    assert messages[:2] == prefix
    assert messages[-1]['_contextComposer'] is True
    assert task['_contextManifest'][-1]['id'] == 'attachment'


def test_manifest_contains_budget_hash_and_provenance():
    block = ContextBlock(
        id='budgeted', source='test.source', content='x' * 2000,
        authority='evidence', placement='tail', stability='turn',
        lifecycle='task', max_tokens=10, provenance={'match': 'exact'},
    )
    result = render_context(
        [{'role': 'user', 'content': 'request'}], [block],
        ComposeRequest(model=''),
    )
    row = result.manifest[0]
    assert row['injected'] is True
    assert row['reason'] == 'truncated'
    assert row['hash']
    assert row['tokens'] > 0
    assert row['provenance'] == {'match': 'exact'}
    assert row['order'] == 0


def test_project_rules_reuses_inflight_prefetch_without_duplicate_read(
        monkeypatch):
    import lib.project_mod as project_mod

    future = Future()
    direct_reads = []
    monkeypatch.setattr(
        project_mod,
        'get_context_for_prompt',
        lambda *args, **kwargs: direct_reads.append((args, kwargs)) or 'duplicate',
    )
    future.set_result('prefetched rules')
    request = ComposeRequest(
        project_path='/project', project_enabled=True,
        task={'_prefetch_project': future},
    )

    assert providers._project_rules(request) == 'prefetched rules'
    assert direct_reads == []


def test_provider_deadline_does_not_allow_late_live_task_mutation(monkeypatch):
    monkeypatch.setattr(providers, '_CONTEXT_PROVIDER_DEADLINE_SECONDS', 0.03)

    def slow_profile(request):
        time.sleep(0.10)
        request.task['_appliedPreferences'] = {'late': True}
        return 'late profile'

    monkeypatch.setattr(providers, '_profile_block', slow_profile)
    task = {'config': {}}
    started = time.monotonic()
    blocks = providers.collect_context_blocks(
        [{'role': 'user', 'content': 'request'}], ComposeRequest(task=task),
    )
    elapsed = time.monotonic() - started

    assert elapsed < 0.09
    assert next(block for block in blocks if block.id == 'user_context').content == ''
    timing = {row['provider']: row for row in task['_contextProviderTimings']}
    assert timing['profile']['status'] == 'timeout'
    assert '_appliedPreferences' not in task
    time.sleep(0.11)
    assert '_appliedPreferences' not in task


def test_successful_project_provider_returns_cursor_receipt_to_live_task(
        monkeypatch):
    monkeypatch.setattr(providers, '_project_blocks', lambda request, _query: (
        request.task.__setitem__('_projectNarrativeDelivery', {
            'deliveryToken': 'token', 'toSequence': 9,
        }) or []
    ))
    task = {'config': {}}

    providers.collect_context_blocks(
        [{'role': 'user', 'content': 'request'}], ComposeRequest(task=task),
    )

    assert task['_projectNarrativeDelivery'] == {
        'deliveryToken': 'token', 'toSequence': 9,
    }


def test_context_provider_executor_bounds_wedged_work():
    executor = provider_executor.BoundedContextProviderExecutor(
        max_workers=1, queue_capacity=1)
    release = threading.Event()
    started = threading.Event()

    def blocked():
        started.set()
        release.wait(timeout=2)
        return 'done'

    running = executor.submit(blocked)
    assert started.wait(timeout=1)
    queued = executor.submit(blocked)
    rejected = executor.submit(blocked)
    snapshot = executor.snapshot()

    assert snapshot == {
        'workers': 1,
        'residentThreads': 1,
        'queued': 1,
        'queueCapacity': 1,
    }
    assert executor._threads[0].daemon is True
    with pytest.raises(
            provider_executor.ContextProviderCapacityError,
            match='queue is saturated'):
        rejected.result(timeout=0)

    release.set()
    assert running.result(timeout=1) == 'done'
    assert queued.result(timeout=1) == 'done'
    executor.shutdown()
    assert executor.snapshot()['residentThreads'] == 0


@pytest.mark.parametrize(
    ('agent_workers', 'expected'),
    [
        (1, (2, 8)),
        (2, (4, 12)),
        (4, (8, 24)),
        (64, (8, 24)),
    ],
)
def test_context_provider_budget_is_launch_derived_and_hard_bounded(
        agent_workers, expected):
    assert provider_executor.context_provider_budget_from_agent_workers(
        agent_workers) == expected


def test_pending_project_prefetch_does_not_occupy_a_second_worker(monkeypatch):
    executor = provider_executor.BoundedContextProviderExecutor(
        max_workers=1, queue_capacity=16)
    pending_project = Future()
    monkeypatch.setattr(providers, '_CONTEXT_PROVIDER_EXECUTOR', executor)
    monkeypatch.setattr(providers, '_CONTEXT_PROVIDER_DEADLINE_SECONDS', 0.05)
    monkeypatch.setattr(providers, '_profile_block', lambda _request: '')
    monkeypatch.setattr(providers, '_memory_guidance', lambda _request: '')
    monkeypatch.setattr(providers, '_skill_index', lambda _request: '')
    monkeypatch.setattr(
        providers, '_swarm_guidance', lambda _request, _query: '')
    monkeypatch.setattr(
        providers, '_project_blocks', lambda _request, _query: [])
    task = {
        'config': {},
        '_prefetch_project': pending_project,
    }

    providers.collect_context_blocks(
        [{'role': 'user', 'content': 'request'}],
        ComposeRequest(
            task=task,
            project_enabled=True,
            project_path='/project',
        ),
    )

    timings = {row['provider']: row for row in task['_contextProviderTimings']}
    assert timings['project_rules']['status'] == 'timeout'
    assert all(
        timings[name]['status'] == 'ok'
        for name in providers._CONTEXT_PROVIDER_NAMES
        if name != 'project_rules'
    )
    pending_project.cancel()
    executor.shutdown()


def test_environment_is_a_tail_block_and_static_survives_project_path_change():
    """# Environment left the static prompt: the composer renders it as a
    per-turn tail block, so changing the project path rewrites only the tail
    while platform_static stays byte-identical (prefix cache preserved)."""

    providers._reset_tail_transitions_for_tests()

    def _blocks(path, task, disabled_blocks=frozenset()):
        return {
            block.id: block
            for block in providers.collect_context_blocks(
                [{'role': 'user', 'content': 'request'}],
                ComposeRequest(
                    project_path=path, project_enabled=True, model='m',
                    user_id=7, conv_id='env-tail-conv', task=task,
                    disabled_blocks=disabled_blocks),
            )
        }

    task1 = {'id': 'env-tail-task'}
    first = _blocks('/tmp/env-alpha', task1)
    task2 = {'id': 'env-tail-task'}
    second = _blocks('/tmp/env-beta', task2)
    assert '# Environment' not in first['platform_static'].content
    assert (first['platform_static'].content
            == second['platform_static'].content)
    assert first['environment'].placement == 'tail'
    assert first['environment'].stability == 'turn'
    assert '/tmp/env-alpha' in first['environment'].content
    assert '/tmp/env-beta' in second['environment'].content

    # Path-change transition: first sight only baselines; the change fires a
    # provenance chip + a one-turn model-facing note; steady state clears it.
    assert '_projectPathChange' not in task1
    assert task2['_projectPathChange'] == {
        'from': '/tmp/env-alpha', 'to': '/tmp/env-beta'}
    assert ('project path changed from "/tmp/env-alpha" to "/tmp/env-beta"'
            in second['environment'].content)
    task3 = {'id': 'env-tail-task'}
    third = _blocks('/tmp/env-beta', task3)
    assert '_projectPathChange' not in task3
    assert 'project path changed' not in third['environment'].content

    task4 = {'id': 'env-tail-task'}
    disabled = _blocks('/tmp/env-alpha', task4,
                       disabled_blocks=frozenset({'environment'}))
    assert disabled['environment'].content == ''
    assert disabled['environment'].suppressed_reason == 'disabled'


def test_mcp_tools_delta_surfaces_late_connected_tools(monkeypatch):
    """Tools that (re)connect after the wire froze are rendered as a
    per-turn tail block with name/description/input_schema, callable via
    execute_tools; on-wire and absent tools never appear."""
    from lib.mcp.tool_search import (
        freeze_wire_definitions,
        mcp_selection_scope_id,
    )

    scope = mcp_selection_scope_id(
        task_id='delta-task', conv_id='delta-conv', owner_user_id=7)
    wire_tool = {'type': 'function', 'function': {
        'name': 'mcp__docs__read', 'description': 'read doc',
        'parameters': {'type': 'object', 'properties': {}}}}
    freeze_wire_definitions(scope, [wire_tool])
    late_tool = {'type': 'function', 'function': {
        'name': 'mcp__docs__write', 'description': 'write doc',
        'parameters': {'type': 'object',
                       'properties': {'path': {'type': 'string'}}}}}
    connected = {'defs': [wire_tool, late_tool]}

    class _Bridge:
        connected = True

        @staticmethod
        def get_openai_tool_defs():
            return connected['defs']

    import lib.mcp as mcp_module
    monkeypatch.setattr(mcp_module, 'get_bridge', lambda: _Bridge())

    providers._reset_tail_transitions_for_tests()

    def _delta_block(conv_id='delta-conv', task_id='delta-task'):
        task = {'id': task_id}
        blocks = providers.collect_context_blocks(
            [{'role': 'user', 'content': 'request'}],
            ComposeRequest(conv_id=conv_id, user_id=7, task=task),
        )
        block = next(block for block in blocks
                     if block.id == 'mcp_tools_delta')
        return block, task

    delta, task = _delta_block()
    assert delta.placement == 'tail'
    assert '<available_mcp_tools>' in delta.content
    assert 'mcp__docs__write' in delta.content
    assert 'input_schema' in delta.content
    assert 'execute_tools' in delta.content
    assert 'mcp__docs__read' not in delta.content
    assert delta.provenance['delta'] == 1
    # First sight only learns the baseline — no transition chip yet.
    assert '_mcpToolsDelta' not in task

    # Server drops the late tool again → block empties (refreshes per turn)
    # and the transition surfaces as a provenance chip on that turn's task.
    connected['defs'] = [wire_tool]
    dropped, task = _delta_block()
    assert dropped.content == ''
    assert dropped.suppressed_reason == 'no_delta'
    assert task['_mcpToolsDelta'] == {
        'added': [], 'removed': ['mcp__docs__write'], 'total': 0}

    # Steady state: the chip is a transition event, not a permanent label.
    _, task = _delta_block()
    assert '_mcpToolsDelta' not in task

    # Reconnect with the extra tool → chip lists the addition.
    connected['defs'] = [wire_tool, late_tool]
    _, task = _delta_block()
    assert task['_mcpToolsDelta'] == {
        'added': ['mcp__docs__write'], 'removed': [], 'total': 1}

    # A scope whose wire was never frozen (first turn) renders nothing.
    unfrozen, _ = _delta_block(conv_id='delta-conv-new',
                               task_id='delta-task-new')
    assert unfrozen.content == ''
    assert unfrozen.suppressed_reason == 'wire_not_frozen'
