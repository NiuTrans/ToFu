"""Durable background wrappers retain original workspace write admission."""
from unittest.mock import Mock

import pytest

from lib.project_mod import tools
from lib.project_mod.tool_admission import RecordedCommandSource
from lib.tasks_pkg.background_commands import _wrap_for_record

pytestmark = pytest.mark.unit


@pytest.fixture
def dispatch(monkeypatch, tmp_path):
    runner = Mock(return_value='executed')
    monkeypatch.setattr(tools, '_exec_run_command_with_writer', runner)
    # Model a reference root: real workspace policy must reject writes there.
    task = {'id': 'recorded-admission', 'config': {
        'projectPath': str(tmp_path), 'readOnlyPaths': [str(tmp_path)]},
        '_workspaceExecution': {'repositories': [
            {'canonicalRoot': str(tmp_path), 'workspacePath': str(tmp_path / 'owned')} ]}}
    paths = {key: str(tmp_path / key) for key in ('stdout', 'stderr', 'exit')}

    def execute(original, *, binding=True, suffix='', spoof=False):
        wrapped = _wrap_for_record(original, paths)
        args = {'command': wrapped + suffix}
        if spoof:
            args['recorded_command_source'] = RecordedCommandSource(original, wrapped)
        return tools.execute_tool(
            'run_command', args, str(tmp_path), task=task,
            recorded_command_source=(RecordedCommandSource(original, wrapped)
                                     if binding else None))

    return execute, runner, tmp_path


def test_trusted_record_wrapper_does_not_turn_read_into_reference_write(dispatch):
    execute, runner, root = dispatch
    assert execute('printf hello') == 'executed'
    executed = runner.call_args.args[1]
    assert executed == _wrap_for_record('printf hello', {
        key: str(root / key) for key in ('stdout', 'stderr', 'exit')})


def test_original_reference_write_still_refused(dispatch):
    execute, runner, root = dispatch
    assert 'workspace-isolation' in execute(f'printf bad > {root}/model-output')
    runner.assert_not_called()


@pytest.mark.parametrize('spoof', [False, True])
def test_unbound_wrapper_has_no_system_output_exemption(dispatch, spoof):
    execute, runner, _ = dispatch
    assert 'workspace-isolation' in execute('printf hello', binding=False, spoof=spoof)
    runner.assert_not_called()


def test_modified_wrapper_fails_closed(dispatch):
    execute, runner, _ = dispatch
    with pytest.raises(ValueError, match='wrapper changed'):
        execute('printf hello', suffix='; printf extra')
    runner.assert_not_called()


def test_trusted_wrapper_executes_and_records_streams_on_reference_root(tmp_path):
    reference = tmp_path / 'reference'
    reference.mkdir()
    records = tmp_path / 'records'
    records.mkdir()
    paths = {key: str(records / key) for key in ('stdout', 'stderr', 'exit')}
    original = 'printf hello; printf warning >&2'
    wrapped = _wrap_for_record(original, paths)
    task = {'id': 'recorded-real', 'config': {
        'projectPath': str(reference), 'readOnlyPaths': [str(reference)]},
        '_workspaceExecution': {'repositories': [
            {'canonicalRoot': str(reference),
             'workspacePath': str(tmp_path / 'owned')}]}}
    result = tools.execute_tool(
        'run_command', {'command': wrapped}, str(reference), task=task,
        recorded_command_source=RecordedCommandSource(original, wrapped))
    assert '[exit code: 0]' in result
    assert (records / 'stdout').read_text() == 'hello'
    assert (records / 'stderr').read_text() == 'warning'
    assert (records / 'exit').read_text() == '0'
    assert list(reference.iterdir()) == []


def test_opaque_python_background_wrapper_keeps_owned_workspace_admission(tmp_path):
    import sys
    import shlex

    canonical = tmp_path / 'canonical'
    owned = tmp_path / 'owned'
    records = canonical / 'records'
    records.mkdir(parents=True)
    owned.mkdir()
    (owned / 'check.py').write_text('print("checked")\n')
    paths = {key: str(records / key) for key in ('stdout', 'stderr', 'exit')}
    original = f'{shlex.quote(sys.executable)} check.py'
    wrapped = _wrap_for_record(original, paths)
    task = {'id': 'opaque-recorded', 'config': {'projectPath': str(owned)},
            '_workspaceExecution': {'repositories': [
                {'canonicalRoot': str(canonical), 'workspacePath': str(owned)}]}}
    result = tools.execute_tool(
        'run_command', {'command': wrapped}, str(owned), task=task,
        recorded_command_source=RecordedCommandSource(original, wrapped))
    assert '[exit code: 0]' in result
    assert (records / 'stdout').read_text() == 'checked\n'
    assert (records / 'exit').read_text() == '0'
