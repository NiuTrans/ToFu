"""Optional edit captions must not control anchored-write admission."""

from __future__ import annotations

import json

from jsonschema import Draft7Validator
import pytest

from lib.project_mod.tools import execute_tool, project_tool_display
from lib.tool_input_repair import ingest_tool_call
from lib.tools.contracts import adapt_legacy_tool_contract
from lib.tools.project import PROJECT_TOOL_EDIT_FILE


pytestmark = pytest.mark.unit


@pytest.mark.parametrize('target_form', ['batch', 'per-edit'])
@pytest.mark.parametrize('caption,schema_valid,effective_caption', [
    pytest.param(None, True, '', id='omitted'),
    pytest.param('Update value', True, 'Update value', id='supplied'),
    pytest.param('', True, '', id='empty'),
    pytest.param(42, False, None, id='non-string'),
    pytest.param('x' * 121, False, 'x' * 120, id='overlong-trimmed'),
])
def test_optional_caption_preserves_contract_write_and_display(
        tmp_path, target_form, caption, schema_valid, effective_caption):
    target = tmp_path / 'sample.txt'
    target.write_text('value = 1\n', encoding='utf-8')
    edit = {'operation': 'replace', 'anchor': 'value = 1',
            'content': 'value = 2'}
    args = {'edits': [edit]}
    if target_form == 'batch':
        args['path'] = target.name
    else:
        edit['path'] = target.name
    if caption is not None:
        args['description'] = caption

    contract = adapt_legacy_tool_contract(PROJECT_TOOL_EDIT_FILE)
    ingested = ingest_tool_call(
        {'id': 'optional-caption', 'function': {
            'name': 'edit_file', 'arguments': json.dumps(args)}},
        emit_audit=False,
        contract_documents_by_name={'edit_file': contract.search_document()},
    )

    assert Draft7Validator(contract.parameters).is_valid(args) is schema_valid
    assert (ingested.parse_error is None) is (effective_caption is not None)
    if effective_caption is None:
        assert ingested.contract_error is not None
        assert ingested.contract_error['path'] == '$.description'
        assert target.read_text(encoding='utf-8') == 'value = 1\n'
        return

    if caption is None:
        assert 'description' not in ingested.fn_args
    assert ingested.fn_args.get('description', '') == effective_caption
    output = execute_tool('edit_file', ingested.fn_args, str(tmp_path))
    assert output.startswith('Applied 1/1 edits'), output
    assert target.read_text(encoding='utf-8') == 'value = 2\n'
    expected = 'Edit sample.txt'
    if effective_caption:
        expected += f' — {effective_caption}'
    assert project_tool_display('edit_file', ingested.fn_args) == expected
