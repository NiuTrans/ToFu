"""Comparison preserves sparse-field meaning, ordered evidence and report caps."""
from __future__ import annotations

import copy

from itertools import product
import random
import pytest

from lib.tasks_pkg.wire_fingerprint import diff_byte_field_prefix, diff_canonical

pytestmark = pytest.mark.unit


def field_changes(old, new, limit, raw):
    """Independent straightforward oracle for the existing diagnostic format."""
    changes = []
    for index, (previous, current) in enumerate(zip(old, new)):
        previous, current = previous or {}, current or {}
        before, after = previous.get('fields') or {}, current.get('fields') or {}
        key = current.get('key') or previous.get('key') or f'[{index}]'
        for name in sorted(set(before).union(after)):
            if before.get(name) == after.get(name):
                continue
            changes.append(f'<bytes>{key}{{{name}}}' if raw else f'{key}.{name}')
            if len(changes) >= limit:
                return changes + ['…']
    if len(old) != len(new):
        changes.append(f'{"byte-field-len" if raw else "len"} {len(old)}→{len(new)}')
    return changes


@pytest.mark.parametrize('limit', [-1, 0, 1, 2, 8])
def test_field_comparisons_match_complete_diagnostics_without_mutation(limit):
    # A fixed corpus avoids wall-clock-sensitive input-generation health checks
    # on shared CI hosts. Include exhaustive sparse rows and longer histories.
    variants = [None, {}, {'fields': {}}, {'fields': {'content': None}},
                {'key': 'tool-1', 'fields': {'content': 0, 'role': 1}},
                {'key': 'tool-2', 'fields': {'role': 0, 'tool_calls': -1}}]
    cases = [([old], [new]) for old, new in product(variants, repeat=2)]
    rng = random.Random(20261027)

    def history():
        return [None if rng.randrange(6) == 0 else {
            'key': rng.choice([None, '', 'tool-1', 'message-2']),
            'fields': {field: rng.choice([None, -2, -1, 0, 1, 2])
                       for field in ('content', 'role', 'tool_calls')
                       if rng.randrange(2)},
        } for _ in range(rng.randrange(13))]

    for index in range(300):
        old = history()
        cases.append((old, copy.deepcopy(old) if index % 4 == 0 else history()))
    for old, new in cases:
        original = copy.deepcopy((old, new))
        assert diff_canonical(old, new, limit) == field_changes(old, new, limit, False)
        assert diff_byte_field_prefix(old, new, limit) == field_changes(old, new, limit, True)
        assert (old, new) == original


@pytest.mark.parametrize('compare', [diff_canonical, diff_byte_field_prefix])
def test_unchanged_large_prefix_does_not_hide_a_late_change(compare):
    old = [{'key': str(i), 'fields': {'content': i}} for i in range(10000)]
    new = copy.deepcopy(old)
    new[-1]['fields']['content'] = -1
    assert compare(old, new) == field_changes(old, new, 8, compare is diff_byte_field_prefix)


def test_canonical_fast_path_preserves_invalid_field_mapping_failure():
    malformed = [{'fields': ['content']}]
    with pytest.raises(AttributeError):
        diff_canonical(malformed, malformed)


def test_custom_field_maps_keep_field_access_without_an_extra_equality_call():
    class Fields(dict):
        def __eq__(self, other):
            raise AssertionError('comparison must use field access for custom maps')

    old = [{'key': 'message', 'fields': Fields(content=1)}]
    new = [{'key': 'message', 'fields': Fields(content=2)}]
    assert diff_canonical(old, new) == ['message.content']


def test_tool_result_entries_without_block_evidence_keep_message_labels():
    """Snapshots without per-block evidence (single results, frozen benchmark
    rows) keep the established message-level report byte for byte."""
    old = [{'key': 'tool_result(a)', 'fields': {'role': 1, 'tool_result': 1}},
           {'key': 'user:1', 'fields': {'role': 2, 'content': 3}},
           {'fields': {'tool_result': 5}}]
    new = [{'key': 'tool_result(a)', 'fields': {'role': 1, 'tool_result': 2}},
           {'key': 'user:2', 'fields': {'role': 2, 'tool_result': 4}},
           {'fields': {'tool_result': 6}}]
    for limit in (-1, 0, 1, 2, 8):
        assert diff_canonical(old, new, limit) == field_changes(
            old, new, limit, False)


def _merged(*blocks):
    return {'key': blocks[0][0],
            'fields': {'role': 7, 'tool_result': hash(blocks)},
            'blocks': blocks}


def test_block_evidence_names_changed_added_and_removed_results():
    old = [_merged(('tool_result(a)', 1), ('tool_result(b)', 2),
                   ('tool_result(c)', 3))]
    changed = [_merged(('tool_result(a)', 1), ('tool_result(b)', 20),
                       ('tool_result(c)', 3))]
    added = [_merged(('tool_result(a)', 1), ('tool_result(b)', 2),
                     ('tool_result(c)', 3), ('tool_result(d)', 4))]
    single = [{'key': 'tool_result(a)',
               'fields': {'role': 7, 'tool_result': 1}}]

    assert diff_canonical(old, changed) == ['tool_result(b).tool_result']
    assert diff_canonical(old, added) == ['tool_result(d).tool_result']
    # A merged batch shrinking to one ordinary result names what disappeared.
    assert diff_canonical(old, single) == ['tool_result(b).tool_result',
                                           'tool_result(c).tool_result']


def test_block_evidence_respects_the_report_cap():
    old = [_merged(('tool_result(a)', 1), ('tool_result(b)', 2),
                   ('tool_result(c)', 3))]
    new = [_merged(('tool_result(a)', 10), ('tool_result(b)', 20),
                   ('tool_result(c)', 30))]

    assert diff_canonical(old, new, 2) == ['tool_result(a).tool_result',
                                           'tool_result(b).tool_result', '…']
