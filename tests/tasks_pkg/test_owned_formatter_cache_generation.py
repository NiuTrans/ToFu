"""Owned completion fences read bytes independently of freshness-token CAS."""
import os
import subprocess
import threading
import time
import uuid

import pytest

from lib import write_freshness as freshness
from lib.project_mod import run_command, tools
from lib.tasks_pkg.streaming_tool_executor import StreamingToolAccumulator
from lib.tasks_pkg.tool_dispatch import _flags as flags
from lib.tasks_pkg.workspace_delivery import execution_root_key

pytestmark = pytest.mark.unit


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / 'checkout'
    root.mkdir()
    subprocess.run(['git', 'init', '-q', str(root)], check=True)
    (root / 'a.py').write_text('x=1\n')
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    formatter = bindir / 'ruff'
    formatter.write_text('''#!/usr/bin/env python3
import os
from pathlib import Path
Path('a.py').write_text('x = 1\\n')
raise SystemExit(3 if os.environ.get('FORMAT_FAIL') else 0)
''')
    formatter.chmod(0o755)
    monkeypatch.setenv('PATH', str(bindir) + os.pathsep + os.environ['PATH'])
    monkeypatch.setenv('TOFU_STICKY_CWD', '0')
    monkeypatch.setenv('TOFU_WORKSPACE_TOOL_WAIT_SECONDS', '0')
    return root


def task():
    return {'id': 'cache-' + uuid.uuid4().hex, 'convId': 'not-the-root-key',
            'aborted': False, 'events': [], 'events_lock': threading.Lock(),
            'toolRounds': [], '_transientRuntime': True, '_userId': 1}


def prefetch(owner, project, *, call_id='prefetch'):
    acc = StreamingToolAccumulator(owner, str(project), project_enabled=True)
    args = {'path': 'a.py'}
    future = acc._pool.submit(acc._execute_one_for_call, 'read_files', args, call_id)
    acc._futures[call_id] = (future, 'read_files', args, time.time())
    future.result(timeout=10)
    return acc


def drive(owner, project, monkeypatch):
    from lib.tasks_pkg.tool_dispatch import _pipeline as pipeline
    monkeypatch.setattr(pipeline, '_finalize_tool_round', lambda *a, **k: None)
    call_id = uuid.uuid4().hex
    parsed = [({'id': call_id}, 'read_files', call_id, {'path': 'a.py'}, 1,
               {'query': 'read_files', 'toolCallId': call_id}, None)]
    messages = []
    pipeline.execute_tool_pipeline(owner, parsed, {}, str(project), True,
                                   None, messages, [], 1, 'test-model')
    return '\n'.join(str(m.get('content')) for m in messages)


def format_project(owner, project):
    return tools.execute_tool('run_command', {'command': 'ruff format a.py'},
                              str(project), task=owner)


def test_actual_detached_background_completion_bypasses_prefetched_cache(
        project, monkeypatch):
    from lib.tasks_pkg import background_commands as background

    from lib.tasks_pkg.handlers import _background_command
    monkeypatch.setattr(_background_command, '_queue_completion', lambda **k: True)
    from lib.tasks_pkg.handlers._write_freshness_gate import cached_read_is_stale
    monkeypatch.setattr(background, '_records_root', lambda: str(project.parent / 'records'))
    owner = task()
    acc = prefetch(owner, project)
    assert acc.inject_into_cache(owner) == 1
    key = flags._make_cache_key('read_files', {'path': 'a.py'})
    old_receipt = owner['_tool_result_cache'][key]
    original_generation = freshness.project_read_cache_generation()
    worker_has_no_cache = []

    def execute(worker, context):
        worker_has_no_cache.append('_tool_result_cache' not in worker)
        return format_project(worker, project)

    command_id = background.start_background_command(
        task=owner, command='ruff format a.py', execute=execute)
    info = background.check_background_command(command_id, user_id=1, wait_s=10)
    assert info['status'] == 'completed'
    assert worker_has_no_cache == [True]
    assert freshness.project_read_cache_generation() is not original_generation
    # This is the original bug: CAS advanced the token, so disk/token agree
    # although this receipt still contains x=1. The independent fence rejects it.
    assert not cached_read_is_stale(owner, {'path': 'a.py'}, str(project))
    assert owner['_tool_result_cache'][key] is old_receipt
    assert not flags._project_cache_entry_is_current('read_files', old_receipt)
    assert 'x = 1' in drive(owner, project, monkeypatch)
    current = owner['_tool_result_cache'][key]
    assert flags._project_cache_entry_is_current('read_files', current)
    assert 'x = 1' in drive(owner, project, monkeypatch)


@pytest.mark.parametrize('wait_during_injection', [False, True])
def test_inflight_prefetch_crosses_completion_then_live_read_caches(
        project, monkeypatch, wait_during_injection):
    owner = task()
    acc = StreamingToolAccumulator(owner, str(project), project_enabled=True)
    read_done, release = threading.Event(), threading.Event()
    original = acc._execute_one

    def delayed(name, args):
        content = original(name, args)  # actual old read before completion
        read_done.set()
        assert release.wait(10)
        return content

    monkeypatch.setattr(acc, '_execute_one', delayed)
    args = {'path': 'a.py'}
    future = acc._pool.submit(acc._execute_one_for_call, 'read_files', args, 'racing')
    acc._futures['racing'] = (future, 'read_files', args, time.time())
    assert read_done.wait(10)
    try:
        assert '[exit code: 0]' in format_project(owner, project)
    finally:
        if not wait_during_injection:
            release.set()
            future.result(timeout=10)
    if wait_during_injection:
        # Deliberately enter the pending-future collection path.
        timer = threading.Timer(.05, release.set)
        timer.start()
    try:
        assert acc.inject_into_cache(owner) == 0
    finally:
        release.set()
        if wait_during_injection:
            timer.join()
    assert flags._make_cache_key('read_files', args) not in owner['_tool_result_cache']
    assert 'x = 1' in drive(owner, project, monkeypatch)
    assert flags._project_cache_entry_is_current(
        'read_files', owner['_tool_result_cache'][flags._make_cache_key('read_files', args)])


@pytest.mark.parametrize('token', ['fresh', 'missing', 'stale'])
@pytest.mark.parametrize('failed_exit', [False, True])
def test_completion_fences_even_missing_stale_token_or_partial_failure(
        project, monkeypatch, token, failed_exit):
    owner = task()
    key = execution_root_key(owner)
    path = str(project / 'a.py')
    if token != 'missing':
        freshness.record(key, path)
    if token == 'stale':
        (project / 'a.py').write_text('y=2\n')
    if failed_exit:
        monkeypatch.setenv('FORMAT_FAIL', '1')
    before = freshness.project_read_cache_generation()
    output = format_project(owner, project)
    assert f'[exit code: {3 if failed_exit else 0}]' in output
    assert freshness.project_read_cache_generation() is not before
    if token == 'missing':
        assert not freshness.has_token(key, path)
    elif token == 'stale':
        assert freshness.is_stale(key, path)


@pytest.mark.parametrize('boundary', ['launch', 'incomplete', 'cancelled'])
def test_no_generation_mutation_before_proven_supervised_completion(
        project, monkeypatch, boundary):
    owner = task()
    before = freshness.project_read_cache_generation()
    if boundary == 'launch':
        def fail(*a, **k):
            raise FileNotFoundError('injected launch failure')
        monkeypatch.setattr(run_command.subprocess, 'Popen', fail)
        assert 'Error starting command' in format_project(owner, project)
    else:
        def incomplete(base, command, task=None):
            if boundary == 'cancelled':
                raise RuntimeError('cancelled before completion')
            return 'incomplete execution'
        wrapped = run_command._with_command_writer(incomplete)
        if boundary == 'cancelled':
            with pytest.raises(RuntimeError, match='cancelled'):
                wrapped(str(project), 'ruff format a.py', task=owner)
        else:
            wrapped(str(project), 'ruff format a.py', task=owner)
    assert freshness.project_read_cache_generation() is before


def test_receipt_projection_fifo_and_external_cache_scope(monkeypatch):
    owner = task()
    monkeypatch.setattr(flags, '_resolve_tool_result_cache_capacity', lambda: 32)
    generation = freshness.project_read_cache_generation()
    projection = [{'path': 'a.py'}]
    receipt = ('old', False, 'prefetch', None, None, None, None, projection)
    for i in range(40):
        flags._store_tool_result_cache_entry(owner, f'read_files::{i}', receipt,
                                            generation=generation)
    cache = owner['_tool_result_cache']
    assert len(cache) == 32
    assert list(cache)[0] == 'read_files::8'
    assert cache['read_files::39'][0:3] == receipt[0:3]
    assert flags._cache_entry_projection_items(cache['read_files::39']) is projection
    flags._store_tool_result_cache_entry(owner, 'web_search::{}', ('external', True, 'dedup'))
    freshness.advance_project_read_cache_generation()
    assert flags._project_cache_entry_is_current('web_search', cache['web_search::{}'])
    flags._store_tool_result_cache_entry(owner, 'read_files::late', receipt,
                                        generation=generation)
    assert 'read_files::late' not in cache
    assert len(cache) == 32
    assert not flags._project_cache_entry_is_current('read_files', cache['read_files::39'])
    # Legacy manually-created tuples are adopted once, not rebound at every hit.
    cache['read_files::manual'] = ('manual', False, 'dedup')
    flags._ensure_tool_result_cache(owner)
    manual = cache['read_files::manual']
    assert isinstance(manual, tuple)
    freshness.advance_project_read_cache_generation()
    flags._ensure_tool_result_cache(owner)
    assert cache['read_files::manual'] is manual
    assert not flags._project_cache_entry_is_current('read_files', manual)


def test_normal_parallel_read_late_insertion_is_rejected(project, monkeypatch):
    from lib.tasks_pkg.tool_dispatch import _pipeline as pipeline
    owner = task()
    read_done, release = threading.Event(), threading.Event()
    original = pipeline._execute_tool_one_in_pool
    outcome = []

    def delayed(*a, **k):
        result = original(*a, **k)
        read_done.set()
        assert release.wait(10)
        return result

    monkeypatch.setattr(pipeline, '_execute_tool_one_in_pool', delayed)
    worker = threading.Thread(target=lambda: outcome.append(drive(owner, project, monkeypatch)))
    worker.start()
    assert read_done.wait(10)
    try:
        format_project(owner, project)
    finally:
        release.set()
        worker.join(10)
    assert not worker.is_alive()
    assert outcome and 'x=1' in outcome[0]
    key = flags._make_cache_key('read_files', {'path': 'a.py'})
    assert key not in owner['_tool_result_cache']
    monkeypatch.setattr(pipeline, '_execute_tool_one_in_pool', original)
    assert 'x = 1' in drive(owner, project, monkeypatch)
    assert flags._project_cache_entry_is_current('read_files', owner['_tool_result_cache'][key])


def test_admission_failure_does_not_change_generation(project):
    from lib.project_mod.workspace_lease import workspace_write_lease
    owner = task()
    ready, release = threading.Event(), threading.Event()

    def hold():
        with workspace_write_lease(str(project)):
            ready.set()
            assert release.wait(10)

    worker = threading.Thread(target=hold)
    worker.start()
    assert ready.wait(10)
    before = freshness.project_read_cache_generation()
    try:
        assert 'workspace-busy' in format_project(owner, project)
        assert freshness.project_read_cache_generation() is before
        assert (project / 'a.py').read_text() == 'x=1\n'
    finally:
        release.set()
        worker.join(10)


def test_completion_generation_advances_once_before_reconciliation(project, monkeypatch):
    owner = task()
    freshness.record(execution_root_key(owner), str(project / 'a.py'))
    before = freshness.project_read_cache_generation()
    advance = freshness.advance_project_read_cache_generation
    reconcile = freshness.reconcile_owned_write
    calls = []

    def advanced():
        calls.append('advance')
        advance()

    def reconciled(evidence):
        assert freshness.project_read_cache_generation() is not before
        calls.append('reconcile')
        return reconcile(evidence)

    monkeypatch.setattr(freshness, 'advance_project_read_cache_generation', advanced)
    monkeypatch.setattr(freshness, 'reconcile_owned_write', reconciled)
    format_project(owner, project)
    assert calls == ['advance', 'reconcile']


def test_cancel_before_admission_does_not_advance(project):
    owner = task()
    owner['aborted'] = True
    before = freshness.project_read_cache_generation()
    spawned = []
    result = tools.execute_tool('run_command', {'command': 'ruff format a.py'},
        str(project), task=owner, on_spawn=lambda *args: spawned.append(args))
    assert '[exit code: -1]' in result
    assert spawned == []
    assert freshness.project_read_cache_generation() is before
    assert (project / 'a.py').read_text() == 'x=1\n'


def test_cancelled_spawned_formatter_fences_after_supervised_retirement(project):
    owner = task()
    formatter = project.parent / 'bin' / 'ruff'
    formatter.write_text('''#!/usr/bin/env python3
from pathlib import Path
import time
Path('a.py').write_text('x = 1\\n')
print('formatter wrote', flush=True)
time.sleep(10)
''')
    before = freshness.project_read_cache_generation()
    saw_output = []

    def stop_after_write(stream, chunk):
        if 'formatter wrote' in chunk:
            saw_output.append(chunk)
            owner['aborted'] = True

    output = run_command.tool_run_command(str(project), 'ruff format a.py',
                                           task=owner, on_chunk=stop_after_write)
    assert saw_output
    assert 'abort' in output.lower()
    assert freshness.project_read_cache_generation() is not before
    assert '_subprocess_pid' not in owner
    assert (project / 'a.py').read_text() == 'x = 1\n'
