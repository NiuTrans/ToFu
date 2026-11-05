"""FileHistory never resurrects a retired project root.

An isolated task's managed checkout can be reclaimed before its deferred round
commit runs. The store lives inside the project root, so recording history for
that root must not recreate the checkout directory as an orphan.
"""
from __future__ import annotations

import pytest

from lib.file_history import api
from lib.file_history import store

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.setenv('TOFU_FILE_HISTORY', '1')


def test_round_commit_for_reclaimed_checkout_does_not_recreate_it(tmp_path):
    retired = tmp_path / 'worktrees' / 'a58634e1b0ba' / 'u1' / 'task-clone'
    retired.parent.mkdir(parents=True)

    assert api.get_last_snapshot_id(str(retired)) is None
    assert api.make_snapshot(str(retired), task_id='task', rel_paths=['task.txt']) is None
    assert api.track_edit(str(retired), 'task.txt', pre_content=b'old\n') is None

    assert not retired.exists()


def test_store_bootstrap_requires_an_existing_project_root(tmp_path):
    retired = tmp_path / 'task-clone'

    with pytest.raises(FileNotFoundError):
        store.ensure_store(str(retired))
    assert not retired.exists()

    retired.mkdir()
    assert store.ensure_store(str(retired)) == store.store_dir(str(retired))
    assert (retired / '.tofu' / 'file-history' / 'README.txt').is_file()
    assert (retired / '.tofu' / 'file-history' / 'backups').is_dir()
