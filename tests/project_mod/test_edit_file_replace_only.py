"""Executable specification: replace-only ``edit_file`` and content-required
``write_file`` (owner decision 2026-11-06, "Replace-only edit_file" in
docs/modules/tools_execution.md).

TB2.1 round 1 refused 17 of 210 edit_file calls before they ran: 13 omitted
``operation``, 3 sent ``operation: "replace_all"``, 1 had no anchor. Editbench
measured replace-only at 98.7% final success against 96.7% insert-first
(GLM-5.3: 99.4% vs 93.9%). The field is gone; legacy values are repaired or
refused with the replace that expresses them.
"""

from __future__ import annotations

import json

import pytest

from lib.tool_input_repair._ingest import ingest_tool_call
from lib.tools.contracts import (
    ToolContractError,
    compile_execution_contract_documents,
    validate_tool_arguments_from_documents,
)
from lib.tools.project import PROJECT_TOOL_EDIT_FILE, PROJECT_TOOL_WRITE_FILE

pytestmark = pytest.mark.unit

CONTRACTS = compile_execution_contract_documents(
    [PROJECT_TOOL_EDIT_FILE, PROJECT_TOOL_WRITE_FILE])


def _ingest(name, args, known=('edit_file', 'write_file')):
    return ingest_tool_call(
        {'id': 'call-1', 'function': {'name': name, 'arguments': json.dumps(args)}},
        known_tools=set(known), contract_documents_by_name=CONTRACTS,
        emit_audit=False)


def _edit(tmp_path, call):
    from lib.project_mod.write_tools import tool_edit_file
    args = call.fn_args
    return tool_edit_file(str(tmp_path), args['edits'], batch_path=args.get('path'))


def test_schema_has_no_operation_and_requires_anchor_and_content():
    item = PROJECT_TOOL_EDIT_FILE['function']['parameters']['properties']['edits']['items']
    assert 'operation' not in item['properties']
    assert item['required'] == ['anchor', 'content']
    text = json.dumps(PROJECT_TOOL_EDIT_FILE)
    for retired in ('insert_after', 'insert_before', '"operation"'):
        assert retired not in text, retired


def test_edit_without_operation_executes(tmp_path):
    (tmp_path / 'a.txt').write_text('A\nC\n', encoding='utf-8')
    call = _ingest('edit_file', {'path': 'a.txt', 'edits': [
        {'anchor': 'A', 'content': 'A\nB'}]})
    assert call.parse_error is None, call.parse_error
    result = _edit(tmp_path, call)
    assert result.startswith('Applied 1/1 edits'), result
    assert (tmp_path / 'a.txt').read_text(encoding='utf-8') == 'A\nB\nC\n'


def test_legacy_replace_operation_is_repaired_not_refused(tmp_path):
    (tmp_path / 'a.txt').write_text('x = 1\n', encoding='utf-8')
    call = _ingest('edit_file', {'path': 'a.txt', 'edits': [
        {'operation': 'replace', 'anchor': 'x = 1', 'content': 'x = 2'}]})
    assert call.parse_error is None, call.parse_error
    assert 'operation' not in call.fn_args['edits'][0]
    assert call.repair_log
    assert _edit(tmp_path, call).startswith('Applied 1/1 edits')
    assert (tmp_path / 'a.txt').read_text(encoding='utf-8') == 'x = 2\n'


@pytest.mark.parametrize('operation, expected', [
    ('insert_after', '{"anchor":"A","content":"A\\nNEW"}'),
    ('insert_before', '{"anchor":"A","content":"NEW\\nA"}'),
])
def test_insert_operation_is_refused_with_the_equivalent_replace(operation, expected):
    call = _ingest('edit_file', {'path': 'a.txt', 'edits': [
        {'anchor': 'keep', 'content': 'x', 'operation': 'replace'},
        {'operation': operation, 'anchor': 'A', 'content': 'NEW'}]})
    assert call.contract_error and call.contract_error['code'] == 'invalid_argument_value'
    assert 'was NOT executed' in call.parse_error
    assert 'edits[1]' in call.parse_error and 'edits[0]' not in call.parse_error
    assert 'only replaces' in call.parse_error
    assert expected in call.parse_error


def test_replace_all_operation_points_at_the_existing_flag():
    call = _ingest('edit_file', {'path': 'a.txt', 'edits': [
        {'operation': 'replace_all', 'anchor': 'A', 'content': 'B'}]})
    assert call.contract_error is not None
    assert '"replace_all": true' in call.parse_error


def test_replace_all_flag_replaces_every_occurrence(tmp_path):
    (tmp_path / 'a.txt').write_text('A A A\n', encoding='utf-8')
    call = _ingest('edit_file', {'path': 'a.txt', 'edits': [
        {'anchor': 'A', 'content': 'B', 'replace_all': True}]})
    assert call.parse_error is None
    assert 'occurrences' in _edit(tmp_path, call)
    assert (tmp_path / 'a.txt').read_text(encoding='utf-8') == 'B B B\n'


@pytest.mark.parametrize('name, args', [
    ('insert_content', {'path': 'a.txt', 'anchor': 'A', 'content': 'B'}),
    ('insert_contents', {'edits': [
        {'path': 'a.txt', 'anchor': 'A', 'content': 'B', 'position': 'before'}]}),
])
def test_retired_insert_tools_route_to_the_instructive_refusal(name, args):
    call = _ingest(name, args)
    assert call.fn_name == 'edit_file'
    assert call.contract_error is not None
    assert 'only replaces' in call.parse_error
    assert '`anchor`' in call.parse_error


def test_executor_refuses_an_insert_that_bypassed_repair(tmp_path):
    from lib.project_mod.write_tools import tool_edit_file
    (tmp_path / 'a.txt').write_text('A\n', encoding='utf-8')
    result = tool_edit_file(str(tmp_path), [
        {'path': 'a.txt', 'operation': 'insert_after', 'anchor': 'A', 'content': 'B'}])
    assert '[invalid-edit]' in result and 'only replaces' in result
    assert (tmp_path / 'a.txt').read_text(encoding='utf-8') == 'A\n'


def test_write_file_without_content_is_refused_clearly():
    for args in ({'path': 'a.txt'}, {'path': 'a.txt', 'content_ref': {'tool_round': 1}}):
        call = _ingest('write_file', args)
        assert call.parse_error and 'was NOT executed' in call.parse_error
        assert 'content' in call.parse_error
    with pytest.raises(ToolContractError) as refusal:
        validate_tool_arguments_from_documents(CONTRACTS, 'write_file', {'path': 'a.txt'})
    assert refusal.value.code == 'missing_required_arguments'
    params = PROJECT_TOOL_WRITE_FILE['function']['parameters']
    assert 'content_ref' not in params['properties']
    assert params['required'] == ['path', 'content']


def test_write_file_append_is_unchanged(tmp_path):
    from lib.project_mod.write_tools import tool_write_file
    assert _ingest('write_file', {'path': 'log.txt', 'content': 'a\n', 'append': True}).parse_error is None
    created = tool_write_file(str(tmp_path), 'log.txt', 'a\n', append=True)
    assert created['ok'] and created['created']
    appended = tool_write_file(str(tmp_path), 'log.txt', 'b\n', append=True)
    assert appended['ok'] and appended['appended']
    assert (tmp_path / 'log.txt').read_text(encoding='utf-8') == 'a\nb\n'
    props = PROJECT_TOOL_WRITE_FILE['function']['parameters']['properties']
    assert props['append']['type'] == 'boolean'
