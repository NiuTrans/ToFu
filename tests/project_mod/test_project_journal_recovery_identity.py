"""Stable prepared-write identities and cross-process journal serialization."""
import json
import subprocess
import sys
import uuid

import pytest

from lib.project_mod import modifications as mods

pytestmark = pytest.mark.unit


@pytest.fixture
def journal(tmp_path, monkeypatch):
    monkeypatch.setattr(mods, 'SESSIONS_DIR', str(tmp_path / 'sessions'))
    base = tmp_path / 'project'
    base.mkdir()
    (base / 'file.txt').write_bytes(b'new\n')
    return str(base)


def test_replayed_identity_does_not_duplicate_history_or_listeners(journal, monkeypatch):
    from lib import file_history
    tracked, events = [], []
    monkeypatch.setattr(file_history, 'track_edit', lambda *a, **kw: tracked.append(kw) or None)
    monkeypatch.setattr(mods, '_live_change_listeners', [lambda *args: events.append(args)])
    change_id = uuid.uuid4().hex
    assert mods._record_modification(journal, 'apply_diff', 'file.txt', 'old\n',
        reverse_patch={'search':'new\n','replace':'old\n'}, conv_id='conv', task_id='task', change_id=change_id)
    evidence = mods.get_modifications(journal)[0]['postImage']
    assert mods._record_modification(journal, 'write_file', 'file.txt', b'old\n',
        conv_id='conv', task_id='task', change_id=change_id, post_image=evidence)
    assert len(mods.get_modifications(journal)) == len(tracked) == len(events) == 1


@pytest.mark.parametrize('changed', ['path', 'original', 'task', 'post'])
def test_conflicting_identity_never_replaces_committed_evidence(journal, changed):
    change_id = uuid.uuid4().hex
    kwargs = dict(original_content=b'old', task_id='task', change_id=change_id)
    assert mods._record_modification(journal, 'write_file', 'file.txt', **kwargs)
    before = mods.get_modifications(journal)
    path = 'file.txt'
    if changed == 'path': path = 'other.txt'
    elif changed == 'original': kwargs['original_content'] = b'other'
    elif changed == 'task': kwargs['task_id'] = 'another'
    else: kwargs['post_image'] = {'kind': 'unavailable'}
    with pytest.raises(ValueError, match='conflicting journal evidence'):
        mods._record_modification(journal, 'write_file', path, **kwargs)
    assert mods.get_modifications(journal) == before


def test_recovery_uses_prepared_postimage_not_later_user_edit(journal):
    from pathlib import Path
    from lib.project_mod.undo_filesystem import fingerprint_file
    target = Path(journal) / 'file.txt'
    expected = fingerprint_file(journal, str(target))
    target.write_bytes(b'later user edit')
    assert mods._record_modification(journal, 'write_file', 'file.txt', b'old',
        task_id='task', change_id=uuid.uuid4().hex, post_image=expected)
    assert mods.get_modifications(journal)[0]['postImage'] == expected
    result = mods.undo_task_modifications(journal, 'task')
    assert result['undone'] == 0 and result['failed'] == 1
    assert target.read_bytes() == b'later user edit'


def test_cross_process_append_cannot_be_erased_by_warm_cache(tmp_path):
    session = str(tmp_path)
    mods._locked_rmw(session, lambda rows: rows.append({'type':'write_file', 'path':'first'}))
    program = '''
import sys
from lib.project_mod import modifications as mods
mods._locked_rmw(sys.argv[1], lambda rows: rows.append({'type':'write_file', 'path':'child'}))
'''
    result = subprocess.run([sys.executable, '-c', program, session], capture_output=True, timeout=90)
    assert result.returncode == 0, result.stderr.decode(errors='replace')
    mods._locked_rmw(session, lambda rows: rows.append({'type':'write_file', 'path':'parent'}))
    rows = json.loads((tmp_path / 'modifications.json').read_text())['modifications']
    assert [row['path'] for row in rows] == ['first', 'child', 'parent']
