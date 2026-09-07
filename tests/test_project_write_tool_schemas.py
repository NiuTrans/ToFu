"""Resident whole-file and anchored-edit model contracts."""

from __future__ import annotations

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
            'Create or replace one whole file', 'new files/major rewrites',
            'edit_file for targeted changes', 'Read an existing file first',
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
        [PROJECT_TOOL_WRITE_FILE], budget_tokens=120, model='kimi-k3')
    assert compact and compact[0]['function']['name'] == 'write_file'
    assert tool_schema_tokens(compact, model='kimi-k3') <= 120


def test_edit_schema_keeps_anchor_efficiency_and_partial_failure_contracts():
    desc = PROJECT_TOOL_EDIT_FILE['function']['description']
    for phrase in (
            '1–30 ordered anchored edits', 'earlier round',
            'insert_after/insert_before', 'content is only new text',
            'do not repeat', 'add B between A/C', 'Safe insertion echoes',
            'pure echoes', 'wraps its unchanged anchor',
            'replace only to change/remove', 'match exactly once',
            'replace_all permits multiple matches only for replace',
            'Later edits see earlier changes',
            'neither rolls back nor stops the others'):
        assert phrase in desc, phrase


def test_edit_schema_rejects_empty_and_oversized_batches_before_execution():
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
    assert not validator.is_valid({
        'description': 'Too much', 'edits': [edit] * 31})
    assert not validator.is_valid({
        'description': 'No anchor', 'edits': [{**edit, 'anchor': ''}]})
    assert sanitize_wire_tools([PROJECT_TOOL_EDIT_FILE]) == [
        PROJECT_TOOL_EDIT_FILE]


def test_resident_pair_stays_within_schema_budget():
    # 2026-08-31 deliberate bump (+53/+37/+90): the out-of-workspace write
    # confirmation gate added the ``allow_outside_workspace`` parameter to
    # both resident schemas (name + type + minimal contract prose). Budgets
    # retain ~7 tokens of headroom over the measured 353/377/727.
    assert tool_schema_tokens([PROJECT_TOOL_WRITE_FILE], model='kimi-k3') <= 360
    assert tool_schema_tokens([PROJECT_TOOL_EDIT_FILE], model='kimi-k3') <= 385
    assert tool_schema_tokens(
        [PROJECT_TOOL_WRITE_FILE, PROJECT_TOOL_EDIT_FILE],
        model='kimi-k3') <= 735
