"""A background command reports what the shell reported, syntax errors included.

The durable wrapper writes its exit sentinel only after the command ran. When
the shell rejected the script first, the record held no streams and no status,
and the result read ``[exit code: -1]`` with no output while the same command
in the foreground printed the shell's diagnostic and exit status 2.
"""
import pytest

from lib.project_mod import tools
from lib.project_mod.tool_admission import RecordedCommandSource
from lib.tasks_pkg.background_commands import _result_from_record, _wrap_for_record

pytestmark = [pytest.mark.unit, pytest.mark.skipif(
    __import__('os').name != 'posix', reason='durable records are POSIX-only')]


def _run(root, command, *, original=None):
    task = {'id': 'background-shell-errors', 'config': {'projectPath': str(root)}}
    binding = RecordedCommandSource(original, command) if original is not None else None
    return tools.execute_tool('run_command', {'command': command}, str(root), task=task,
                              recorded_command_source=binding)


@pytest.fixture
def record(tmp_path):
    root = tmp_path / 'project'
    root.mkdir()
    records = tmp_path / 'record'
    records.mkdir()
    paths = {key: str(records / key) for key in ('stdout', 'stderr', 'exit')}
    return root, paths


def test_a_rejected_script_matches_the_foreground_result(record):
    root, paths = record
    command = 'echo start; if then fi'
    runner_result = _run(root, _wrap_for_record(command, paths), original=command)

    background = _result_from_record({'command': command, 'credentials': {}}, paths, runner_result=runner_result)

    assert background == _run(root, command)


@pytest.mark.parametrize('command', [
    'echo start; if then fi',
    'echo a; done',
    'case x in esac esac',
    'for x in a; do',
])
def test_a_shell_syntax_error_reports_the_shell_diagnostic_and_status(record, command):
    # Around the wrapper's subshell the parser may word a diagnostic
    # differently (it expects the closing parenthesis); the shell's own
    # diagnostic and its exit status are what the result must carry.
    root, paths = record
    runner_result = _run(root, _wrap_for_record(command, paths), original=command)

    background = _result_from_record({'command': command, 'credentials': {}}, paths, runner_result=runner_result)

    assert background.startswith(f'$ {command}\n')
    assert background.endswith('\n[exit code: 2]')
    assert 'rror' in background  # dash: "Syntax error"; bash: "syntax error"


def test_a_completed_command_still_reports_its_recorded_streams(record):
    root, paths = record
    command = 'printf out; printf err >&2; exit 3'
    runner_result = _run(root, _wrap_for_record(command, paths), original=command)

    background = _result_from_record({'command': command, 'credentials': {}}, paths, runner_result=runner_result)

    assert background == f'$ {command}\nout\n[stderr]\nerr\n\n[exit code: 3]'


def test_without_any_status_evidence_the_record_reports_an_unknown_exit(record):
    _root, paths = record
    assert _result_from_record({'command': 'true', 'credentials': {}}, paths, runner_result='') == (
        '$ true\n\n[exit code: -1]')
    assert _result_from_record({'command': 'true', 'credentials': {}}, paths, runner_result='garbled') == (
        '$ true\n\n[exit code: -1]')
