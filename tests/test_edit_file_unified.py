import json

import pytest

from lib.project_mod.write_tools import tool_edit_file
from lib.project_mod.write_tools._ops import (
    _pure_addition_stats,
    _pure_wrap_insert,
)
from lib.tool_changes import extract_file_changes
from lib.tools.meta import build_project_tool_meta
from lib.tools.project import (
    PROJECT_TOOLS,
    PROJECT_TOOLS_LEGACY,
    project_tools_for_runtime,
)


def _tool_names(tools):
    return [tool['function']['name'] for tool in tools]


@pytest.mark.unit
def test_unified_schema_is_the_default_model_surface(monkeypatch):
    monkeypatch.delenv('TOFU_UNIFIED_EDIT_TOOL', raising=False)
    names = _tool_names(PROJECT_TOOLS)
    assert 'edit_file' in names
    assert 'list_dir' not in names
    assert not {'apply_diff', 'apply_diffs', 'insert_content',
                'insert_contents'} & set(names)
    assert _tool_names(project_tools_for_runtime()) == names
    edit_schema = next(
        tool for tool in PROJECT_TOOLS if tool['function']['name'] == 'edit_file')
    parameters = edit_schema['function']['parameters']
    properties = parameters['properties']
    assert list(properties)[:2] == ['description', 'edits']
    assert properties['description']['maxLength'] == 120
    assert parameters['required'] == ['description', 'edits']
    assert 'description' not in properties['edits']['items']['properties']


@pytest.mark.unit
def test_legacy_schema_has_an_emergency_rollback(monkeypatch):
    monkeypatch.setenv('TOFU_UNIFIED_EDIT_TOOL', '0')
    assert _tool_names(project_tools_for_runtime()) == _tool_names(
        PROJECT_TOOLS_LEGACY)
    assert 'edit_file' not in _tool_names(project_tools_for_runtime())
    assert 'list_dir' not in _tool_names(project_tools_for_runtime())


@pytest.mark.unit
def test_mixed_edit_batch_is_sequential(tmp_path):
    target = tmp_path / 'sample.txt'
    target.write_text('A\nC\nold\n', encoding='utf-8')

    result = tool_edit_file(str(tmp_path), [
        {'path': 'sample.txt', 'operation': 'insert_after',
         'anchor': 'A', 'content': 'B'},
        {'path': 'sample.txt', 'operation': 'insert_before',
         'anchor': 'old', 'content': 'D'},
        {'path': 'sample.txt', 'operation': 'replace',
         'anchor': 'old', 'content': 'new'},
    ])

    assert result.startswith('Applied 3/3 edits')
    assert target.read_text(encoding='utf-8') == 'A\nB\nC\nD\nnew\n'


@pytest.mark.unit
def test_failed_edit_does_not_stop_remaining_edits(tmp_path):
    target = tmp_path / 'sample.txt'
    target.write_text('A\nC\n', encoding='utf-8')

    result = tool_edit_file(str(tmp_path), [
        {'path': 'sample.txt', 'operation': 'replace',
         'anchor': 'missing', 'content': 'x'},
        {'path': 'sample.txt', 'operation': 'insert_after',
         'anchor': 'A', 'content': 'B'},
    ])

    assert result.startswith('Applied 1/2 edits (1 failed)')
    assert target.read_text(encoding='utf-8') == 'A\nB\nC\n'


@pytest.mark.unit
def test_malformed_operation_is_reported_without_aborting_batch(tmp_path):
    target = tmp_path / 'sample.txt'
    target.write_text('A\nC\n', encoding='utf-8')

    result = tool_edit_file(str(tmp_path), [
        {'path': 'sample.txt', 'operation': [],
         'anchor': 'A', 'content': 'bad'},
        {'path': 'sample.txt', 'operation': 'insert_after',
         'anchor': 'A', 'content': 'B'},
    ])

    assert result.startswith('Applied 1/2 edits (1 failed)')
    assert 'Invalid operation' in result
    assert target.read_text(encoding='utf-8') == 'A\nB\nC\n'


@pytest.mark.unit
@pytest.mark.parametrize(('operation', 'replace_all', 'expected'), [
    ('insert_after', False, 'A\nB\n'),
    ('insert_after', True, 'A\nB\n'),
    ('insert_before', False, 'B\nA\n'),
    ('insert_before', True, 'B\nA\n'),
])
def test_insert_ignores_replace_all_when_anchor_is_unique(
        tmp_path, operation, replace_all, expected):
    target = tmp_path / 'sample.txt'
    target.write_text('A\n', encoding='utf-8')
    result = tool_edit_file(str(tmp_path), [{
        'path': 'sample.txt', 'operation': operation,
        'anchor': 'A', 'content': 'B', 'replace_all': replace_all,
    }])
    assert result.startswith('Applied 1/1 edits')
    assert target.read_text(encoding='utf-8') == expected


@pytest.mark.unit
def test_replace_all_does_not_make_an_insert_anchor_ambiguous(tmp_path):
    target = tmp_path / 'sample.txt'
    target.write_text('A\nA\n', encoding='utf-8')
    result = tool_edit_file(str(tmp_path), [{
        'path': 'sample.txt', 'operation': 'insert_after',
        'anchor': 'A', 'content': 'B', 'replace_all': True,
    }])
    assert result.startswith('Applied 0/1 edits (1 failed)')
    assert 'matches 2 locations' in result
    assert target.read_text(encoding='utf-8') == 'A\nA\n'


@pytest.mark.unit
def test_pure_wrap_insert_detection_boundaries():
    assert _pure_wrap_insert('A', 'A\nB') == ('insert_after', '\nB')
    assert _pure_wrap_insert('A', 'B\nA') == ('insert_before', 'B\n')
    # anchor at BOTH ends → the append reading wins and stays exact
    assert _pure_wrap_insert('}', '}\nx\n}') == ('insert_after', '\nx\n}')
    # no-op replace (content == anchor) is not an insertion
    assert _pure_wrap_insert('A', 'A') is None
    # genuine rewrites keep the replace vocabulary
    assert _pure_wrap_insert('old', 'new') is None
    assert _pure_wrap_insert('A', 'B\nA\nC') is None
    # insertion INSIDE the anchor has no mechanical insert fix — not gated
    assert _pure_wrap_insert('A C', 'A B C') is None


@pytest.mark.unit
def test_wrap_replace_is_rejected_pre_execution(tmp_path):
    target = tmp_path / 'sample.txt'
    target.write_text('A\nC\n', encoding='utf-8')

    result = tool_edit_file(str(tmp_path), [
        {'path': 'sample.txt', 'operation': 'replace',
         'anchor': 'A', 'content': 'A\nB'},
        {'path': 'sample.txt', 'operation': 'insert_after',
         'anchor': 'C', 'content': 'D'},
    ])

    assert result.startswith('Applied 1/2 edits (1 failed)')
    assert 'pure insertion rejected' in result
    assert "operation='insert_after'" in result
    # the rejected edit never touched the file; the sibling insert ran
    assert target.read_text(encoding='utf-8') == 'A\nC\nD\n'


@pytest.mark.unit
def test_wrap_replace_at_end_suggests_insert_before(tmp_path):
    target = tmp_path / 'sample.txt'
    target.write_text('A\n', encoding='utf-8')

    result = tool_edit_file(str(tmp_path), [{
        'path': 'sample.txt', 'operation': 'replace',
        'anchor': 'A', 'content': 'B\nA',
    }])

    assert 'pure insertion rejected' in result
    assert "operation='insert_before'" in result
    assert target.read_text(encoding='utf-8') == 'A\n'


@pytest.mark.unit
def test_wrap_gate_exempts_replace_all_and_genuine_replace(tmp_path):
    target = tmp_path / 'sample.txt'
    target.write_text('A\nA\nold\n', encoding='utf-8')

    result = tool_edit_file(str(tmp_path), [
        {'path': 'sample.txt', 'operation': 'replace',
         'anchor': 'A', 'content': 'A\nB', 'replace_all': True},
        {'path': 'sample.txt', 'operation': 'replace',
         'anchor': 'old', 'content': 'new'},
    ])

    assert result.startswith('Applied 2/2 edits')
    assert target.read_text(encoding='utf-8') == 'A\nB\nA\nB\nnew\n'


@pytest.mark.unit
def test_wrap_gate_kill_switch(monkeypatch, tmp_path):
    monkeypatch.setenv('TOFU_EDIT_WRAP_GATE', '0')
    target = tmp_path / 'sample.txt'
    target.write_text('A\n', encoding='utf-8')

    result = tool_edit_file(str(tmp_path), [{
        'path': 'sample.txt', 'operation': 'replace',
        'anchor': 'A', 'content': 'A\nB',
    }])

    assert result.startswith('Applied 1/1 edits')
    assert target.read_text(encoding='utf-8') == 'A\nB\n'


@pytest.mark.unit
def test_additive_legacy_diff_is_detected_without_source_retention():
    stats = _pure_addition_stats('A\nC', 'A\nB\nC')
    assert stats == {
        'anchor_chars': 3,
        'content_chars': 2,
        'legacy_arg_chars': 8,
        'repeated_unchanged_chars': 3,
    }
    assert _pure_addition_stats('old', 'new') is None


@pytest.mark.unit
def test_edit_file_meta_and_file_changes_keep_operations():
    args = {'edits': [
        {'path': 'a.py', 'operation': 'insert_after',
         'anchor': 'A', 'content': 'B'},
        {'path': 'b.py', 'operation': 'replace',
         'anchor': 'old', 'content': 'new'},
    ]}
    content = ('Applied 2/2 edits\n'
               '[1] OK a.py [insert_after]\n'
               '[2] OK b.py [replace]')
    meta = build_project_tool_meta('edit_file', args, content)
    assert meta['writeOk'] is True
    assert meta['editOperations'] == ['insert_after', 'replace']
    assert [row['operation'] for row in meta['editSummaries']] == [
        'insert_after', 'replace']

    changes = extract_file_changes([{
        'toolName': 'edit_file', 'toolArgs': json.dumps(args),
        'results': [{'writeOk': True}],
    }])
    assert [(c.path, c.action) for c in changes] == [
        ('a.py', 'inserted'), ('b.py', 'patched')]


@pytest.mark.unit
def test_offline_evaluator_scores_additive_insert():
    from evaluations.edit_file.evaluation import score_row, summarize

    score = score_row({
        'model': 'fixture-model',
        'case_id': 'insert_between_blocks',
        'tool_name': 'edit_file',
        'arguments': {'edits': [{
            'path': 'sample.txt', 'operation': 'insert_after',
            'anchor': 'A', 'content': 'B',
        }]},
        'baseline_argument_chars': 300,
    })
    assert score['valid_call'] is True
    assert score['correct_edit'] is True
    assert score['efficient_operation'] is True
    assert score['argument_reduction_rate'] > 0.30
    summary = summarize([{
        'model': 'fixture-model',
        'case_id': 'insert_between_blocks',
        'tool_name': 'edit_file',
        'arguments': {'edits': [{
            'path': 'sample.txt', 'operation': 'insert_after',
            'anchor': 'A', 'content': 'B',
        }]},
        'baseline_argument_chars': 300,
    }])
    assert summary['fixture-model']['meets_30_percent_reduction'] is True


@pytest.mark.unit
def test_legacy_edit_call_repairs_to_unified_shape():
    from lib.tool_input_repair import resolve_tool_name, validate_then_repair

    name, kind = resolve_tool_name('apply_diff', known={'edit_file'})
    assert (name, kind) == ('edit_file', 'alias')
    args, log = validate_then_repair(name, {
        'path': 'a.py', 'search': 'old', 'replace': 'new',
    })
    assert args == {'edits': [{
        'path': 'a.py', 'operation': 'replace',
        'anchor': 'old', 'content': 'new',
    }]}
    assert ('edit_file', 'structural_transform') in log


@pytest.mark.unit
def test_read_gate_partitions_unread_unified_edits(tmp_path):
    from lib.tasks_pkg.handlers._read_gate import partition_batch_edits

    (tmp_path / 'a.py').write_text('A\n', encoding='utf-8')
    args = {'edits': [{
        'path': 'a.py', 'operation': 'insert_after',
        'anchor': 'A', 'content': 'B',
    }]}
    skipped, paths = partition_batch_edits(
        {'convId': 'c', 'toolRounds': [], 'messages': []},
        'edit_file', args, str(tmp_path))
    assert skipped == [0]
    assert paths == ['a.py']


@pytest.mark.unit
def test_insert_after_neighbour_echo_auto_repaired(tmp_path):
    """Regression for the mswlvsfgzwiywr incident: insert_after whose content
    begins with a verbatim copy of the first line AFTER the anchor (the model
    quoting diff context) used to land as-is, leaving an empty-bodied
    duplicate def and an IndentationError. The echo is provably the mistake
    (as-given result no longer parses), so it is stripped locally."""
    target = tmp_path / 'sample_test.py'
    target.write_text(
        'def test_previous():\n'
        '    assert True\n'
        '\n\n'
        'def test_workbench_has_responsive_and_focus_visible_styles():\n'
        "    assert 'workbench' in 'workbench'\n",
        encoding='utf-8')

    result = tool_edit_file(str(tmp_path), [{
        'path': 'sample_test.py', 'operation': 'insert_after',
        'anchor': '    assert True',
        'content': (
            'def test_workbench_has_responsive_and_focus_visible_styles():\n'
            'def test_full_page_chat_drop_stands_down_while_workbench_open():\n'
            "    assert 'drop' in 'drop'\n"),
    }])

    assert result.startswith('Applied 1/1 edits')
    assert 'auto-repaired: stripped 1 echoed context line' in result
    text = target.read_text(encoding='utf-8')
    assert text.count('def test_workbench_has_responsive_and_focus_visible_styles') == 1
    assert 'def test_full_page_chat_drop_stands_down_while_workbench_open' in text
    import ast
    ast.parse(text)  # the repaired file must stay importable


@pytest.mark.unit
def test_insert_after_anchor_echo_stripped(tmp_path):
    target = tmp_path / 'sample.txt'
    target.write_text('A\nB\n', encoding='utf-8')

    result = tool_edit_file(str(tmp_path), [{
        'path': 'sample.txt', 'operation': 'insert_after',
        'anchor': 'A', 'content': 'A\nnew',
    }])

    assert result.startswith('Applied 1/1 edits')
    assert 'auto-repaired: stripped the anchor copy' in result
    assert target.read_text(encoding='utf-8') == 'A\nnew\nB\n'


@pytest.mark.unit
def test_insert_before_anchor_echo_stripped(tmp_path):
    target = tmp_path / 'sample.txt'
    target.write_text('A\nB\n', encoding='utf-8')

    result = tool_edit_file(str(tmp_path), [{
        'path': 'sample.txt', 'operation': 'insert_before',
        'anchor': 'B', 'content': 'new\nB',
    }])

    assert result.startswith('Applied 1/1 edits')
    assert 'auto-repaired: stripped the anchor copy' in result
    assert target.read_text(encoding='utf-8') == 'A\nnew\nB\n'


@pytest.mark.unit
def test_anchor_echo_only_edit_fails_without_touching_file(tmp_path):
    target = tmp_path / 'sample.txt'
    target.write_text('A\nB\n', encoding='utf-8')

    result = tool_edit_file(str(tmp_path), [
        {'path': 'sample.txt', 'operation': 'insert_after',
         'anchor': 'A', 'content': 'A'},
        {'path': 'sample.txt', 'operation': 'insert_after',
         'anchor': 'B', 'content': 'C'},
    ])

    assert result.startswith('Applied 1/2 edits (1 failed)')
    assert 'just the anchor text repeated' in result
    assert target.read_text(encoding='utf-8') == 'A\nB\nC\n'


@pytest.mark.unit
def test_mid_token_anchor_prefix_is_not_an_echo(tmp_path):
    # 'x' anchor + 'xyz' content: the shared prefix is mid-token, so the
    # content is genuinely new text — must NOT be rewritten.
    target = tmp_path / 'sample.txt'
    target.write_text('x\ny\n', encoding='utf-8')

    result = tool_edit_file(str(tmp_path), [{
        'path': 'sample.txt', 'operation': 'insert_after',
        'anchor': 'x', 'content': 'xyz',
    }])

    assert result.startswith('Applied 1/1 edits')
    assert 'auto-repaired' not in result
    assert target.read_text(encoding='utf-8') == 'x\nxyz\ny\n'


@pytest.mark.unit
def test_whole_neighbour_echo_fails_with_escape_hatch(tmp_path):
    target = tmp_path / 'sample.md'
    target.write_text('A\n| a | b |\n| c | d |\n', encoding='utf-8')

    result = tool_edit_file(str(tmp_path), [{
        'path': 'sample.md', 'operation': 'insert_after',
        'anchor': 'A', 'content': '| a | b |',
    }])

    assert result.startswith('Applied 0/1 edits (1 failed)')
    assert 'verbatim copy' in result
    assert 'operation=replace' in result
    assert target.read_text(encoding='utf-8') == 'A\n| a | b |\n| c | d |\n'


@pytest.mark.unit
def test_syntax_guard_warns_when_edit_breaks_python(tmp_path):
    target = tmp_path / 'broken_by_edit.py'
    target.write_text('x = 1\n', encoding='utf-8')

    result = tool_edit_file(str(tmp_path), [{
        'path': 'broken_by_edit.py', 'operation': 'replace',
        'anchor': 'x = 1', 'content': 'def f(:',
    }])

    assert result.startswith('Applied 1/1 edits')
    assert 'SYNTAX GUARD' in result
    assert 'py_compile' in result
    assert target.read_text(encoding='utf-8') == 'def f(:\n'


@pytest.mark.unit
def test_syntax_guard_silent_when_file_was_already_broken(tmp_path):
    target = tmp_path / 'already_broken.py'
    target.write_text('def broken(:\n', encoding='utf-8')

    result = tool_edit_file(str(tmp_path), [{
        'path': 'already_broken.py', 'operation': 'insert_after',
        'anchor': 'def broken(:', 'content': 'still = broken(:',
    }])

    assert result.startswith('Applied 1/1 edits')
    assert 'SYNTAX GUARD' not in result


@pytest.mark.unit
def test_echo_repair_kill_switch_lands_echo_verbatim(monkeypatch, tmp_path):
    monkeypatch.setenv('TOFU_EDIT_ECHO_REPAIR', '0')
    target = tmp_path / 'sample.txt'
    target.write_text('A\nB\n', encoding='utf-8')

    result = tool_edit_file(str(tmp_path), [{
        'path': 'sample.txt', 'operation': 'insert_after',
        'anchor': 'A', 'content': 'A\nnew',
    }])

    assert result.startswith('Applied 1/1 edits')
    assert 'auto-repaired' not in result
    assert target.read_text(encoding='utf-8') == 'A\nA\nnew\nB\n'
