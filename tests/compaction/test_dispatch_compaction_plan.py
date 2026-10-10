"""One summary call must fit the complete dispatch budget without clipping history."""
from copy import deepcopy

import pytest

from lib.tasks_pkg.compaction._layer2 import _compact as compact

pytestmark = pytest.mark.unit


def messages_with_long_execution():
    messages = [{'role': 'system', 'content': 'policy ' * 5000},
                {'role': 'user', 'content': 'Complete the recovery work'}]
    for index in range(30):
        messages.extend([
            {'role': 'assistant', 'content': f'finding-{index} ' * 3000},
            {'role': 'assistant', 'content': None, 'tool_calls': [
                {'id': f'call-{index}', 'type': 'function', 'function': {
                    'name': 'read_files', 'arguments': '{}'}}]},
            {'role': 'tool', 'tool_call_id': f'call-{index}', 'content': 'evidence ' * 1000},
        ])
    return messages


def complete_count(messages):
    # Includes fixed tool-schema cost; unlike the planner's heuristic this
    # controlled canonical seam charges one token per content character.
    return 14240 + sum(len(str(m.get('content') or '')) for m in messages)


def task():
    return {'id': 'plan-task', 'convId': 'plan-conversation', '_userId': 1,
            'config': {'model': 'gpt-6.1-sol'}}


def test_one_summary_fits_full_prompt_and_receives_cold_assistant_evidence(monkeypatch):
    messages = messages_with_long_execution()
    source = deepcopy(messages)
    calls = []
    def summarize(cold, *args, **kwargs):
        calls.append((deepcopy(cold), kwargs))
        return '### Objective\nComplete the recovery work\n### Progress\nVerified earlier evidence.'
    monkeypatch.setattr(compact, '_generate_query_aware_summary', summarize)
    monkeypatch.setattr(compact, '_archive_transcript', lambda *a, **k: None)
    meta = {}
    assert compact.force_compact_if_needed(
        messages, task=task(), force=True, preserve_budget_tokens=32000,
        _compaction_trigger='dispatch_guard', _dispatch_prompt_target=144000,
        _dispatch_prompt_measure=complete_count, _result_meta=meta)
    assert len(calls) == 1
    assert calls[0][1]['output_budget_tokens'] == 36000
    assert any(m.get('content') == source[2]['content'] for m in calls[0][0])
    assert not any(m.get('content') == source[2]['content'] for m in messages)
    assert any(m.get('tool_call_id') == 'call-29' for m in messages)
    assert meta['plannedPromptTokens'] <= 144000
    assert complete_count(messages) <= 144000
    assert messages[0] == source[0]
    assert source[-1] in messages


def test_irreducible_floor_refuses_before_summary_and_keeps_source(monkeypatch):
    messages = messages_with_long_execution()
    messages[0]['content'] = 'required policy ' * 14000
    source = deepcopy(messages)
    monkeypatch.setattr(compact, '_generate_query_aware_summary',
                        lambda *a, **k: pytest.fail('No summary call for an impossible plan'))
    meta = {}
    assert not compact.force_compact_if_needed(
        messages, task=task(), force=True, preserve_budget_tokens=32000,
        _compaction_trigger='dispatch_guard', _dispatch_prompt_target=144000,
        _dispatch_prompt_measure=complete_count, _result_meta=meta)
    assert messages == source
    assert meta['summaryFailureReason'] == 'irreducible_prompt_floor'
