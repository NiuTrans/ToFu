"""The L2 compaction summary must never inherit max-effort reasoning.

TB2.1 v8 (2026-09-15): on the forced-thinking GLM-5.3 arm, the summary
dispatch inherited the task's max-reasoning intent, burned the 2,200-token
budget on thinking alone, and the truncated receipt failed the dispatch —
17 of 19 unscored trials died on that single defect
(``summary_dispatch_failed``), 19% of the benchmark. The dispatch must
compile ``thinking_enabled=False`` — off where the model allows it, the
cheapest legal rung on cannot-disable models (GLM-5.3 → 'low', proven to
fit the budget: 247 completion tokens on a summary-shaped prompt).
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def _minimal_messages():
    return [
        {'role': 'system', 'content': 'You are a coding agent.'},
        {'role': 'user', 'content': 'Fix the bug in main.py'},
        {'role': 'assistant', 'content': 'I will inspect the failing test.',
         'tool_calls': [{'id': 'c1', 'type': 'function',
                         'function': {'name': 'run_command',
                                      'arguments': '{"command": "pytest -q"}'}}]},
        {'role': 'tool', 'tool_call_id': 'c1', 'content': '1 failed'},
    ]


def test_summary_dispatch_disables_reasoning_chat_and_stream(monkeypatch):
    from lib.tasks_pkg.compaction._layer2 import _summary
    import lib.llm_dispatch as llm_dispatch

    calls = []

    def fake_chat(messages, **kwargs):
        calls.append(('chat', kwargs))
        return '### Objective\nFix the bug\n', {'prompt_tokens': 10,
                                               'completion_tokens': 5, 'finish_reason': 'stop'}

    def fake_stream(body, **kwargs):
        calls.append(('stream', kwargs))
        from lib.llm.stream_result import ProviderStreamResult
        return ProviderStreamResult.from_legacy(
            {'role': 'assistant',
             'content': '### Objective\nFix the bug\n'},
            'stop', {'prompt_tokens': 10, 'completion_tokens': 5})

    monkeypatch.setattr(llm_dispatch, 'dispatch_chat', fake_chat)
    monkeypatch.setattr(llm_dispatch, 'dispatch_stream', fake_stream)

    task = {'_userId': 1, 'config': {'model': 'glm-5.3-m17'}}

    # Non-streaming path (on_delta=None) uses dispatch_chat.
    result = _summary._generate_query_aware_summary(
        _minimal_messages(), 'fix the bug', task=task)
    assert result and 'Objective' in result

    # Streaming path (on_delta set) uses dispatch_stream.
    result2 = _summary._generate_query_aware_summary(
        _minimal_messages(), 'fix the bug', task=task,
        on_delta=lambda _chunk: None)
    assert result2 and 'Objective' in result2

    assert {kind for kind, _ in calls} == {'chat', 'stream'}
    for kind, kwargs in calls:
        assert kwargs.get('thinking_enabled') is False, (
            f'{kind} dispatch lost the reasoning-off intent: {kwargs}')


def test_accumulate_usage_into_sums_nested_billing_details():
    """Aggregate must keep the reasoning/cache splits each round carried."""
    from lib.cost import accumulate_usage_into

    total = {}
    for _ in range(2):
        accumulate_usage_into(total, {
            'prompt_tokens': 100,
            'completion_tokens': 10,
            'completion_tokens_details': {'reasoning_tokens': 7},
            'prompt_tokens_details': {'cached_tokens': 60,
                                      'cache_write_tokens': 5},
            '_dispatch': {'route': 'x'},          # evidence, never summed
            '_extra_billing_rounds': [{'usage': {'prompt_tokens': 1}}],
            'stream': True,                        # bool must not become 2
        })
    assert total['prompt_tokens'] == 200
    assert total['completion_tokens'] == 20
    assert total['completion_tokens_details'] == {'reasoning_tokens': 14}
    assert total['prompt_tokens_details'] == {'cached_tokens': 120,
                                              'cache_write_tokens': 10}
    assert '_dispatch' not in total
    assert '_extra_billing_rounds' not in total
    assert 'stream' not in total


def test_prebuilt_body_with_explicit_thinking_enabled_compiles_reasoning():
    """is_body=True adaptation must bake the caller's reasoning intent.

    TB2.1 v9: the L2 summary dispatch hands dispatch_stream a pre-built
    body (is_body=True), which skipped build_body — and with it, reasoning
    compilation. The upstream default effort applied, so every summary call
    on the forced-thinking GLM arm burned its whole 2,200-token budget on
    reasoning (reasoning_tokens≈2,199/2,200) and the trial died on
    summary_dispatch_failed. Explicit thinking_enabled=False must bake the
    off intent BEFORE the adaptation's single compile point.
    """
    from types import SimpleNamespace
    from lib.llm_dispatch._api_stream_state import _adapt_stream_body_for_slot

    slot = SimpleNamespace(
        model='glm-5.3-m17', thinking_format='', inference_profile={
            'reasoning': {
                'support': 'supported', 'wire_format': 'thinking_type',
                'supported_efforts': ['low', 'high', 'max'],
                'default_effort': 'high',
                'effort_map': {'off': 'low', 'low': 'low', 'medium': 'high',
                               'high': 'high', 'xhigh': 'max', 'max': 'max',
                               'ultra': 'max'},
                'can_disable': False}},
        protocol='openai', responses_profile='',
        provider_id='harbor-benchmark',
        routing_provider_id='harbor-benchmark')

    body = {'messages': [{'role': 'user', 'content': 'summarize'}],
            'max_tokens': 2200, 'temperature': 0, 'stream': True}

    from copy import deepcopy
    before = deepcopy(body)
    out = _adapt_stream_body_for_slot(
        slot, dict(body), True, tools=None, max_tokens=2200, temperature=0,
        thinking_enabled=False, preset='low', effort=None)
    # off degrades to the cheapest legal rung on a cannot-disable model.
    assert out.get('reasoning_effort') == 'low', out
    assert body == before

    # Retry defaults cannot replace intent captured by the original caller.
    retry = _adapt_stream_body_for_slot(
        slot, out, True, tools=None, max_tokens=2200, temperature=0,
        thinking_enabled=True, preset='max', effort=None)
    assert retry['_reasoning_intent'] == out['_reasoning_intent']
    assert retry.get('reasoning_effort') == 'low'

    # thinking_enabled=None keeps the historical upstream-default behavior.
    out_none = _adapt_stream_body_for_slot(
        slot, dict(body), True, tools=None, max_tokens=2200, temperature=0,
        thinking_enabled=None, preset='low', effort=None)
    assert out_none.get('reasoning_effort') is None


def test_dispatch_allows_natural_budget_and_preserves_complete_receipt():
    """A complete result is never locally clipped to a nominal summary target."""
    from lib.tasks_pkg.compaction._layer2 import _summary
    from lib.tasks_pkg.compaction._constants import _SUMMARY_MAX_TOKENS

    assert _summary._SUMMARY_DISPATCH_MAX_TOKENS > _SUMMARY_MAX_TOKENS

    calls = []

    def fake_chat(messages, **kwargs):
        calls.append(kwargs)
        return '### Objective\n' + 'x' * 10_000, {'prompt_tokens': 10,
                                                  'completion_tokens': 5, 'finish_reason': 'stop'}
    import lib.llm_dispatch as llm_dispatch
    import pytest
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(llm_dispatch, 'dispatch_chat', fake_chat)
    try:
        task = {'_userId': 1, 'config': {'model': 'glm-5.3-m17'}}
        messages = [
            {'role': 'system', 'content': 'sys'},
            {'role': 'user', 'content': 'fix the bug'},
            {'role': 'assistant', 'content': 'checking',
             'tool_calls': [{'id': 'c1', 'type': 'function',
                             'function': {'name': 'run_command',
                                          'arguments': '{}'}}]},
            {'role': 'tool', 'tool_call_id': 'c1', 'content': 'ok'},
        ]
        out = _summary._generate_query_aware_summary(
            messages, 'fix the bug', task=task)
        assert calls[0].get('max_tokens') == (
            _summary._SUMMARY_DISPATCH_MAX_TOKENS)
        assert calls[0].get('thinking_enabled') is False
        assert out == '### Objective\n' + 'x' * 10_000
        assert 'receipt capped' not in out
    finally:
        monkeypatch.undo()
