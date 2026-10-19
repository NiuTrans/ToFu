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


pytestmark = pytest.mark.unit

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
        round_entry['status'] = kwargs.get('status', 'done')
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
            '_invocationStorageFree': True,
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
    round_entry = {'query': fn_name, 'toolCallId': f'tc-{fn_name}-1'}
    out = _handle_project_tool(
        task, {}, fn_name, f'tc-{fn_name}-1', fn_args, 1,
        round_entry, None, project_path, True)
    task['toolRounds'].append({
        **round_entry,
        'toolName': fn_name,
        'toolArgs': _json.dumps(fn_args),

        'result': out[1],
        'status': round_entry.get('status', 'done'),
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
    """NEUTER both checks: handler admission and locked preimage recheck.

    Removing only the handler no longer permits clobbering: the lock-bound
    check is deliberately independent to close the wait/acquire race.
    """
    import lib.tasks_pkg.handlers.project as hp
    monkeypatch.setattr(hp, 'check_write_freshness', lambda *a, **k: None)
    from lib import write_freshness
    monkeypatch.setattr(write_freshness, 'is_stale', lambda *a, **k: False)
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


@pytest.mark.unit
def test_handler_neutered_locked_preimage_check_still_preserves_other_writer(
        workspace, _isolate, monkeypatch):
    import lib.tasks_pkg.handlers.project as hp
    monkeypatch.setattr(hp, 'check_write_freshness', lambda *args, **kwargs: None)
    a, b = _task('convA'), _task('convB')
    _drive('read_files', {'reads': [{'path': 'a.py'}]}, a, workspace)
    _drive('write_file', {'path': 'a.py', 'content': 'def foo():\n    return 2  # B\n'},
           b, workspace)
    _tc_id, content, _ = _drive(
        'write_file', {'path': 'a.py', 'content': 'def foo():\n    return 3  # A\n'},
        a, workspace)
    assert content.startswith('Write failed:'), content
    assert 'observed content version differs' in content
    assert 're-read and reconcile' in content
    assert _read(workspace, 'a.py') == 'def foo():\n    return 2  # B\n'


@pytest.mark.unit
def test_refusal_then_retry_cannot_manufacture_read_first(workspace, _isolate):
    actor = _task('retry-actor')
    args = {'edits': [{'path': 'a.py', 'operation': 'replace',
                       'anchor': 'return 1', 'content': 'return 9'}]}
    for _ in range(2):
        _tc, content, _ = _drive('edit_file', args, actor, workspace)
        assert 'must read each target file first' in content
        assert 'return 1' in _read(workspace, 'a.py')
    from lib import write_freshness
    from lib.tasks_pkg.handlers._write_freshness_gate import _conv_key
    assert not write_freshness.has_token(_conv_key(actor), os.path.join(workspace, 'a.py'))


@pytest.mark.unit
def test_failed_edit_is_not_a_read_when_gate_reenabled(workspace, _isolate, monkeypatch):
    actor = _task('failed-actor')
    monkeypatch.setenv('TOFU_APPLY_DIFF_READ_GATE', '0')
    args = {'edits': [{'path': 'a.py', 'operation': 'replace',
                       'anchor': 'not in this file', 'content': 'guess'}]}
    _tc, content, _ = _drive('edit_file', args, actor, workspace)
    assert '[1] FAIL' in content
    monkeypatch.delenv('TOFU_APPLY_DIFF_READ_GATE')
    args['edits'][0]['anchor'] = 'return 1'
    _tc, content, _ = _drive('edit_file', args, actor, workspace)
    assert 'must read each target file first' in content
    assert 'return 1' in _read(workspace, 'a.py')


@pytest.mark.unit
@pytest.mark.parametrize('failure', ['binary', 'legacy_encoding', 'range'])
def test_mixed_read_failure_never_certifies_failed_item(workspace, _isolate, failure):
    actor = _task('mixed-actor')
    bad_spec = {'path': 'b.py'}
    if failure == 'range':
        bad_spec.update(start_line=999, end_line=1000)
    else:
        with open(os.path.join(workspace, 'b.py'), 'wb') as stream:
            stream.write(b'\x00unread' if failure == 'binary' else b'\xffunread')
    _drive('read_files', {'reads': [{'path': 'a.py'}, bad_spec]}, actor, workspace)
    from lib import write_freshness
    from lib.tasks_pkg.handlers._write_freshness_gate import _conv_key
    assert write_freshness.has_token(_conv_key(actor), os.path.join(workspace, 'a.py'))
    assert not write_freshness.has_token(_conv_key(actor), os.path.join(workspace, 'b.py'))
    _tc, content, _ = _drive('edit_file', {'edits': [{'path': 'b.py', 'operation': 'replace',
        'anchor': 'unread', 'content': 'guess'}]}, actor, workspace)
    assert 'must read each target file first' in content


@pytest.mark.unit
@pytest.mark.parametrize('streaming', [False, True])
def test_mutation_after_read_before_stamp_stays_stale(workspace, _isolate, monkeypatch, streaming):
    from lib.tasks_pkg.handlers import project, _write_freshness_gate
    from lib.tasks_pkg.streaming_tool_executor import StreamingToolAccumulator
    actor = _task('snapshot-race')
    seam = _write_freshness_gate if streaming else project
    real_record = seam.record_read_paths
    def mutate_then_record(*args, **kwargs):
        with open(os.path.join(workspace, 'a.py'), 'w') as target:
            target.write('unobserved sibling bytes\n')
        return real_record(*args, **kwargs)
    monkeypatch.setattr(seam, 'record_read_paths', mutate_then_record)
    if streaming:
        accumulator = StreamingToolAccumulator(actor, workspace, project_enabled=True)
        try:
            content = accumulator._execute_one('read_files', {'path': 'a.py'})
        finally:
            accumulator._pool.shutdown(wait=True)
    else:
        _tc, content, _ = _drive('read_files', {'path': 'a.py'}, actor, workspace)
    assert 'def foo()' in content and 'unobserved' not in content
    _tc, refusal, _ = _drive('write_file', {'path': 'a.py', 'content': 'overwrite'}, actor, workspace)
    assert 'changed on disk' in refusal
    assert _read(workspace, 'a.py') == 'unobserved sibling bytes\n'


@pytest.mark.unit
def test_preexecuted_read_does_not_restamp_a_later_disk_version(workspace, _isolate):
    from lib import write_freshness
    from lib.tasks_pkg.handlers._write_freshness_gate import record_read_paths
    from lib.tasks_pkg.streaming_tool_executor import StreamingToolAccumulator
    actor = _task('preexecuted')
    accumulator = StreamingToolAccumulator(actor, workspace, project_enabled=True)
    try:
        observed = accumulator._execute_one('read_files', {'path': 'a.py'})
    finally:
        accumulator._pool.shutdown(wait=True)
    with open(os.path.join(workspace, 'a.py'), 'w') as target:
        target.write('unseen revision\n')
    # Replayed display text has no new observation carrier. In particular it
    # must not reinterpret the current path as the already-prefetched version.
    assert record_read_paths(actor, {'path': 'a.py'}, workspace, observed) == 0
    assert 'def foo()' in observed
    from lib.tasks_pkg.handlers._write_freshness_gate import _conv_key
    assert write_freshness.is_stale(_conv_key(actor), os.path.join(workspace, 'a.py'))


@pytest.mark.unit
@pytest.mark.parametrize('tool,atomic', [('edit_file', None), ('edit_file', True), ('apply_diffs', True)])
@pytest.mark.parametrize('blocked', ['unread', 'stale'])
def test_atomic_handler_refuses_original_whole_batch(workspace, _isolate, tool, atomic, blocked):
    import copy
    actor = _task('whole-batch')
    paths = ['a.py'] if blocked == 'unread' else ['a.py', 'b.py']
    _drive('read_files', {'reads': [{'path': path} for path in paths]}, actor, workspace)
    if blocked == 'stale':
        with open(os.path.join(workspace, 'b.py'), 'w') as target:
            target.write('sibling owns this revision\n')
    edits = [{'path': 'a.py', 'search': 'return 1', 'replace': 'return 10'},
             {'path': 'b.py', 'search': 'return 2', 'replace': 'return 20'}]
    if tool == 'edit_file':
        edits = [{'path': item['path'], 'operation': 'replace', 'anchor': item['search'],
                  'content': item['replace']} for item in edits]
    args = {'edits': edits}
    if atomic is not None:
        args['atomic'] = atomic
    original = copy.deepcopy(args)
    before = [_read(workspace, path) for path in ('a.py', 'b.py')]
    _tc, refusal, _ = _drive(tool, args, actor, workspace)
    assert 'refused' in refusal
    assert [_read(workspace, path) for path in ('a.py', 'b.py')] == before
    assert args == original


@pytest.mark.unit
@pytest.mark.parametrize('execution_fails', [False, True])
def test_nonatomic_keeps_original_indexes_args_and_honest_status(workspace, _isolate, execution_fails):
    import copy
    actor = _task('partial-batch')
    _drive('read_files', {'path': 'a.py'}, actor, workspace)
    args = {'atomic': False, 'edits': [
        {'path': 'b.py', 'operation': 'replace', 'anchor': 'return 2', 'content': 'return 20'},
        {'path': 'a.py', 'operation': 'replace',
         'anchor': 'missing' if execution_fails else 'return 1', 'content': 'return 10'}]}
    original = copy.deepcopy(args)
    _tc, content, _ = _drive('edit_file', args, actor, workspace)
    assert args == original
    assert '[1] SKIP b.py' in content
    assert f'[2] {"FAIL" if execution_fails else "OK"}' in content
    assert f'Applied {0 if execution_fails else 1}/2 edits' in content
    assert 'were applied' not in content
    assert _read(workspace, 'b.py') == 'def bar():\n    return 2\n'


@pytest.mark.parametrize('gate', ['partition_batch_edits', 'partition_stale_edits'])
@pytest.mark.parametrize('tool,atomic', [('edit_file', None), ('edit_file', True),
                                        ('apply_diffs', True)])
def test_atomic_batch_refuses_when_gate_check_raises(tmp_path, monkeypatch, gate, tool, atomic):
    import copy
    import lib.project_mod as project_tools
    import lib.tasks_pkg.handlers.project as handler

    root = str(tmp_path)
    path = tmp_path / 'file.txt'
    path.write_text('before\n')
    task = _task(f'atomic-check-{gate}-{tool}-{atomic}')
    _drive('read_files', {'path': 'file.txt'}, task, root)
    edit = ({'path': 'file.txt', 'operation': 'replace', 'anchor': 'before', 'content': 'after'}
            if tool == 'edit_file' else
            {'path': 'file.txt', 'search': 'before', 'replace': 'after'})
    args = {'edits': [edit]}
    if atomic is not None:
        args['atomic'] = atomic
    original_args = copy.deepcopy(args)
    invoked = []
    original_execute = project_tools.execute_tool

    def unavailable(*_args, **_kwargs):
        raise RuntimeError('injected gate failure')

    def execute(*call_args, **call_kwargs):
        invoked.append(call_args)
        return original_execute(*call_args, **call_kwargs)

    monkeypatch.setattr(handler, gate, unavailable)
    monkeypatch.setattr(project_tools, 'execute_tool', execute)
    entry = {}
    _, result, failed = handler._handle_project_tool(task, {}, tool, 'blocked-check', args,
                                                    2, entry, None, root, True)
    assert failed and entry['status'] == 'error'
    assert 'safety check unavailable' in result
    assert not invoked and path.read_text() == 'before\n'
    assert args == original_args
