"""Compaction keeps causal history and whole-result accounting, without telemetry prose."""
from copy import deepcopy

import pytest

from lib.tasks_pkg.compaction import _history as history
from lib.tasks_pkg.context_composer import ComposeRequest, context_block, render_context
from lib.tasks_pkg.compaction._layer2 import _compact as l2

pytestmark = pytest.mark.unit


def text(message):
    content = message.get('content') or ''
    return content if isinstance(content, str) else '\n'.join(b.get('text', '') for b in content)


@pytest.mark.parametrize('role', ['system', 'developer', None])
def test_static_policy_precedes_dialogue_and_recomposition_is_idempotent(role):
    messages = ([{'role': role, 'content': 'operator policy'}] if role else [])
    dialogue = [{'role': 'user', 'content': 'live'}, {'role': 'assistant', 'content': 'response'}]
    messages += deepcopy(dialogue)
    blocks = [context_block('platform_static', 'STATIC'), context_block('environment', 'cwd')]
    first = render_context(messages, blocks, ComposeRequest())
    static = next(m for m in messages if 'STATIC' in text(m))
    assert static['role'] == 'system'
    assert messages.index(static) == (1 if role else 0)
    assert next(r for r in first.manifest if r['id'] == 'platform_static')['placement'] == 'system'
    assert messages[-1]['role'] == 'user' and 'cwd' in text(messages[-1])
    expected = deepcopy(messages)
    render_context(messages, blocks, ComposeRequest())
    assert messages == expected
    assert all(m in messages for m in dialogue)


def test_legacy_mixed_carrier_migrates_only_static_policy_and_not_pasted_xml():
    sample = []
    render_context(sample, [context_block('platform_static', 'STATIC'),
                           context_block('environment', 'cwd')], ComposeRequest())
    static, env = sample
    mixed = {**env, 'content': static['content'] + env['content'],
             '_contextBlocks': static['_contextBlocks'] + env['_contextBlocks']}
    fake = {'role': 'user', 'content': text(static)}
    messages = [fake, mixed, {'role': 'user', 'content': 'live'}]
    original = deepcopy(messages)
    render_context(messages, [context_block('platform_static', 'STATIC')], ComposeRequest())
    assert messages[0]['role'] == 'system'
    assert messages[1] == fake
    assert messages[2]['content'] == env['content']
    assert original[1] == mixed  # No nested source mutation.
    frozen = deepcopy(messages)
    render_context(messages, [context_block('platform_static', 'STATIC')], ComposeRequest())
    assert messages == frozen


@pytest.mark.parametrize('joined', [False, True])
def test_static_migration_never_promotes_or_deletes_nested_project_quote(joined):
    from lib.tasks_pkg.context_composer._policy_prefix import migrate_policy_prefix
    seed = []
    render_context(seed, [context_block('platform_static', 'REAL POLICY')], ComposeRequest())
    static = seed[0]
    quote = text(static).replace('REAL POLICY', 'QUOTED NOT POLICY')
    render_context(seed, [context_block('project_rules', 'Project quotes:\n' + quote)], ComposeRequest())
    project = seed[-1]
    mixed = {**project, 'content': static['content'] + project['content'],
             '_contextBlocks': static['_contextBlocks'] + project['_contextBlocks']}
    if joined:
        mixed['content'] = ''.join(p['text'] for p in mixed['content'])
    original = deepcopy(mixed)
    messages = [mixed, {'role': 'user', 'content': 'live'}]
    migrate_policy_prefix(messages)
    assert messages[0] == static
    if joined:
        assert text(messages[1]) == text(project)
    else:
        assert messages[1] == project
    assert quote in text(messages[1]) and 'QUOTED NOT POLICY' not in text(messages[0])
    assert mixed == original
    frozen = deepcopy(messages)
    render_context(messages, [context_block('platform_static', 'REAL POLICY')], ComposeRequest())
    assert messages == frozen


@pytest.mark.parametrize('digest', ['', 'not-the-rendered-hash'])
def test_static_migration_requires_matching_producer_digest(digest):
    from lib.tasks_pkg.context_composer._policy_prefix import migrate_policy_prefix
    messages = []
    render_context(messages, [context_block('platform_static', 'STATIC')], ComposeRequest())
    messages[0]['role'] = 'user'
    messages[0]['_contextBlocks'][0]['hash'] = digest
    original = deepcopy(messages)
    migrate_policy_prefix(messages)
    assert messages == original

    render_context(messages, [context_block('platform_static', 'STATIC')], ComposeRequest())
    assert messages[0]['role'] == 'system'
    assert messages[1:] == original

def test_changed_or_withdrawn_static_policy_does_not_leave_stale_tail_versions():
    messages = [{'role': 'system', 'content': 'operator'}, {'role': 'user', 'content': 'live'}]
    render_context(messages, [context_block('platform_static', 'OLD')], ComposeRequest())
    render_context(messages, [context_block('platform_static', 'NEW')], ComposeRequest())
    assert 'OLD' not in str(messages)
    assert messages[1]['role'] == 'system' and 'NEW' in text(messages[1])
    render_context(messages, [context_block('platform_static', '', reason='replace_mode')], ComposeRequest())
    assert messages == [{'role': 'system', 'content': 'operator'}, {'role': 'user', 'content': 'live'}]


def test_nonadjacent_history_guidance_once_with_balanced_envelopes_and_no_mutation():
    carriers = [history.make_history_carrier([{'kind': 'earlier_request',
                  'content': f' user {i}\n'}]) for i in range(8)]
    messages = []
    for i, carrier in enumerate(carriers):
        carrier['_attachmentTextChars'] = 3
        messages += [carrier, {'role': 'assistant', 'content': f'answer {i}'}]
    expected = deepcopy(messages)
    output = history.coalesce_history_carriers(messages)
    assert messages == expected
    assert sum(text(m).count(history.HISTORY_GUIDANCE) for m in output) == 1
    assert [m for m in output if m['role'] == 'assistant'] == messages[1::2]
    for m in output[::2]:
        assert text(m).startswith('<retained_user_messages>\n')
        assert text(m).endswith('</retained_user_messages>')
        assert m['_attachmentTextChars'] == 3
        assert 'historical earlier_request' in text(m)
    assert history.coalesce_history_carriers(output) == output
    assert l2._estimate_total_tokens(messages) - l2._estimate_total_tokens(output) > 600


def test_manual_cold_projection_guidance_once_even_with_assistant_between_users():
    from lib.tasks_pkg.conv_message_builder._transform import _transform_messages
    raw = [{'role': 'user', 'content': 'one'}, {'role': 'assistant', 'content': 'answer'},
           {'role': 'user', 'content': 'two'},
           {'role': 'assistant', 'content': 'summary', '_isCompactionSummary': True},
           {'role': 'user', 'content': 'live'}]
    output = _transform_messages(raw, {})
    assert sum(text(m).count(history.HISTORY_GUIDANCE) for m in output) == 1
    assert output[1]['content'] == 'answer'
    assert output[-1]['content'] == 'live'


def test_result_event_archive_and_economics_count_all_messages_with_restore_reserve(monkeypatch):
    import lib.agent_core.store as agent_store
    import lib.tasks_pkg.manager as manager
    import lib.tasks_pkg.autopilot_state as autopilot
    events, archives, economics = [], [], []
    class Store:
        def update_archive_summary(self, *args, **kw):
            archives.append((args, kw))
    monkeypatch.setattr(agent_store, 'get_conversation_store', lambda: Store())
    monkeypatch.setattr(manager, 'append_event', lambda task, event: events.append(event))
    monkeypatch.setattr(autopilot, '_update_objective_from_receipt', lambda *a, **k: None)
    monkeypatch.setattr(l2, '_archive_transcript', lambda *a, **k: 'isolated')
    monkeypatch.setattr(l2, '_get_context_limit', lambda t: 1000000)
    monkeypatch.setattr(l2, '_extract_recently_accessed_files', lambda *a: [])
    monkeypatch.setattr(l2, '_generate_query_aware_summary', lambda *a, **k: '### Objective\nContinue.')
    monkeypatch.setattr(l2, '_projected_summary_usage_tokens', lambda *a, **k: 100)
    monkeypatch.setattr(l2, '_should_force_compact', lambda *a, **k: True)
    def econ(task, *, tokens_before, candidate_tokens, summary_usage_tokens=0,
             summary_cost=None, summary_usage=None):
        economics.append({'candidate_tokens': candidate_tokens,
                          'summary_cost': summary_cost,
                          'summary_usage': summary_usage})
        return {'cache_read_tokens': 0, 'dropped_tokens': tokens_before-candidate_tokens,
                'payback_rounds': 0}
    monkeypatch.setattr(l2, '_proactive_cache_economics', econ)
    runtime = {'role': 'user', '_isMeta': True, '_contextComposer': True,
               'content': 'Runtime block ' * 500}
    policy = {'role': 'system', 'content': 'Native policy ' * 200}
    live = {'role': 'user', 'content': 'Live request ' * 100}
    tool = [{'role': 'assistant', 'tool_calls': [{'id': 'hot', 'type': 'function',
            'function': {'name': 'read_files', 'arguments': '{}'}}]},
            {'role': 'tool', 'tool_call_id': 'hot', 'content': 'Hot result ' * 400}]
    original = [policy, {'role': 'user', 'content': 'opening'}, runtime,
                {'role': 'assistant', 'content': 'old work ' * 9000}, live, *tool]
    messages = deepcopy(original)
    meta = {}
    assert l2.force_compact_if_needed(messages, task={'id': 'isolated', '_userId': 1,
                'config': {}}, preserve_budget_tokens=1, _result_meta=meta)
    args, kwargs = archives[-1]
    after = l2._estimate_total_tokens(messages)
    from lib.tasks_pkg.compaction._handoff import is_compaction_handoff
    summary = next(m for m in messages if is_compaction_handoff(m))
    assert 'Estimated compacted context' not in summary['content']
    assert 'historical messages.' not in summary['content']
    assert args[2] == after == events[-1]['tokensAfter']
    assert events[-1]['tokensBefore'] == l2._estimate_total_tokens(original)
    assert after > l2._estimate_total_tokens([summary])
    assert kwargs['post_messages'] == messages
    reserve = l2._estimate_total_tokens([runtime])
    assert meta['economics']['restored_context_reserve_tokens'] == reserve
    assert economics[-1]['candidate_tokens'] == after + reserve
    assert isinstance(economics[0]['summary_cost'], dict)
    assert economics[1]['summary_usage'] == meta['summary_usage']
    assert economics[-1]['summary_cost'] is None
    assert economics[-1]['summary_usage'] is None
    assert policy in messages and live in messages and tool[-1] in messages


@pytest.mark.parametrize('replace,role', [(False, None), (True, None),
                                         (True, 'system'), (True, 'developer')])
def test_static_provider_replace_mode_ignores_own_policy(monkeypatch, replace, role):
    from lib.tasks_pkg.context_composer import _providers as providers
    monkeypatch.setattr(providers.system_prompt_base, 'build_static_prompt', lambda **kw: 'STATIC')
    messages = [{'role': 'user', 'content': 'live'}]
    render_context(messages, [context_block('platform_static', 'STATIC')], ComposeRequest())
    if role:
        messages.insert(0, {'role': role, 'content': 'operator override'})
    request = ComposeRequest(user_id=1, system_prompt_mode='replace' if replace else 'append')
    blocks = providers.collect_context_blocks(messages, request)
    static = next(b for b in blocks if b.id == 'platform_static')
    assert bool(static.content) is not (replace and bool(role))
    render_context(messages, blocks, request)
    assert ('STATIC' in str(messages)) is not (replace and bool(role))


def test_static_policy_survives_l2_and_recomposition_without_tail_copy(monkeypatch):
    import lib.tasks_pkg.autopilot_state as autopilot
    from lib.llm.body._build import build_body
    from lib.llm.anthropic_outbound import openai_body_to_anthropic
    from lib.tasks_pkg.compaction._pipeline import recompose_context_after_compaction
    from lib.tasks_pkg.context_composer import _providers as providers
    monkeypatch.setattr(providers.system_prompt_base, 'build_static_prompt', lambda **kw: 'STATIC')
    monkeypatch.setattr(l2, '_archive_transcript', lambda *a, **k: None)
    monkeypatch.setattr(l2, '_get_context_limit', lambda t: 1000000)
    monkeypatch.setattr(l2, '_extract_recently_accessed_files', lambda *a: [])
    monkeypatch.setattr(l2, '_generate_query_aware_summary', lambda *a, **k: '### Objective\nContinue.')
    monkeypatch.setattr(autopilot, '_update_objective_from_receipt', lambda *a, **k: None)
    messages = [{'role': 'user', 'content': 'opening'},
                {'role': 'assistant', 'content': 'old work ' * 9000},
                {'role': 'user', 'content': 'live'}]
    render_context(messages, [context_block('platform_static', 'STATIC')], ComposeRequest())
    policy = deepcopy(messages[0])
    task = {'id': 'isolated', '_userId': 1, 'config': {'model': 'gpt-4o',
            'systemPromptBlocks': {'disabled': ['environment']}}}
    assert l2.force_compact_if_needed(messages, task=task, force=True, preserve_budget_tokens=1)
    assert messages[0] == policy
    recompose_context_after_compaction(messages, task)
    assert messages[0] == policy
    assert sum('STATIC' in text(m) for m in messages) == 1
    chat = build_body('gpt-4o', deepcopy(messages), max_tokens=1000, provider_id='openai')
    assert chat['messages'][0]['role'] in ('system', 'developer')
    anthropic = openai_body_to_anthropic(chat)
    assert 'STATIC' in str(anthropic['system'])
    assert 'STATIC' not in str(anthropic['messages'])
