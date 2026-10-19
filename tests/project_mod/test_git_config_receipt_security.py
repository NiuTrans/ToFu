"""Risk and automatic-receipt axes for literal Git configuration."""
import pytest

from lib.project_mod.command_analysis import _git_guard_classify, git_invocation_parts

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('global_args', [
    '-c core.editor=/tmp/helper', '-ccore.editor=/tmp/helper',
    '-c core.pager=/tmp/helper', '-c alias.merge=!helper',
    '--config-env=core.editor=EDITOR', '--config-env core.editor=EDITOR',
    '--exec-path=/tmp/helpers', '--paginate', '-p',
    '-c merge.autostash=false', '-c merge.autostash=invalid',
])
@pytest.mark.parametrize('verb', ['merge --edit topic', 'status', 'add file'])
def test_helper_globals_never_borrow_native_authority(global_args, verb):
    command = f'git {global_args} {verb}'
    assert _git_guard_classify(command) is None
    assert _git_guard_classify(command, require_known_segments=True) is None


@pytest.mark.parametrize('global_args', [
    '-c core.editor=/tmp/helper', '--config-env=core.editor=EDITOR',
    '--exec-path=/tmp/helpers', '--paginate',
])
@pytest.mark.parametrize('verb', ['reset --hard', 'merge --abort', 'stash pop'])
def test_native_danger_outranks_helper_uncertainty(global_args, verb):
    command = f'git {global_args} {verb}'
    assert _git_guard_classify(command) == 'tree_destroying'
    assert _git_guard_classify(command, require_known_segments=True) is None


@pytest.mark.parametrize('value', ['true', 'yes', 'on', '1', ''])
def test_config_autostash_is_danger_without_receipt(value):
    command = f'git -c merge.autostash={value} merge topic'
    assert _git_guard_classify(command) == 'tree_destroying'
    assert _git_guard_classify(command, require_known_segments=True) is None


@pytest.mark.parametrize('prefix', [
    'GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.editor GIT_CONFIG_VALUE_0=helper',
    'env GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.editor GIT_CONFIG_VALUE_0=helper',
    'GIT_CONFIG_PARAMETERS=helper', 'GIT_CONFIG_KEY_2=core.editor',
    'GIT_CONFIG_VALUE_2=helper',
])
def test_inline_configuration_has_no_receipt(prefix):
    assert _git_guard_classify(f'{prefix} git merge topic', require_known_segments=True) is None
    assert _git_guard_classify(f'{prefix} git reset --hard') == 'tree_destroying'
    assert _git_guard_classify(f'{prefix} git reset --hard', require_known_segments=True) is None


@pytest.mark.parametrize('command', [
    'git -c color.ui=false merge topic', 'git -ccolor.ui=auto status',
    'git -c color.diff=never diff',
])
def test_bounded_presentation_configuration_preserves_authority(command):
    assert _git_guard_classify(command, require_known_segments=True) in {'readonly', 'safe_write'}


@pytest.mark.parametrize('command', [
    'git stash -m list', 'git stash -m show', 'git stash --message list',
    'git stash --message=show', 'git stash -mlist',
])
def test_stash_message_is_not_a_read_verb(command):
    assert _git_guard_classify(command) == 'tree_destroying'


@pytest.mark.parametrize('command, expected', [
    ('git branch --format --force', 'readonly'),
    ('git tag --format --delete', 'readonly'),
    ('git push --push-option --force origin main', 'safe_write'),
    ('git push --push-option=--delete origin main', 'safe_write'),
    ('git branch --format --force -D topic', 'tree_destroying'),
    ('git push --push-option --force --force origin main', 'tree_destroying'),
    ('git push --unknown origin main', None),
])
def test_values_do_not_masquerade_as_danger_flags(command, expected):
    assert _git_guard_classify(command) == expected


@pytest.mark.parametrize('suffix', ['git status', 'python helper.py'])
def test_pipeline_risk_and_receipt_are_independent(suffix):
    command = f'git -c core.editor=helper merge --abort && {suffix}'
    assert _git_guard_classify(command) == 'tree_destroying'
    assert _git_guard_classify(command, require_known_segments=True) is None


def test_public_global_parser_stays_syntax_only():
    assert git_invocation_parts(['-c', 'core.editor=helper', 'merge', 'topic']) == ('merge', ['topic'])


@pytest.mark.parametrize('command', [
    'git -c core.editor=/tmp/helper merge --edit topic',
    'git --config-env=core.editor=EDITOR merge topic',
    'git --exec-path=/tmp/helpers merge topic',
    'git -c merge.autostash=true merge topic',
    'env GIT_CONFIG_COUNT=1 git merge topic',
])
def test_real_pipeline_requests_approval_not_automatic_receipt(monkeypatch, command):
    import lib.project_mod.git_guard as guard
    import lib.tasks_pkg.tool_dispatch._approval as approval
    import lib.tasks_pkg.tool_dispatch._pipeline as pipeline

    receipts, approvals = [], []
    monkeypatch.setattr(guard, 'active_peers', lambda *args, **kwargs: [])
    monkeypatch.setattr(approval, 'mint_approval_receipt',
                        lambda *args, **kwargs: receipts.append(kwargs) or True)
    monkeypatch.setattr(pipeline, '_handle_approval',
                        lambda *args, **kwargs: approvals.append(kwargs) or (False, 'denied'))
    monkeypatch.setattr(pipeline, 'stamp_tool_rejection', lambda *args, **kwargs: None)
    monkeypatch.setattr(pipeline, '_settle_tool_result', lambda *args, **kwargs: 'denied')
    pipeline._approval_stops_tool_call(
        task={'id': 'security-task', 'convId': 'security-conv', '_userId': 1,
              'aborted': False},
        fn_name='run_command', fn_args={'command': command}, tc_id='security-call',
        rn=1, round_entry={}, confirmation_tools={'run_command'},
        write_tools={'run_command'}, attended=True, auto_apply=True, cfg={},
        project_path='/synthetic/git-security', round_num=1, model='m',
        tool_results={}, idempotent_tools=frozenset(), cache={}, tid='security-task')
    assert receipts == []
    assert len(approvals) == 1


@pytest.mark.parametrize('command', [
    'git branch --unknown -D topic', 'git push --unknown --force origin main',
])
def test_unknown_ref_options_do_not_hide_risk_or_mint_receipts(command):
    assert _git_guard_classify(command) == 'tree_destroying'
    assert _git_guard_classify(command, require_known_segments=True) is None


@pytest.mark.parametrize('command', ['git stash list --unknown', 'git stash show --unknown'])
def test_unknown_stash_inspection_requires_approval(command):
    assert _git_guard_classify(command) is None
    assert _git_guard_classify(command, require_known_segments=True) is None
