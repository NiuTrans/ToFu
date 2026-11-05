"""Redo re-applies one round's own file changes, never the whole tracked map.

Each round snapshot pins the version of EVERY tracked file. Redo of an older
round must therefore restore only the paths that round changed relative to
its predecessor snapshot; restoring the full map rewinds files that later
rounds (or the user) changed after that round.
"""
from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def project(tmp_path, monkeypatch):
    from lib.project_mod import config as pm_config

    monkeypatch.setenv('TOFU_FILE_HISTORY', '1')
    root = tmp_path / 'project'
    root.mkdir()
    (root / 'shared.txt').write_text('shared original\n')
    (root / 'own.txt').write_text('own original\n')
    with pm_config._lock:
        pm_config._roots.clear()
        pm_config._roots['project'] = pm_config._make_root_state(str(root))
        pm_config._state['path'] = str(root)
    pm_config.set_conv_roots('conv-redo-scope', str(root))
    try:
        yield root
    finally:
        with pm_config._lock:
            pm_config._roots.clear()
            pm_config._state['path'] = ''


def _round(root, task_id, rel_path, content):
    from lib import file_history
    from lib.project_mod.write_tools import tool_write_file

    written = tool_write_file(str(root), rel_path, content,
                              conv_id='conv-redo-scope', task_id=task_id)
    assert written['ok'], written
    assert file_history.make_snapshot(
        str(root), task_id=task_id, conv_id='conv-redo-scope',
        rel_paths=[rel_path])


def test_redo_of_older_round_keeps_later_rounds_edits(project):
    from lib.project_mod.modifications import (
        redo_task_modifications,
        undo_task_modifications,
    )

    _round(project, 'task-first', 'shared.txt', 'first round\n')
    _round(project, 'task-target', 'own.txt', 'target round\n')
    _round(project, 'task-later', 'shared.txt', 'later round\n')

    undone = undo_task_modifications(str(project), 'task-target')
    assert undone['undone'] == 1 and undone['failed'] == 0, undone
    assert (project / 'own.txt').read_text() == 'own original\n'

    redone = redo_task_modifications(str(project), 'task-target')

    assert redone['ok'] is True, redone
    assert redone['files'] == ['own.txt']
    assert (project / 'own.txt').read_text() == 'target round\n'
    assert (project / 'shared.txt').read_text() == 'later round\n'


def test_backups_after_a_redo_never_overwrite_versions_other_rounds_pin(project):
    from lib import write_freshness
    from lib.project_mod.modifications import (
        redo_task_modifications,
        undo_task_modifications,
    )

    _round(project, 'task-first', 'shared.txt', 'first round\n')
    _round(project, 'task-second', 'shared.txt', 'second round\n')
    assert undo_task_modifications(str(project), 'task-second')['undone'] == 1
    assert undo_task_modifications(str(project), 'task-first')['undone'] == 1
    assert redo_task_modifications(str(project), 'task-first')['ok'] is True
    # The next round re-reads the file the redo changed before editing it.
    write_freshness.record('conv-redo-scope', os.path.realpath(project / 'shared.txt'))
    _round(project, 'task-third', 'shared.txt', 'third round\n')

    redone = redo_task_modifications(str(project), 'task-second')

    assert redone['ok'] is True, redone
    assert (project / 'shared.txt').read_text() == 'second round\n'


def test_redo_keeps_user_edits_to_files_the_round_did_not_touch(project):
    from lib.project_mod.modifications import (
        redo_task_modifications,
        undo_task_modifications,
    )

    _round(project, 'task-first', 'shared.txt', 'first round\n')
    _round(project, 'task-target', 'own.txt', 'target round\n')
    assert undo_task_modifications(str(project), 'task-target')['undone'] == 1
    (project / 'shared.txt').write_text('user edit after undo\n')

    redone = redo_task_modifications(str(project), 'task-target')

    assert redone['ok'] is True, redone
    assert (project / 'own.txt').read_text() == 'target round\n'
    assert (project / 'shared.txt').read_text() == 'user edit after undo\n'


@pytest.mark.parametrize('failure', ['missing_backup', 'denied_write'])
def test_redo_failures_are_typed_refused_or_fault_rows(project, monkeypatch, failure):
    from lib.file_history import api as fh_api
    from lib.project_mod import undo_filesystem
    from lib.project_mod.modifications import (
        redo_task_modifications,
        undo_task_modifications,
    )

    _round(project, 'task-target', 'own.txt', 'target round\n')
    assert undo_task_modifications(str(project), 'task-target')['undone'] == 1
    withheld = '/srv/private/project/own.txt'

    def deny(*_args, **_kwargs):
        raise PermissionError(13, 'Permission denied', withheld)

    if failure == 'missing_backup':
        monkeypatch.setattr(fh_api, 'read_blob', lambda *_args: None)
    else:
        monkeypatch.setattr(undo_filesystem, 'write_file', deny)

    redone = redo_task_modifications(str(project), 'task-target')

    assert redone['ok'] is False
    [row] = redone['details']['failed']
    assert row['path'] == 'own.txt'
    if failure == 'missing_backup':
        assert redone['refused'] is True and row['refused'] is True
        assert row['reason'].startswith('no backup for v')
    else:
        assert redone['refused'] is False and row['refused'] is False
        assert row['reason'] == 'internal error (PermissionError)'
    assert (project / 'own.txt').read_text() == 'own original\n'
