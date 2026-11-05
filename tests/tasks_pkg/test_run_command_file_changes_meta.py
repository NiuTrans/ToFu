"""A run_command round reports exactly the file changes that command recorded.

The round is presentation of one command, so it must neither re-read the
project's whole undo history after every command nor repeat changes that
earlier commands of the same task recorded.
"""
from unittest.mock import Mock

import pytest

from lib import file_history
from lib.project_mod import modifications as mods
from lib.project_mod import tools
from lib.tasks_pkg.handlers import project

pytestmark = pytest.mark.unit


@pytest.fixture
def sessions(tmp_path, monkeypatch):
    monkeypatch.setattr(mods, 'SESSIONS_DIR', str(tmp_path / 'sessions'))
    monkeypatch.setattr(file_history, 'track_edit', lambda *a, **kw: None)


def test_command_publishes_the_changes_it_recorded_for_undo(sessions, tmp_path):
    base = tmp_path / 'project'
    base.mkdir()
    (base / 'old.txt').write_text('before\n')
    recorded = []
    result = tools.execute_tool(
        'run_command', {'command': 'echo made > new.txt && echo after > old.txt'},
        str(base), task_id='task', recorded_file_changes=recorded)
    assert '[exit code: 0]' in result
    assert sorted(recorded, key=lambda change: change['path']) == [
        {'basePath': str(base), 'path': 'new.txt', 'action': 'created'},
        {'basePath': str(base), 'path': 'old.txt', 'action': 'modified'}]
    journal = {(row['path'], row['action']) for row in mods.get_modifications(str(base))}
    assert journal == {('new.txt', 'created'), ('old.txt', 'modified')}


def run_round(monkeypatch, root, recorded):
    finalized = Mock()

    def domain(fn_name, fn_args, base_path, **kwargs):
        kwargs['recorded_file_changes'].extend(recorded)
        return f"$ {fn_args['command']}\n\n[exit code: 0]"

    monkeypatch.setattr('lib.project_mod.execute_tool', domain)
    monkeypatch.setattr(project, '_finalize_tool_round', finalized)
    task = {'id': 'task-run', 'convId': 'conv', '_userId': 7, 'config': {},
            '_unattended': True}
    project._handle_project_tool(
        task, {}, 'run_command', 'tc', {'command': 'make'}, 1,
        {'toolName': 'run_command', 'toolCallId': 'tc'}, {}, root, True)
    return finalized.call_args.args[3][0]


def test_round_reports_only_this_commands_changes_without_reading_history(
        monkeypatch, tmp_path):
    monkeypatch.setattr(mods, 'get_modifications',
                        lambda *a, **kw: pytest.fail('whole undo history re-read'))
    root = str(tmp_path)
    meta = run_round(monkeypatch, root, [
        {'basePath': root, 'path': 'built.o', 'action': 'created'},
        {'basePath': root, 'path': 'stale.o', 'action': 'deleted'},
        # Recorded in a subdirectory's own journal, as before: not this root's.
        {'basePath': str(tmp_path / 'sub'), 'path': 'other.o', 'action': 'created'}])
    assert meta['fileChanges'] == [{'path': 'built.o', 'action': 'created'},
                                   {'path': 'stale.o', 'action': 'deleted'}]
    assert 'fileChanges' not in run_round(monkeypatch, root, [])
