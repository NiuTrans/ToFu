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
def test_replace_all_is_rejected_for_insert(tmp_path):
    target = tmp_path / 'sample.txt'
    target.write_text('A\n', encoding='utf-8')
    result = tool_edit_file(str(tmp_path), [{
        'path': 'sample.txt', 'operation': 'insert_after',
        'anchor': 'A', 'content': 'B', 'replace_all': False,
    }])
    assert 'replace_all is valid only' in result
    assert target.read_text(encoding='utf-8') == 'A\n'


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
