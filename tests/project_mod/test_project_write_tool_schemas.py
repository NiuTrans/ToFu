"""Resident whole-file and anchored-edit model contracts."""

from __future__ import annotations

import json
import re

from jsonschema import Draft7Validator
import pytest

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
            'system paths', '$HOME itself', 'exactly one source',
            'empty string creates an empty file', 'content_ref',
            'previous tool result'):
        assert phrase in desc, phrase


def test_write_schema_accepts_empty_content_or_ref_but_not_missing_or_both():
    params = PROJECT_TOOL_WRITE_FILE['function']['parameters']
    props = params['properties']
    Draft7Validator.check_schema(params)
    validator = Draft7Validator(params)
    base = {'description': 'Create marker', 'path': 'tmp/.keep'}

    assert validator.is_valid({**base, 'content': ''})
    assert validator.is_valid({
        **base, 'content_ref': {'tool_round': 3, 'start': 0, 'end': 12}})
    assert not validator.is_valid(base)
    assert not validator.is_valid({
        **base, 'content': 'x', 'content_ref': {'tool_round': 3}})
    assert not validator.is_valid({**base, 'content': 'x', 'extra': True})
    assert sanitize_wire_tools([PROJECT_TOOL_WRITE_FILE]) == [
        PROJECT_TOOL_WRITE_FILE]

    compact = fit_tool_schema_budget(
        [PROJECT_TOOL_WRITE_FILE], budget_tokens=200, model='kimi-k3')
    assert compact and compact[0]['function']['name'] == 'write_file'
    assert tool_schema_tokens(compact, model='kimi-k3') <= 200


def test_edit_schema_keeps_anchor_efficiency_and_partial_failure_contracts():
    desc = PROJECT_TOOL_EDIT_FILE['function']['description']
    for phrase in (
            'ordered anchored edits', 'earlier round', 'Prefer replace',
            'complete desired replacement', 'retained anchor', 'all line breaks',
            'insert_after/insert_before', 'content is only new text',
            'match exactly once',
            'replace_all permits multiple matches only for replace',
            'Later edits see earlier changes',
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
    assert 'Prefer replace' in item['summary']
    projected = fit_tool_schema_budget([resident], budget_tokens=1000, model='kimi-k3')
    assert projected and projected[0]['function']['description'] == resident['function']['description']
    assert tool_schema_tokens(projected, model='kimi-k3') <= 1000


def test_edit_description_examples_execute_the_promised_order(tmp_path):
    from lib.project_mod.write_tools import tool_edit_file

    function = PROJECT_TOOL_EDIT_FILE['function']
    examples = re.findall(r'`(\{"operation"[^`]+\})`', function['description'])
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
        'path': 'src/app.py', 'operation': 'insert_after',
        'anchor': 'def main():', 'content': '\n    run()',
    }
    assert validator.is_valid({'description': 'Wire main', 'edits': [edit]})
    assert not validator.is_valid({'description': '', 'edits': [edit]})
    assert not validator.is_valid({'description': 'No work', 'edits': []})
    assert validator.is_valid({
        'description': 'Complete requested batch', 'edits': [edit] * 31})
    assert not validator.is_valid({
        'description': 'No anchor', 'edits': [{**edit, 'anchor': ''}]})
    assert sanitize_wire_tools([PROJECT_TOOL_EDIT_FILE]) == [
        PROJECT_TOOL_EDIT_FILE]


def test_resident_pair_stays_within_schema_budget():
    # Includes content_ref/oneOf, explicit edit examples and path guidance.
    # The old 410/395 pins predated these declarations; they failed even
    # before the insert fix. Keep a measured ceiling for the current schema.
    assert tool_schema_tokens([PROJECT_TOOL_WRITE_FILE], model='kimi-k3') <= 700
    assert tool_schema_tokens([PROJECT_TOOL_EDIT_FILE], model='kimi-k3') <= 1000
    assert tool_schema_tokens(
        [PROJECT_TOOL_WRITE_FILE, PROJECT_TOOL_EDIT_FILE],
        model='kimi-k3') <= 1700
