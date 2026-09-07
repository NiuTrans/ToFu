"""Structured ``meta.refusal`` pins for the write-gate refusal cards.

The frontend renders a localized, explanatory refusal card (badge +
notice) off STRUCTURED data — ``meta.refusal = {kind, paths, skipped,
proceeded}`` attached by ``_handle_project_tool`` at each interception
point — instead of parsing the raw badge tokens ('stale', 'read first',
…). This suite drives the REAL handler (same discipline as
tests/test_write_freshness_handler.py: ``_finalize_tool_round`` replaced
with a recorder, everything else real) and pins the refusal payload for
every interception shape:

  * FreshGate full refusal (single write)   → kind 'stale' + paths
  * FreshGate partial skip (batch)          → kind 'partial_stale' + counts
  * ReadGate full refusal (single edit)     → kind 'read_first' + paths
  * ReadGate partial skip (batch)           → kind 'partial_read_first' + counts
  * write_file content_ref resolution fail  → kind 'content_ref'

Plus the meta-builder badge fix: a batch-edit error WITHOUT the
"Applied/Inserted X/Y" header (hard failure) must badge 'failed', never
the gibberish '?/N edits'.
"""
from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """Fresh token store + finalize recorder for every test."""
    monkeypatch.delenv('TOFU_WRITE_FRESHNESS_GATE', raising=False)
    monkeypatch.delenv('TOFU_APPLY_DIFF_READ_GATE', raising=False)
    from lib import write_freshness
    write_freshness._reset_for_tests()
    import lib.tasks_pkg.handlers.project as hp
    finalized = []

    def _recorder(task, rn, round_entry, results, **kwargs):
        round_entry['results'] = results
        round_entry['status'] = 'done'
        finalized.append({'task': task.get('id'), 'rn': rn,
                          'results': results, 'round_entry': round_entry})

    monkeypatch.setattr(hp, '_finalize_tool_round', _recorder)
    yield {'finalized': finalized}
    write_freshness._reset_for_tests()


@pytest.fixture
def workspace(tmp_path):
    proj = tmp_path / 'proj'
    proj.mkdir()
    (proj / 'a.py').write_text('def foo():\n    return 1\n')
    (proj / 'b.py').write_text('def bar():\n    return 2\n')
    return str(proj)


def _task(conv_id):
    return {'id': f'task-{conv_id}', 'convId': conv_id,
            'messages': [], 'toolRounds': []}


def _drive(fn_name, fn_args, task, project_path):
    """Invoke the REAL project-tool handler exactly as the dispatcher does."""
    import json as _json
    from lib.tasks_pkg.handlers.project import _handle_project_tool
    out = _handle_project_tool(
        task, {}, fn_name, f'tc-{fn_name}-1', fn_args, 1,
        {'query': fn_name, 'toolCallId': f'tc-{fn_name}-1'},
        None, project_path, True)
    task['toolRounds'].append({
        'toolName': fn_name,
        'toolArgs': _json.dumps(fn_args),
        'status': 'done',
        'roundNum': 1,
    })
    return out


def _last_meta(_isolate, task_id):
    """The meta dict of the most recent finalized round for ``task_id``."""
    rows = [r for r in _isolate['finalized'] if r['task'] == task_id]
    assert rows, f'no finalized round recorded for {task_id}'
    return rows[-1]['results'][0]


@pytest.mark.unit
def test_full_stale_refusal_carries_structured_refusal(workspace, _isolate):
    a, b = _task('convA'), _task('convB')
    _drive('read_files', {'reads': [{'path': 'a.py'}]}, a, workspace)
    _drive('write_file', {'path': 'a.py', 'content': 'def foo():\n    return 2  # B\n'},
           b, workspace)
    tc_id, content, _ = _drive(
        'write_file', {'path': 'a.py', 'content': 'def foo():\n    return 3  # A\n'},
        a, workspace)
    assert content.startswith('Error: write_file refused'), content
    meta = _last_meta(_isolate, 'task-convA')
    assert meta['badge'] == 'stale'
    assert meta['refusal'] == {'kind': 'stale', 'paths': ['a.py']}


@pytest.mark.unit
def test_partial_stale_refusal_carries_counts(workspace, _isolate):
    a, b = _task('convA'), _task('convB')
    _drive('read_files', {'reads': [{'path': 'a.py'}, {'path': 'b.py'}]}, a, workspace)
    _drive('write_file', {'path': 'b.py', 'content': 'def bar():\n    return 20  # B\n'},
           b, workspace)
    tc_id, content, _ = _drive(
        'apply_diffs',
        {'edits': [
            {'path': 'a.py', 'search': 'return 1', 'replace': 'return 10'},
            {'path': 'b.py', 'search': 'return 2', 'replace': 'return 99'},
        ]},
        a, workspace)
    assert 'Write-freshness guard' in content
    meta = _last_meta(_isolate, 'task-convA')
    assert meta['badge'] == 'partial: stale'
    assert meta['refusal'] == {'kind': 'partial_stale', 'paths': ['b.py'],
                               'skipped': 1, 'proceeded': 1}


@pytest.mark.unit
def test_read_gate_full_refusal_carries_refusal(workspace, _isolate):
    a = _task('convA')
    tc_id, content, _ = _drive(
        'apply_diff', {'path': 'a.py', 'search': 'return 1', 'replace': 'return 2'},
        a, workspace)
    assert 'must read each target file first' in content
    meta = _last_meta(_isolate, 'task-convA')
    assert meta['badge'] == 'read first'
    assert meta['refusal'] == {'kind': 'read_first', 'paths': ['a.py']}


@pytest.mark.unit
def test_read_gate_partial_refusal_carries_counts(workspace, _isolate):
    a = _task('convA')
    _drive('read_files', {'reads': [{'path': 'a.py'}]}, a, workspace)
    tc_id, content, _ = _drive(
        'apply_diffs',
        {'edits': [
            {'path': 'a.py', 'search': 'return 1', 'replace': 'return 10'},
            {'path': 'b.py', 'search': 'return 2', 'replace': 'return 20'},
        ]},
        a, workspace)
    assert 'Read-before-edit gate' in content
    meta = _last_meta(_isolate, 'task-convA')
    assert meta['badge'] == 'partial: read first'
    assert meta['refusal'] == {'kind': 'partial_read_first', 'paths': ['b.py'],
                               'skipped': 1, 'proceeded': 1}


@pytest.mark.unit
def test_content_ref_failure_carries_refusal(workspace, _isolate):
    a = _task('convA')
    tc_id, content, _ = _drive(
        'write_file',
        {'path': 'a.py', 'content_ref': {'tool_round': 99}},
        a, workspace)
    assert 'content_ref resolution failed' in content
    meta = _last_meta(_isolate, 'task-convA')
    assert meta['badge'] == 'ref failed'
    assert meta['refusal'] == {'kind': 'content_ref'}


@pytest.mark.unit
def test_meta_badge_failed_when_no_batch_header():
    """A hard apply_diffs/insert_contents failure has no 'Applied X/Y'
    header — '?/N edits' on the red badge is gibberish; pin 'failed'."""
    from lib.tools.meta import build_project_tool_meta
    edits = {'edits': [
        {'path': 'a.py', 'search': 'x', 'replace': 'y'},
        {'path': 'b.py', 'search': 'x', 'replace': 'y'},
    ]}
    meta = build_project_tool_meta('apply_diffs', edits, 'Error: something exploded')
    assert meta['badge'] == 'failed'
    assert meta['writeOk'] is False
    meta = build_project_tool_meta('insert_contents', edits, 'Error: something exploded')
    assert meta['badge'] == 'failed'
    assert meta['writeOk'] is False
    # A real batch header keeps the counted badge.
    meta = build_project_tool_meta(
        'apply_diffs', edits,
        'Applied 1/2 edits (1 failed)\n[1] OK a.py: 1 lines changed\n[2] FAIL b.py: nope')
    assert meta['badge'] == '1/2 edits'
