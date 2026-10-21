import json

import pytest

from lib.project_mod.write_tools import tool_edit_file
from lib.project_mod.write_tools._ops import _pure_addition_stats
from lib.tool_results.tool_changes import extract_file_changes
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

    assert properties['atomic']['type'] == 'boolean'
    assert properties['atomic']['default'] is True
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
    ], atomic=False)

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
    ], atomic=False)

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
@pytest.mark.parametrize(('before', 'anchor', 'replacement', 'expected'), [
    ('A\nC\n', 'A', 'A\nB', 'A\nB\nC\n'),
    ('A\n', 'A', 'B\nA', 'B\nA\n'),
    ('}\n', '}', '}\nx\n}', '}\nx\n}\n'),
    ('A\n', 'A', 'A\nA', 'A\nA\n'),
    ('return x;\n', 'x', 'x + 1', 'return x + 1;\n'),
    ('return x;\n', 'x', '1 + x', 'return 1 + x;\n'),
    ('value = 1\n', '1', '10', 'value = 10\n'),
    ('A', 'A', 'A\nB', 'A\nB'),
    ('A\nC\n', 'A\n', 'A\nB\n', 'A\nB\nC\n'),
    ('A\r\nC\r\n', 'A\r\n', 'A\r\nB\r\n', 'A\r\nB\r\nC\r\n'),
    ('A\n', 'A', 'B\nA\nC', 'B\nA\nC\n'),
    ('A C\n', 'A C', 'A B C', 'A B C\n'),
    ('中文\n', '中文', '中文追加', '中文追加\n'),
    ('A\n', 'A', 'A', 'A\n'),
    ('A\n', 'A', '', '\n'),
])
def test_replace_preserves_supplied_text_without_style_retry(
        tmp_path, before, anchor, replacement, expected):
    target = tmp_path / 'sample.txt'
    target.write_bytes(before.encode('utf-8'))

    result = tool_edit_file(str(tmp_path), [{
        'path': 'sample.txt', 'operation': 'replace',
        'anchor': anchor, 'content': replacement,
    }])

    assert result.startswith('Applied 1/1 edits'), result
    assert '[replace]' in result
    assert 'Re-issue' not in result
    assert 'auto-repaired' not in result
    assert target.read_bytes() == expected.encode('utf-8')


@pytest.mark.unit
@pytest.mark.parametrize(('operation', 'anchor', 'replacement'), [
    ('insert_after', 'A', 'B'), ('insert_before', 'C', 'B'),
])
def test_line_insert_keeps_universal_newline_behavior(
        tmp_path, operation, anchor, replacement):
    target = tmp_path / 'sample.txt'
    target.write_bytes(b'A\r\nC\r\n')
    result = tool_edit_file(str(tmp_path), [{
        'path': 'sample.txt', 'operation': operation,
        'anchor': anchor, 'content': replacement,
    }])

    assert result.startswith('Applied 1/1 edits'), result
    assert target.read_bytes() == b'A\nB\nC\n'


@pytest.mark.unit
def test_retained_anchor_replace_sequences_with_dependent_edits(tmp_path):
    target = tmp_path / 'sample.txt'
    target.write_text('A\nC\n', encoding='utf-8')

    result = tool_edit_file(str(tmp_path), [
        {'path': 'sample.txt', 'operation': 'replace',
         'anchor': 'A', 'content': 'A\nB'},
        {'path': 'sample.txt', 'operation': 'insert_after',
         'anchor': 'B', 'content': 'next'},
        {'path': 'sample.txt', 'operation': 'replace',
         'anchor': 'C', 'content': 'D'},
    ])

    assert result.startswith('Applied 3/3 edits'), result
    assert target.read_text(encoding='utf-8') == 'A\nB\nnext\nD\n'


@pytest.mark.unit
def test_retained_anchor_replace_all_and_genuine_replace(tmp_path):
    target = tmp_path / 'sample.txt'
    target.write_text('A\nA\nold\n', encoding='utf-8')

    result = tool_edit_file(str(tmp_path), [
        {'path': 'sample.txt', 'operation': 'replace',
         'anchor': 'A', 'content': 'A\nB', 'replace_all': True},
        {'path': 'sample.txt', 'operation': 'replace',
         'anchor': 'old', 'content': 'new'},
    ])

    assert result.startswith('Applied 2/2 edits'), result
    assert '[2 occurrences]' in result
    assert target.read_text(encoding='utf-8') == 'A\nB\nA\nB\nnew\n'


@pytest.mark.unit
@pytest.mark.parametrize(('anchor', 'error'), [
    ('missing', 'not found'), ('A', 'matches 2 locations'),
])
def test_retained_anchor_replace_keeps_match_safety(tmp_path, anchor, error):
    target = tmp_path / 'sample.txt'
    target.write_text('A\nA\nold\n', encoding='utf-8')

    result = tool_edit_file(str(tmp_path), [
        {'path': 'sample.txt', 'operation': 'replace',
         'anchor': anchor, 'content': anchor + '\nnew'},
        {'path': 'sample.txt', 'operation': 'replace',
         'anchor': 'old', 'content': 'updated'},
    ], atomic=False)

    assert result.startswith('Applied 1/2 edits (1 failed)'), result
    assert error in result
    assert target.read_text(encoding='utf-8') == 'A\nA\nupdated\n'


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
@pytest.mark.parametrize(('case_id', 'edit'), [
    ('insert_between_blocks', {
        'path': 'sample.txt', 'operation': 'replace',
        'anchor': 'A', 'content': 'A\nB',
    }),
    ('add_import', {
        'path': 'app.py', 'operation': 'replace',
        'anchor': 'import os', 'content': 'import os\nimport json',
    }),
])
def test_offline_scorer_separates_correct_replace_from_operation_preference(
        tmp_path, case_id, edit):
    from evaluations.edit_file.dataset import CASES_BY_ID
    from evaluations.edit_file.evaluation import score_row

    case = CASES_BY_ID[case_id]
    for path, text in case['files'].items():
        (tmp_path / path).write_text(text, encoding='utf-8')
    result = tool_edit_file(str(tmp_path), [edit])
    assert result.startswith('Applied 1/1 edits'), result
    assert {path: (tmp_path / path).read_text(encoding='utf-8')
            for path in case['files']} == case['expected']
    score = score_row({
        'model': 'fixture-model', 'case_id': case_id,
        'tool_name': 'edit_file', 'arguments': {'edits': [edit]},
    })
    assert score['valid_call'] is True
    assert score['correct_edit'] is True
    assert score['efficient_operation'] is False


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
def test_insert_partial_anchor_echo_is_stripped(tmp_path):
    # Only the first line of a two-line anchor repeated: it is still a copy of
    # text the insert already keeps, and the duplicate parses, so no guard
    # would have fired.
    target = tmp_path / 'sample.txt'
    target.write_text('A\nB\nnew_tail\n', encoding='utf-8')

    result = tool_edit_file(str(tmp_path), [{
        'path': 'sample.txt', 'operation': 'insert_after',
        'anchor': 'A\nB', 'content': 'A\nextra\n',
    }])

    assert result.startswith('Applied 1/1 edits')
    assert 'auto-repaired: stripped the anchor copy' in result
    assert target.read_text(encoding='utf-8') == 'A\nB\nextra\nnew_tail\n'


@pytest.mark.unit
@pytest.mark.parametrize(('operation', 'content', 'other'), [
    ('insert_after', 'extra\nA\nB', 'insert_before'),
    ('insert_before', 'A\nB\nextra', 'insert_after'),
])
def test_far_side_anchor_echo_is_refused(tmp_path, operation, content, other):
    # A copy of the anchor on the side away from the insertion either
    # duplicates it or moves the anchor's own body into the inserted block;
    # both still parse, so the edit must fail visibly instead of guessing.
    target = tmp_path / 'sample.txt'
    target.write_text('A\nB\nbody\n', encoding='utf-8')

    result = tool_edit_file(str(tmp_path), [{
        'path': 'sample.txt', 'operation': operation,
        'anchor': 'A\nB', 'content': content,
    }])

    assert result.startswith('Applied 0/1 edits (1 failed)')
    assert 'echo-content' in result
    assert other in result
    assert target.read_text(encoding='utf-8') == 'A\nB\nbody\n'


@pytest.mark.unit
def test_anchor_middle_line_repeat_is_not_an_echo(tmp_path):
    # A run taken from the middle of a longer anchor can be intended
    # repetition; only head/tail alignment is provably the anchor again.
    target = tmp_path / 'sample.txt'
    target.write_text('A\nB\nC\ntail\n', encoding='utf-8')

    result = tool_edit_file(str(tmp_path), [{
        'path': 'sample.txt', 'operation': 'insert_after',
        'anchor': 'A\nB\nC', 'content': 'B\nextra\n',
    }])

    assert result.startswith('Applied 1/1 edits')
    assert 'auto-repaired' not in result
    assert target.read_text(encoding='utf-8') == 'A\nB\nC\nB\nextra\ntail\n'


@pytest.mark.unit
def test_anchor_echo_only_edit_fails_without_touching_file(tmp_path):
    target = tmp_path / 'sample.txt'
    target.write_text('A\nB\n', encoding='utf-8')

    result = tool_edit_file(str(tmp_path), [
        {'path': 'sample.txt', 'operation': 'insert_after',
         'anchor': 'A', 'content': 'A'},
        {'path': 'sample.txt', 'operation': 'insert_after',
         'anchor': 'B', 'content': 'C'},
    ], atomic=False)

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


@pytest.mark.unit
@pytest.mark.parametrize('first_operation', ['replace', 'insert_after'])
def test_batch_syntax_feedback_ignores_repaired_intermediate_state(
        tmp_path, monkeypatch, first_operation):
    from lib.project_mod.write_tools import _ops

    target = tmp_path / 'sample.py'
    target.write_text('x = 1\n', encoding='utf-8')
    first_content = 'def f(:' if first_operation == 'replace' else 'def f():'
    second_anchor = first_content
    second_content = 'def f():\n    pass'
    writes = []
    guard_calls = []
    original_write = _ops._atomic_write_text
    original_guard = _ops._py_syntax_guard

    def record_write(path, text):
        writes.append(text)
        return original_write(path, text)

    def record_guard(path, before, after):
        guard_calls.append((before, after))
        return original_guard(path, before, after)

    monkeypatch.setattr(_ops, '_atomic_write_text', record_write)
    monkeypatch.setattr(_ops, '_py_syntax_guard', record_guard)
    result = tool_edit_file(str(tmp_path), [
        {'path': 'sample.py', 'operation': first_operation,
         'anchor': 'x = 1', 'content': first_content},
        {'path': 'sample.py', 'operation': 'replace',
         'anchor': second_anchor, 'content': second_content},
    ])

    expected = ('' if first_operation == 'replace' else 'x = 1\n') + second_content + '\n'
    assert result.startswith('Applied 2/2 edits'), result
    assert 'SYNTAX GUARD' not in result
    assert target.read_text(encoding='utf-8') == expected
    assert writes == [expected]  # no invalid intermediate state reaches disk
    assert guard_calls == [('x = 1\n', expected)]


@pytest.mark.unit
def test_batch_syntax_feedback_warns_once_for_final_committed_state(tmp_path):
    target = tmp_path / 'sample.py'
    target.write_text('x = 1\n', encoding='utf-8')
    result = tool_edit_file(str(tmp_path), [
        {'path': 'sample.py', 'operation': 'replace',
         'anchor': 'x = 1', 'content': 'def f(:'},
        {'path': 'sample.py', 'operation': 'replace',
         'anchor': 'f(', 'content': 'g('},
        {'path': 'sample.py', 'operation': 'replace',
         'anchor': 'missing', 'content': 'missing + 1'},
    ], atomic=False)

    assert result.startswith('Applied 2/3 edits (1 failed)'), result
    assert result.count('SYNTAX GUARD') == 1
    warning_line = next(line for line in result.splitlines() if 'SYNTAX GUARD' in line)
    assert warning_line.startswith('[2] OK')
    assert target.read_text(encoding='utf-8') == 'def g(:\n'


@pytest.mark.unit
def test_retained_anchor_replace_warns_on_real_duplicate_definition(tmp_path):
    target = tmp_path / 'sample.py'
    target.write_text('def f(x):\n    return x\n', encoding='utf-8')
    result = tool_edit_file(str(tmp_path), [{
        'path': 'sample.py', 'operation': 'replace',
        'anchor': 'def f(x):', 'content': 'def f(x):\ndef f(x, y):',
    }])

    assert result.startswith('Applied 1/1 edits'), result
    assert result.count('SYNTAX GUARD') == 1
    assert target.read_text(encoding='utf-8') == (
        'def f(x):\ndef f(x, y):\n    return x\n')


@pytest.mark.unit
def test_batch_syntax_feedback_uses_each_files_original_baseline(tmp_path):
    (tmp_path / 'clean.py').write_text('x = 1\n', encoding='utf-8')
    (tmp_path / 'broken.py').write_text('def f(:\n', encoding='utf-8')
    result = tool_edit_file(str(tmp_path), [
        {'path': 'clean.py', 'operation': 'replace',
         'anchor': 'x = 1', 'content': 'def new(:'},
        {'path': 'broken.py', 'operation': 'replace',
         'anchor': 'def f(:', 'content': 'x = 1'},
        {'path': 'broken.py', 'operation': 'replace',
         'anchor': 'x = 1', 'content': 'def broken(:'},
    ])

    assert result.startswith('Applied 3/3 edits'), result
    assert result.count('SYNTAX GUARD') == 1
    warning_line = next(line for line in result.splitlines() if 'SYNTAX GUARD' in line)
    assert 'clean.py' in warning_line
    assert (tmp_path / 'broken.py').read_text(encoding='utf-8') == 'def broken(:\n'


@pytest.mark.unit
def test_failed_batch_write_keeps_original_without_syntax_warning(tmp_path, monkeypatch):
    from lib.project_mod.write_tools import _ops

    target = tmp_path / 'sample.py'
    target.write_text('x = 1\n', encoding='utf-8')

    def fail_write(path, text):
        raise OSError('simulated write failure')

    monkeypatch.setattr(_ops, '_atomic_write_text', fail_write)
    result = tool_edit_file(str(tmp_path), [{
        'path': 'sample.py', 'operation': 'replace',
        'anchor': 'x = 1', 'content': 'x = 1\ndef f(:',
    }])

    assert result.startswith('Applied 0/1 edits (1 failed)'), result
    assert 'simulated write failure' in result
    assert 'SYNTAX GUARD' not in result
    assert target.read_text(encoding='utf-8') == 'x = 1\n'


@pytest.mark.unit
def test_batch_write_failure_isolated_without_false_freshness_or_history(
        tmp_path, monkeypatch):
    from lib.project_mod.write_tools import _ops

    (tmp_path / 'failed.py').write_text('x = 1\n', encoding='utf-8')
    (tmp_path / 'good.txt').write_text('A\n', encoding='utf-8')
    original_write = _ops._atomic_write_text
    freshness_paths = []
    history_paths = []

    def fail_one_file(path, text, **publication_hooks):
        if path.endswith('failed.py'):
            raise OSError('simulated write failure')
        return original_write(path, text, **publication_hooks)

    monkeypatch.setattr(_ops, '_atomic_write_text', fail_one_file)
    monkeypatch.setattr(_ops, '_record_write_freshness',
                        lambda conv, path, data: freshness_paths.append(path))
    monkeypatch.setattr(_ops, '_should_record_modification', lambda *a, **kw: True)
    monkeypatch.setattr(_ops, '_mod_attribution',
                        lambda target, base, rel, **kw: (base, rel))
    monkeypatch.setattr(_ops, '_record_modification',
                        lambda base, action, path, **kw: history_paths.append(path) or True)
    result = tool_edit_file(str(tmp_path), [
        {'path': 'failed.py', 'operation': 'replace',
         'anchor': 'x = 1', 'content': 'x = 1\ndef f(:'},
        {'path': 'good.txt', 'operation': 'replace',
         'anchor': 'A', 'content': 'A\nB'},
    ], atomic=False)

    assert result.startswith('Applied 1/2 edits (1 failed)'), result
    assert 'SYNTAX GUARD' not in result
    assert (tmp_path / 'failed.py').read_bytes() == b'x = 1\n'
    assert (tmp_path / 'good.txt').read_bytes() == b'A\nB\n'
    assert freshness_paths == [str(tmp_path / 'good.txt')]
    assert history_paths == ['good.txt']


@pytest.mark.unit
def test_retained_anchor_batch_records_one_physical_transition(tmp_path, monkeypatch):
    from lib.project_mod.write_tools import _ops

    target = tmp_path / 'sample.txt'
    target.write_bytes(b'A\nC\n')
    records = []
    monkeypatch.setattr(_ops, '_should_record_modification', lambda *a, **kw: True)
    monkeypatch.setattr(_ops, '_mod_attribution',
                        lambda target, base, rel, **kw: (base, rel))
    monkeypatch.setattr(_ops, '_record_modification',
                        lambda base, action, path, **kw: records.append(kw) or True)
    result = tool_edit_file(str(tmp_path), [
        {'path': 'sample.txt', 'operation': 'replace',
         'anchor': 'A', 'content': 'A\nB'},
        {'path': 'sample.txt', 'operation': 'replace',
         'anchor': 'B', 'content': 'B\nD'},
    ])

    assert result.startswith('Applied 2/2 edits'), result
    assert [record['original_content'] for record in records] == [
        'A\nC\n']
    restored = target.read_text(encoding='utf-8')
    for record in reversed(records):
        patch = record['reverse_patch']
        restored = restored.replace(patch['search'], patch['replace'], 1)
    assert restored == 'A\nC\n'


@pytest.mark.unit
def test_batch_syntax_feedback_respects_disabled_guard(tmp_path, monkeypatch):
    monkeypatch.setenv('TOFU_EDIT_SYNTAX_GUARD', '0')
    target = tmp_path / 'sample.py'
    target.write_text('x = 1\n', encoding='utf-8')
    result = tool_edit_file(str(tmp_path), [{
        'path': 'sample.py', 'operation': 'replace',
        'anchor': 'x = 1', 'content': 'x = 1\ndef f(:',
    }])

    assert result.startswith('Applied 1/1 edits'), result
    assert 'SYNTAX GUARD' not in result
    assert target.read_text(encoding='utf-8') == 'x = 1\ndef f(:\n'


@pytest.mark.unit
@pytest.mark.parametrize(('before', 'replacement'), [
    ('x = 1\n# larger than test cap\n', 'def f(:\n'),
    ('x = 1\n', 'def much_larger_than_test_cap(:\n'),
])
def test_batch_syntax_feedback_bounds_both_parser_inputs(
        tmp_path, monkeypatch, before, replacement):
    from lib.project_mod.write_tools import _ops

    monkeypatch.setattr(_ops, '_ECHO_REPAIR_MAX_CHARS', 16)

    def unexpected_parse(text):
        pytest.fail('over-budget syntax comparison must not parse either input')

    monkeypatch.setattr(_ops, '_py_parses', unexpected_parse)
    target = tmp_path / 'sample.py'
    target.write_text(before, encoding='utf-8')
    result = tool_edit_file(str(tmp_path), [{
        'path': 'sample.py', 'operation': 'replace',
        'anchor': before, 'content': replacement,
    }])

    assert result.startswith('Applied 1/1 edits'), result
    assert 'SYNTAX GUARD' not in result
    assert target.read_text(encoding='utf-8') == replacement
