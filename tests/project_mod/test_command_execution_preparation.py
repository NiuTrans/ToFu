"""Command execution preparation is unchanged by lease removal.

Hardened Git inspections must not refresh the shared index or run configured
helpers, and the runner still executes the command as written, while Tofu's
own whole-workspace writer holds the checkout (tool calls never wait for it).
Only private temporary Git checkouts are used.
"""
from contextlib import contextmanager
import shlex
import subprocess
import threading

import pytest

from lib.project_mod import tools
from lib.project_mod.workspace_lease import workspace_lease_fds, workspace_write_lease

pytestmark = pytest.mark.unit


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv('TOFU_WORKSPACE_TOOL_WAIT_SECONDS', '0')
    root = tmp_path / 'checkout'
    root.mkdir()
    subprocess.run(['git', 'init', '-q', str(root)], check=True)
    (root / 'source').write_text('new contents\n')
    (root / 'target').write_text('old\n')
    monkeypatch.setenv('TOFU_RUN_CP_FASTPATH', '1')
    monkeypatch.setenv('TOFU_STICKY_CWD', '0')
    return root


@contextmanager
def _internal_writer(root):
    ready, release = threading.Event(), threading.Event()
    errors = []

    def hold():
        try:
            with workspace_write_lease(root):
                ready.set()
                release.wait()
        except BaseException as exc:
            errors.append(exc)
            ready.set()

    worker = threading.Thread(target=hold)
    worker.start()
    try:
        assert ready.wait(5)
        assert not errors
        yield
    finally:
        release.set()
        worker.join()
        assert not worker.is_alive()
        assert not errors


def _run(repo, route, command):
    import lib.project_mod.run_command as commands
    if route == 'runner':
        return commands.tool_run_command(str(repo), command)
    return tools.execute_tool('run_command', {'command': command}, str(repo))


def _commit_source(repo):
    subprocess.run(['git', '-C', str(repo), 'add', '--', 'source'], check=True)
    subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Test',
                    '-c', 'user.email=test@example.invalid', 'commit',
                    '-qm', 'private fixture'], check=True)


@pytest.mark.parametrize('route', ['runner', 'dispatch'])
@pytest.mark.parametrize('command', [
    'git status --short',
    'git status --porcelain=v2 -uno',
    "git -C '.' --no-pager status -b --short",
    'git log --oneline -1',
    'git log -1',
    'git --no-pager log --graph --max-count 2 --all',
])
def test_hardened_git_inspection_writes_no_index_and_runs_no_helper(repo, route, command):
    import os
    _commit_source(repo)
    hook = repo / 'fsmonitor'
    marker = repo / 'helper-ran'
    hook.write_text(f'#!/bin/sh\nprintf called > {shlex.quote(str(marker))}\n')
    hook.chmod(0o700)
    subprocess.run(['git', '-C', str(repo), 'config', 'core.fsmonitor', str(hook)], check=True)
    subprocess.run(['git', '-C', str(repo), 'config', 'format.pretty', 'unsafe-default'], check=True)
    # Make an optional index refresh meaningful without changing file bytes.
    stamp = (repo / 'source').stat().st_mtime + 10
    os.utime(repo / 'source', (stamp, stamp))
    index = repo / '.git' / 'index'
    before = index.read_bytes()
    with _internal_writer(repo):
        result = _run(repo, route, command)
        assert '[exit code: 0]' in result
        assert index.read_bytes() == before
        assert not marker.exists()


@pytest.mark.parametrize('route', ['runner', 'dispatch'])
@pytest.mark.parametrize('command', [
    'git -C \"\" status --short',
    'git diff --stat HEAD', 'git diff --cached --name-only', 'git show --stat HEAD',
    'git log --format="%h | %s" --max-count=2 HEAD', 'git ls-files --stage',
    'git ls-tree -r HEAD', 'git rev-parse --verify HEAD', 'git merge-base HEAD HEAD',
    'git rev-list --count HEAD', 'git cat-file -p HEAD',
    'git reflog show -1', 'git stash list', 'git config --get log.showSignature',
    'git describe --always HEAD', 'git name-rev --name-only HEAD',
    'git blame --line-porcelain source', 'git shortlog -s HEAD',
    'git branch --format=\"%(refname:short)\"',
    'git diff --stat HEAD | python3 -c "import sys; print(sys.stdin.read())"',
])
def test_local_git_query_catalog_is_helper_free(repo, route, command):
    _commit_source(repo)
    helper = repo / 'helper'
    marker = repo / 'helper-ran'
    helper.write_text('#!/bin/sh\nprintf called > ' + shlex.quote(str(marker)) + '\n')
    helper.chmod(0o700)
    for key in ('core.fsmonitor', 'diff.external', 'core.pager', 'gpg.program'):
        subprocess.run(['git', '-C', str(repo), 'config', key, str(helper)], check=True)
    subprocess.run(['git', '-C', str(repo), 'config', 'log.showSignature', 'true'], check=True)
    expected_code = 0
    if command.startswith('git branch --format='):
        # Git 2.11 lacks this read-only option; preserve its native error.
        expected_code = subprocess.run(['git', '-C', str(repo), 'branch',
                                        '--format=%(refname:short)'],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode
    before = (repo / '.git' / 'index').read_bytes()
    with _internal_writer(repo):
        result = _run(repo, route, command)
    assert f'[exit code: {expected_code}]' in result
    assert (repo / '.git' / 'index').read_bytes() == before
    assert not marker.exists()


@pytest.mark.parametrize('route', ['runner', 'dispatch'])
@pytest.mark.parametrize('command', [
    "python3 - <<'PY'\nprint(3 & 1)\n# $() > target; rm target\nPY\n",
    'npm run test:unit -- --run example.test.ts && python3 - <<"PY"\n'
    'print(2 > 1)\nPY\n',
    "node - <<'JS'\nconsole.log(`literal ${1}`);\nJS\n",
    "python3 - <<'PY-stdin'\n# unmatched quote: '\nprint(1)\nPY-stdin",
    "python3 - <<'PY literal'\n# PY literal suffix\nprint(1)\nPY literal\n",
    "python3 - 0<<'PY'\nprint(1)\nPY\necho done",
    "python3 - <<'PY' 2>&1 | head -n 1\nprint(3 & 1)\nPY\n",
    "python3 - <<'PY' 3<&0\nprint(1)\nPY\n",
    "python3 - <<'PY'\nprint(1)\nPY\n"
    "node - <<'JS'\nconsole.log(1);\nJS\n",
    "python3 - <<'PY'\nfrom pathlib import Path\n"
    "Path('target').write_text('opaque runtime can write')\nPY\n",
])
def test_literal_runtime_heredoc_reaches_the_process_as_written(
        repo, monkeypatch, route, command):
    import lib.project_mod.run_command as commands
    observed = []

    def process_boundary(execution_source, *_args, **_kwargs):
        assert workspace_lease_fds() == ()
        assert command.strip() in execution_source
        observed.append(execution_source)
        return '$ runtime heredoc\n[exit code: 0]'

    monkeypatch.setattr(commands, '_run_command_simple', process_boundary)
    with _internal_writer(repo):
        result = _run(repo, route, command)
    assert '[exit code: 0]' in result
    assert len(observed) == 1


def test_execution_projection_has_stable_bytes():
    import lib.project_mod.run_command as commands
    source = 'printf ready; git status --short; git branch example; printf done'
    projected = commands.command_execution_source(source)
    assert commands.command_execution_source(projected) == projected


def test_transfer_suppresses_only_its_own_implicit_sinks(repo, monkeypatch, tmp_path):
    import os
    import sys
    import lib.project_mod.run_command as commands
    from lib.project_mod import network_command
    monkeypatch.setattr(network_command, 'prepare_network_command', lambda *a, **k: None)
    binary = tmp_path / 'bin'
    binary.mkdir()
    curl = binary / 'curl'
    curl.write_text(f'#!{sys.executable}\nimport os, sys\n'
                    'assert sys.argv[1] == "-q"\n'
                    'assert "SSLKEYLOGFILE" not in os.environ\n'
                    'assert "QLOGDIR" not in os.environ\n'
                    'assert "CURL_MEMDEBUG" not in os.environ\n'
                    'open(sys.argv[sys.argv.index("-o")+1], "w").write("downloaded")\n')
    curl.chmod(0o700)
    environment = dict(os.environ, PATH=str(binary) + os.pathsep + os.environ['PATH'],
                       SSLKEYLOGFILE='unchanged', QLOGDIR='unchanged', CURL_MEMDEBUG='unchanged')
    monkeypatch.setattr(commands, '_get_cmd_env', lambda *a, **k: dict(environment))
    command = ('curl -o result.json https://example.invalid/models; '
               'printf "%s" "$SSLKEYLOGFILE"')
    result = commands.tool_run_command(str(repo), command)
    assert '[exit code: 0]' in result and 'unchanged' in result
    assert (repo / 'result.json').read_text() == 'downloaded'


def test_recorded_binding_is_internal_and_exact(repo):
    from lib.project_mod.tool_admission import RecordedCommandSource, tool_admission_scope
    import lib.project_mod.run_command as commands
    original = 'python3 -c "print(1)"'
    wrapped = f'( {original} ) > /dev/null'
    with pytest.raises(ValueError, match='Invalid recorded'):
        with tool_admission_scope(command_source={'original': original, 'wrapped': wrapped}):
            pass
    with tool_admission_scope(command_source=RecordedCommandSource(original, wrapped)):
        with pytest.raises(ValueError, match='wrapper changed'):
            commands.tool_run_command(str(repo), wrapped + '; touch victim')
    assert not (repo / 'victim').exists()


def test_background_binding_uses_the_hardened_execution_source():
    import lib.project_mod.run_command as commands
    original = 'git log --oneline -3 && python3 -m pytest -q'
    executed = commands.command_execution_source(original)
    assert executed != original and '--no-optional-locks' in executed
    assert commands.command_execution_source(executed) == executed


def test_hardened_recorded_git_log_disables_actual_signature_helper(repo, tmp_path):
    import os
    import lib.project_mod.run_command as commands

    def git(*arguments, input=None):
        return subprocess.run(['git', '-C', str(repo), *arguments], input=input,
                              capture_output=True, text=True, check=True).stdout.strip()

    git('add', 'source')
    tree = git('write-tree')
    # No key or signing service: a syntactically signed commit is enough to
    # invoke Git's configured verifier.
    commit = (f'tree {tree}\nauthor Probe <probe@example.invalid> 1 +0000\n'
              'committer Probe <probe@example.invalid> 1 +0000\n'
              'gpgsig -----BEGIN PGP SIGNATURE-----\n dummy\n'
              ' -----END PGP SIGNATURE-----\n\nprobe\n')
    git('update-ref', 'HEAD', git('hash-object', '-t', 'commit', '-w', '--stdin', input=commit))
    marker, helper = tmp_path / 'helper-effect', tmp_path / 'verifier'
    helper.write_text('#!/bin/sh\nprintf invoked >> "$TOFU_TEST_HELPER_MARKER"\nexit 1\n')
    helper.chmod(0o700)
    git('config', 'log.showSignature', 'true')
    git('config', 'gpg.program', str(helper))
    environment = {**os.environ, 'TOFU_TEST_HELPER_MARKER': str(marker)}
    original = 'git log --oneline -1'
    for hardened in (False, True):
        source = commands.command_execution_source(original) if hardened else original
        subprocess.run(source, shell=True, cwd=repo, env=environment,
                       capture_output=True, text=True, check=True)
        assert marker.exists() is not hardened
        marker.unlink(missing_ok=True)


def test_recorded_file_mutator_tracks_original_targets_not_server_spool(repo, tmp_path, monkeypatch):
    from lib.project_mod.tool_admission import RecordedCommandSource
    scopes = []
    snapshot_scope = tools._snapshot_scope_rel_targets

    def observe(targets):
        scopes.append(targets)
        return snapshot_scope(targets)

    monkeypatch.setattr(tools, '_snapshot_scope_rel_targets', observe)
    original = 'cp source copied'
    wrapped = f'( {original} ) > {shlex.quote(str(tmp_path / "server-record"))}'
    result = tools.execute_tool('run_command', {'command': wrapped}, str(repo),
                                recorded_command_source=RecordedCommandSource(original, wrapped))
    assert '[exit code: 0]' in result
    assert (repo / 'copied').read_text() == 'new contents\n'
    assert scopes == [{'copied'}]


def test_internal_lease_wait_bound_default_and_validation(monkeypatch):
    from lib.project_mod.tool_admission import admission_timeout

    monkeypatch.delenv('TOFU_WORKSPACE_TOOL_WAIT_SECONDS', raising=False)
    assert admission_timeout() is None
    for value in ['0', '600', '5000']:
        monkeypatch.setenv('TOFU_WORKSPACE_TOOL_WAIT_SECONDS', value)
        assert admission_timeout() == float(value)
    for value in ['-1', 'inf', 'nan', 'invalid']:
        monkeypatch.setenv('TOFU_WORKSPACE_TOOL_WAIT_SECONDS', value)
        with pytest.raises(ValueError, match='nonnegative and finite'):
            admission_timeout()
