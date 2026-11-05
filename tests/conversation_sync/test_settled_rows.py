"""Settled projection rows are deep-immutable, JSON-transparent and shareable."""
import collections
import copy
import dataclasses
import json
import pickle
import random
import sys

import pytest

import lib.conversation_sync.settled_rows as settled_rows
from lib.conversation_sync.settled_rows import (
    SettledDict,
    SettledList,
    SettledRowMutation,
    UnsettleableValue,
    compact_json_bytes,
    derive_rows,
    detach_frame_sharing_settled,
    detach_sharing_settled,
    interchangeable,
    is_settled,
    json_array_text,
    memo_get,
    memo_put,
    settle,
    settle_items,
    settle_or_none,
    settle_unless_same,
    settled_retained_bytes,
)
from lib.tools.result_value import CanonicalReceipt, TrustedResultText
from lib.utils import fast_deepcopy, fast_deepcopy_with_retained_bytes, retained_graph_bytes

pytestmark = pytest.mark.unit


def _row():
    return {
        'roundNum': 3, 'toolName': 'read_files', 'status': 'done',
        'toolArgs': '{"path": "a.py"}', 'ratio': -0.0, 'ok': True, 'none': None,
        'results': [{'title': 'a.py', 'output': 'x' * 40, 'tags': ['a', 'b']}],
        'pair': (1, 'two'), 'nested': {'deep': {'deeper': [1, 2.5, {'k': 'v'}]}},
    }


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def test_settled_twin_serializes_byte_identically_and_compares_equal():
    row = _row()
    settled = settle(row)
    assert type(settled) is SettledDict
    assert type(settled['results']) is SettledList
    assert type(settled['results'][0]['tags']) is SettledList
    assert type(settled['nested']['deep']) is SettledDict
    assert settled == row
    assert _encoded(settled) == _encoded(row)
    assert json.dumps(settled, sort_keys=True, indent=2) == json.dumps(row, sort_keys=True, indent=2)
    assert list(settled) == list(row)
    # Atoms are shared, and settling a settled tree is the identity.
    assert settled['toolArgs'] is row['toolArgs']
    assert settled['pair'] is row['pair']
    assert settle(settled) is settled


@pytest.mark.parametrize('mutate', [
    lambda row: row.__setitem__('status', 'error'),
    lambda row: row.__delitem__('status'),
    lambda row: row.update(status='error'),
    lambda row: row.setdefault('fresh', 1),
    lambda row: row.pop('status'),
    lambda row: row.popitem(),
    lambda row: row.clear(),
    lambda row: row.__ior__({'fresh': 1}),
    lambda row: row.__init__({'fresh': 1}),
    lambda row: row['results'].append({}),
    lambda row: row['results'].extend([{}]),
    lambda row: row['results'].insert(0, {}),
    lambda row: row['results'].__setitem__(0, {}),
    lambda row: row['results'].__delitem__(0),
    lambda row: row['results'].pop(),
    lambda row: row['results'].remove(row['results'][0]),
    lambda row: row['results'].clear(),
    lambda row: row['results'].sort(),
    lambda row: row['results'].reverse(),
    lambda row: row['results'].__iadd__([{}]),
    lambda row: row['results'].__imul__(2),
    lambda row: row['results'][0]['tags'].append('c'),
    lambda row: row['nested']['deep'].__setitem__('deeper', []),
])
def test_every_in_place_mutation_of_a_settled_row_raises(mutate):
    settled = settle(_row())
    before = _encoded(settled)
    with pytest.raises(SettledRowMutation):
        mutate(settled)
    assert _encoded(settled) == before


def test_augmented_assignment_on_a_settled_value_raises():
    settled = settle(_row())
    results = settled['results']
    with pytest.raises(SettledRowMutation):
        results += [{}]
    row = settled
    with pytest.raises(SettledRowMutation):
        row |= {'fresh': 1}


def test_copies_are_ordinary_mutable_containers():
    settled = settle(_row())
    shallow = dict(settled)
    assert type(shallow) is dict and type(copy.copy(settled)) is dict
    assert type(settled.copy()) is dict and type(settled | {}) is dict
    assert type(settled['results'][:]) is list and type(copy.copy(settled['results'])) is list
    shallow['status'] = 'error'
    assert settled['status'] == 'done'
    # Nested values of a shallow copy stay settled: replace, never edit them.
    with pytest.raises(SettledRowMutation):
        shallow['results'].append({})
    thawed = copy.deepcopy(settled)
    assert type(thawed) is dict and type(thawed['results']) is list
    assert type(thawed['results'][0]['tags']) is list
    thawed['results'][0]['tags'].append('c')
    assert settled['results'][0]['tags'] == ['a', 'b']
    unpickled = pickle.loads(pickle.dumps(settled))
    assert unpickled == settled
    assert type(unpickled) is dict and type(unpickled['nested']['deep']) is dict


def test_generic_rebuilders_produce_plain_mutable_copies():
    # dataclasses.asdict and the type(obj)(items) idiom rebuild containers
    # through the row's own type; a rebuilt row is a copy like any other.
    settled = settle(_row())
    assert type(type(settled)(settled)) is dict
    assert type(type(settled['results'])(settled['results'])) is list

    @dataclasses.dataclass
    class Carrier:
        row: dict

    plain = dataclasses.asdict(Carrier(settled))['row']
    assert plain == settled and type(plain) is dict
    assert type(plain['results']) is list and type(plain['nested']['deep']) is dict
    plain['results'][0]['tags'].append('c')
    assert settled['results'][0]['tags'] == ['a', 'b']
    assert type(SettledDict()) is SettledDict and type(SettledList()) is SettledList


def test_settle_preserves_aliasing_and_refuses_mutable_custom_objects():
    shared = {'v': [1]}
    settled = settle({'a': shared, 'b': shared})
    assert settled['a'] is settled['b']
    receipt = CanonicalReceipt({'resultId': 'r1'}, canonical=None)
    with pytest.raises(UnsettleableValue):
        settle({'results': [receipt]})
    assert settle_or_none({'results': [receipt]}) is None
    assert settle_or_none({'x': {1, 2}}) is None
    # Immutable value subclasses (canonical result text) are shared values.
    text = TrustedResultText('payload', canonical=None)
    assert settle({'toolContent': text})['toolContent'] is text
    items = settle_items([{'a': 1}, receipt, 'atom'])
    assert type(items) is list and type(items[0]) is SettledDict
    assert items[1] is receipt and items[2] == 'atom'


def test_rebuilt_lists_reuse_interchangeable_settled_items_by_position():
    first = settle_items([{'id': 1, 'v': [1]}, {'id': 2}])
    rebuilt = settle_items([{'id': 1, 'v': [1]}, {'id': 2, 'v': True}, {'id': 3}],
                           previous=first)
    assert rebuilt[0] is first[0]
    assert rebuilt[1] is not first[1] and rebuilt[1] == {'id': 2, 'v': True}
    assert type(rebuilt[2]) is SettledDict
    assert settle_items([{'id': 1, 'v': [True]}], previous=first)[0] is not first[0]


def test_interchangeable_distinguishes_every_json_or_type_difference():
    assert interchangeable(settle(_row()), _row())
    assert not interchangeable({'a': 1}, {'a': True})
    assert not interchangeable({'a': 1}, {'a': 1.0})
    assert not interchangeable({'a': 0.0}, {'a': -0.0})
    assert not interchangeable({'a': 1, 'b': 2}, {'b': 2, 'a': 1})
    assert not interchangeable({'a': [1]}, {'a': (1,)})
    assert not interchangeable({'a': 'x'}, {'a': TrustedResultText('x')})
    assert interchangeable({'a': float('nan')}, {'a': float('nan')})
    row = settle(_row())
    assert settle_unless_same(row, dict(row)) is row
    changed = settle_unless_same(row, {**row, 'status': 'error'})
    assert changed is not row and type(changed) is SettledDict
    assert changed['status'] == 'error'


def test_memo_lives_on_the_row_and_tolerates_identity_results():
    row = settle(_row())
    assert memo_get(row, 'k') is None
    assert memo_put(row, 'k', row) is row
    assert memo_get(row, 'k') is row
    assert memo_put(row, 'none', None) is None
    sentinel = object()
    assert memo_get(row, 'none', sentinel) is None
    assert memo_get(row, 'absent', sentinel) is sentinel
    # Memo state is invisible to equality, JSON and copies.
    assert row == _row() and _encoded(row) == _encoded(_row())


def _identity_walk_bytes(value):
    pending, seen, total = [value], set(), 0
    while pending:
        item = pending.pop()
        if id(item) in seen:
            continue
        seen.add(id(item))
        total += sys.getsizeof(item)
        if isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, (list, tuple)):
            pending.extend(item)
    return total


def test_detach_matches_exact_copy_accounting_without_settled_rows():
    graph = {'rows': [_row(), _row()], 'meta': {'k': [1, 2, 3]}, 'shared': None}
    graph['shared'] = graph['rows'][0]
    expected, expected_bytes = fast_deepcopy_with_retained_bytes(graph)
    detached, charged = detach_sharing_settled(graph)
    assert _encoded(detached) == _encoded(expected)
    assert charged == expected_bytes
    assert detached['shared'] is detached['rows'][0]
    assert detached['rows'][0] is not graph['rows'][0]
    assert detach_sharing_settled(graph, measure=False)[1] == 0


def test_detach_shares_settled_rows_and_charges_each_standalone_once():
    settled = settle(_row())
    live = _row()
    graph = {'rows': [settled, live, settled], 'mirror': [settled]}
    detached, charged = detach_sharing_settled(graph)
    assert detached['rows'][0] is settled and detached['mirror'][0] is settled
    assert detached['rows'][1] is not live and detached['rows'][1] == live
    assert settled_retained_bytes(settled) == _identity_walk_bytes(settled)
    # Owner allowance + unsettled structure walked once + each settled row once.
    expected = 1024 + settled_retained_bytes(settled)
    pending, seen = [detached], set()
    while pending:
        item = pending.pop()
        if id(item) in seen:
            continue
        seen.add(id(item))
        if type(item) in (SettledDict, SettledList):
            continue
        expected += sys.getsizeof(item)
        if isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, (list, tuple)):
            pending.extend(item)
    assert charged == expected


def test_canonical_text_never_forces_a_whole_graph_rewalk():
    text = TrustedResultText('canonical output', canonical=None)
    graph = {'rows': [{'toolContent': text}]}
    expected, expected_bytes = fast_deepcopy_with_retained_bytes(graph)
    detached, charged = detach_sharing_settled(graph)
    assert detached['rows'][0]['toolContent'] is text
    assert _encoded(detached) == _encoded(expected)
    assert charged == expected_bytes


@pytest.mark.parametrize('layout', ['mixed', 'settled-only', 'plain-only', 'empty'])
def test_compact_json_bytes_equals_the_full_encoding(layout):
    settled_rows = [settle({'id': f'r{i}', 'text': 'é' * i}) for i in range(5)]
    rows = {
        'mixed': [{'id': 'live'}, *settled_rows[:2], {'id': '中'}, *settled_rows[2:]],
        'settled-only': settled_rows,
        'plain-only': [{'id': 'live'}, {'id': 'x'}],
        'empty': [],
    }[layout]
    document = {'version': 1, 'entries': rows, 'tail': 'x', 'other': settled_rows[:1]}
    expected = len(_encoded(document).encode('utf-8'))
    assert compact_json_bytes(document, row_fields=('entries', 'other')) == expected
    assert compact_json_bytes(document, row_fields=('entries', 'other')) == expected
    assert compact_json_bytes(document) == expected


def _documented_bytes(*values):
    """Owner allowance + unsettled objects once + each settled row standalone once."""
    pending, seen, total = list(values), set(), 1024
    while pending:
        item = pending.pop()
        if id(item) in seen:
            continue
        seen.add(id(item))
        if type(item) in (SettledDict, SettledList):
            total += _identity_walk_bytes(item)
            continue
        total += sys.getsizeof(item)
        if isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, (list, tuple)):
            pending.extend(item)
    return total


def _frame():
    shared = {'tokens': ['alpha', 'beta'], 'count': 7}
    return {
        'type': 'tool_result', 'roundNum': 12, 'status': 'done', 'ratio': 0.5,
        'results': [{'title': 'a.py', 'output': 'x' * 300, 'meta': shared},
                    {'title': 'b.py', 'meta': shared, 'none': None}],
        'usage': shared, 'tags': ['done', 'alpha'],
    }


def test_frame_charge_equals_a_fresh_walk_of_the_frame_as_enqueued():
    source = _frame()
    detached, charge = detach_frame_sharing_settled(source)
    assert _encoded(detached) == _encoded(fast_deepcopy(source))
    assert detached['results'][0]['meta'] is detached['usage'] is not source['usage']
    # The producer stamps the root after detachment: new keys, an overwrite
    # that orphans an atom, and an atom that also lives deeper in the frame.
    detached['seq'] = 40_961
    detached['taskId'] = 'task-' + 'x' * 30
    detached['ratio'] = 0.75
    detached['label'] = detached['tags'][1]
    phase = {'phase': detached['type'], 'detail': 'beat', 'seq': detached['seq']}
    content = 'visible ' * 9
    frame_bytes, total_bytes = charge.complete(content, '', phase)
    assert frame_bytes == retained_graph_bytes(detached)
    assert total_bytes == retained_graph_bytes(detached, content, '', phase)
    source['usage']['tokens'].append('later')
    assert detached['usage']['tokens'] == ['alpha', 'beta']


def test_frame_charge_reads_text_sizes_when_the_frame_is_enqueued():
    orjson = pytest.importorskip('orjson')
    text = 'é中文 output ' * 40
    source = {'type': 'tool_result', 'query': 'q é',
              'results': [{'output': text, 'title': 'résumé'}]}
    detached, charge = detach_frame_sharing_settled(source)
    before = sys.getsizeof(text)
    # The runtime sizes its replay window with orjson before enqueueing; that
    # attaches a cached UTF-8 form to non-ASCII text.
    orjson.dumps(detached)
    assert sys.getsizeof(text) > before
    detached['seq'] = 4
    frame_bytes, _ = charge.complete()
    assert frame_bytes == retained_graph_bytes(detached)


def test_frame_charge_without_side_values_is_the_frame_alone():
    detached, charge = detach_frame_sharing_settled(_frame())
    detached['seq'] = 3
    frame_bytes, total_bytes = charge.complete()
    assert frame_bytes == total_bytes == retained_graph_bytes(detached)


def test_frame_charge_measures_the_final_frame_after_a_generic_copy():
    class Key:
        def __deepcopy__(self, memo):
            return 'same-key'

    text = TrustedResultText('canonical', canonical=None)
    source = {'type': 'phase', 'merged': {Key(): ['discarded'], Key(): ['kept']},
              'pair': (1, ['x']), 'text': text}
    detached, charge = detach_frame_sharing_settled(source)
    assert detached['merged'] == {'same-key': ['kept']}
    assert detached['text'] is text and detached['pair'] == (1, ['x'])
    detached['seq'] = 9
    frame_bytes, total_bytes = charge.complete('side')
    assert frame_bytes == retained_graph_bytes(detached)
    assert total_bytes == retained_graph_bytes(detached, 'side')


def test_frame_shares_settled_rows_and_charges_the_documented_formula():
    settled = settle(_row())
    source = {'type': 'state', 'toolRounds': [settled, _row(), settled], 'last': settled}
    detached, charge = detach_frame_sharing_settled(source)
    assert detached['toolRounds'][0] is settled is detached['last']
    assert type(detached['toolRounds'][1]) is dict and detached['toolRounds'][1] == _row()
    detached['seq'] = 3
    frame_bytes, total_bytes = charge.complete('a', None)
    assert frame_bytes == _documented_bytes(detached)
    assert total_bytes == _documented_bytes(detached, 'a', None)


def _containers(*values):
    pending, seen, found = list(values), set(), []
    while pending:
        item = pending.pop()
        if id(item) in seen or is_settled(item):
            continue
        seen.add(id(item))
        if isinstance(item, dict):
            found.append(item)
            pending.extend(item.values())
        elif isinstance(item, (list, tuple)):
            found.append(item)
            pending.extend(item)
    return found


def test_frame_copy_and_completion_measure_each_object_once(monkeypatch):
    measured = collections.Counter()
    getsizeof = sys.getsizeof

    def counting_getsizeof(value, *default):
        measured[id(value)] += 1
        return getsizeof(value, *default)

    source = _frame()
    phase = {'phase': 'tool_exec', 'detail': ['beat']}
    monkeypatch.setattr(sys, 'getsizeof', counting_getsizeof)
    detached, charge = detach_frame_sharing_settled(source)
    detached['seq'] = 77
    charge.complete('text', phase)
    monkeypatch.undo()
    containers = _containers(detached, phase)
    assert len(containers) == 9
    assert all(measured[id(item)] == 1 for item in containers)
    assert set(measured.values()) == {1}


_DERIVED = ('test-derived',)


def _counting_derive(calls):
    def derive(items):
        calls.extend(items)
        derived = []
        for item in items:
            if is_settled(item):
                output = memo_get(item, _DERIVED)
                if output is None:
                    output = memo_put(item, _DERIVED, settle({**item, 'derived': True}))
            elif isinstance(item, dict):
                output = {**item, 'derived': True}
            else:
                output = item
            derived.append(output)
        return derived
    return derive


def _weigh(row):
    return len(row.get('text', ''))


def _assert_memo_describes(memo, rows, derived):
    reused = [position for position, (row, output) in enumerate(zip(rows, derived))
              if is_settled(row) and is_settled(output)]
    distinct = {id(derived[position]): derived[position] for position in reused}
    assert memo.inputs == tuple(rows) and memo.outputs == tuple(derived)
    assert memo.live == tuple(position for position in range(len(rows))
                              if position not in reused)
    assert memo.shared == frozenset(distinct)
    assert memo.charge == sum(settled_retained_bytes(output) for output in distinct.values())
    assert memo.weight == sum(_weigh(derived[position]) for position in reused)


def test_derived_rows_derive_each_settled_row_once_and_live_rows_every_time():
    calls = []
    derive = _counting_derive(calls)
    live = {'id': 'live', 'text': 'running'}
    rows = [settle({'id': index, 'text': 'é' * index}) for index in range(4)] + [live]

    derived, memo = derive_rows(rows, derive, weigh=_weigh)
    assert calls == rows and derived is not rows
    assert [type(item) for item in derived] == [SettledDict] * 4 + [dict]
    assert derived[4] == {**live, 'derived': True}
    _assert_memo_describes(memo, rows, derived)

    # Unchanged settled rows are reused; the live row is derived again.
    calls.clear()
    live['text'] = 'running longer'
    again, same = derive_rows(rows, derive, memo, weigh=_weigh)
    assert calls == [live] and same is memo
    assert all(new is old for new, old in zip(again[:4], derived))
    assert again[4] == {**live, 'derived': True}

    # The live row settles in its own position and a new live row follows.
    rows[4] = settle(live)
    rows.append({'id': 'next', 'text': ''})
    calls.clear()
    derived, memo = derive_rows(rows, derive, memo, weigh=_weigh)
    assert calls == rows[4:]
    _assert_memo_describes(memo, rows, derived)

    # A bounded window drops its oldest rows.
    del rows[:2]
    calls.clear()
    derived, memo = derive_rows(rows, derive, memo, weigh=_weigh)
    assert calls == [rows[-1]]
    _assert_memo_describes(memo, rows, derived)

    # A writer replaces one settled row copy-on-write.
    rows[1] = settle({**rows[1], 'text': 'replaced'})
    calls.clear()
    derived, memo = derive_rows(rows, derive, memo, weigh=_weigh)
    assert calls == [rows[1], rows[-1]]
    _assert_memo_describes(memo, rows, derived)
    assert derived == _counting_derive([])(list(rows))


def test_derived_rows_rebuild_when_a_repeated_row_leaves_or_rows_reorder():
    calls = []
    derive = _counting_derive(calls)
    repeated = settle({'id': 'repeated', 'text': 'x'})
    rows = [repeated, settle({'id': 1, 'text': 'y'}), repeated, 'atom']
    derived, memo = derive_rows(rows, derive, weigh=_weigh)
    assert derived[0] is derived[2] and memo.distinct is False
    _assert_memo_describes(memo, rows, derived)
    del rows[0]
    calls.clear()
    derived, memo = derive_rows(rows, derive, memo, weigh=_weigh)
    assert calls == rows
    _assert_memo_describes(memo, rows, derived)
    rows.reverse()
    derived, memo = derive_rows(rows, derive, memo, weigh=_weigh)
    _assert_memo_describes(memo, rows, derived)
    rows.clear()
    derived, memo = derive_rows(rows, derive, memo, weigh=_weigh)
    assert derived == [] and memo.charge == 0 and memo.shared == frozenset()


def test_derived_rows_memo_never_survives_a_copy():
    _, memo = derive_rows([settle({'id': 1})], _counting_derive([]))
    assert copy.deepcopy(memo) is None and copy.copy(memo) is None
    assert pickle.loads(pickle.dumps(memo)) is None
    assert copy.deepcopy({'rows': memo}) == {'rows': None}


def _random_rows(rng):
    nested = {'tokens': ['a', 'b']}
    rows = []
    for index in range(rng.randint(0, 40)):
        roll = rng.random()
        if roll < 0.6:
            rows.append(settle({'id': index, 'text': 'é' * rng.randint(0, 30),
                                'tags': ['x', 'y'], 'ok': index % 2 == 0}))
        elif roll < 0.85:
            rows.append({'id': index, 'meta': nested, 'text': 't' * rng.randint(0, 9)})
        elif roll < 0.95 and rows:
            rows.append(rng.choice(rows))
        else:
            rows.append(rng.choice([None, 'atom', 7, 2 ** 70]))
    return rows


def _alias_pattern(items):
    first = {}
    return [first.setdefault(id(item), index) for index, item in enumerate(items)]


@pytest.mark.parametrize('seed', range(10))
def test_detaching_derived_rows_equals_the_per_row_copy(seed):
    rng = random.Random(seed)
    rows = _random_rows(rng)
    derived, memo = derive_rows(rows, lambda items: items)
    settled_items = [row for row in rows if is_settled(row)]
    elsewhere = (rng.choice(settled_items) if settled_items and rng.random() < 0.7
                 else {'k': 'v'})
    live_items = [row for row in rows if type(row) is dict]
    for graph in ({'before': elsewhere, 'rows': derived},
                  {'rows': derived, 'after': elsewhere},
                  {'rows': derived, 'alias': derived,
                   'nested': [live_items[:1], {'row': elsewhere}]}):
        expected, expected_bytes = detach_sharing_settled(graph)
        planned = {id(derived): memo}
        detached, charged = detach_sharing_settled(graph, derived_rows=planned)
        assert charged == expected_bytes
        assert _encoded(detached) == _encoded(expected)
        assert sys.getsizeof(detached['rows']) == sys.getsizeof(expected['rows'])
        assert _alias_pattern(detached['rows']) == _alias_pattern(expected['rows'])
        for got, want, row in zip(detached['rows'], expected['rows'], derived):
            assert (got is row) == (want is row)
        if 'alias' in graph:
            assert detached['alias'] is detached['rows']
            for item, copied in zip(graph['nested'][0], detached['nested'][0]):
                position = next(index for index, row in enumerate(derived) if row is item)
                assert copied is detached['rows'][position]
        unmeasured, zero = detach_sharing_settled(graph, measure=False, derived_rows=planned)
        assert zero == 0 and _encoded(unmeasured) == _encoded(expected)


def test_detaching_derived_rows_never_visits_reused_rows(monkeypatch):
    rows = [settle({'id': index, 'text': 'x' * index}) for index in range(50)]
    rows.append({'id': 'live', 'results': [{'output': 'o'}]})
    derived, memo = derive_rows(rows, lambda items: items)
    expected, expected_bytes = detach_sharing_settled({'rows': derived})
    visited = []
    original = settled_rows.settled_retained_bytes
    monkeypatch.setattr(settled_rows, 'settled_retained_bytes',
                        lambda row: visited.append(row) or original(row))
    detached, charged = detach_sharing_settled({'rows': derived},
                                               derived_rows={id(derived): memo})
    assert visited == [] and charged == expected_bytes
    assert all(got is row for got, row in zip(detached['rows'][:50], rows))
    assert detached['rows'][50] == rows[50] and detached['rows'][50] is not rows[50]


@pytest.mark.parametrize('layout', ['mixed', 'settled-only', 'plain-only', 'empty'])
def test_json_array_text_equals_the_default_encoding_and_encodes_settled_rows_once(
        layout, monkeypatch):
    from lib.conversation_sync import settled_rows
    settled = [settle({'id': f'r{i}', 'text': 'é "q"\n' * i, 'ratio': 1e-05 * i})
               for i in range(4)]
    rows = {
        'mixed': [{'id': 'live', 'v': [1, 2.5]}, *settled[:2], {'id': '中'}, *settled[2:]],
        'settled-only': settled,
        'plain-only': [{'id': 'live'}, 'atom', None],
        'empty': [],
    }[layout]
    expected = json.dumps(rows, ensure_ascii=False)
    encoded = []

    class CountingJson:
        @staticmethod
        def dumps(value, **kwargs):
            encoded.append(value)
            return json.dumps(value, **kwargs)

    monkeypatch.setattr(settled_rows, 'json', CountingJson)
    assert json_array_text(rows) == expected
    first = len(encoded)
    assert json_array_text(rows) == expected
    assert len(encoded) - first == sum(type(row) is not SettledDict for row in rows)
    assert json_array_text(settle(rows)) == expected
    assert json_array_text({'not': 'a list'}) == json.dumps({'not': 'a list'}, ensure_ascii=False)
