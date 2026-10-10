"""Contracts for the agent-runner → flow tool-usage evidence boundary."""

import json

import pytest

from lib.orchestration.tool_usage import (
    OrchestrationToolUsage,
    classify_orchestration_tool_usage,
)
from lib.tools.meta import build_project_tool_meta


pytestmark = pytest.mark.unit


def _round(name, args, output, *, status='done'):
    return {
        'toolName': name, 'toolArgs': json.dumps(args), 'status': status,
        'results': [build_project_tool_meta(name, args, output)],
        'toolContent': output,
    }


def _create():
    return _round('write_file', {'path': 'new.txt', 'content': 'new'},
                  'File created: new.txt (3 bytes)')


def test_tool_names_are_activity_not_verified_mutation():
    source = ['write_file', 'read_file', 'run_command', 'code_exec']
    usage = classify_orchestration_tool_usage({'tool_names': source})
    source.append('apply_diff')
    assert usage == OrchestrationToolUsage(
        exploratory_tools=('write_file', 'read_file', 'run_command', 'code_exec'),
        reported=True,
    )
    assert usage.engine_tuple() == (0, 4, [], True)
    assert classify_orchestration_tool_usage(
        {'tool_names': 'write_file'}).engine_tuple() == (0, 1, [], True)


def test_verified_native_create_is_detached_and_overrides_names_only():
    source = [_create()]
    usage = classify_orchestration_tool_usage({
        'tool_names': [], 'tool_log': source,
    })
    source[0]['results'][0]['writeOk'] = False
    legacy = usage.engine_tuple()
    assert legacy == (1, 0, ['write_file'], True)
    legacy[2].append('mutated')
    assert usage.state_changing_tools == ('write_file',)
    assert classify_orchestration_tool_usage({
        'tool_log': [], 'tool_names': ['write_file'],
    }).engine_tuple() == (0, 0, [], True)


@pytest.mark.parametrize('command,output', [
    ('git status --short', '$ git status --short\n\n[exit code: 0]'),
    ('python -c "print(1)"', '$ python\n1\n[exit code: 0]'),
    ('true', '$ true\n[exit code: 0]'),
    ('false', '$ false\n[exit code: 1]'),
    ('touch new.txt', '$ touch new.txt\n[exit code: 0]'),
    ('blocked', 'Error: command refused'),
    ('sleep 1', '$ sleep 1\n[Command moved to background: cmd-1]'),
])
def test_shell_success_failure_and_noop_are_not_mutation_proof(command, output):
    row = _round('run_command', {'command': command}, output)
    # This field is task-cumulative, so a read can inherit an earlier write.
    row['results'][0]['fileChanges'] = [{'path': 'old.txt', 'action': 'modified'}]
    row['toolResultEvidence'] = {
        'format': 'tool-result-evidence/v1', 'executionStatus': 'success',
        'worldVersionBefore': 2, 'worldVersionAfter': 3,
    }
    assert classify_orchestration_tool_usage({'tool_log': [row]}).engine_tuple() == (
        0, 1, [], True)


@pytest.mark.parametrize('status', ['error', 'rejected', 'aborted', 'running', None])
def test_failed_or_unsettled_round_never_counts_even_with_success_metadata(status):
    row = _create()
    row['status'] = status
    assert classify_orchestration_tool_usage({'tool_log': [row]}).state_changing_count == 0


@pytest.mark.parametrize('failure', [
    {'writeOk': False}, {'type': 'error'}, {'notRun': True}, {'exitCode': 1},
    {'timedOut': True}, {'refusal': {'kind': 'stale'}}, {'interrupted': True},
])
def test_failure_metadata_overrides_done(failure):
    row = _create()
    row['results'][0].update(failure)
    assert classify_orchestration_tool_usage({'tool_log': [row]}).state_changing_count == 0


@pytest.mark.parametrize('name,args,output,expected', [
    ('write_file', {'path': 'a', 'content': 'same'}, 'File updated: a', 0),

    ('write_file', {'path': 'created.txt', 'content': 'same'}, 'File updated: created.txt', 0),
    ('write_file', {'path': 'a', 'append': True, 'content': ''}, 'File updated: a', 0),
    ('write_file', {'path': 'a', 'append': True, 'content': 'new'}, 'File updated: a', 1),
    ('apply_diff', {'path': 'a', 'search': 'old', 'replace': 'new'}, 'Applied diff to a', 1),
    ('apply_diff', {'path': 'a', 'search': 'same', 'replace': 'same'}, 'Applied diff to a', 0),
    ('apply_diff', {'path': 'a', 'search': 'same ', 'replace': 'same'}, 'Applied diff to a', 0),
    ('edit_file', {'edits': [
        {'path': 'a', 'operation': 'replace', 'anchor': 'old', 'content': 'new'},
    ]}, 'Applied 1/1 edits\n[1] OK a [replace]: 1 lines changed', 1),
    ('edit_file', {'edits': [
        {'path': 'a', 'operation': 'replace', 'anchor': 'same', 'content': 'same'},
    ]}, 'Applied 1/1 edits\n[1] OK a [replace]: 1 lines changed', 0),
    ('edit_file', {'edits': [
        {'path': 'a', 'operation': 'replace', 'anchor': 'old', 'content': 'new'},
        {'path': 'a', 'operation': 'replace', 'anchor': 'new', 'content': 'old'},
    ]}, 'Applied 2/2 edits\n[1] OK a\n[2] OK a', 0),
    ('edit_file', {'edits': [
        {'path': 'a', 'operation': 'replace', 'anchor': 'old', 'content': 'new'},
    ]}, 'Applied 0/1 edits\n[1] FAIL a: not found', 0),
    ('code_exec', {'code': 'print(1)'}, '1', 0),
])
def test_native_write_receipts_require_non_noop_success(name, args, output, expected):
    usage = classify_orchestration_tool_usage({'tool_log': [_round(name, args, output)]})
    assert usage.state_changing_count == expected
    assert usage.exploratory_count == 1 - expected


def test_malformed_telemetry_fails_safe_without_false_runner_support():
    malformed = classify_orchestration_tool_usage({
        'tool_log': [None, 3, {}, {'tool': []}, {'toolName': '  '}],
    })
    assert malformed.engine_tuple() == (0, 0, [], True)
    assert classify_orchestration_tool_usage({}).reported is False
    assert classify_orchestration_tool_usage(None).reported is False
    assert classify_orchestration_tool_usage({'tool_names': []}).reported is True
    row = _create()
    row.update(toolName='edit_file', toolArgs='{bad')
    assert classify_orchestration_tool_usage({'tool_log': [row]}).state_changing_count == 0


@pytest.mark.parametrize('anchor,replacement,expected', [
    ('old', 'new', 1), ('old', 'old', 0), ('absent', 'new', 0),
])
def test_real_edit_bytes_and_executor_receipt_agree(tmp_path, monkeypatch, anchor, replacement, expected):
    from lib.project_mod.write_tools import _ops

    path = tmp_path / 'work.txt'
    path.write_text('old\n')
    # Keep actual matching, editing, final result rendering and metadata. Only
    # isolate the durable undo/index side effects from the repository runtime.
    monkeypatch.setattr(_ops, '_write_with_undo',
                        lambda target, content, *_a, **_kw: path.write_text(content))
    monkeypatch.setattr(_ops, '_record_write_freshness', lambda *_a, **_kw: None)
    monkeypatch.setattr(_ops, '_touch_for_vscode', lambda *_a, **_kw: None)
    args = {'edits': [{'path': 'work.txt', 'operation': 'replace',
                       'anchor': anchor, 'content': replacement}]}
    output = _ops.tool_edit_file(str(tmp_path), args['edits'])
    usage = classify_orchestration_tool_usage({'tool_log': [_round('edit_file', args, output)]})
    assert usage.state_changing_count == expected
    assert (path.read_text() != 'old\n') is bool(expected)


def test_unicode_normalized_identity_edit_is_unverified():
    from lib.project_mod.write_tools import _ops

    args = {'path': 'a', 'search': r'\u006f\u006c\u0064', 'replace': 'old'}
    computed = _ops._compute_diff('old', 'a', args['search'], args['replace'])
    assert computed['ok'] is True and computed['new_content'] == 'old'
    usage = classify_orchestration_tool_usage({'tool_log': [
        _round('apply_diff', args, 'Applied diff to a'),
    ]})
    assert usage.state_changing_count == 0


@pytest.mark.parametrize('evidence', [
    {'status': 'error'}, {'executionStatus': 'failed'},
    {'executionStatus': 'cancelled'}, {'executionStatus': 'partial'},
])
def test_result_evidence_failure_overrides_other_success_receipts(evidence):
    row = _create()
    row['toolResultEvidence'] = evidence
    assert classify_orchestration_tool_usage({'tool_log': [row]}).state_changing_count == 0
