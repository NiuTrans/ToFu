"""Project gate authority uses physical Git identity, never process ownership."""
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from lib import git_checkpoint_policy as policy
from lib import integration_control as ic
from lib import linear_git_checkpoint as linear

pytestmark = pytest.mark.unit


def git(root, *args):
    result = subprocess.run(['git', '-C', str(root), *args],
                            env=policy.git_policy_environment(), text=True,
                            capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def repository(path):
    path.mkdir()
    git(path, 'init', '-b', 'main')
    git(path, 'config', 'user.name', 'Gate test')
    git(path, 'config', 'user.email', 'gate@example.invalid')
    (path / 'base.txt').write_text('base\n')
    git(path, 'add', '.')
    git(path, 'commit', '-m', 'base')
    return path


@pytest.fixture
def roots(tmp_path, monkeypatch):
    app = repository(tmp_path / 'application')
    foreign = repository(tmp_path / 'foreign')
    monkeypatch.setattr(policy, '__file__', str(app / 'lib' / 'git_checkpoint_policy.py'))
    for name in ('TOFU_INTEGRATION_TEST_ROOT', 'TOFU_INTEGRATION_TEST_CMD',
                 'TOFU_INTEGRATION_STABLE_TEST_CMD', 'TOFU_LINEAR_GIT_CHECKPOINT_TEST_CMD'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('TOFU_INTEGRATION_WORKSPACE_DIR', str(tmp_path / 'gates'))
    return app, foreign


def test_environment_scope_uses_common_dir_and_preserves_linked_app(roots, tmp_path, monkeypatch):
    app, foreign = roots
    command = f'{shlex.quote(sys.executable)} scripts/test_select.py --base {{base}}'
    monkeypatch.setenv('TOFU_INTEGRATION_TEST_CMD', command)
    linked = tmp_path / 'linked'
    git(app, 'worktree', 'add', '--detach', str(linked), 'HEAD')
    (linked / 'nested').mkdir()
    for root in (app, linked, linked / 'nested'):
        gates = policy.select_gate_commands(root)
        assert gates.integration == gates.stable == gates.linear == command
    assert policy.select_gate_commands(foreign).integration == ''
    # Even an identically named project cannot borrow the application policy.
    renamed = foreign.parent / 'application-copy'
    foreign.rename(renamed)
    assert policy.select_gate_commands(renamed).stable == ''


def test_explicit_pin_and_local_overrides(roots, tmp_path, monkeypatch):
    app, foreign = roots
    monkeypatch.setenv('TOFU_INTEGRATION_TEST_ROOT', str(foreign))
    monkeypatch.setenv('TOFU_INTEGRATION_TEST_CMD', 'env-integration')
    monkeypatch.setenv('TOFU_INTEGRATION_STABLE_TEST_CMD', 'env-stable')
    monkeypatch.setenv('TOFU_LINEAR_GIT_CHECKPOINT_TEST_CMD', 'env-linear')
    assert policy.select_gate_commands(app).integration == ''
    assert policy.select_gate_commands(foreign).linear == 'env-linear'
    for key, value in (('integrationTestCommand', 'local-integration'),
                       ('integrationStableTestCommand', 'local-stable'),
                       ('linearCheckpointTestCommand', 'local-linear')):
        git(foreign, 'config', '--local', 'tofu.' + key, value)
    linked = tmp_path / 'external-linked'
    git(foreign, 'worktree', 'add', '--detach', str(linked), 'HEAD')
    gates = policy.select_gate_commands(linked)
    assert (gates.integration, gates.stable, gates.linear) == (
        'local-integration', 'local-stable', 'local-linear')
    assert gates.stable_explicit


@pytest.mark.parametrize('kind', ['integrationTestCommand', 'integrationStableTestCommand',
                                  'linearCheckpointTestCommand'])
@pytest.mark.parametrize('invalid', ['', '  ', '"', "''", 'one\ntwo'])
def test_invalid_local_command_never_downgrades_to_env(roots, monkeypatch, kind, invalid):
    _, foreign = roots
    monkeypatch.setenv('TOFU_INTEGRATION_TEST_ROOT', str(foreign))
    monkeypatch.setenv('TOFU_INTEGRATION_TEST_CMD', 'configured-env')
    git(foreign, 'config', '--local', 'tofu.' + kind, invalid)
    with pytest.raises(policy.GatePolicyError):
        policy.select_gate_commands(foreign)


def test_duplicate_local_command_fails_closed(roots):
    _, root = roots
    git(root, 'config', '--add', 'tofu.integrationTestCommand', 'one')
    git(root, 'config', '--add', 'tofu.integrationTestCommand', 'two')
    with pytest.raises(policy.GatePolicyError, match='ambiguous'):
        policy.select_gate_commands(root)


def test_includes_and_global_policy_are_not_authority(roots, tmp_path, monkeypatch):
    _, root = roots
    config = tmp_path / 'outside-config'
    config.write_text('[tofu]\n integrationTestCommand = outside\n')
    git(root, 'config', '--local', 'include.path', str(config))
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', str(config))
    assert policy.select_gate_commands(root).integration == ''
    git(root, 'config', '--local', 'tofu.integrationTestCommand', 'local')
    assert policy.select_gate_commands(root).stable == 'local'


@pytest.mark.parametrize('name', ['GIT_DIR', 'GIT_COMMON_DIR', 'GIT_WORK_TREE',
                                  'GIT_CONFIG', 'GIT_CONFIG_PARAMETERS'])
def test_ambient_git_redirects_do_not_borrow_policy(roots, monkeypatch, name):
    app, foreign = roots
    git(app, 'config', '--local', 'tofu.integrationTestCommand', 'app-only')
    monkeypatch.setenv('TOFU_INTEGRATION_TEST_CMD', 'app-env')
    value = str(app / '.git') if name in ('GIT_DIR', 'GIT_COMMON_DIR') else str(app)
    monkeypatch.setenv(name, value)
    monkeypatch.setenv('GIT_CONFIG_COUNT', '1')
    monkeypatch.setenv('GIT_CONFIG_KEY_0', 'tofu.integrationTestCommand')
    monkeypatch.setenv('GIT_CONFIG_VALUE_0', 'injected')
    assert policy.select_gate_commands(foreign).integration == ''
    assert ic._repo_root(foreign) == foreign
    assert linear._repository_root(foreign) == foreign


def test_packaged_source_requires_explicit_scope(roots, tmp_path, monkeypatch):
    _, root = roots
    package = tmp_path / 'package'
    package.mkdir()
    monkeypatch.setattr(policy, '__file__', str(package / 'lib' / 'policy.py'))
    monkeypatch.setenv('TOFU_INTEGRATION_TEST_CMD', 'env-command')
    assert policy.select_gate_commands(root).integration == ''
    git(root, 'config', '--local', 'tofu.integrationTestCommand', 'local-command')
    assert policy.select_gate_commands(root).linear == 'local-command'
    monkeypatch.setenv('TOFU_INTEGRATION_TEST_ROOT', str(root))
    assert policy.select_gate_commands(root).integration == 'local-command'


@pytest.mark.parametrize('pin', ['', 'missing', 'non-git'])
def test_invalid_explicit_scope_refuses(roots, tmp_path, monkeypatch, pin):
    _, root = roots
    path = tmp_path / pin
    if pin == 'non-git':
        path.mkdir()
    monkeypatch.setenv('TOFU_INTEGRATION_TEST_ROOT', str(path) if pin else '')
    with pytest.raises(policy.GatePolicyError):
        policy.select_gate_commands(root)


@pytest.mark.parametrize('failure', ['config', 'identity', 'unknown'])
def test_read_errors_are_not_absent_policy(roots, monkeypatch, failure):
    _, root = roots
    real = policy._policy_git
    def fault(path, args):
        if (failure in ('config', 'unknown') and args[0] == 'config'):
            if failure == 'unknown':
                return subprocess.CompletedProcess(args, 0, 'tofu.unknown\nvalue\0', '')
            return subprocess.CompletedProcess(args, 3, '', 'unreadable config')
        if failure == 'identity' and args[0] == 'rev-parse':
            return subprocess.CompletedProcess(args, 0, 'incomplete\n', '')
        return real(path, args)
    monkeypatch.setattr(policy, '_policy_git', fault)
    with pytest.raises(policy.GatePolicyError):
        policy.select_gate_commands(root)


def test_foreign_text_passes_but_semantic_suffix_still_refuses(roots, monkeypatch):
    _, root = roots
    monkeypatch.setenv('TOFU_INTEGRATION_TEST_CMD', 'python3 scripts/test_select.py --run')
    base = git(root, 'rev-parse', 'HEAD')
    (root / 'acceptance.txt').write_text('delivered\n')
    git(root, 'add', '.')
    git(root, 'commit', '-m', 'text')
    target = git(root, 'rev-parse', 'HEAD')
    assert ic._gate_commands(root, base, target, policy.select_gate_commands(root).integration)[0]
    (root / 'code.py').write_text('VALUE = 1\n')
    git(root, 'add', '.')
    git(root, 'commit', '-m', 'semantic')
    passed, detail = ic._gate_commands(root, base, git(root, 'rev-parse', 'HEAD'),
                                      policy.select_gate_commands(root).integration)
    assert not passed and 'required' in detail


@pytest.mark.parametrize('exists', [True, False])
def test_local_script_executes_only_on_target_tree_or_fails(roots, tmp_path, monkeypatch, exists):
    app, root = roots
    base = git(root, 'rev-parse', 'HEAD')
    receipt = tmp_path / 'receipt'
    (root / 'base.txt').write_text('immutable target\n')
    if exists:
        (root / 'verify.py').write_text(
            'import sys\nfrom pathlib import Path\n'
            "assert Path('base.txt').read_text() == 'immutable target\\n'\n"
            'Path(sys.argv[1]).write_text(str(Path.cwd()))\n')
    git(root, 'add', '.')
    git(root, 'commit', '-m', 'target')
    target = git(root, 'rev-parse', 'HEAD')
    (root / 'base.txt').write_text('live checkout must not run\n')
    # A same-named app script must never rescue a missing target script.
    (app / 'verify.py').write_text('raise AssertionError("wrong project")\n')
    monkeypatch.setenv('TOFU_INTEGRATION_TEST_CMD', 'borrowed-app-command')
    git(root, 'config', '--local', 'tofu.integrationTestCommand',
        f'{shlex.quote(sys.executable)} verify.py {shlex.quote(str(receipt))}')
    passed, _ = ic._gate_commands(root, base, target, policy.select_gate_commands(root).integration)
    assert passed is exists
    assert receipt.exists() is exists
    if exists:
        assert Path(receipt.read_text()) != root
    assert (root / 'base.txt').read_text() == 'live checkout must not run\n'


def test_nested_packaged_copy_cannot_borrow_foreign_git(roots, monkeypatch):
    _, root = roots
    package = root / 'installed-copy'
    package.mkdir()
    monkeypatch.setattr(policy, '__file__', str(package / 'lib' / 'policy.py'))
    monkeypatch.setenv('TOFU_INTEGRATION_TEST_CMD', 'package-default')
    assert policy.select_gate_commands(root).integration == ''


def test_nested_foreign_git_does_not_borrow_app_scope(roots, monkeypatch):
    app, _ = roots
    nested = repository(app / 'other')
    monkeypatch.setenv('TOFU_INTEGRATION_TEST_CMD', 'app-default')
    assert policy.select_gate_commands(nested).integration == ''


def test_copied_worktree_pointer_cannot_borrow_policy(roots, tmp_path, monkeypatch):
    app, foreign = roots
    linked = tmp_path / 'real-linked'
    git(app, 'worktree', 'add', '--detach', str(linked), 'HEAD')
    forged = tmp_path / 'forged'
    forged.mkdir()
    (forged / '.git').write_text((linked / '.git').read_text())
    monkeypatch.setenv('TOFU_INTEGRATION_TEST_CMD', 'app-default')
    with pytest.raises(policy.GatePolicyError, match='worktree'):
        policy.select_gate_commands(forged)
    assert policy.select_gate_commands(foreign).integration == ''


@pytest.mark.parametrize('name', ['main.rs', 'main.go', 'main.cpp', 'main.h',
                                  'settings.ini', '.env', 'Dockerfile', 'Makefile'])
@pytest.mark.parametrize('operation', ['add', 'delete', 'rename'])
def test_semantic_non_python_changes_require_gate(roots, name, operation):
    _, root = roots
    if operation != 'add':
        (root / name).write_text('baseline\n')
        git(root, 'add', '.')
        git(root, 'commit', '-m', 'semantic baseline')
    base = git(root, 'rev-parse', 'HEAD')
    if operation == 'add':
        (root / name).write_text('new\n')
    elif operation == 'delete':
        (root / name).unlink()
    else:
        (root / name).rename(root / 'only-text.txt')
    git(root, 'add', '-A')
    git(root, 'commit', '-m', operation)
    passed, detail = ic._gate_commands(root, base, git(root, 'rev-parse', 'HEAD'), '')
    assert not passed and 'required' in detail


def test_gate_mutating_local_policy_cannot_pass(roots):
    _, root = roots
    base = git(root, 'rev-parse', 'HEAD')
    (root / 'note.txt').write_text('task\n')
    git(root, 'add', '.')
    git(root, 'commit', '-m', 'task')
    command = 'git config --local tofu.integrationTestCommand changed-command'
    git(root, 'config', '--local', 'tofu.integrationTestCommand', command)
    passed, detail = ic._gate_commands(root, base, git(root, 'rev-parse', 'HEAD'),
                                      policy.select_gate_commands(root).integration)
    assert not passed and 'policy changed' in detail


def test_symlinked_gitfile_cannot_borrow_app_policy(roots, tmp_path, monkeypatch):
    app, _ = roots
    linked = tmp_path / 'real-worktree'
    git(app, 'worktree', 'add', '--detach', str(linked), 'HEAD')
    forged = tmp_path / 'gitfile-symlink'
    forged.mkdir()
    (forged / '.git').symlink_to(linked / '.git')
    monkeypatch.setenv('TOFU_INTEGRATION_TEST_CMD', 'app-default')
    with pytest.raises(policy.GatePolicyError, match='symlink'):
        policy.select_gate_commands(forged)
