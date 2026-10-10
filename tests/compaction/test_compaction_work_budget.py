"""L1 work budgets preserve exact request projections and durable receipts."""
from __future__ import annotations

import copy

import pytest

pytestmark = pytest.mark.unit


class CountedRows(list):
    def __init__(self, rows):
        super().__init__(rows)
        self.visits = 0
        self.index_reads = 0

    def __iter__(self):
        for row in super().__iter__():
            self.visits += 1
            yield row

    def __getitem__(self, key):
        self.index_reads += 1
        return super().__getitem__(key)


@pytest.fixture
def cheap_evidence(monkeypatch):
    from lib.tasks_pkg import manager
    from lib.tasks_pkg.compaction import _tokens
    from lib.tasks_pkg.compaction._builtin_steps import _toolresults
    import lib.context_telemetry as telemetry
    import lib.token_counter as counter
    events = []
    monkeypatch.setattr(manager, 'append_event', lambda task, event: events.append(event))
    monkeypatch.setattr(counter, 'count_text', lambda text, **kw: len(text) // 4)
    monkeypatch.setattr(_tokens, '_estimate_total_tokens', lambda messages: len(messages))
    monkeypatch.setattr(_toolresults, '_estimate_msg_tokens', lambda message: 1)
    monkeypatch.setattr(telemetry, 'record_compaction_event', lambda *a, **kw: None)
    return events


def _compact(messages, task=None):
    from lib.tasks_pkg.compaction._layer1 import micro_compact
    return micro_compact(messages, task=task, steps=['compact_tool_results'],
                         constant_overrides={'MICRO_HOT_TAIL': 0,
                                             'MICRO_HOT_TAIL_TOKENS': 0})


def _batch(size):
    calls = [{'id': f'c{i}', 'function': {'name': 'read_files', 'arguments': '{}'}}
             for i in range(size)]
    return [{'role': 'assistant', 'content': '', 'tool_calls': calls}] + [
        {'role': 'tool', 'name': 'read_files', 'tool_call_id': f'c{i}',
         'content': 'first\nsecond\n' + 'evidence\n' * 400}
        for i in range(size)
    ] + [{'role': 'assistant', 'content': 'consumed'}]


def test_l1_round_metadata_work_is_linear_and_durable_rows_unchanged(cheap_evidence):
    count = 400
    rows = CountedRows([{'toolCallId': f'c{i}', 'roundNum': i + 1,
                         'toolName': 'read_files', 'toolContent': 'durable'}
                        for i in range(count)])
    original = copy.deepcopy(list(rows))
    rows.visits = 0
    messages = _batch(count)
    _compact(messages, task={'_userId': 1, 'model': 'fixture', 'toolRounds': rows})
    assert len(cheap_evidence) == count
    assert [e['roundNum'] for e in cheap_evidence] == list(range(1, count + 1))
    assert rows.visits <= count * 2
    assert list(rows) == original
    assert all(e['compactedContent'] == messages[i + 1]['content']
               for i, e in enumerate(cheap_evidence))


def test_l1_single_fanout_batch_pairing_has_linear_work(cheap_evidence):
    count = 400
    messages = CountedRows(_batch(count))
    _compact(messages)
    # Iterations and positional reads, not machine timing: a wide tool batch
    # cannot cause a backward scan through every prior sibling per result.
    assert messages.index_reads < count * 8
    assert 'compacted' in messages[1]['content']


@pytest.mark.parametrize('count', [200, 800])
def test_newest_tool_batch_unmatched_ids_have_linear_work(count):
    from lib.tasks_pkg.compaction._builtin_steps._toolresults import (
        _newest_tool_batch_indices)
    messages = CountedRows([
        {'role': 'assistant', 'tool_calls': [{'id': f'missing{i}'}]}
        for i in range(count)
    ] + [
        {'role': 'tool', 'tool_call_id': f'orphan{i}'}
        for i in range(count)
    ])
    assert _newest_tool_batch_indices(
        messages, list(range(count, count * 2))) == set()
    # The old helper used n*n + 2*n indexed reads (40,400 at n=200).
    assert messages.index_reads + messages.visits <= count * 3


def _newest_tool_batch_reference(messages, tool_indices):
    # Exact pre-index policy, including duplicate IDs and assistant-only
    # consumption evidence across malformed/recovery role sequences.
    for assistant_idx in range(len(messages) - 1, -1, -1):
        assistant = messages[assistant_idx]
        if assistant.get('role') != 'assistant' or not assistant.get('tool_calls'):
            continue
        call_ids = {
            str(call.get('id') or '')
            for call in assistant.get('tool_calls') or ()
            if isinstance(call, dict) and call.get('id')
        }
        if not call_ids:
            continue
        batch = {
            idx for idx in tool_indices
            if idx > assistant_idx
            and str(messages[idx].get('tool_call_id') or '') in call_ids
        }
        if batch:
            last_result = max(batch)
            if any(messages[idx].get('role') == 'assistant'
                   for idx in range(last_result + 1, len(messages))):
                return set()
            return batch
    return set()


def test_newest_tool_batch_matches_original_on_random_recovery_histories():
    import random
    from lib.tasks_pkg.compaction._builtin_steps._toolresults import (
        _newest_tool_batch_indices)
    rng = random.Random(61742)
    ids = [None, '', 0, 1, '1', 'a', 'b', 'orphan']
    calls = [None, 'malformed', {}, {'id': None}, {'id': ''},
             {'id': 0}, {'id': 1}, {'id': '1'}, {'id': 'a'}, {'id': 'b'}]
    for _ in range(1500):
        messages = []
        for index in range(rng.randrange(90)):
            message = {
                'role': rng.choice(['assistant', 'tool', 'user', 'system',
                                    'historical_user', 'recovery', None]),
                'content': f'unchanged {index}',
            }
            if rng.randrange(4):
                message['tool_calls'] = rng.choices(calls, k=rng.randrange(6))
            if rng.randrange(4):
                message['tool_call_id'] = rng.choice(ids)
            messages.append(message)
        tool_indices = [i for i, m in enumerate(messages) if m['role'] == 'tool']
        # Recovery callers may supply a subset, repeated positions, or a
        # different ordering. Selection still returns deduplicated positions.
        if tool_indices and rng.randrange(2):
            tool_indices = rng.choices(tool_indices, k=len(tool_indices) * 2)
        rng.shuffle(tool_indices)
        original = copy.deepcopy(messages)
        assert _newest_tool_batch_indices(messages, tool_indices) == (
            _newest_tool_batch_reference(messages, tool_indices))
        assert messages == original


@pytest.mark.parametrize('prefix', ['a\nb\n', '\n\n', 'a\n', 'x' * 119,
                                    'x' * 120, 'x' * 121, 'x' * 130 + '\n'])
def test_l1_preview_is_byte_identical_with_bounded_source_split(cheap_evidence, prefix):
    class BoundedPreviewText(str):
        def split(self, sep=None, maxsplit=-1):
            assert len(self) <= 121, 'preview split copied the entire result'
            return super().split(sep, maxsplit)
    content = prefix + 'payload\n' * 2000
    expected = '\n'.join(content.split('\n')[:2])
    if len(expected) > 120:
        expected = expected[:120] + '…'
    messages = _batch(1)
    messages[1]['content'] = BoundedPreviewText(content)
    _compact(messages)
    assert f'Preview: {expected}\n[Re-call' in messages[1]['content']


def test_l1_unchanged_second_pass_emits_nothing_and_does_not_index(cheap_evidence):
    rows = CountedRows([{'toolCallId': 'c0', 'roundNum': 1, 'toolName': 'read_files'}])
    task = {'_userId': 1, 'toolRounds': rows}
    messages = _batch(1)
    assert _compact(messages, task) > 0
    cheap_evidence.clear()
    rows.visits = 0
    assert _compact(messages, task) == 0
    assert rows.visits == 0 and cheap_evidence == []


def test_l1_duplicate_metadata_keeps_first_match_and_failed_emit_does_not_stop(
        cheap_evidence, monkeypatch):
    from lib.tasks_pkg import manager
    calls = []
    def emit(task, event):
        calls.append(event)
        if len(calls) == 1:
            raise RuntimeError('synthetic observer failure')
    monkeypatch.setattr(manager, 'append_event', emit)
    rows = [{'toolCallId': 'c0', 'roundNum': 7, 'toolName': 'read_files'},
            {'toolCallId': 'c0', 'roundNum': 8, 'toolName': 'read_files'},
            {'toolCallId': 'c1', 'roundNum': 9, 'toolName': 'read_files'}]
    messages = _batch(2)
    assert _compact(messages, {'_userId': 1, 'toolRounds': rows}) > 0
    assert [e['roundNum'] for e in calls] == [7, 9]
    assert all('compacted' in messages[i]['content'] for i in (1, 2))


def _elision_reference(parts, budget, marker):
    keep = [True] * len(parts)
    def render():
        output = []
        dropped = False
        for retained, (_, value) in zip(keep, parts):
            if retained:
                output.append(value)
                dropped = False
            elif not dropped:
                output.append(marker)
                dropped = True
        return '\n\n'.join(output)
    candidates = [i for i, (role, _) in enumerate(parts)
                  if role not in ('user', 'historical_user')]
    for index in sorted(candidates, key=lambda i: abs(i - len(parts) / 2)):
        if len(render()) <= budget:
            break
        keep[index] = False
    return render()


def test_summary_elision_matches_original_output_on_random_overlaps():
    import random
    from lib.tasks_pkg.compaction._layer2._prompt import _elide_to_budget, _ELISION_MARKER
    rng = random.Random(8427)
    for _ in range(180):
        parts = [(rng.choice(('user', 'historical_user', 'assistant', 'context')),
                  str(i) + 'x' * rng.randrange(200))
                 for i in range(rng.randrange(35))]
        budget = rng.randrange(2500)
        expected = _elision_reference(parts, budget, _ELISION_MARKER.strip())
        assert _elide_to_budget(parts, budget) == expected
        for role, text in parts:
            if role in ('user', 'historical_user'):
                assert text in expected


def test_summary_elision_scans_input_a_constant_number_of_times():
    from lib.tasks_pkg.compaction._layer2._prompt import _elide_to_budget
    parts = CountedRows([('assistant', 'x' * 100) for _ in range(600)])
    _elide_to_budget(parts, 10)
    assert parts.visits <= len(parts) * 4


def test_summary_format_reports_full_size_without_losing_human_text():
    from lib.tasks_pkg.compaction._layer2._prompt import _format_messages_for_summary
    messages = [{'role': 'user', 'content': '  preserve whitespace and intent  '}] + [
        {'role': 'assistant', 'content': 'state' * 100} for _ in range(10)]
    full = _format_messages_for_summary(messages)
    measurement = {}
    bounded = _format_messages_for_summary(messages, char_budget=100,
                                            measurement_out=measurement)
    assert measurement == {'fullChars': len(full), 'renderedChars': len(bounded)}
    assert '  preserve whitespace and intent  ' in bounded


def test_stage_timing_is_monotonic_content_free_and_preserves_failure(monkeypatch):
    from lib.tasks_pkg.compaction import _timing
    clock = iter((10.0, 12.5))
    recorded = []
    monkeypatch.setattr(_timing.time, 'monotonic', lambda: next(clock))
    monkeypatch.setattr(_timing.logger, 'info', lambda *args: recorded.append(args))
    error = RuntimeError('preserve original failure')
    with pytest.raises(RuntimeError) as caught:
        with _timing.compaction_stage_timing('l2', {'id': 'abcdefgh-more',
                                                  'messages': ['PRIVATE']}, 2):
            raise error
    assert caught.value is error
    assert recorded[0][1:] == ('abcdefgh', 2, 'l2', 'error', 2500.0)
    assert 'PRIVATE' not in str(recorded)
