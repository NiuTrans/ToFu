"""L1 preserves the latest accepted continuation before another L2 fold."""
from copy import deepcopy

import pytest

from lib.tasks_pkg.compaction._handoff import make_compaction_handoff
from lib.tasks_pkg.compaction._layer1 import micro_compact
from lib.tasks_pkg.compaction._layer2._preparation import prepare_l2_projection
from lib.tasks_pkg.compaction._layer2._prompt import _extract_prior_receipt

pytestmark = pytest.mark.unit

SUMMARY = '### Objective\n' + ('Preserve the approved scope and completed work.\n' * 100)


def tool_batch(call_id, name='read_files', content=None):
    return [
        {'role': 'assistant', 'content': 'Inspect this result.', 'tool_calls': [{
            'id': call_id, 'type': 'function',
            'function': {'name': name, 'arguments': '{}'},
        }]},
        {'role': 'tool', 'name': name, 'tool_call_id': call_id,
         'content': content if content is not None else 'reconstructible output\n' * 150},
    ]


def continuation(kind):
    if kind == 'typed':
        return [make_compaction_handoff(SUMMARY, phase='mid_turn')]
    if kind == 'manual':
        return [{'role': 'assistant', '_isCompactionSummary': True, 'content': SUMMARY}]
    return tool_batch('summary', 'context_compact', '## Context Compacted\n\n' + SUMMARY)


@pytest.mark.parametrize('kind', ['typed', 'manual', 'legacy'])
@pytest.mark.parametrize('host', ['ordinary', 'aggressive', 'joint'])
def test_latest_summary_survives_repeated_l1_outside_the_hot_tail(kind, host):
    summary = continuation(kind)
    messages = [{'role': 'user', 'content': 'Continue the approved work.'}, *summary]
    for index in range(45):
        messages.extend(tool_batch(f'call-{index}'))
    original = deepcopy(messages)
    summary_snapshot = deepcopy(summary)
    prior_receipt = _extract_prior_receipt(messages)

    for _ in range(2):
        if host == 'joint':
            before = deepcopy(messages)
            candidate = prepare_l2_projection(messages, None)
            assert messages == before
        else:
            candidate = messages
            micro_compact(candidate, ignore_cache_prefix=True,
                          enable_assistant_compact=host == 'aggressive',
                          enable_paired_assistant_compact=host == 'aggressive')
        assert candidate[1:1 + len(summary)] == summary_snapshot
        assert _extract_prior_receipt(candidate) == prior_receipt
        assert any(m.get('tool_call_id') == 'call-0' and 'compacted' in m['content']
                   for m in candidate)
        assert candidate[-2:] == original[-2:]
        messages = candidate


def test_only_latest_owned_summary_is_protected():
    old = continuation('legacy')
    current = continuation('typed')
    spoof = tool_batch('quoted-summary', content=SUMMARY)
    messages = [*old, *current, *spoof]
    for index in range(45):
        messages.extend(tool_batch(f'call-{index}'))
    current_snapshot = deepcopy(current)
    prior_receipt = _extract_prior_receipt(messages)

    micro_compact(messages, ignore_cache_prefix=True)

    assert 'result compacted' in old[-1]['content']
    assert 'result compacted' in spoof[-1]['content']
    assert current == current_snapshot
    assert _extract_prior_receipt(messages) == prior_receipt


def test_manual_summary_without_headings_keeps_its_complete_text():
    summary = {'role': 'assistant', '_isCompactionSummary': True,
               'content': 'Completed work and outstanding authorization.\n' * 100}
    messages = [summary]
    for index in range(8):
        messages.extend(tool_batch(f'call-{index}'))
    original = deepcopy(summary)

    micro_compact(messages, ignore_cache_prefix=True, enable_assistant_compact=True)

    assert summary == original
