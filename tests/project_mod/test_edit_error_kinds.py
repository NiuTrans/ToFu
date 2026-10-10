"""Edit-failure category tags (errorKind).

Every edit-surface failure (edit_file / apply_diff(s) / insert_content(s))
carries a bracketed category tag in the model-visible line —
``[1] FAIL x.py [replace] [match-not-found]: …`` /
``Diff failed [match-not-found]: …`` — so the model can tell which repair
move applies without parsing prose. Contracts that must NOT change: the
``Write failed`` / ``Diff failed`` / ``Insert failed`` prefixes (read-gate
success probe) and the ``[N] FAIL`` batch marker (meta parser).
"""

import pytest

from lib.project_mod.write_tools import (
    tool_apply_diff,
    tool_apply_diffs,
    tool_edit_file,
    tool_insert_content,
    tool_insert_contents,
)


@pytest.fixture
def workspace(tmp_path):
    (tmp_path / 'a.txt').write_text('A\nA\nold\n', encoding='utf-8')
    return str(tmp_path)


# ── edit_file: one tag per failure class ──

@pytest.mark.unit
def test_edit_file_match_not_found_tag(workspace):
    result = tool_edit_file(workspace, [
        {'path': 'a.txt', 'operation': 'replace',
         'anchor': 'missing', 'content': 'x'},
    ])
    assert '[replace] [match-not-found]: Search text not found' in result


@pytest.mark.unit
def test_edit_file_ambiguous_match_tag(workspace):
    result = tool_edit_file(workspace, [
        {'path': 'a.txt', 'operation': 'replace',
         'anchor': 'A', 'content': 'x'},
    ])
    assert '[replace] [ambiguous-match]: Search text matches 2 locations' in result


@pytest.mark.unit
def test_edit_file_echo_content_tag(workspace):
    result = tool_edit_file(workspace, [
        {'path': 'a.txt', 'operation': 'insert_after',
         'anchor': 'old', 'content': 'old'},
    ])
    assert '[insert_after] [echo-content]: content is just the anchor' in result


@pytest.mark.unit
def test_edit_file_file_not_found_tag(workspace):
    result = tool_edit_file(workspace, [
        {'path': 'ghost.txt', 'operation': 'replace',
         'anchor': 'q', 'content': 'x'},
    ])
    assert '[replace] [file-not-found]: File not found: ghost.txt' in result


@pytest.mark.unit
@pytest.mark.parametrize('edit,fragment', [
    ({'path': 'a.txt', 'operation': 'bogus', 'anchor': 'A', 'content': 'x'},
     'Invalid operation'),
    ({'path': '', 'operation': 'replace', 'anchor': 'A', 'content': 'x'},
     'Missing required field'),
    ('not-an-object', 'Invalid edit entry'),
])
def test_edit_file_invalid_edit_tag(workspace, edit, fragment):
    result = tool_edit_file(workspace, [edit])
    assert f'[invalid-edit] {fragment}' in result


# ── legacy batch surfaces ──

@pytest.mark.unit
def test_apply_diffs_failure_tags(workspace):
    result = tool_apply_diffs(workspace, [
        {'path': 'a.txt', 'search': 'missing', 'replace': 'x'},
        {'path': 'ghost.txt', 'search': 'q', 'replace': 'x'},
    ])
    assert '[1] FAIL a.txt [match-not-found]: Search text not found' in result
    assert '[2] FAIL ghost.txt [file-not-found]: File not found' in result


@pytest.mark.unit
def test_insert_contents_failure_tags(workspace):
    result = tool_insert_contents(workspace, [
        {'path': 'a.txt', 'anchor': 'missing', 'content': 'x'},
        {'path': 'a.txt', 'anchor': 'A', 'content': 'x'},
    ])
    assert '[1] FAIL a.txt [match-not-found]: Anchor text not found' in result
    assert '[2] FAIL a.txt [ambiguous-match]: Anchor text matches 2' in result


# ── single-edit cores: structured errorKind on the result dict ──

@pytest.mark.unit
def test_single_edit_result_dicts_carry_error_kind(workspace):
    diff = tool_apply_diff(workspace, 'a.txt', 'missing', 'x')
    assert diff['ok'] is False
    assert diff['errorKind'] == 'match-not-found'

    missing_file = tool_apply_diff(workspace, 'ghost.txt', 'q', 'x')
    assert missing_file['errorKind'] == 'file-not-found'

    ins = tool_insert_content(workspace, 'a.txt', 'old', 'old')
    assert ins['ok'] is False
    assert ins['errorKind'] == 'echo-content'


# ── tools.py wrapper: tag rides AFTER the read-gate prefix ──

@pytest.mark.unit
def test_exec_apply_diff_renders_tag_after_prefix(workspace):
    from lib.project_mod.tools import _exec_apply_diff
    out = _exec_apply_diff(
        {'path': 'a.txt', 'search': 'missing', 'replace': 'x'},
        workspace, None, None, {})
    assert out.startswith('Diff failed [match-not-found]: Search text not found')


@pytest.mark.unit
def test_exec_insert_content_renders_tag_after_prefix(workspace):
    from lib.project_mod.tools import _exec_insert_content
    out = _exec_insert_content(
        {'path': 'a.txt', 'anchor': 'missing', 'content': 'x'},
        workspace, None, None, {})
    assert out.startswith('Insert failed [match-not-found]: Anchor text not found')


# ── downstream contracts stay intact with tagged lines ──

@pytest.mark.unit
def test_read_gate_success_probe_still_sees_tagged_failure():
    from lib.tasks_pkg.handlers._read_gate import _result_indicates_success
    assert _result_indicates_success(
        'apply_diff', 'Diff failed [match-not-found]: Search text not found in a.txt.') is False
    assert _result_indicates_success(
        'insert_content', 'Insert failed [echo-content]: content is just the anchor') is False


@pytest.mark.unit
def test_meta_parser_handles_tagged_and_untagged_fail_lines():
    from lib.tools.meta import build_project_tool_meta
    edits = [
        {'path': 'a.txt', 'search': 'x', 'replace': 'y'},
        {'path': 'b.txt', 'search': 'x', 'replace': 'y'},
    ]
    tagged = ('Applied 0/2 edits (2 failed)\n'
              '[1] FAIL a.txt [match-not-found]: Search text not found in a.txt.\n'
              '[2] FAIL b.txt: Search text not found in b.txt.')
    meta = build_project_tool_meta('apply_diffs', {'edits': edits}, tagged)
    statuses = [s['status'] for s in meta['editSummaries']]
    assert statuses == ['fail', 'fail']
    assert '[match-not-found]' in meta['editSummaries'][0]['detail']
