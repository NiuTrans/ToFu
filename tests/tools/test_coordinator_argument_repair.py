"""Coordinator input repair preserves exact revisions and delegated limits."""
from copy import deepcopy
import json

import pytest

from lib.tool_input_repair import ingest_tool_call, validate_then_repair
from lib.tools.contracts import compile_execution_contract_documents
from lib.tools.coordinator import COORDINATOR_TOOLS

pytestmark = pytest.mark.unit


@pytest.fixture
def documents():
    return compile_execution_contract_documents(COORDINATOR_TOOLS)


def ingest(name, arguments, documents):
    return ingest_tool_call(
        {'id': 'call-coordinator', 'function': {
            'name': name, 'arguments': json.dumps(arguments)}},
        known_tools=set(documents), contract_documents_by_name=documents,
        emit_audit=False)


@pytest.mark.parametrize(('name', 'arguments', 'expected', 'repair'), [
    ('dispatch_project_assignment',
     {'assignment_id': 'worker-a', 'expected_revision': '7'},
     {'assignment_id': 'worker-a', 'expected_revision': 7},
     ('expected_revision', 'stringified_primitive')),
    ('get_project_workers', {'limit': '3'}, {'limit': 3},
     ('limit', 'stringified_primitive')),
    ('await_project_workers', {'assignment_ids': '["worker-a","worker-b"]'},
     {'assignment_ids': ['worker-a', 'worker-b']},
     ('assignment_ids', 'stringified_json')),
    ('delegate_project_work',
     {'title': 'Check evidence', 'brief': 'Verify the source and return evidence.',
      'idempotency_key': 'delegation-a', 'max_tokens': '1000'},
     {'title': 'Check evidence', 'brief': 'Verify the source and return evidence.',
      'idempotency_key': 'delegation-a', 'max_tokens': 1000},
     ('max_tokens', 'stringified_primitive')),
])
def test_coordinator_repairs_types_before_authoritative_validation(
        name, arguments, expected, repair, documents):
    original = deepcopy(arguments)
    repaired, log = validate_then_repair(name, arguments)
    assert repaired == expected
    assert repair in log
    call = ingest(name, arguments, documents)
    assert call.parse_error is None and call.contract_error is None
    assert all(call.fn_args[key] == value for key, value in expected.items())
    assert repair in call.repair_log
    assert arguments == original


def test_missing_coordinator_argument_has_actionable_schema_hint(documents):
    call = ingest('dispatch_project_assignment', {'assignment_id': 'worker-a'}, documents)
    assert call.contract_error['path'] == '$'
    assert 'expected_revision*(integer)' in call.parse_error
    assert 'NOT executed' in call.parse_error
    assert call.fn_args == {'assignment_id': 'worker-a'}


_DELEGATE = {'title': 'Check evidence', 'brief': 'Verify the source and return evidence.',
             'idempotency_key': 'delegation-a'}
_PIN = {'assignmentId': 'worker-a', 'grantRevision': 7, 'resultRevision': 3}
_AUTHORITY_CASES = [
    ('dispatch_project_assignment',
     {'assignment_id': 'worker-a', 'expected_revision': value}, '$.expected_revision')
    for value in (0, -1)
] + [
    ('review_project_work',
     {'assignment_id': 'worker-a', 'result_revision': value, 'decision': 'accept',
      'note': 'Reviewed the retained evidence.'},
     '$.result_revision')
    for value in (0, -1)
] + [
    ('delegate_project_work', {**_DELEGATE, field: value}, f'$.{field}')
    for field, maximum in (('max_tokens', 2147483647),
                           ('max_model_requests', 64), ('max_tool_calls', 512))
    for value in (0, -1, maximum + 1)
] + [
    ('delegate_project_work',
     {**_DELEGATE, container: ([{**_PIN, field: 0}] if container == 'dependencies'
                              else {**_PIN, field: 0})},
     f'$.{container}' + ('[0]' if container == 'dependencies' else '') + f'.{field}')
    for container in ('dependencies', 'rework_of')
    for field in ('grantRevision', 'resultRevision')
]


@pytest.mark.parametrize(('name', 'arguments', 'path'), _AUTHORITY_CASES)
def test_invalid_revision_or_allowance_is_rejected_without_semantic_repair(
        name, arguments, path, documents):
    original = deepcopy(arguments)
    repaired, log = validate_then_repair(name, arguments)
    assert repaired == original
    assert not any(pattern.startswith('clamp_') for _, pattern in log)
    call = ingest(name, arguments, documents)
    assert call.contract_error is not None
    assert call.contract_error['path'] == path
    assert 'NOT executed' in call.parse_error
    assert call.fn_args == original
    assert arguments == original


def test_coordinator_page_size_still_repairs_to_its_declared_limit(documents):
    call = ingest('get_project_workers', {'limit': 1000}, documents)
    assert call.parse_error is None
    assert call.fn_args['limit'] == 32
    assert ('$.limit', 'clamp_maximum') in call.repair_log


@pytest.mark.parametrize('revision', [0, -1, '0'])
def test_invalid_revision_reaches_model_without_dispatch_or_approval(
        revision, documents, monkeypatch):
    from lib.tasks_pkg.tool_dispatch import _parse, _pipeline
    from tests.tools.test_tool_settle_all_lanes import _mk_task

    task = _mk_task(config={}, toolRounds=[],
                    _executable_tool_catalog=COORDINATOR_TOOLS,
                    _toolContractDocumentsByName=documents)
    assistant = {'role': 'assistant', 'tool_calls': [{
        'id': 'bad-revision', 'type': 'function',
        'function': {'name': 'dispatch_project_assignment', 'arguments': json.dumps({
            'assignment_id': 'worker-a', 'expected_revision': revision})}}]}
    parsed, _ = _parse.parse_tool_calls(assistant, task, 0, 0, False)

    def forbidden(*args, **kwargs):
        pytest.fail('Invalid revision must be rejected before any effect or approval')

    monkeypatch.setattr(_pipeline, '_execute_tool_one', forbidden)
    monkeypatch.setattr(_pipeline, '_approval_stops_tool_call', forbidden)
    messages = []
    _pipeline.execute_tool_pipeline(
        task, parsed, task['config'], None, False, COORDINATOR_TOOLS,
        messages, [], 0, 'test-model')
    assert len(messages) == 1
    assert messages[0]['tool_call_id'] == 'bad-revision'
    assert 'expected_revision' in messages[0]['content']
    assert 'NOT executed' in messages[0]['content']
    assert task['toolRounds'][0]['status'] == 'rejected'


def test_stringified_revision_is_repaired_and_dispatched_once(documents, monkeypatch):
    from lib.tasks_pkg.executor._finalize import _finalize_tool_round
    from lib.tasks_pkg.tool_dispatch import _parse, _pipeline
    from tests.tools.test_tool_settle_all_lanes import _mk_task

    task = _mk_task(config={}, toolRounds=[],
                    _executable_tool_catalog=COORDINATOR_TOOLS,
                    _toolContractDocumentsByName=documents)
    assistant = {'role': 'assistant', 'tool_calls': [{
        'id': 'valid-revision', 'type': 'function',
        'function': {'name': 'dispatch_project_assignment', 'arguments': json.dumps({
            'assignment_id': 'worker-a', 'expected_revision': '7'})}}]}
    parsed, _ = _parse.parse_tool_calls(assistant, task, 0, 0, False)
    effects = []

    def execute(task, tc, name, call_id, arguments, round_num, entry,
                cfg, project_path, project_enabled, all_tools=None):
        effects.append((name, deepcopy(arguments)))
        _finalize_tool_round(task, round_num, entry, [])
        return call_id, '{"dispatch":"accepted"}', False

    monkeypatch.setattr(_pipeline, '_execute_tool_one', execute)
    messages = []
    _pipeline.execute_tool_pipeline(
        task, parsed, task['config'], None, False, COORDINATOR_TOOLS,
        messages, [], 0, 'test-model')
    assert effects == [('dispatch_project_assignment', {
        'assignment_id': 'worker-a', 'expected_revision': 7})]
    assert len(messages) == 1
    assert messages[0]['tool_call_id'] == 'valid-revision'
    assert 'accepted' in messages[0]['content']
    assert task['toolRounds'][0]['status'] == 'done'
