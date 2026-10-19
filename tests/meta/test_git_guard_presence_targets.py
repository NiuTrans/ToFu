"""Git presence decisions follow actual command targets, not dispatch roots.

No Git writes execute: these tests stop at approval/settlement and use private
checkout metadata only to resolve nested paths and symlink aliases.
"""
import shlex

import pytest

from lib.project_mod import git_guard as guard
from lib.tasks_pkg.tool_dispatch import _approval as approval
from lib.tasks_pkg.tool_dispatch import _pipeline as pipeline

pytestmark = pytest.mark.unit


@pytest.fixture
def checkouts(tmp_path, monkeypatch):
    monkeypatch.setenv('TOFU_STICKY_CWD', '1')
    monkeypatch.setattr('lib.project_mod.config.get_conv_cwd', lambda _conv: None)
    roots = []
    for name in ('dispatch', 'target checkout'):
        root = tmp_path / name
        (root / '.git').mkdir(parents=True)
        (root / 'nested').mkdir()
        roots.append(root)
    guard._reset_for_tests()
    yield roots
    guard._reset_for_tests()


@pytest.fixture
def harness(monkeypatch):
    observed = {'roots': [], 'receipts': [], 'settles': [], 'peers_at': set()}

    def peers(root, user_id, exclude_conv=''):
        assert user_id == 17
        assert exclude_conv == 'executor'
        observed['roots'].append(root)
        if root in observed['peers_at']:
            return [{'convId': 'peer', 'title': 'Target writer', 'currentFile': 'file'}]
        return []

    def receipt(*args, authority, **kwargs):
        observed['receipts'].append(authority)
        return True

    def settle(*args, **kwargs):
        observed['settles'].append(kwargs['terminal_status'])

    monkeypatch.setattr(guard, 'active_peers', peers)
    monkeypatch.setattr(approval, 'mint_approval_receipt', receipt)
    monkeypatch.setattr(pipeline, '_settle_tool_result', settle)
    monkeypatch.setattr(pipeline, '_handle_approval',
                        lambda *a, **kw: pytest.fail('literal Git target reached human approval'))
    return observed


def _invoke(project_path, command, **extra_args):
    entry = {'toolName': 'run_command', 'toolCallId': 'call'}
    results = {}
    stopped = pipeline._approval_stops_tool_call(
        task={'id': 'task', 'convId': 'executor', '_userId': 17, 'aborted': False},
        fn_name='run_command', fn_args={'command': command, **extra_args},
        tc_id='call', rn=1, round_entry=entry,
        confirmation_tools={'run_command'}, write_tools={'run_command'},
        attended=True, auto_apply=True, cfg={}, project_path=str(project_path),
        round_num=1, model='test', tool_results=results,
        idempotent_tools=frozenset(), cache={}, tid='task')
    return stopped, results, entry


@pytest.mark.parametrize('shape', [
    'absolute', 'relative', 'nested', 'repeated', 'wrapper', 'alias',
    'work_tree', 'work_tree_equals', 'working_dir', 'sticky', 'namespaced', 'cd',
])
def test_target_writer_blocks_even_when_dispatch_root_is_solo(
        checkouts, tmp_path, monkeypatch, harness, shape):
    dispatch, target = checkouts
    quote = shlex.quote
    extra = {}
    command = f'git -C {quote(str(target))} reset --hard'
    if shape == 'relative':
        command = f'git -C {quote("../" + target.name)} reset --hard'
    elif shape == 'cd':
        command = f'cd {quote(str(target))} && git reset --hard'
    elif shape == 'nested':
        command = f'git -C {quote(str(target / "nested"))} reset --hard'
    elif shape == 'repeated':
        command = f'git -C .. -C {quote(target.name)} -C nested reset --hard'
    elif shape == 'wrapper':
        command = f'MODE=test sudo -u writer /usr/bin/git -c probe.key=-C -C {quote(str(target))} reset --hard'
    elif shape == 'alias':
        alias = tmp_path / 'alias'
        alias.symlink_to(target, target_is_directory=True)
        command = f'git -C {quote(str(alias / "nested"))} reset --hard'
    elif shape in ('work_tree', 'work_tree_equals'):
        sep = ' ' if shape == 'work_tree' else '='
        command = f'git --work-tree{sep}{quote(str(target))} reset --hard'
    elif shape == 'working_dir':
        extra['working_dir'] = str(target / 'nested')
        command = 'git reset --hard'
    elif shape == 'sticky':
        monkeypatch.setattr('lib.project_mod.config.get_conv_cwd',
                            lambda _conv: str(target / 'nested'))
        command = 'git reset --hard'
    elif shape == 'namespaced':
        def resolve(base, path, conv_id=None):
            assert (base, path, conv_id) == (str(dispatch), 'target:nested', 'executor')
            return str(target), 'nested'
        monkeypatch.setattr('lib.project_mod.path_resolution._resolve_base', resolve)
        extra['working_dir'] = 'target:nested'
        command = 'git reset --hard'

    harness['peers_at'].add(str(target))
    stopped, results, entry = _invoke(dispatch, command, **extra)
    assert stopped is True
    expected = ([str(dispatch), str(target)]
                if shape in {'work_tree', 'work_tree_equals'} else [str(target)])
    # --work-tree redirects working bytes, not the dispatch index/refs.
    assert harness['roots'] == expected
    assert 'Target writer' in results['call'][0]

    assert f'Checkout root: {target}' in results['call'][0]
    assert f'checkout root: {target}' in results['call'][0]
    assert 'convId=peer agentId=(main)' in results['call'][0]
    if shape in {'work_tree', 'work_tree_equals'}:
        assert f'Checkout root: {dispatch}' in results['call'][0]
    else:
        assert f'Checkout root: {dispatch}' not in results['call'][0]
    assert harness['settles'] == ['rejected']
    assert harness['receipts'] == []
    # Conservative project presence is not a confirmed writer/lease fence.
    assert entry['rejection']['kind'] == 'git_guard'
    assert entry['rejection']['condition'] == 'active_project_presence_settled'
    assert entry['rejection']['retryPolicy'] == {
        'mode': 'after_condition_change', 'automatic': False,
    }


def test_other_checkout_peer_does_not_block_actual_solo_target(checkouts, harness):
    dispatch, target = checkouts
    harness['peers_at'].add(str(dispatch))
    stopped, _, _ = _invoke(dispatch, f'git -C {shlex.quote(str(target))} reset --hard')
    assert stopped is False
    assert harness['roots'] == [str(target)]
    assert harness['receipts'] == ['agent_git_guard_solo']


def test_each_destructive_git_target_is_checked(checkouts, harness):
    dispatch, target = checkouts
    harness['peers_at'].add(str(target))
    stopped, results, _ = _invoke(
        dispatch, f'git reset --hard && git -C {shlex.quote(str(target))} restore .')
    assert stopped is True
    assert harness['roots'] == [str(dispatch), str(target)]

    assert f'Checkout root: {dispatch}' in results['call'][0]
    assert f'Checkout root: {target}' in results['call'][0]
    assert f'checkout root: {target}' in results['call'][0]
    assert not harness['receipts']


@pytest.mark.parametrize('command', [
    'git commit --amend -m "-C {target}"',
    'git branch -C old new',
    'git -C {target} status && git reset --hard',
])
def test_non_target_c_tokens_and_readonly_git_do_not_redirect_presence(
        checkouts, harness, command):
    dispatch, target = checkouts
    harness['peers_at'].add(str(dispatch))
    stopped, _, _ = _invoke(dispatch, command.format(target=shlex.quote(str(target))))
    assert stopped is True
    assert harness['roots'] == [str(dispatch)]


def test_explicit_working_dir_overrides_sticky_cwd(checkouts, monkeypatch, harness):
    dispatch, target = checkouts
    monkeypatch.setattr('lib.project_mod.config.get_conv_cwd', lambda _conv: str(target))
    harness['peers_at'].add(str(dispatch))
    stopped, _, _ = _invoke(dispatch, 'git -C "" reset --hard', working_dir=str(dispatch))
    assert stopped is True
    assert harness['roots'] == [str(dispatch)]


def test_disabled_sticky_cwd_uses_dispatch_root(checkouts, monkeypatch, harness):
    dispatch, target = checkouts
    monkeypatch.setenv('TOFU_STICKY_CWD', '0')
    monkeypatch.setattr('lib.project_mod.config.get_conv_cwd', lambda _conv: str(target))
    harness['peers_at'].add(str(dispatch))
    stopped, _, _ = _invoke(dispatch, 'git reset --hard')
    assert stopped is True
    assert harness['roots'] == [str(dispatch)]


@pytest.mark.parametrize('command', [
    'cd "$UNKNOWN_CHECKOUT" && git reset --hard',
    'git -C "$UNKNOWN_CHECKOUT" reset --hard',
])
def test_unresolved_or_opaque_target_retains_existing_gate(
        checkouts, harness, monkeypatch, command):
    dispatch, target = checkouts
    approvals = []

    def deny(*args, **kwargs):
        approvals.append(True)
        return False, 'denied by existing gate'

    monkeypatch.setattr(pipeline, '_handle_approval', deny)
    stopped, _, _ = _invoke(dispatch, command.format(target=shlex.quote(str(target))))
    assert stopped is True
    assert approvals
    assert not harness['receipts']
    assert not harness['roots']


def test_new_worktree_cherry_pick_does_not_borrow_parent_presence(checkouts, harness):
    dispatch, _ = checkouts
    destination = dispatch / '.worktrees' / 'new-task'
    harness['peers_at'].add(str(dispatch))
    command = ('/usr/bin/git worktree add -b feature/new-task .worktrees/new-task HEAD'
               ' && /usr/bin/git -C .worktrees/new-task cherry-pick d6ecbb6c1')
    stopped, _, _ = _invoke(dispatch, command)
    assert stopped is False
    assert harness['roots'] == []
    assert harness['receipts'] == ['git_safe_write']


def test_worktree_creation_does_not_hide_shared_checkout_mutation(checkouts, harness):
    dispatch, _ = checkouts
    harness['peers_at'].add(str(dispatch))
    stopped, _, _ = _invoke(
        dispatch, 'git worktree add -b new .worktrees/new HEAD && git reset --hard')
    assert stopped is True
    assert harness['roots'] == [str(dispatch)]
    assert not harness['receipts']


def test_existing_noncheckout_destination_keeps_parent_presence(checkouts, harness):
    dispatch, _ = checkouts
    (dispatch / 'existing').mkdir()
    harness['peers_at'].add(str(dispatch))
    stopped, _, _ = _invoke(
        dispatch, 'git worktree add -b new existing HEAD && git -C existing reset --hard')
    assert stopped is True
    assert harness['roots'] == [str(dispatch)]


def test_prospective_worktree_presence_is_still_checked(checkouts, harness):
    dispatch, _ = checkouts
    destination = dispatch / '.worktrees' / 'new-task'
    harness['peers_at'].add(str(destination))
    stopped, _, _ = _invoke(
        dispatch, 'git worktree add -b new .worktrees/new-task HEAD'
        ' && git -C .worktrees/new-task reset --hard')
    assert stopped is True
    assert harness['roots'] == [str(destination)]
    assert not harness['receipts']


def test_add_then_cherry_pick_checks_future_checkout_not_parent(checkouts, harness):
    dispatch, _ = checkouts
    child = dispatch / '.worktrees' / 'own-task'
    harness['peers_at'].add(str(dispatch))
    stopped, _, _ = _invoke(dispatch,
        '/usr/bin/git worktree add -b own-task ' + shlex.quote(str(child))
        + ' HEAD && /usr/bin/git -C ' + shlex.quote(str(child)) + ' cherry-pick abc123')
    assert not stopped
    assert harness['roots'] == []
    assert harness['receipts'] == ['git_safe_write']


def test_add_then_shared_reset_still_checks_parent(checkouts, harness):
    dispatch, _ = checkouts
    harness['peers_at'].add(str(dispatch))
    stopped, _, _ = _invoke(dispatch,
        'git worktree add -b own-task .worktrees/own-task HEAD && git reset --hard')
    assert stopped
    assert harness['roots'] == [str(dispatch)]


@pytest.mark.parametrize('separator', [';', '||', '|'])
def test_failed_creation_does_not_authorize_future_parent_git(checkouts, harness, separator):
    dispatch, _ = checkouts
    harness['peers_at'].add(str(dispatch))
    stopped, _, _ = _invoke(dispatch,
        'git worktree add -b own-task .worktrees/own-task HEAD ' + separator
        + ' git -C .worktrees/own-task reset --hard')
    assert stopped
    assert harness['roots'] == [str(dispatch)]


@pytest.mark.parametrize('option', ['--work-tree ', '--work-tree='])
def test_worktree_redirect_still_checks_dispatch_index_owner(checkouts, harness, option):
    dispatch, target = checkouts
    harness['peers_at'].add(str(dispatch))
    stopped, _, _ = _invoke(dispatch, 'git ' + option + shlex.quote(str(target)) + ' reset --hard')
    assert stopped is True
    assert harness['roots'] == [str(dispatch), str(target)]
    assert harness['receipts'] == []


@pytest.mark.parametrize('setup', ['GIT_COMMON_DIR={common} git', 'env GIT_COMMON_DIR={common} git',
                                   'export GIT_COMMON_DIR={common}; git'])
def test_common_directory_owner_presence_is_not_erased(checkouts, harness, setup):
    dispatch, target = checkouts
    harness['peers_at'].add(str(target))
    command = setup.format(common=shlex.quote(str(target / '.git'))) + ' branch -D unmerged'
    stopped, _, _ = _invoke(dispatch, command)
    assert stopped is True
    assert str(target) in harness['roots']
    assert harness['receipts'] == []
