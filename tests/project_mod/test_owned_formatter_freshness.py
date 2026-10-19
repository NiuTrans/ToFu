"""Owned formatter observations follow admitted, supervised worker lifetimes."""
import os
import subprocess
import threading
import uuid

import pytest

from lib import write_freshness as freshness
from lib.project_mod import run_command, tools
from lib.project_mod.tool_admission import RecordedCommandSource
from lib.project_mod.workspace_lease import workspace_lease_fds
from lib.tasks_pkg.workspace_delivery import execution_root_key

pytestmark = pytest.mark.unit


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / 'checkout'
    root.mkdir()
    subprocess.run(['git', 'init', '-q', str(root)], check=True)
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    ruff = bindir / 'ruff'
    ruff.write_text('''#!/usr/bin/env python3
import os, sys, time
args = sys.argv[1:]
if '--check' in args or '--diff' in args or (args[0] == 'check' and '--fix' not in args):
    raise SystemExit(0)
files = [a for a in args[1:] if not a.startswith('-')]
if os.environ.get('FORMATTER_CHILD') == '1':
    if os.fork():
        raise SystemExit(0)
    os.close(1); os.close(2)
    time.sleep(.15)
for name in files:
    st = os.stat(name)
    with open(name, 'wb') as f:
        f.write(b'new\\n')
    os.utime(name, ns=(st.st_atime_ns, st.st_mtime_ns))
    if os.environ.get('FORMATTER_FAIL') == '1':
        raise SystemExit(3)
''')
    ruff.chmod(0o755)

    pytest_command = bindir / 'pytest'
    pytest_command.write_text('#!/bin/sh\nprintf "synthetic test runner\\n"\n')
    pytest_command.chmod(0o755)
    monkeypatch.setenv('PATH', str(bindir) + os.pathsep + os.environ['PATH'])
    monkeypatch.setenv('TOFU_STICKY_CWD', '0')
    monkeypatch.setenv('TOFU_WORKSPACE_TOOL_WAIT_SECONDS', '0')
    for name in ('a.py', 'b.py', 'unrelated.py'):
        (root / name).write_bytes(b'old\n')
    return root


def task():
    return {'id': 'formatter-' + uuid.uuid4().hex, 'convId': 'not-the-task-key'}


@pytest.mark.parametrize('command', [
    'ruff format a.py b.py && ruff check a.py && pytest --version',
    'ruff check --fix a.py b.py && ruff check a.py',
])
def test_literal_file_claims(project, command):
    assert run_command.command_write_lease_claims(str(project), command) == tuple(
        ('file', str(project / name)) for name in ('a.py', 'b.py'))


@pytest.mark.parametrize('command', ['ruff format .', 'ruff format',
    'ruff format *.py', 'ruff format "$TARGET"', 'ruff check --fix .',
    'ruff format --unknown a.py', 'ruff format missing.py'])
def test_ambiguous_formatter_requires_tree(project, command):
    assert ('tree', str(project)) in run_command.command_write_lease_claims(str(project), command)


@pytest.mark.parametrize('command', ['ruff check --no-fix a.py', 'ruff format --check a.py',
    'ruff format --diff a.py', 'ruff check --fix --diff a.py'])
def test_report_only_has_no_ownership(project, command):
    assert not run_command.command_write_lease_claims(str(project), command)


def test_shell_cwd_and_quoted_target(project):
    directory = project / 'nested'
    directory.mkdir()
    (directory / 'with space.py').write_bytes(b'old\n')
    command = "cd nested && ruff format 'with space.py' && ruff check --no-fix ."
    assert run_command.command_write_lease_claims(str(project), command) == (
        ('file', str(directory / 'with space.py')),)


@pytest.mark.parametrize('mode', ['foreground', 'partial', 'descendant'])
def test_completion_only_advances_matched_task_tokens(project, monkeypatch, mode):
    owner = task()
    key = execution_root_key(owner)
    paths = {name: str(project / name) for name in ('a.py', 'b.py', 'unrelated.py')}
    for path in paths.values():
        freshness.record(key, path)
        freshness.record('sibling-' + key, path)
    freshness.record(owner['convId'], paths['a.py'])
    (project / 'b.py').write_bytes(b'bad\n')  # Pre-stale, explicitly formatted.
    before = {path: freshness._tokens[(key, path)] for path in paths.values()}
    if mode == 'partial':
        monkeypatch.setenv('FORMATTER_FAIL', '1')
    if mode == 'descendant':
        monkeypatch.setenv('FORMATTER_CHILD', '1')
    captures, reconciles = [], []
    capture, reconcile = freshness.capture_owned_write, freshness.reconcile_owned_write

    def capture_held(identity, path):
        assert workspace_lease_fds()
        captures.append((identity, path))
        return capture(identity, path)

    def reconcile_held(evidence):
        assert workspace_lease_fds()
        reconciles.append(evidence)
        return reconcile(evidence)

    monkeypatch.setattr(freshness, 'capture_owned_write', capture_held)
    monkeypatch.setattr(freshness, 'reconcile_owned_write', reconcile_held)
    command = 'ruff format a.py b.py && ruff check a.py && pytest --version'
    output = tools.execute_tool('run_command', {'command': command}, str(project), task=owner)
    assert ('[exit code: 3]' if mode == 'partial' else '[exit code: 0]') in output
    assert (project / 'a.py').read_bytes() == b'new\n'
    assert not freshness.is_stale(key, paths['a.py'])
    assert freshness.is_stale('sibling-' + key, paths['a.py'])
    assert freshness.is_stale(owner['convId'], paths['a.py'])
    assert freshness.is_stale(key, paths['b.py'])
    for name in ('b.py', 'unrelated.py'):
        assert freshness._tokens[(key, paths[name])] is before[paths[name]]
    assert captures == [(key, paths['a.py']), (key, paths['b.py'])]
    assert len(reconciles) == 1  # No duplicate outer/runner observation.
    assert 'old\n' not in output and 'bad\n' not in output


def test_launch_failure_does_not_reconcile(project, monkeypatch):
    owner = task()
    path = str(project / 'a.py')
    key = execution_root_key(owner)
    freshness.record(key, path)
    before = freshness._tokens[(key, path)]

    def fail(*args, **kwargs):
        raise FileNotFoundError('injected launch failure')

    monkeypatch.setattr(run_command.subprocess, 'Popen', fail)
    result = tools.execute_tool('run_command', {'command': 'ruff format a.py'}, str(project), task=owner)
    assert 'Error starting command' in result
    assert freshness._tokens[(key, path)] is before


def test_direct_dispatch_conv_fallback(project):
    key = 'direct-' + uuid.uuid4().hex
    path = str(project / 'a.py')
    freshness.record(key, path)
    tools.execute_tool('run_command', {'command': 'ruff format a.py'}, str(project), conv_id=key)
    assert not freshness.is_stale(key, path)


def test_background_worker_original_source_and_identity(project, monkeypatch):
    from lib.tasks_pkg import background_commands as background
    monkeypatch.setattr(background, '_records_root', lambda: str(project.parent / 'records'))
    owner = task()
    key = execution_root_key(owner)
    path = str(project / 'a.py')
    freshness.record(key, path)
    caller_thread = threading.get_ident()
    worker_threads = []
    original = 'ruff format a.py && ruff check a.py && pytest --version'

    def execute(command_task, context):
        worker_threads.append(threading.get_ident())
        wrapped = context.get('wrapped_command') or original
        binding = RecordedCommandSource(original, wrapped) if wrapped != original else None
        return tools.execute_tool('run_command', {'command': wrapped}, str(project),
                                  task=command_task, recorded_command_source=binding)

    command_id = background.start_background_command(task=owner, command=original, execute=execute)
    info = background.check_background_command(command_id, wait_s=10)
    assert info['status'] == 'completed'

    assert '[exit code: 0]' in info['result']
    assert worker_threads and worker_threads[0] != caller_thread
    assert (project / 'a.py').read_bytes() == b'new\n'
    assert not freshness.is_stale(key, path)


def test_unknown_writer_gets_no_owned_observation(project):
    owner = task()
    key = execution_root_key(owner)
    path = str(project / 'a.py')
    freshness.record(key, path)
    result = tools.execute_tool('run_command', {'command': "printf 'new\\n' > a.py"}, str(project), task=owner)
    assert '[exit code: 0]' in result
    assert freshness.is_stale(key, path)


@pytest.mark.parametrize('mode', ['plain', 'no-fix', 'diff'])
def test_installed_ruff_configuration_fix_true(project, monkeypatch, mode):
    import shlex
    import sys
    (project / 'ruff.toml').write_text('fix = true\n')
    (project / 'a.py').write_text('import os\n')
    executable = project.parent / 'bin' / 'ruff'
    executable.write_text('#!/bin/sh\nexec ' + shlex.quote(sys.executable) + ' -m ruff "$@"\n')
    executable.chmod(0o755)
    owner = task()
    key = execution_root_key(owner)
    path = str(project / 'a.py')
    freshness.record(key, path)
    before = freshness._tokens[(key, path)]
    flag = {'plain': '', 'no-fix': '--no-fix ', 'diff': '--diff '}[mode]
    command = f'ruff check {flag}a.py'
    claims = run_command.command_write_lease_claims(str(project), command)
    assert claims == ((('file', path),) if mode == 'plain' else ())
    tools.execute_tool('run_command', {'command': command}, str(project), task=owner)
    assert (project / 'a.py').read_text() == ('' if mode == 'plain' else 'import os\n')
    assert freshness._tokens[(key, path)] is before  # No CLI fix ownership.
    assert freshness.is_stale(key, path) == (mode == 'plain')


def test_formatter_never_creates_missing_token(project):
    owner = task()
    key = execution_root_key(owner)
    path = str(project / 'a.py')
    assert not freshness.has_token(key, path)
    tools.execute_tool('run_command', {'command': 'ruff format a.py'}, str(project), task=owner)
    assert (project / 'a.py').read_bytes() == b'new\n'
    assert not freshness.has_token(key, path)


def test_admission_failure_does_not_capture_or_advance(project, monkeypatch):
    from lib.project_mod.workspace_lease import workspace_write_lease
    owner = task()
    key = execution_root_key(owner)
    path = str(project / 'a.py')
    freshness.record(key, path)
    before = freshness._tokens[(key, path)]

    ready, release = threading.Event(), threading.Event()

    def hold():
        with workspace_write_lease(str(project)):
            ready.set()
            assert release.wait(5)

    worker = threading.Thread(target=hold)
    worker.start()
    try:
        assert ready.wait(5)
        monkeypatch.setattr(freshness, 'capture_owned_write', lambda *_: pytest.fail('capture before admission'))
        result = tools.execute_tool('run_command', {'command': 'ruff format a.py'}, str(project), task=owner)
        assert '[workspace-busy]' in result
    finally:
        release.set()
        worker.join(5)
        assert not worker.is_alive()
    assert (project / 'a.py').read_bytes() == b'old\n'
    assert freshness._tokens[(key, path)] is before


@pytest.mark.parametrize('kind', ['hardlink', 'symlink-parent', 'git-metadata'])
def test_unsafe_formatter_leaf_retains_tree_barrier(project, kind):
    if kind == 'hardlink':
        os.link(project / 'a.py', project / 'alias.py')
        target = 'alias.py'
    elif kind == 'symlink-parent':
        directory = project / 'real'
        directory.mkdir()
        (directory / 'a.py').write_bytes(b'old\n')
        (project / 'alias').symlink_to(directory, target_is_directory=True)
        target = 'alias/a.py'
    else:
        target = '.git/config'
    owned = set()
    claims = run_command.command_write_lease_claims(
        str(project), 'ruff format ' + target, _owned_formatter_paths=owned)
    assert ('tree', str(project)) in claims
    assert not owned


def test_owned_formatter_followup_write_requires_no_reread(project):
    from lib.tasks_pkg.handlers._write_freshness_gate import check_write_freshness
    owner = task()
    key = execution_root_key(owner)
    path = str(project / 'a.py')
    freshness.record(key, path)
    output = tools.execute_tool('run_command', {'command': 'ruff format a.py'},
                                str(project), task=owner)
    assert '[exit code: 0]' in output
    args = {'path': 'a.py', 'content': 'followup\n'}
    assert check_write_freshness(owner, 'write_file', args, str(project)) is None
    result = tools.execute_tool('write_file', args, str(project), conv_id=key)
    assert isinstance(result, str) and not result.startswith(('Error:', 'Write failed:')), result
    assert (project / 'a.py').read_bytes() == b'followup\n'
