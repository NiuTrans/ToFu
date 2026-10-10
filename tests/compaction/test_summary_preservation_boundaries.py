"""Automatic summaries must preserve evidence at alternate entry points."""
from copy import deepcopy

import pytest

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('transient', [False, True])
@pytest.mark.parametrize('failure', ['empty', 'incomplete', 'cancel', 'memory'])
def test_reactive_failure_preserves_complete_source(monkeypatch, transient, failure):
    from lib.tasks_pkg.compaction import _reactive as reactive
    from lib.tasks_pkg.compaction._layer2 import _compact as compact
    task = {'id': 'integrity', 'convId': 'integrity', '_userId': 1,
            '_transientRuntime': transient, 'config': {'model': 'test'}}
    messages = [{'role': 'system', 'content': 'system'}]
    for i in range(8):
        messages.extend([
            {'role': 'user', 'content': f'original constraint {i}'},
            {'role': 'assistant', 'content': '', 'tool_calls': [
                {'id': f'c{i}', 'type': 'function', 'function': {
                    'name': 'read_files', 'arguments': '{}'}}]},
            {'role': 'tool', 'tool_call_id': f'c{i}',
             'content': f'UNIQUE_SOURCE_{i} ' + 'cold evidence ' * 400},
            {'role': 'assistant', 'content': f'verified conclusion {i}'},
        ])
    original = deepcopy(messages)
    calls = []

    def fail(*args, **kwargs):
        calls.append(True)
        if failure == 'cancel':
            task['aborted'] = True
        kwargs['_result_meta'].update(compacted=False, msgs_before=len(messages),
                                      summary_usage={}, summary_duration_ms=5)
        return 'summary failed: ' + failure

    monkeypatch.setattr(compact, 'execute_compact_tool', fail)
    monkeypatch.setattr(compact, '_estimate_total_tokens', lambda *a, **k: 500_000)
    monkeypatch.setattr(reactive, '_get_context_limit', lambda *a: 1_000_000)
    monkeypatch.setattr(reactive, '_head_truncate', lambda *a, **k: pytest.fail('head deletion'))
    assert reactive.reactive_compact(
        messages, task, error_text='context overflow',
        byte_target=1024 if failure == 'memory' else None) is False
    assert messages == original
    assert len(calls) == (0 if failure == 'memory' else 1)


@pytest.mark.parametrize('carrier', ['typed', 'typed_no_heading', 'manual', 'legacy'])
def test_second_summary_keeps_complete_prior_receipt(monkeypatch, carrier):
    from lib.tasks_pkg.compaction._handoff import make_compaction_handoff
    from lib.tasks_pkg.compaction._constants import _COMPACT_TOOL_NAME
    from lib.tasks_pkg.compaction._layer2 import _summary as summary
    import lib.llm_dispatch as dispatch
    from lib.llm.stream_result import ProviderStreamResult
    body = ('unheaded prior receipt' if carrier == 'typed_no_heading' else
            '## Context Compacted\n### Objective\nKeep all constraints')
    body += '\n' + 'accepted detail ' * 1300 + '\nUNRESOLVED_BLOCKER_AT_END'
    if carrier.startswith('typed'):
        messages = [make_compaction_handoff(body, phase='pre_turn')]
    elif carrier == 'manual':
        messages = [{'role': 'assistant', '_isCompactionSummary': True, 'content': body}]
    else:
        messages = [
            {'role': 'assistant', 'content': '', 'tool_calls': [
                {'id': 'legacy', 'type': 'function', 'function': {
                    'name': _COMPACT_TOOL_NAME, 'arguments': '{}'}}]},
            {'role': 'tool', 'tool_call_id': 'legacy', 'content': body},
        ]
    original = deepcopy(messages)
    monkeypatch.setattr(summary, '_codex_subscription_provider', lambda task: '')
    monkeypatch.setattr(summary, '_summary_input_char_budget', lambda task: 120_000)
    seen = []

    def complete(prompt, **kwargs):
        seen.append(prompt['messages'][-1]['content'])
        return ProviderStreamResult.from_legacy(
            {'role': 'assistant', 'content': '### Objective\nKept.'},
            'stop', {'prompt_tokens': 100, 'completion_tokens': 10})

    monkeypatch.setattr(dispatch, 'dispatch_stream', complete)
    assert summary._generate_query_aware_summary(
        messages, 'continue', task={'_transientRuntime': True, '_userId': 1,
                                    'config': {'model': 'test'}})
    # Only the machine-owned header is stripped; all authored sections survive.
    expected = body[body.index('### '):] if '### ' in body else body
    assert expected in seen[0]
    assert 'UNRESOLVED_BLOCKER_AT_END' in seen[0]
    assert messages == original


def test_changed_transient_request_retries_but_identical_failure_does_not(monkeypatch):
    from lib.tasks_pkg.compaction._layer2 import _summary as summary
    import lib.llm_dispatch as dispatch
    from lib.llm.stream_result import ProviderStreamResult
    monkeypatch.setattr(summary, '_codex_subscription_provider', lambda task: '')
    monkeypatch.setattr(summary, '_summary_input_char_budget', lambda task: 12000)
    task = {'_transientRuntime': True, '_userId': 1, 'config': {'model': 'test'}}
    calls = []

    def dispatch_once(prompt, **kwargs):
        calls.append(prompt)
        if len(calls) == 1:
            raise RuntimeError('first provider attempt failed')
        return ProviderStreamResult.from_legacy(
            {'role': 'assistant', 'content': '### Objective\nContinue.'},
            'stop', {'prompt_tokens': 100, 'completion_tokens': 10})

    monkeypatch.setattr(dispatch, 'dispatch_stream', dispatch_once)
    first = [{'role': 'user', 'content': 'source A'}]
    assert summary._generate_query_aware_summary(first, 'continue', task=task) is None
    assert summary._generate_query_aware_summary(first, 'continue', task=task) is None
    assert len(calls) == 1
    second = [{'role': 'user', 'content': 'source B'}]
    assert summary._generate_query_aware_summary(second, 'continue', task=task)
    assert len(calls) == 2
