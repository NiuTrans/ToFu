"""Executable specification: where an edit batch's file comes from.

Live evidence (logs/app.log.2026-10-20 line 156, conv mvg3dqkl7dzinb,
model deepseek-v4.1-flash, round 12): an `edit_file` batch whose later edits
omitted `path` was rejected with

    [invalid_argument_length] Invalid length at $.edits[0].path (got 0 chars...)

after the repair pass had already logged a `structural_transform`. The message
named a length constraint on one index, so the recovery round learned neither
which indices were blank nor how the shape was supposed to look.

`edit_file` now states the shape every other harness teaches: name the file
once for the batch, then list the hunks (an item `path` only overrides the
batch file). These tests pin the accepted forms, the repair fallbacks, and the
shapes that must stay fail-closed.
"""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.unit

from lib.tool_input_repair._ingest import ingest_tool_call
from lib.tool_input_repair._repair import edit_batch_path_gap
from lib.tools.contracts import compile_execution_contract_documents
from lib.tools.project import PROJECT_TOOL_EDIT_FILE

# The pipeline validates against v2 execution documents, not the raw JSON
# schema, so compile the real tool definition instead of hand-building one.
CONTRACTS = compile_execution_contract_documents([PROJECT_TOOL_EDIT_FILE])


def _gap(fn_args):
    return edit_batch_path_gap('edit_file', fn_args)


def test_blank_path_names_the_index_and_where_a_file_may_be_named():
    hint = _gap({'edits': [
        {'path': 'a.py', 'operation': 'replace', 'anchor': 'A', 'content': 'B'},
        {'path': '', 'operation': 'replace', 'anchor': 'C', 'content': 'D'},
    ]})
    assert 'edits[1]' in hint
    assert '`path`' in hint
    assert 'top-level `path`' in hint


def test_missing_key_is_reported_the_same_as_blank():
    hint = _gap({'edits': [
        {'path': 'a.py', 'operation': 'replace', 'anchor': 'A', 'content': 'B'},
        {'operation': 'replace', 'anchor': 'C', 'content': 'D'},
    ]})
    assert 'edits[1]' in hint


def test_single_file_batch_points_at_the_declared_file():
    hint = _gap({'edits': [
        {'path': 'SKILL.md', 'operation': 'replace', 'anchor': 'A', 'content': 'B'},
        {'path': '', 'operation': 'replace', 'anchor': 'C', 'content': 'D'},
    ]})
    assert "'SKILL.md'" in hint


def test_multi_file_batch_calls_the_gap_ambiguous_instead_of_guessing():
    hint = _gap({'edits': [
        {'path': 'a.py', 'operation': 'replace', 'anchor': 'A', 'content': 'B'},
        {'path': '', 'operation': 'replace', 'anchor': 'C', 'content': 'D'},
        {'path': 'b.py', 'operation': 'replace', 'anchor': 'E', 'content': 'F'},
    ]})
    assert 'ambiguous' in hint


def test_no_gap_returns_empty_so_clean_batches_are_untouched():
    assert _gap({'edits': [
        {'path': 'a.py', 'operation': 'replace', 'anchor': 'A', 'content': 'B'},
        {'path': 'a.py', 'operation': 'replace', 'anchor': 'C', 'content': 'D'},
    ]}) == ''
    assert _gap({'edits': []}) == ''
    assert edit_batch_path_gap('write_file', {'path': ''}) == ''


def _batch(*edits):
    return {'id': 'call-e', 'function': {
        'name': 'edit_file',
        'arguments': json.dumps({
            'description': 'edit batch', 'atomic': False, 'edits': list(edits)})}}


def _ingest(payload):
    return ingest_tool_call(
        {'id': 'call-e',
         'function': {'name': 'edit_file', 'arguments': json.dumps(payload)}},
        known_tools={'edit_file'},
        contract_documents_by_name=CONTRACTS,
        emit_audit=False)


def test_rejection_message_carries_the_diagnosis_not_only_the_length():
    """The model-facing error must name the cause, or it retries blind."""
    call = ingest_tool_call(
        _batch(
            {'path': 'a.py', 'operation': 'replace', 'anchor': 'A', 'content': 'B'},
            {'path': '', 'operation': 'replace', 'anchor': 'C', 'content': 'D'},
            {'path': 'b.py', 'operation': 'replace', 'anchor': 'E', 'content': 'F'}),
        known_tools={'edit_file'},
        contract_documents_by_name=CONTRACTS,
        emit_audit=False)
    assert call.contract_error is not None, 'blank path must stay fail-closed'
    # A blank item path is a length violation; a batch naming no file at all
    # is refused by the ingest coverage check. Both stay fail-closed.
    assert call.contract_error['code'] in {
        'invalid_argument_length', 'missing_required_arguments'}
    message = call.parse_error or ''
    assert 'edits[1]' in message
    # A batch that already names two files must not be guessed at: the message
    # says the blank item is ambiguous and which files were declared.
    assert 'spans several files' in message
    assert "'a.py'" in message and "'b.py'" in message


def test_a_batch_may_name_its_file_once_instead_of_on_every_edit():
    """The preferred shape: one file per batch, stated once.

    Every other harness teaches "name the file, then list the hunks". The
    schema now states that shape directly, so the model does not have to guess
    that `path` is a per-item field it must repeat.
    """
    call = _ingest({
        'description': 'one file, two hunks', 'path': 'SKILL.md',
        'edits': [
            {'operation': 'replace', 'anchor': 'A', 'content': 'B'},
            {'operation': 'insert_after', 'anchor': 'C', 'content': 'D'}]})
    assert call.contract_error is None


def test_batch_path_is_the_file_for_edits_that_omit_their_own():
    call = _ingest({
        'description': 'one file, two hunks', 'path': 'SKILL.md',
        'edits': [
            {'operation': 'replace', 'anchor': 'A', 'content': 'B'},
            {'operation': 'insert_after', 'anchor': 'C', 'content': 'D'}]})
    assert call.contract_error is None
    assert [edit['path'] for edit in call.fn_args['edits']] == ['SKILL.md'] * 2


def test_per_edit_path_overrides_the_batch_path_for_a_multi_file_batch():
    call = _ingest({
        'description': 'two files', 'path': 'a.py',
        'edits': [
            {'operation': 'replace', 'anchor': 'A', 'content': 'B'},
            {'path': 'b.py', 'operation': 'replace', 'anchor': 'C', 'content': 'D'}]})
    assert call.contract_error is None
    assert [edit['path'] for edit in call.fn_args['edits']] == ['a.py', 'b.py']


def test_a_batch_naming_no_file_anywhere_stays_rejected():
    call = _ingest({
        'description': 'nameless',
        'edits': [{'operation': 'replace', 'anchor': 'A', 'content': 'B'}]})
    assert call.contract_error is not None
    assert 'no edit or batch `path` names a file' in (call.parse_error or '')


def test_repair_still_fills_a_single_file_batch_so_it_does_not_reach_contract():
    """The repair path is the preferred outcome: it must not regress."""
    call = ingest_tool_call(
        _batch(
            {'path': 'SKILL.md', 'operation': 'replace', 'anchor': 'A', 'content': 'B'},
            {'path': '', 'operation': 'replace', 'anchor': 'C', 'content': 'D'}),
        known_tools={'edit_file'},
        contract_documents_by_name=CONTRACTS,
        emit_audit=False)
    assert call.contract_error is None
    assert all(edit['path'] == 'SKILL.md' for edit in call.fn_args['edits'])
