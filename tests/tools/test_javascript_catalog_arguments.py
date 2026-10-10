"""JavaScript catalog discovery validates inputs before crossing its callback port."""

import json

import pytest

from lib.tools.gateway import search_tools_schema
from lib.tools.javascript import execute_javascript_program
from lib.tools.program_contract import ProgramExecutionError


PROPERTIES = search_tools_schema()['function']['parameters']['properties']

pytestmark = pytest.mark.unit


def run_search(arguments, search):
    return execute_javascript_program(
        'return catalog.search(' + ','.join(json.dumps(arg) for arg in arguments)
        + ');', search=search, call=lambda *args: None,
        call_many=lambda *args: [])


@pytest.mark.parametrize('object_form', [False, True])
@pytest.mark.parametrize('arguments', [
    ['read files'],
    ['read files', 'project'],
    ['read files', 'project', 1],
    ['read files', 'project', 20, 'next-page'],
    ['  read files  ', '', 8, ''],
    ['read files', 'project', 8.0],
    ['查找' * 256, 'n' * 128, 20, 'c' * 128],
])
def test_valid_search_forms_preserve_values_and_defaults(object_form, arguments):
    received = []
    result = {'items': [{'name': 'read_files'}], 'next_cursor': 'next-page'}

    def search(query, namespace='', limit=8, cursor=''):
        received.append((query, namespace, limit, cursor))
        return result

    inputs = ([dict(zip(('query', 'namespace', 'limit', 'cursor'), arguments))]
              if object_form else arguments)
    value, stats = run_search(inputs, search)
    expected = ['read files', '', 8, '']
    expected[:len(arguments)] = arguments
    assert received == [tuple(expected)]
    # JavaScript has one Number type; integral 8.0 becomes the integer value 8
    # before the host validates the shared JSON-schema integer constraint.
    assert type(received[0][2]) is int
    assert value == result
    assert stats['tool_calls'] == 0


def test_object_optional_fields_do_not_shift_positional_slots():
    received = []
    run_search([{'query': 'files', 'cursor': 'page', 'limit': 3}],
               lambda *args: received.append(args))
    assert received == [('files', '', 3, 'page')]


@pytest.mark.parametrize('arguments', [
    [], [{}], [{'namespace': 'project'}],
    [{'query': 'files', 'extra': 1}],
    [{'query': 'files'}, 'project'],
    ['files', '', 8, '', 'extra'],
])
def test_invalid_search_shapes_never_invoke_callback(arguments):
    assert_rejected(arguments)


def assert_rejected(arguments):
    received = []
    with pytest.raises(ProgramExecutionError) as error:
        run_search(arguments, lambda *args: received.append(args))
    assert received == []
    detail = error.value.as_dict()
    assert detail['code'] == 'invalid_search_arguments'
    assert 'catalog.search' in detail['message']
    assert 'Retry catalog.search({query:' in detail['retry_hint']
    assert 'catalog.search(query, namespace, limit, cursor)' in detail['retry_hint']


@pytest.mark.parametrize('object_form', [False, True])
@pytest.mark.parametrize('name,value', [
    ('query', ''), ('query', ' \t\n'), ('query', None), ('query', 123),
    ('query', True), ('query', []), ('query', {}),
    ('query', 'q' * (PROPERTIES['query']['maxLength'] + 1)),
    ('namespace', None), ('namespace', False), ('namespace', 1),
    ('namespace', []), ('namespace', {}),
    ('namespace', 'n' * (PROPERTIES['namespace']['maxLength'] + 1)),
    ('cursor', None), ('cursor', True), ('cursor', 1),
    ('cursor', []), ('cursor', {}),
    ('cursor', 'c' * (PROPERTIES['cursor']['maxLength'] + 1)),
    ('limit', None), ('limit', True), ('limit', False), ('limit', '8'),
    ('limit', 1.5), ('limit', []), ('limit', {}),
    ('limit', PROPERTIES['limit']['minimum'] - 1),
    ('limit', PROPERTIES['limit']['maximum'] + 1), ('limit', -1),
])
def test_invalid_search_fields_never_invoke_callback(object_form, name, value):
    options = {'query': 'files', 'namespace': '', 'limit': 8, 'cursor': ''}
    options[name] = value
    assert_rejected([options] if object_form else list(options.values()))


def test_legacy_positional_callback_arity_is_preserved():
    received = []
    run_search(['files'], lambda query: received.append(query))
    assert received == ['files']
