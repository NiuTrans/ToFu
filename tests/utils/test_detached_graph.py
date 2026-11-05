"""Detached graph copying preserves aliases and exact retained accounting."""
from __future__ import annotations

import copy
import sys

import pytest

from lib.utils import fast_deepcopy_with_retained_bytes

pytestmark = pytest.mark.unit


def _retained_oracle(value):
    objects = {}

    def visit(node):
        if id(node) in objects:
            return
        objects[id(node)] = node
        if isinstance(node, dict):
            for key, child in node.items():
                visit(key)
                visit(child)
        elif isinstance(node, (list, tuple)):
            for child in node:
                visit(child)

    visit(value)
    return 1024 + sum(sys.getsizeof(node) for node in objects.values())


@pytest.mark.parametrize('value', [
    None, True, 193243, 2.5, '内容', b'bytes', [], {},
    {'rows': [{'id': index, 'tags': ['alpha', 'beta'], 'empty': None}
              for index in range(33)]},
])
def test_exact_accounting_and_detachment(value):
    detached, retained = fast_deepcopy_with_retained_bytes(value)
    assert detached == copy.deepcopy(value)
    assert retained == _retained_oracle(detached)
    if isinstance(value, (list, dict)):
        assert detached is not value


def test_shared_subtrees_and_cycles_keep_deepcopy_identity():
    shared = {'tokens': ['one', 'two']}
    cycle = {'shared': shared}
    cycle['self'] = cycle
    items = [shared, cycle]
    items.append(items)
    source = {'first': shared, 'second': shared, 'cycle': cycle, 'items': items}

    detached, retained = fast_deepcopy_with_retained_bytes(source)
    assert detached['first'] is detached['second'] is detached['items'][0]
    assert detached['cycle']['self'] is detached['cycle']
    assert detached['items'][2] is detached['items']
    assert detached['cycle']['shared'] is detached['first']
    assert detached['first'] is not shared
    detached['first']['tokens'].append('owned')
    assert shared['tokens'] == ['one', 'two']
    detached['first']['tokens'].pop()
    assert retained == _retained_oracle(detached)


def test_deepcopy_hooks_subclasses_and_tuple_cycles_keep_identity():
    class Subdict(dict):
        pass

    class Hook:
        def __deepcopy__(self, memo):
            result = Hook()
            result.copied = True
            return result

    shared = [1, {'nested': ['value']}]
    tuple_cycle = (shared,)
    shared.append(tuple_cycle)
    source = {'shared': shared, 'tuple': tuple_cycle,
              'subclass': Subdict(value=shared), 'hook': Hook(), 'set': {1, 2}}

    detached, retained = fast_deepcopy_with_retained_bytes(source)
    assert detached['shared'] is detached['tuple'][0]
    assert detached['shared'][2] is detached['tuple']
    assert type(detached['subclass']) is Subdict
    assert detached['subclass']['value'] is detached['shared']
    assert detached['hook'].copied is True
    assert detached['set'] == {1, 2} and detached['set'] is not source['set']
    assert retained == _retained_oracle(detached)


def test_custom_key_collision_accounts_only_the_final_reachable_graph():
    class Key:
        def __deepcopy__(self, memo):
            return 'same-key'

    source = {Key(): ['discarded-value'], Key(): ['retained-value']}
    detached, retained = fast_deepcopy_with_retained_bytes(source)
    assert detached == copy.deepcopy(source) == {'same-key': ['retained-value']}
    assert retained == _retained_oracle(detached)


def test_copy_failure_is_not_reinterpreted_as_an_empty_snapshot():
    class RefusesCopy:
        def __deepcopy__(self, memo):
            raise RuntimeError('copy refused')

    with pytest.raises(RuntimeError, match='copy refused'):
        fast_deepcopy_with_retained_bytes({'value': RefusesCopy()})
