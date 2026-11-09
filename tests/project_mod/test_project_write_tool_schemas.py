"""Resident whole-file and anchored-edit model contracts."""

from __future__ import annotations

import json
import re

from jsonschema import Draft7Validator
import pytest

from lib.tools.contracts import (
    ARGUMENT_ALTERNATIVES,
    ToolContractError,
    compile_execution_contract_documents,
    validate_tool_arguments_from_documents,
)
from lib.tools.gateway import (
    fit_tool_schema_budget,
    sanitize_wire_tools,
    tool_schema_tokens,
)
from lib.tools.project import PROJECT_TOOL_EDIT_FILE, PROJECT_TOOL_WRITE_FILE


pytestmark = pytest.mark.unit


def test_write_schema_keeps_rewrite_path_and_reuse_contracts():
    function = PROJECT_TOOL_WRITE_FILE['function']
    desc = function['description']
    for phrase in (
            'Create or replace one whole file, or append to its end',
            'new files/major rewrites',
            'use edit_file for mid-file changes',
            'append=true', 'append to the end', 'no read or anchor needed',
            'Read an existing file first',
            'complete replacement', 'omitted lines are deleted',
            'Relative paths', 'outside registered roots',
            'allow_outside_workspace=true', 'registers as a new root',
            'system paths', '$HOME itself', 'content is required',
            'empty string creates an empty file'):
        assert phrase in desc, phrase


def test_write_schema_requires_content_and_accepts_empty_content():
    params = PROJECT_TOOL_WRITE_FILE['function']['parameters']
    Draft7Validator.check_schema(params)
    validator = Draft7Validator(params)
    base = {'description': 'Create marker', 'path': 'tmp/.keep'}

    # The caption is optional; the destination and its text are mandatory.
    assert params['required'] == ['path', 'content']
    assert validator.is_valid({'path': 'tmp/.keep', 'content': ''})
    assert validator.is_valid({**base, 'content': ''})
    assert not validator.is_valid(base)
    assert not validator.is_valid({**base, 'content_ref': {'tool_round': 3}})
    assert 'write_file' not in ARGUMENT_ALTERNATIVES
    documents = compile_execution_contract_documents([PROJECT_TOOL_WRITE_FILE])
    validate_tool_arguments_from_documents(documents, 'write_file', {**base, 'content': ''})
    with pytest.raises(ToolContractError) as rejection:
        validate_tool_arguments_from_documents(documents, 'write_file', base)
    assert rejection.value.code == 'missing_required_arguments'
    assert 'content' in str(rejection.value)
    assert not validator.is_valid({**base, 'content': 'x', 'extra': True})
    assert sanitize_wire_tools([PROJECT_TOOL_WRITE_FILE]) == [
        PROJECT_TOOL_WRITE_FILE]

    budget = tool_schema_tokens([PROJECT_TOOL_WRITE_FILE], model='kimi-k3') - 1
    compact = fit_tool_schema_budget(
        [PROJECT_TOOL_WRITE_FILE], budget_tokens=budget, model='kimi-k3')
    assert compact and compact[0]['function']['name'] == 'write_file'
    assert tool_schema_tokens(compact, model='kimi-k3') <= budget


def test_edit_schema_keeps_anchor_efficiency_and_partial_failure_contracts():
    desc = PROJECT_TOOL_EDIT_FILE['function']['description']
    for phrase in (
            'Replace text', 'earlier round', 'copied exactly',
            'empty content deletes', 'must match once',
            'replace_all replaces every match', 'keep neighbouring text',
            'write_file(append=true)', 'Later edits see earlier changes',
            'neither rolls back nor stops the others'):
        assert phrase in desc, phrase


def test_edit_contract_survives_resident_projection_search_and_budget():
    from lib.tools.gateway import search_executable_catalog
    from lib.tools.project import project_tools_for_runtime

    resident = next(tool for tool in project_tools_for_runtime()
                    if tool['function']['name'] == 'edit_file')
    assert resident == PROJECT_TOOL_EDIT_FILE
    found = search_executable_catalog([resident], 'edit_file', limit=1)
    item = found['items'][0]
    assert item['name'] == 'edit_file'
    assert item['arguments_schema'] == resident['function']['parameters']
    assert 'Replace text' in item['summary']
    projected = fit_tool_schema_budget(
        [resident], budget_tokens=tool_schema_tokens([resident], model='kimi-k3'),
        model='kimi-k3')
    assert projected and projected[0]['function']['description'] == resident['function']['description']


def test_edit_description_examples_execute_the_promised_order(tmp_path):
    from lib.project_mod.write_tools import tool_edit_file

    function = PROJECT_TOOL_EDIT_FILE['function']
    examples = re.findall(r'`(\{"anchor"[^`]+\})`', function['description'])
    assert len(examples) == 2
    for example, expected in zip(examples, ['A\nB\nC\n', 'B\nA\nC\n']):
        target = tmp_path / 'sample.txt'
        target.write_text('A\nC\n', encoding='utf-8')
        edit = {'path': target.name, **json.loads(example)}
        args = {'description': 'Insert B', 'edits': [edit]}
        Draft7Validator(function['parameters']).validate(args)
        result = tool_edit_file(str(tmp_path), args['edits'])
        assert result.startswith('Applied 1/1 edits'), result
        assert target.read_text(encoding='utf-8') == expected


def test_edit_schema_rejects_empty_edits_and_preserves_requested_batch_size():
    params = PROJECT_TOOL_EDIT_FILE['function']['parameters']
    Draft7Validator.check_schema(params)
    validator = Draft7Validator(params)
    edit = {
        'path': 'src/app.py',
        'anchor': 'def main():', 'content': 'def main():\n    run()',
    }
    assert validator.is_valid({'description': 'Wire main', 'edits': [edit]})
    assert validator.is_valid({'edits': [edit]})
    assert validator.is_valid({'description': '', 'edits': [edit]})
    assert not validator.is_valid({'description': 'No work', 'edits': []})
    assert validator.is_valid({
        'description': 'Complete requested batch', 'edits': [edit] * 31})
    assert not validator.is_valid({
        'description': 'No anchor', 'edits': [{**edit, 'anchor': ''}]})
    assert sanitize_wire_tools([PROJECT_TOOL_EDIT_FILE]) == [
        PROJECT_TOOL_EDIT_FILE]
