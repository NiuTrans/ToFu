"""Handler-level end-to-end tests for the write-freshness guard.

The unit suite (test_write_freshness_gate.py) drives
``check_write_freshness`` / ``record_read_paths`` / ``tool_write_file``
DIRECTLY. If the WIRING inside ``_handle_project_tool`` silently dies (the
check call removed, the read-record seam dropped, the refusal early-return
lost), every one of those tests stays green while production clobbers
siblings. This suite closes that hole by driving the REAL
``lib.tasks_pkg.handlers.project._handle_project_tool`` through the full
loop:

    conv A: read_files  → token recorded
    conv B: write_file  → allowed (B holds no token), disk changed
    conv A: write_file  → REFUSED ('stale' badge), B's bytes intact
    conv A: read_files  → token refreshed
    conv A: write_file  → now succeeds

``_finalize_tool_round`` is the documented monkeypatch target (see
lib/tasks_pkg/executor/_finalize.py) — replaced with a recorder so the
suite needs no manager/DB/WebSocket infra; the freshness wiring itself
(check call, partition, read-record, refusal early-return) stays fully
real. The NEUTER test amputates the handler's check seam and proves the
refusal above was driven by it (the clobber returns).
"""
from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """Fresh token store + finalize recorder for every test."""
    monkeypatch.delenv('TOFU_WRITE_FRESHNESS_GATE', raising=False)
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
    """Invoke the REAL project-tool handler exactly as the dispatcher does.

    Also appends the completed round to ``task['toolRounds']`` — the real
    orchestrator does this after every call, and the read-before-edit
    gate's satisfied-set is fed from exactly this history shape (a refused
    or skipped call records a round too, status 'done').
    """
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


def _read(proj, rel):
    with open(os.path.join(proj, rel), encoding='utf-8') as f:
        return f.read()


@pytest.mark.unit
def test_full_loop_read_stamp_sibling_write_refused(workspace, _isolate):
    """THE wiring loop: A reads (stamp), B writes through the SAME handler,
    A's write_file is refused and B's bytes survive."""
    a, b = _task('convA'), _task('convB')
    # A reads via the real handler → token recorded under convA.
    tc_id, content, _ = _drive('read_files', {'reads': [{'path': 'a.py'}]}, a, workspace)
    assert 'def foo()' in content and 'Error' not in content.split('\n')[0]
    # B writes via the real handler → allowed (B holds no token).
    tc_id, content, _ = _drive(
        'write_file', {'path': 'a.py', 'content': 'def foo():\n    return 2  # B\n'},
        b, workspace)
    assert not content.startswith('Error:'), content
    assert _read(workspace, 'a.py') == 'def foo():\n    return 2  # B\n'
    # A now writes from its stale memory → REFUSED; B's bytes intact.
    tc_id, content, _ = _drive(
        'write_file', {'path': 'a.py', 'content': 'def foo():\n    return 3  # A\n'},
        a, workspace)
    assert content.startswith('Error: write_file refused'), content
    assert 'changed on disk' in content and 'a.py' in content
    assert _read(workspace, 'a.py') == 'def foo():\n    return 2  # B\n'
    # The refusal rode the wire with the 'stale' badge.
    badges = [r['results'][0].get('badge') for r in _isolate['finalized']
              if r['task'] == 'task-convA']
    assert 'stale' in badges


@pytest.mark.unit
def test_reread_refreshes_then_write_succeeds(workspace, _isolate):
    """The recovery half of the loop: after the refusal, re-reading through
    the handler re-stamps and the SAME write then succeeds."""
    a, b = _task('convA'), _task('convB')
    _drive('read_files', {'reads': [{'path': 'a.py'}]}, a, workspace)
    _drive('write_file', {'path': 'a.py', 'content': 'def foo():\n    return 2  # B\n'},
           b, workspace)
    tc_id, content, _ = _drive(
        'write_file', {'path': 'a.py', 'content': 'def foo():\n    return 3  # A\n'},
        a, workspace)
    assert content.startswith('Error:'), content  # refused, as above
    # A heeds the message: re-read, then re-issue → succeeds.
    tc_id, content, _ = _drive('read_files', {'reads': [{'path': 'a.py'}]}, a, workspace)
    assert 'return 2  # B' in content  # A now saw B's content
    tc_id, content, _ = _drive(
        'write_file', {'path': 'a.py', 'content': 'def foo():\n    return 3  # A\n'},
        a, workspace)
    assert not content.startswith('Error:'), content
    assert _read(workspace, 'a.py') == 'def foo():\n    return 3  # A\n'


@pytest.mark.unit
def test_batch_apply_diffs_skips_only_the_stale_edit(workspace, _isolate):
    """Batch path through the handler: only the stale-target edit is
    skipped; the fresh one applies; the note names the skipped file."""
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
    # Fresh edit applied, stale edit skipped, note names b.py.
    assert 'Write-freshness guard' in content and 'b.py' in content
    assert _read(workspace, 'a.py') == 'def foo():\n    return 10\n'
    assert _read(workspace, 'b.py') == 'def bar():\n    return 20  # B\n'
    badges = [r['results'][0].get('badge') for r in _isolate['finalized']
              if r['task'] == 'task-convA']
    assert 'partial: stale' in badges


@pytest.mark.unit
def test_neuter_amputated_handler_check_clobbers(workspace, _isolate, monkeypatch):
    """NEUTER: amputate the handler's freshness-check seam → the identical
    stale write now CLOBBERS B's change. Proves the refusals above are
    driven by the wired check, not by incidental state."""
    import lib.tasks_pkg.handlers.project as hp
    monkeypatch.setattr(hp, 'check_write_freshness', lambda *a, **k: None)
    a, b = _task('convA'), _task('convB')
    _drive('read_files', {'reads': [{'path': 'a.py'}]}, a, workspace)
    _drive('write_file', {'path': 'a.py', 'content': 'def foo():\n    return 2  # B\n'},
           b, workspace)
    tc_id, content, _ = _drive(
        'write_file', {'path': 'a.py', 'content': 'def foo():\n    return 3  # A\n'},
        a, workspace)
    assert not content.startswith('Error:'), content  # no refusal anymore
    assert _read(workspace, 'a.py') == 'def foo():\n    return 3  # A\n'  # B clobbered


@pytest.mark.unit
def test_streaming_preexec_read_stamps_and_recovers(workspace, _isolate):
    """The PRODUCTION read path: streaming pre-exec bypasses _handle_project_tool
    (its result is cached as authoritative, serial dispatch skips re-execution),
    so the handler's record_read_paths never runs for streamed reads. The
    streaming executor must stamp the freshness token itself — otherwise the
    recovery loop (refused → re-read → re-issue) can NEVER succeed, the exact
    2026-07-25 production refusal loop (repeated 'stale' after every re-read).

    Drives the REAL StreamingToolAccumulator._execute_one for the read and the
    REAL _handle_project_tool for the writes (B's sibling write + A's attempts).
    """
    from lib.tasks_pkg.streaming_tool_executor import StreamingToolAccumulator
    a, b = _task('convA'), _task('convB')
    # A reads via the STREAMING pre-exec path (production's dominant read path).
    acc = StreamingToolAccumulator(a, workspace, project_enabled=True)
    try:
        content = acc._execute_one('read_files', {'reads': [{'path': 'a.py'}]})
    finally:
        acc._pool.shutdown(wait=False)
    assert 'def foo()' in content
    # B writes via the serial handler → allowed; disk changed.
    tc_id, content, _ = _drive(
        'write_file', {'path': 'a.py', 'content': 'def foo():\n    return 2  # B\n'},
        b, workspace)
    assert not content.startswith('Error:'), content
    # A writes → REFUSED (its streaming-read token went stale).
    tc_id, content, _ = _drive(
        'write_file', {'path': 'a.py', 'content': 'def foo():\n    return 3  # A\n'},
        a, workspace)
    assert content.startswith('Error: write_file refused'), content
    # A re-reads via the STREAMING path → token refreshed…
    acc2 = StreamingToolAccumulator(a, workspace, project_enabled=True)
    try:
        content = acc2._execute_one('read_files', {'reads': [{'path': 'a.py'}]})
    finally:
        acc2._pool.shutdown(wait=False)
    assert 'return 2  # B' in content
    # …and the re-issued write now SUCCEEDS — the recovery loop closes.
    tc_id, content, _ = _drive(
        'write_file', {'path': 'a.py', 'content': 'def foo():\n    return 3  # A\n'},
        a, workspace)
    assert not content.startswith('Error:'), content
    assert _read(workspace, 'a.py') == 'def foo():\n    return 3  # A\n'


@pytest.mark.unit
def test_neuter_streaming_preexec_record_never_recovers(workspace, _isolate, monkeypatch):
    """NEUTER: no-op the record seam the streaming pre-exec calls → a task
    holding a (write-side) token is refused after a sibling's change, its
    streaming re-read refreshes NOTHING, and the re-issued write is refused
    AGAIN — the recovery loop never closes. Proves the streaming-side stamp
    is load-bearing, not decoration."""
    import lib.tasks_pkg.handlers._write_freshness_gate as wfg
    monkeypatch.setattr(wfg, 'record_read_paths', lambda *a, **k: 0)
    from lib.tasks_pkg.streaming_tool_executor import StreamingToolAccumulator
    a, b = _task('convA'), _task('convB')
    # A writes first via the handler → write-side recording stamps a token.
    tc_id, content, _ = _drive(
        'write_file', {'path': 'a.py', 'content': 'def foo():\n    return 10  # A\n'},
        a, workspace)
    assert not content.startswith('Error:'), content
    # B changes the file → A's token is now stale.
    _drive('write_file', {'path': 'a.py', 'content': 'def foo():\n    return 2  # B\n'},
           b, workspace)
    tc_id, content, _ = _drive(
        'write_file', {'path': 'a.py', 'content': 'def foo():\n    return 3  # A\n'},
        a, workspace)
    assert content.startswith('Error: write_file refused'), content
    # A heeds the message: re-read via the STREAMING path — but with the
    # record seam amputated, the re-read refreshes nothing…
    acc = StreamingToolAccumulator(a, workspace, project_enabled=True)
    try:
        acc._execute_one('read_files', {'reads': [{'path': 'a.py'}]})
    finally:
        acc._pool.shutdown(wait=False)
    # …so the re-issued write is refused AGAIN — the loop never closes.
    tc_id, content, _ = _drive(
        'write_file', {'path': 'a.py', 'content': 'def foo():\n    return 3  # A\n'},
        a, workspace)
    assert content.startswith('Error: write_file refused'), content
    assert _read(workspace, 'a.py') == 'def foo():\n    return 2  # B\n'


@pytest.mark.unit
def test_neuter_amputated_read_record_leaves_A_blind(workspace, _isolate, monkeypatch):
    """NEUTER #2: amputate the post-read recording seam → A never gets a
    token → the same stale write passes silently. Proves the read-side
    wiring is load-bearing for the guard."""
    import lib.tasks_pkg.handlers.project as hp
    monkeypatch.setattr(hp, 'record_read_paths', lambda *a, **k: 0)
    a, b = _task('convA'), _task('convB')
    _drive('read_files', {'reads': [{'path': 'a.py'}]}, a, workspace)
    _drive('write_file', {'path': 'a.py', 'content': 'def foo():\n    return 2  # B\n'},
           b, workspace)
    tc_id, content, _ = _drive(
        'write_file', {'path': 'a.py', 'content': 'def foo():\n    return 3  # A\n'},
        a, workspace)
    assert not content.startswith('Error:'), content  # A was blind
    assert _read(workspace, 'a.py') == 'def foo():\n    return 3  # A\n'
