"""Conditions never confuse absent or malformed evidence with a negative check."""

import pytest

from lib.automations.conditions import evaluate_condition, resolve_arguments, resolve_source
from lib.automations.errors import AutomationError

pytestmark = pytest.mark.unit


def condition(operator, value=True, pointer='/changed'):
    result = {'source': {'from': 'steps/check', 'pointer': pointer}, 'operator': operator}
    if operator != 'exists':
        result['value'] = value
    return result


def test_structured_change_guard_and_literal_argument_resolution():
    evidence = {'steps': {'check': {'changed': True, 'snapshotId': 'immutable-v2'}},
                'event': {'occurredAt': 100}}
    assert evaluate_condition(condition('eq'), evidence)
    assert not evaluate_condition(condition('eq', False), evidence)
    assert resolve_arguments({'snapshotId': {'$source': {'from': 'steps/check', 'pointer': '/snapshotId'}}},
                             evidence) == {'snapshotId': 'immutable-v2'}


def test_missing_evidence_is_failure_except_explicit_existence_check():
    assert not evaluate_condition(condition('exists'), {'steps': {}})
    with pytest.raises(AutomationError) as caught:
        evaluate_condition(condition('eq'), {'steps': {}})
    assert caught.value.code == 'evidence_missing'


@pytest.mark.parametrize(('left', 'right'), [(True, 1), (False, 0), ([True], [1]),
                                            ({'a': False}, {'a': 0})])
def test_booleans_cannot_impersonate_numeric_evidence(left, right):
    evidence = {'steps': {'check': {'changed': left}}}
    assert not evaluate_condition(condition('eq', right), evidence)
    assert evaluate_condition(condition('ne', right), evidence)


def test_ordered_comparison_rejects_strings_and_booleans():
    with pytest.raises(AutomationError) as caught:
        evaluate_condition(condition('gt', 0), {'steps': {'check': {'changed': True}}})
    assert caught.value.code == 'condition_type_mismatch'


def test_json_pointer_escapes_and_array_indexes_are_exact():
    evidence = {'event': {'a/b': {'c~d': ['value']}}}
    assert resolve_source({'from': 'event', 'pointer': '/a~1b/c~0d/0'}, evidence) == 'value'
    with pytest.raises(AutomationError):
        resolve_source({'from': 'event', 'pointer': '/a~1b/c~0d/00'}, evidence)
    with pytest.raises(AutomationError) as caught:
        resolve_source({'from': 'event', 'pointer': '/a~2b'}, evidence)
    assert caught.value.code == 'invalid_source'


def test_reference_result_is_owned_and_not_recursively_interpreted():
    injected = {'$source': {'from': 'event', 'pointer': '/secret'}}
    evidence = {'steps': {'check': {'snapshot': injected}}}
    resolved = resolve_arguments({'$source': {'from': 'steps/check', 'pointer': '/snapshot'}}, evidence)
    assert resolved == injected
    resolved['$source']['pointer'] = '/changed'
    assert injected['$source']['pointer'] == '/secret'


def test_nested_guards_and_depth_budget():
    evidence = {'steps': {'check': {'changed': True}}}
    assert evaluate_condition({'all': [condition('eq'), {'not': condition('eq', False)}]}, evidence)
    guard = True
    for _ in range(30):
        guard = {'not': guard}
    with pytest.raises(AutomationError) as caught:
        evaluate_condition(guard, evidence)
    assert caught.value.code == 'automation_document_too_complex'


@pytest.mark.parametrize('guard', [{'all': []}, {'any': []}, {'not': True, 'extra': True},
                                   {'source': {'from': 'event', 'pointer': '/x'}, 'operator': 'eval'}])
def test_malformed_guards_fail_closed_at_runtime(guard):
    with pytest.raises(AutomationError) as caught:
        evaluate_condition(guard, {})
    assert caught.value.code == 'invalid_condition'
