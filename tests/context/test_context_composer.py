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
    context_block,
    render_context,
)
from lib.tasks_pkg.context_composer import _providers as providers
from lib.tasks_pkg.context_composer import _provider_executor as provider_executor


pytestmark = pytest.mark.unit


def _block(block_id, content, *, authority='ambient', placement='tail',
           priority=10, dedupe_key='', stability='turn'):
    return ContextBlock(
        id=block_id, source=f'test.{block_id}', content=content,
        authority=authority, placement=placement, stability=stability,
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
    assert _texts(result.messages[0]) == 'operator system'
    tail = _texts(result.messages[-1])
    assert tail.index('ambient body') < tail.index('workflow body')
    assert tail.index('workflow body') < tail.index('platform body')
    assert [row['id'] for row in result.manifest if row['injected']] == [
        'ambient', 'workflow', 'platform']
    assert all(row['placement'] == 'tail' for row in result.manifest)
    assert all(row['requestedPlacement'] == 'system'
               for row in result.manifest)


def test_head_tail_placement_and_single_reminder_wrapper():
    messages = [{'role': 'user', 'content': 'real request'}]
    blocks = [
        _block('rules', '<system-reminder>\nRULES\n</system-reminder>',
               authority='project', placement='head'),
        _block('evidence', 'EVIDENCE', authority='evidence', placement='tail'),
    ]
    result = render_context(messages, blocks, ComposeRequest())
    assert result.messages[0] == messages[0]
    assert result.messages[-1]['_contextComposer'] is True
    assert _texts(result.messages[-1]).count('<system-reminder>') == 2
    assert 'RULES' in _texts(result.messages[-1])
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
    assert second.manifest[0]['reused'] is True
    assert second.manifest[0]['reason'] == 'already_present'


def test_same_authority_tail_blocks_amalgamate_into_one_reminder():
    # Three workflow blocks share one <system-reminder>; each keeps its own
    # tofu-context sub-marker so per-block reuse still works.
    messages = [{'role': 'user', 'content': 'request'}]
    blocks = [
        _block('skills_index', 'SKILLS', authority='workflow'),
        _block('parallel_execution', 'PARALLEL', authority='workflow'),
        _block('programmatic_execution', 'PTC', authority='workflow'),
    ]
    result = render_context(messages, blocks, ComposeRequest(model='m'))
    tail = result.messages[-1]
    assert len(tail['content']) == 1
    text = tail['content'][0]['text']
    assert text.count('<system-reminder>') == 1
    assert '[Context authority: workflow; 3 context blocks]' in text
    for block_id in ('skills_index', 'parallel_execution',
                     'programmatic_execution'):
        assert f'<!-- tofu-context:{block_id}:start -->' in text
    assert 'SKILLS' in text and 'PARALLEL' in text and 'PTC' in text


def test_mixed_authorities_do_not_amalgamate_across_groups():
    messages = [{'role': 'user', 'content': 'request'}]
    blocks = [
        _block('relevant_memories', 'MEM', authority='evidence'),
        _block('skills_index', 'SKILLS', authority='workflow'),
        _block('parallel_execution', 'PAR', authority='workflow'),
        _block('user_context', 'PREFS', authority='preference'),
    ]
    result = render_context(messages, blocks, ComposeRequest(model='m'))
    parts = [p['text'] for p in result.messages[-1]['content']]
    # evidence(1) + workflow amalgam(1) + preference(1) = 3 parts.
    assert len(parts) == 3
    workflow = next(p for p in parts if '3 context blocks' in p or '2 context blocks' in p)
    assert 'SKILLS' in workflow and 'PAR' in workflow
    assert any('MEM' in p and 'system-reminder' in p for p in parts)
    assert any('PREFS' in p and 'system-reminder' in p for p in parts)


def test_amalgam_reuse_is_per_block_granular():
    def blocks(skills):
        return [
            _block('skills_index', skills, authority='workflow'),
            _block('parallel_execution', 'PAR', authority='workflow'),
        ]
    messages = [{'role': 'user', 'content': 'request'}]
    render_context(messages, blocks('V1'), ComposeRequest(model='m'))
    frozen = copy.deepcopy(messages)

    # Steady turn: nothing re-appends.
    second = render_context(messages, blocks('V1'), ComposeRequest(model='m'))
    assert messages == frozen
    assert all(row['reason'] == 'already_present' for row in second.manifest)

    # One sibling changes: only it re-appends, as a lone envelope.
    third = render_context(messages, blocks('V2'), ComposeRequest(model='m'))
    appended = [row['id'] for row in third.manifest if row.get('appended')]
    assert appended == ['skills_index']
    new_tail = third.messages[-1]
    assert len(new_tail['content']) == 1
    assert 'V2' in new_tail['content'][0]['text']


def test_lone_block_envelope_is_byte_identical_after_amalgamation_change():
    # A single block of its authority must render exactly the standalone
    # reminder envelope (no amalgam wrapper churn).
    messages = [{'role': 'user', 'content': 'request'}]
    result = render_context(
        messages, [_block('relevant_memories', 'MEM', authority='evidence')],
        ComposeRequest(model='m'))
    text = result.messages[-1]['content'][0]['text']
    assert text == (
        '<!-- tofu-context:relevant_memories:start -->\n'
        '<system-reminder>\n'
        '[Context authority: evidence; source: test.relevant_memories]\n'
        'MEM\n'
        '</system-reminder>\n'
        '<!-- tofu-context:relevant_memories:end -->')


def test_tail_carrier_stamps_carrier_kind_for_wire_projection():
    messages = [{'role': 'user', 'content': 'request'}]
    result = render_context(
        messages, [_block('stable', 'ONE', stability='conversation')],
        ComposeRequest(),
    )
    carrier = result.messages[-1]
    assert carrier['_contextComposer'] is True
    assert carrier['carrier'] == 'tail_context'
    assert result.manifest[0]['carrier'] == 'tail_context'
    # Reuse re-renders nothing: the retained carrier keeps its stamp and the
    # manifest keeps reporting the same carrier kind with zero appended cost.
    second = render_context(
        messages, [_block('stable', 'ONE', stability='conversation')],
        ComposeRequest(),
    )
    assert second.messages[-1] is carrier or second.messages[-1] == carrier
    assert second.messages[-1]['carrier'] == 'tail_context'
    assert second.manifest[0]['reason'] == 'already_present'
    assert second.manifest[0]['carrier'] == 'tail_context'


def test_system_policy_carrier_stamps_system_context_kind():
    from lib.tasks_pkg.context_composer._policy_prefix import policy_carrier
    carrier = policy_carrier([{'_rendered': 'X', 'id': 'platform_static',
                               'hash': 'h', 'stability': 'static',
                               'lifecycle': 'conversation'}])
    assert carrier['role'] == 'system'
    assert carrier['carrier'] == 'system_context'


def test_rerender_keeps_existing_carrier_byte_identical():
    messages = [{'role': 'user', 'content': 'request'}]
    first = render_context(
        messages, [_block('stable', 'ONE', stability='conversation')],
        ComposeRequest(),
    )
    frozen = copy.deepcopy(first.messages)
    render_context(
        messages, [_block('stable', 'ONE', stability='conversation')],
        ComposeRequest(),
    )
    assert messages == frozen


def test_changed_block_appends_version_without_rewriting_prefix():
    messages = [{'role': 'user', 'content': 'request'}]
    render_context(messages, [_block('state', 'VERSION ONE')], ComposeRequest())
    frozen_prefix = copy.deepcopy(messages)

    result = render_context(
        messages, [_block('state', 'VERSION TWO')], ComposeRequest())

    assert messages[:len(frozen_prefix)] == frozen_prefix
    assert len(messages) == len(frozen_prefix) + 1
    assert 'VERSION ONE' in _texts(messages[-2])
    assert 'VERSION TWO' in _texts(messages[-1])
    row = result.manifest[0]
    assert row['injected'] is True
    assert row['reason'].startswith('supersedes:')


def test_explicit_retraction_appends_tombstone_and_keeps_prefix():
    messages = [{'role': 'user', 'content': 'request'}]
    render_context(messages, [_block('plan_mode', 'ACTIVE')], ComposeRequest())
    frozen_prefix = copy.deepcopy(messages)
    disabled = ContextBlock(
        id='plan_mode', source='test.plan', content='', authority='workflow',
        placement='tail', stability='turn', lifecycle='task',
        suppressed_reason='plan_mode_off',
    )

    result = render_context(messages, [disabled], ComposeRequest())

    assert messages[:len(frozen_prefix)] == frozen_prefix
    assert len(messages) == len(frozen_prefix) + 1
    assert 'no longer active' in _texts(messages[-1])
    assert result.manifest[0]['reason'].startswith('supersedes:')


def test_steady_retraction_reuses_one_tombstone_without_growth():
    messages = [{'role': 'user', 'content': 'request'}]
    request = ComposeRequest(model='test-model')
    render_context(
        messages,
        [_block('memory_guidance', 'remember this', stability='conversation')],
        request,
    )
    disabled = ContextBlock(
        id='memory_guidance', source='test.memory', content='',
        authority='ambient', placement='tail', stability='conversation',
        lifecycle='conversation', suppressed_reason='memory_disabled',
    )

    render_context(messages, [disabled], request)
    after_first_retraction = copy.deepcopy(messages)
    result = render_context(messages, [disabled], request)

    assert messages == after_first_retraction
    assert result.manifest[0]['reused'] is True
    assert result.manifest[0]['reason'] == 'already_present'


def test_successful_empty_prefetch_is_silent_without_invalidating_prior_memories():
    # A selection miss says nothing about the truth of historical evidence.
    # No prompt or timeline notice is needed for an empty retrieval.
    messages = [{'role': 'user', 'content': 'request'}]
    request = ComposeRequest(model='test-model')
    render_context(
        messages,
        [_block('relevant_memories', '<relevant_memories>m1</relevant_memories>',
                authority='evidence')],
        request,
    )
    frozen = copy.deepcopy(messages)
    lapsed = ContextBlock(
        id='relevant_memories', source='test.memory', content='',
        authority='evidence', placement='tail', stability='turn',
        lifecycle='task', suppressed_reason='no_high_confidence_matches',
    )

    result = render_context(messages, [lapsed], request)

    assert messages == frozen
    row = result.manifest[0]
    assert row['injected'] is False
    assert not row.get('appended')
    assert row['appendedTokens'] == 0
    assert row['reason'] == 'no_high_confidence_matches'
    render_context(messages, [lapsed], request)
    assert messages == frozen


def test_transient_absence_retains_last_known_block_without_append():
    messages = [{'role': 'user', 'content': 'request'}]
    render_context(messages, [_block('rules', 'STABLE RULES')], ComposeRequest())
    frozen = copy.deepcopy(messages)
    failed = ContextBlock(
        id='rules', source='test.rules', content='', authority='project',
        placement='tail', stability='conversation', lifecycle='conversation',
        suppressed_reason='build_failed',
    )

    result = render_context(messages, [failed], ComposeRequest())

    assert messages == frozen
    assert result.manifest[0]['injected'] is True
    assert result.manifest[0]['reused'] is True
    assert result.manifest[0]['reason'] == 'retained_after:build_failed'


def test_reuse_validates_actual_carrier_bytes_not_stale_metadata():
    messages = [{'role': 'user', 'content': 'request'}]
    render_context(messages, [_block('stable', 'ORIGINAL')], ComposeRequest())
    messages[-1]['content'][0]['text'] = messages[-1]['content'][0][
        'text'].replace('ORIGINAL', 'ALTERED')
    frozen_prefix = copy.deepcopy(messages)

    render_context(messages, [_block('stable', 'ORIGINAL')], ComposeRequest())

    assert messages[:len(frozen_prefix)] == frozen_prefix
    assert len(messages) == len(frozen_prefix) + 1
    assert 'ORIGINAL' in _texts(messages[-1])


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


def test_repeated_round_append_does_not_grow_messages_or_manifest():
    task = {'_contextManifest': []}
    messages = [{'role': 'user', 'content': 'request'}]
    request = ComposeRequest(task=task)
    block = _block('attachment', 'same evidence')

    append_context_blocks(messages, [block], request)
    frozen_messages = copy.deepcopy(messages)
    frozen_manifest = copy.deepcopy(task['_contextManifest'])
    append_context_blocks(messages, [block], request)

    assert messages == frozen_messages
    assert task['_contextManifest'] == frozen_manifest


def test_legacy_marker_text_inside_history_is_never_rewritten():
    historical = {
        'role': 'system',
        'content': (
            'operator text\n<!-- tofu-context:legacy:start -->\n'
            'historical payload\n<!-- tofu-context:legacy:end -->'),
    }
    result = render_context(
        [copy.deepcopy(historical), {'role': 'user', 'content': 'request'}],
        [_block('fresh', 'new tail context', placement='system')],
        ComposeRequest(),
    )
    assert result.messages[0] == historical
    assert result.messages[-1]['role'] == 'user'
    assert result.messages[-1]['_contextComposer'] is True


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
    release = threading.Event()
    finished = threading.Event()

    def slow_profile(request):
        release.wait(2.0)
        request.task['_appliedPreferences'] = {'late': True}
        request.task['config']['nested']['value'] = 'late'
        finished.set()
        return 'late profile'

    monkeypatch.setattr(providers, '_profile_block', slow_profile)
    task = {'config': {'nested': {'value': 'original'}}}
    try:
        blocks = providers.collect_context_blocks(
            [{'role': 'user', 'content': 'request'}], ComposeRequest(task=task),
        )
        # Deadline applies to provider reads, independently of cold imports
        # and pure rendering overhead. A synchronous wait would finish the
        # provider before collect returned and fail this check.
        assert not finished.is_set()
    finally:
        release.set()
    assert next(block for block in blocks if block.id == 'user_context').content == ''
    timing = {row['provider']: row for row in task['_contextProviderTimings']}
    assert timing['profile']['status'] == 'timeout'
    assert '_appliedPreferences' not in task
    assert finished.wait(1.0)
    assert '_appliedPreferences' not in task
    assert task['config']['nested']['value'] == 'original'


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


def test_context_provider_executor_accepts_independent_reads_while_siblings_wedge():
    executor = provider_executor.DemandContextProviderExecutor(
        max_workers=1, queue_capacity=1)
    assert executor.snapshot()['residentThreads'] == 0
    release = threading.Event()
    entered = threading.Barrier(33, timeout=5)
    started = time.monotonic()

    def blocked():
        entered.wait()
        assert release.wait(timeout=5)
        return 'done'

    running = []
    try:
        running = [executor.submit(blocked) for _ in range(32)]
        entered.wait()
        print(f'32 context reads rendezvous in {time.monotonic() - started:.6f}s')
        assert executor.snapshot() == {
            'workers': 1, 'residentThreads': 32, 'queued': 0, 'queueCapacity': 0,
        }
        assert all(thread.daemon for thread in executor.worker_threads_snapshot())
        assert executor.submit(lambda: 'independent').result(timeout=1) == 'independent'
    finally:
        release.set()
        executor.shutdown()
    assert [future.result(timeout=0) for future in running] == ['done'] * 32
    assert executor.snapshot()['residentThreads'] == 0


@pytest.mark.parametrize(
    ('agent_workers', 'expected'),
    [
        (1, (2, 0)),
        (2, (4, 0)),
        (4, (8, 0)),
        (64, (128, 0)),
    ],
)
def test_context_provider_idle_hint_scales_with_launch_capacity_without_queue_quota(
        agent_workers, expected):
    assert provider_executor.context_provider_budget_from_agent_workers(
        agent_workers) == expected


def test_pending_project_prefetch_does_not_occupy_a_second_worker(monkeypatch):
    executor = provider_executor.DemandContextProviderExecutor(
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
    # provenance chip + an independently-addressed one-shot event. Keeping the
    # note out of environment prevents a second environment version next turn.
    assert '_projectPathChange' not in task1
    assert task2['_projectPathChange'] == {
        'from': '/tmp/env-alpha', 'to': '/tmp/env-beta'}
    transition_ids = [
        block_id for block_id in second
        if block_id.startswith('project_path_change_')
    ]
    assert len(transition_ids) == 1
    assert ('project path changed from "/tmp/env-alpha" to "/tmp/env-beta"'
            in second[transition_ids[0]].content)
    assert 'project path changed' not in second['environment'].content
    task3 = {'id': 'env-tail-task'}
    third = _blocks('/tmp/env-beta', task3)
    assert '_projectPathChange' not in task3
    assert not any(
        block_id.startswith('project_path_change_') for block_id in third
    )
    assert second['environment'].content == third['environment'].content

    task4 = {'id': 'env-tail-task'}
    disabled = _blocks('/tmp/env-alpha', task4,
                       disabled_blocks=frozenset({'environment'}))
    assert disabled['environment'].content == ''
    assert disabled['environment'].suppressed_reason == 'disabled'


def test_all_runtime_provider_blocks_use_tail_user_placement(monkeypatch):
    """Runtime context never rewrites the system message or history head.

    Only the registry-declared system blocks (platform_static always,
    ui_nav_directory on browser-frontend turns) may occupy the dedicated
    system prefix; everything else stays a tail user carrier. A non-browser
    task fails closed to no directory.
    """
    monkeypatch.setattr(providers, '_project_rules', lambda _request: 'rules')
    monkeypatch.setattr(providers, '_profile_block', lambda _request: 'prefs')
    monkeypatch.setattr(providers, '_memory_guidance', lambda _request: 'memory')
    monkeypatch.setattr(providers, '_skill_index', lambda _request: 'skills')
    monkeypatch.setattr(
        providers, '_swarm_guidance', lambda _request, _query: 'swarm')

    blocks = providers.collect_context_blocks(
        [{'role': 'system', 'content': 'operator'},
         {'role': 'user', 'content': 'request'}],
        ComposeRequest(
            user_id=7, model='test-model', has_real_tools=True,
            task={'id': 'tail-only', 'config': {}},
        ),
    )
    assert next(b for b in blocks if b.id == 'platform_static').placement == 'system'
    ui_nav = next(b for b in blocks if b.id == 'ui_nav_directory')
    assert ui_nav.placement == 'system'  # registry-declared, gated by surface
    assert ui_nav.content == ''
    assert ui_nav.suppressed_reason == 'non_browser_surface'
    content_blocks = [b for b in blocks
                      if b.content and b.id not in
                      {'platform_static', 'ui_nav_directory'}]
    assert {b.placement for b in content_blocks} == {'tail'}


def test_ui_nav_directory_injects_on_browser_conversation_attempt(
        monkeypatch):
    """The jump-link directory ships only to the browser frontend surface:
    Conversation Sync v3 turn authority (_turnId+_attemptId, not inline)."""
    import lib.ui_nav as ui_nav_module
    monkeypatch.setattr(
        ui_nav_module, 'build_ui_nav_directory', lambda: 'NAV DIRECTORY')
    messages = [{'role': 'user', 'content': 'request'}]

    browser_task = {'id': 'browser', 'config': {},
                    '_turnId': 'turn-1', '_attemptId': 'attempt-1'}
    blocks = providers.collect_context_blocks(
        messages, ComposeRequest(user_id=7, task=browser_task))
    ui_nav = next(b for b in blocks if b.id == 'ui_nav_directory')
    assert ui_nav.content == 'NAV DIRECTORY'
    assert ui_nav.placement == 'system'

    # API/compat/agent surfaces (inline carrier tasks) fail closed.
    api_task = {'id': 'api', 'config': {}, '_inline_messages': True,
                '_turnId': 'turn-1', '_attemptId': 'attempt-1'}
    blocks = providers.collect_context_blocks(
        messages, ComposeRequest(user_id=7, task=api_task))
    ui_nav = next(b for b in blocks if b.id == 'ui_nav_directory')
    assert ui_nav.content == ''
    assert ui_nav.suppressed_reason == 'non_browser_surface'

    # Headless/transient tasks carry no turn authority at all.
    blocks = providers.collect_context_blocks(
        messages, ComposeRequest(user_id=7, task={'id': 'headless',
                                                  'config': {}}))
    ui_nav = next(b for b in blocks if b.id == 'ui_nav_directory')
    assert ui_nav.content == ''
    assert ui_nav.suppressed_reason == 'non_browser_surface'


def test_ui_nav_system_prefix_carrier_lifecycle():
    """The ui_nav system carrier installs after platform_static, reuses
    byte-identical bytes, withdraws on an authoritative surface change, and
    never treats a quoted tail envelope as installed policy."""
    messages = [
        {'role': 'system', 'content': 'operator system'},
        {'role': 'user', 'content': 'request'},
    ]
    blocks = [
        context_block('platform_static', 'PLATFORM v1'),
        context_block('ui_nav_directory', 'NAV v1'),
    ]
    result = render_context(messages, blocks, ComposeRequest(model='m'))
    assert [m.get('role') for m in result.messages[:3]] == [
        'system', 'system', 'system']
    assert _texts(result.messages[0]) == 'operator system'
    assert 'PLATFORM v1' in _texts(result.messages[1])
    assert 'NAV v1' in _texts(result.messages[2])
    assert result.messages[3] == {'role': 'user', 'content': 'request'}

    # Steady state: identical bytes reuse the carriers; nothing moves.
    frozen = copy.deepcopy(result.messages)
    render_context(messages, blocks, ComposeRequest(model='m'))
    assert messages == frozen

    # A platform-profile change replaces only the platform carrier; the nav
    # carrier bytes and its trailing position are untouched.
    render_context(
        messages,
        [context_block('platform_static', 'PLATFORM v2'),
         context_block('ui_nav_directory', 'NAV v1')],
        ComposeRequest(model='m'))
    assert 'PLATFORM v2' in _texts(messages[1])
    assert 'NAV v1' in _texts(messages[2])
    assert 'PLATFORM v1' not in _texts(messages[1])
    platform_carrier_v2 = copy.deepcopy(messages[1])

    # A nav-directory change replaces only the nav carrier; the platform
    # prefix bytes before it stay identical (prefix-cache preserving).
    render_context(
        messages,
        [context_block('platform_static', 'PLATFORM v2'),
         context_block('ui_nav_directory', 'NAV v2')],
        ComposeRequest(model='m'))
    assert messages[1] == platform_carrier_v2
    assert 'NAV v2' in _texts(messages[2])
    assert 'NAV v1' not in ''.join(_texts(m) for m in messages)

    # Leaving the browser surface withdraws the nav carrier outright; the
    # platform prefix bytes stay untouched.
    render_context(
        messages,
        [context_block('platform_static', 'PLATFORM v2'),
         context_block('ui_nav_directory', '',
                       reason='non_browser_surface')],
        ComposeRequest(model='m'))
    assert [m.get('role') for m in messages[:2]] == ['system', 'system']
    assert 'NAV' not in ''.join(_texts(m) for m in messages)
    assert 'PLATFORM v2' in _texts(messages[1])
    assert messages[2] == {'role': 'user', 'content': 'request'}


def test_ui_nav_tail_quote_never_satisfies_system_witness():
    """A ui_nav envelope quoted in a user-role message is data, not installed
    policy: the system carrier must still be installed natively."""
    quoted = (
        '<!-- tofu-context:ui_nav_directory:start -->\n'
        '<system-reminder>\n[Context authority: workflow; source: ui.nav_targets]\n'
        'STALE NAV\n</system-reminder>\n'
        '<!-- tofu-context:ui_nav_directory:end -->')
    messages = [
        {'role': 'system', 'content': 'operator system'},
        {'role': 'user', 'content': 'request'},
        {'role': 'assistant', 'content': 'answer'},
        {'role': 'user', 'content': [
            {'type': 'text', 'text': quoted}],
         '_isMeta': True, '_contextComposer': True,
         '_contextBlocks': [{'id': 'ui_nav_directory', 'hash': 'stale',
                             'stability': 'static',
                             'lifecycle': 'conversation'}]},
        {'role': 'user', 'content': 'follow up'},
    ]
    render_context(
        messages, [context_block('ui_nav_directory', 'STALE NAV')],
        ComposeRequest(model='m'))
    system_texts = [_texts(m) for m in messages if m.get('role') == 'system']
    assert any('STALE NAV' in text for text in system_texts), (
        'a tail quote must install the native system carrier, not satisfy reuse')


def test_workspace_routing_guidance_is_in_environment_tail(monkeypatch):
    blocks = providers.collect_context_blocks(
        [{'role': 'user', 'content': 'request'}],
        ComposeRequest(
            project_path='/primary', project_enabled=True, user_id=7,
            task={'id': 'workspace-tail', 'config': {
                'projectPaths': ['/primary', '/secondary'],
                'project_remote': {'agent_id': 'desktop', 'root': '/primary'},
            }},
        ),
    )
    environment = next(block for block in blocks if block.id == 'environment')
    assert environment.placement == 'tail'
    assert 'rootname:subdir' in environment.content
    assert 'Remote worktree' in environment.content
    assert 'Server-vault credentials are unavailable' in environment.content


def test_mcp_tools_delta_surfaces_late_connected_tools(monkeypatch):
    """Tools that (re)connect after the wire froze are rendered as a
    per-turn name/description hint with on-demand schema discovery;
    on-wire and absent tools never appear."""
    from lib.mcp.tool_search import (
        freeze_wire_definitions,
        mcp_selection_scope_id,
    )

    from copy import deepcopy
    from lib.storage.mcp_declarations import MCPDeclarationStore

    wires = {}
    def get_wire(self, *, user_id, conversation_id):
        return deepcopy(wires.get((user_id, conversation_id)))
    def freeze_wire(self, definitions, *, user_id, conversation_id):
        key = (user_id, conversation_id)
        wires.setdefault(key, deepcopy(definitions))
        return deepcopy(wires[key])
    monkeypatch.setattr(MCPDeclarationStore, 'get', get_wire)
    monkeypatch.setattr(MCPDeclarationStore, 'freeze', freeze_wire)

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
    monkeypatch.setattr(mcp_module, 'get_bridge', lambda owner: _Bridge())

    providers._reset_tail_transitions_for_tests()

    def _delta_block(conv_id='delta-conv', task_id='delta-task'):
        task = {'id': task_id}
        blocks = providers.collect_context_blocks(
            [{'role': 'user', 'content': 'request'}],
            ComposeRequest(conv_id=conv_id, user_id=7, task=task,
                           has_real_tools=True,
                           tool_names=frozenset({'search_tools', 'execute_tools'})),
        )
        block = next(block for block in blocks
                     if block.id == 'mcp_tools_delta')
        return block, task

    delta, task = _delta_block()
    assert delta.placement == 'tail'
    assert '<available_mcp_tools>' in delta.content
    assert 'mcp__docs__write' in delta.content
    assert 'input_schema' not in delta.content
    assert 'search_tools with an exact name' in delta.content
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


def _disclosed_catalog():
    return [
        {'type': 'function', 'function': {
            'name': 'mcp__llm__experiment_run',
            'description': 'Run experiments by id.',
            'parameters': {
                'type': 'object',
                'properties': {'ids': {'type': 'array'},
                               'dry_run': {'type': 'boolean'}},
                'required': ['ids']}}},
        {'type': 'function', 'function': {
            'name': 'read_files',
            'description': 'Read files.',
            'parameters': {'type': 'object', 'properties': {}}}},
    ]


def test_disclosed_tools_block_reminds_deferred_names_and_signatures():
    """Already-disclosed deferred tools resurface as a per-turn tail
    reminder (name + argument signature only) so the model calls them
    through execute_tools instead of re-running broad searches after
    compaction summarized the original search result away."""
    from lib.tools.disclosure_state import record_search_items

    catalog = _disclosed_catalog()
    task = {'id': 'task-disclosed-block',
            '_executable_tool_catalog': catalog}
    record_search_items(task, [
        {'name': 'mcp__llm__experiment_run',
         'arguments_schema': catalog[0]['function']['parameters']},
        {'name': 'read_files',
         'arguments_schema': catalog[1]['function']['parameters']},
    ], catalog=catalog)

    blocks = providers.collect_context_blocks(
        [{'role': 'user', 'content': 'request'}],
        ComposeRequest(
            conv_id='conv-disclosed', user_id=7, task=task,
            has_real_tools=True,
            tool_names=frozenset({'read_files', 'search_tools', 'execute_tools'})),
    )
    block = next(row for row in blocks if row.id == 'disclosed_tools')
    assert block.placement == 'tail'
    assert '<disclosed_tools>' in block.content
    assert 'mcp__llm__experiment_run' in block.content
    assert 'required: ids' in block.content
    assert 'optional: dry_run' in block.content
    assert 'execute_tools' in block.content
    # A wire-visible tool is never reminded as deferred.
    assert 'read_files' not in block.content
    assert block.provenance['deferred'] == 1


def test_disclosed_tools_block_reports_empty_reasons():
    from lib.tools.disclosure_state import record_search_items

    def _block_for(task, **kwargs):
        blocks = providers.collect_context_blocks(
            [{'role': 'user', 'content': 'request'}],
            ComposeRequest(conv_id='conv-x', user_id=7, task=task, **kwargs),
        )
        return next(row for row in blocks if row.id == 'disclosed_tools')

    empty = _block_for({'id': 'task-disclosed-empty'})
    assert empty.content == ''
    assert empty.suppressed_reason == 'no_disclosed_tools'

    visible_only = {'id': 'task-disclosed-visible',
                    '_executable_tool_catalog': _disclosed_catalog()}
    record_search_items(visible_only, [{
        'name': 'read_files',
        'arguments_schema': {'type': 'object', 'properties': {}},
    }], catalog=visible_only['_executable_tool_catalog'])
    suppressed = _block_for(
        visible_only, tool_names=frozenset({'read_files'}))
    assert suppressed.content == ''
    assert suppressed.suppressed_reason == 'no_deferred_tools'

    disabled = _block_for(
        {'id': 'task-disclosed-disabled'},
        disabled_blocks=frozenset({'disclosed_tools'}))
    assert disabled.content == ''
    assert disabled.suppressed_reason == 'disabled'


@pytest.mark.parametrize('extras_include_primary', [True, False])
def test_non_git_primary_keeps_identity_when_additional_repository_is_isolated(extras_include_primary):
    from lib.project_mod.config import build_root_policy
    from lib.tasks_pkg.workspace_delivery import route_project_tool_args
    primary = '/projects/autoharness'
    canonical = '/projects/llm-mcp'
    workspace = '/isolated/task-42'
    policy = build_root_policy(primary, [canonical])
    policy['roots']['llm-mcp']['path'] = workspace
    task = {
        'id': 'multi-root-isolation',
        'config': {'projectPath': primary,
                   'projectPaths': ([primary] if extras_include_primary else []) + [workspace]},
        '_workspaceRootPolicy': policy,
        '_workspaceDelivery': {'repositories': [
            {'canonicalRoot': canonical, 'workspacePath': workspace, 'workId': 'w'}]},
    }
    blocks = providers.collect_context_blocks(
        [{'role': 'user', 'content': 'continue Harbor diagnosis'}],
        ComposeRequest(project_path=primary, project_enabled=True, task=task))
    environment = next(block for block in blocks if block.id == 'environment').content
    assert "autoharness: execution='/projects/autoharness'; canonical='/projects/autoharness'; primary" in environment
    assert "llm-mcp: execution='/isolated/task-42'; canonical='/projects/llm-mcp'; additional" in environment
    assert workspace in environment
    harbor = 'autoharness:work/harbor-experiment-repair'
    routed = route_project_tool_args(task, 'run_command', {'working_dir': harbor})
    assert routed['working_dir'] == primary + '/work/harbor-experiment-repair'
    routed = route_project_tool_args(task, 'run_command', {'working_dir': canonical + '/src'})
    assert routed['working_dir'] == workspace + '/src'


def test_workspace_snapshot_preserves_nested_readonly_root_and_does_not_mutate_policy():
    from lib.project_mod.config import build_root_policy
    from lib.tasks_pkg.workspace_delivery import execution_root_snapshot
    policy = build_root_policy('/isolated/task', ['/isolated/task/reference'],
                               ['/isolated/task/reference'])
    before = copy.deepcopy(policy)
    task = {'_workspaceRootPolicy': policy,
            '_workspaceDelivery': {'repositories': [
                {'canonicalRoot': '/projects/repo', 'workspacePath': '/isolated/task', 'workId': 'w'}]}}
    roots = execution_root_snapshot(task)
    assert roots[0]['canonicalPath'] == '/projects/repo'
    assert roots[1]['canonicalPath'] == '/projects/repo/reference'
    assert roots[1]['access'] == 'ro'
    assert policy == before
