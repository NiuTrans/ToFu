"""Invalid cwd is a not-run refusal, never a retryable child exit."""
from unittest.mock import patch

import pytest

from lib.project_mod.run_command import tool_run_command
from lib.project_mod.tools import _exec_run_command
from lib.project_mod import config
from lib.tools.meta import build_project_tool_meta

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('kind', ['missing', 'file'])
def test_invalid_directory_never_spawns(tmp_path, kind):
    target = tmp_path / 'bad cwd?'
    if kind == 'file':
        target.write_text('not a directory')
    with patch('subprocess.Popen') as spawn:
        result = tool_run_command(str(target), 'echo should-not-run')
    spawn.assert_not_called()
    assert 'Command was not started' in result
    assert str(target) in result
    assert 'Correct working_dir' in result
    assert '[exit code:' not in result
    meta = build_project_tool_meta('run_command', {'command': 'echo should-not-run'}, result)
    assert meta['notRun'] is True


def test_invalid_explicit_cwd_preserves_sticky_and_blocks_fastpath(tmp_path):
    conv = 'test-cwd-preflight'
    sub = tmp_path / 'valid sub'
    sub.mkdir()
    config.set_conv_roots(conv, str(tmp_path))
    config.set_conv_cwd(conv, str(sub))
    try:
        with patch('lib.project_mod.tools.tool_list_dir') as reader, patch('lib.project_mod.tools.tool_run_command') as runner:
            result = _exec_run_command({'command': 'ls', 'working_dir': str(tmp_path / 'missing')}, str(tmp_path), conv, 'test', {})
        reader.assert_not_called()
        runner.assert_not_called()
        assert 'Command was not started' in result
        assert config.get_conv_cwd(conv) == str(sub)
    finally:
        config.clear_conv_state(conv)


def test_standalone_invalid_cwd_blocks_fastpath(tmp_path):
    from lib.project_mod.tools import execute_standalone_command
    with patch('lib.project_mod.tools.tool_list_dir') as reader:
        result = execute_standalone_command('run_command', {'command': 'ls'}, working_dir=str(tmp_path / 'missing'))
    reader.assert_not_called()
    assert 'Command was not started' in result


def test_existing_directory_with_spaces_runs(tmp_path):
    target = tmp_path / 'valid cwd?'
    target.mkdir()
    result = tool_run_command(str(target), 'pwd')
    assert str(target) in result
    assert '[exit code: 0]' in result


def test_invalid_cwd_reports_authorized_root_identity_without_fallback(tmp_path):
    from lib.project_mod.config import build_root_policy, register_root_policy
    primary = tmp_path / 'autoharness'
    secondary = tmp_path / 'llm-mcp-task'
    primary.mkdir()
    secondary.mkdir()
    (primary / 'work' / 'harbor').mkdir(parents=True)
    policy = build_root_policy(str(primary), [str(secondary)])
    conv = 'test-cwd-root-recovery'
    register_root_policy(conv, policy)
    try:
        with patch('subprocess.Popen') as spawn, patch('lib.project_mod.tools.tool_run_command') as runner:
            result = _exec_run_command(
                {'command': 'git status', 'working_dir': str(secondary / 'work' / 'harbor')},
                str(primary), conv, 'test', {})
        spawn.assert_not_called()
        runner.assert_not_called()
        assert 'Resolved workspace root: llm-mcp-task' in result
        assert f"autoharness: {str(primary)!r}" in result
        assert 'rootname:subdir' in result
        assert 'No fallback directory was used' in result
        assert config.get_conv_cwd(conv) is None
    finally:
        config.clear_conv_state(conv)


def test_cwd_recovery_uses_most_specific_root_and_bounds_listing(tmp_path):
    from lib.project_mod.run_command import validate_command_cwd
    roots = {'outer': {'path': str(tmp_path)},
             'nested': {'path': str(tmp_path / 'nested')}}
    roots.update({f'other-{i}': {'path': str(tmp_path / f'other-{i}')} for i in range(10)})
    result = validate_command_cwd(str(tmp_path / 'nested' / 'missing'), 'git status', roots=roots)
    assert 'Resolved workspace root: nested' in result
    assert '4 more registered roots' in result
    assert 'other-5:' in result
    assert 'other-6:' not in result
