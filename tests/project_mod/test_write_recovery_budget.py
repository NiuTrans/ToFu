"""Large files remain editable, recoverable and undoable within one resource budget."""
from pathlib import Path
import base64
import os
import uuid

import pytest

from lib.project_mod import modifications as mods, write_intents as intents
from lib.project_mod.write_tools import _ops

pytestmark = pytest.mark.unit
MIB = 1024 * 1024


@pytest.fixture
def project(tmp_path, monkeypatch):
    from lib import file_history
    base = tmp_path / 'project'
    base.mkdir()
    monkeypatch.setenv('TOFU_PROJECT_WRITE_RECOVERY_MAX_MIB', '4')
    intents.recovery_file_bytes.cache_clear()
    monkeypatch.setattr(mods, 'SESSIONS_DIR', str(tmp_path / 'sessions'))
    monkeypatch.setattr(file_history, 'track_edit', lambda *a, **kw: None)
    monkeypatch.setattr(_ops, '_resolve_write_path', lambda root, path, **kw: str(base / path))
    monkeypatch.setattr(_ops, '_mod_attribution', lambda target, root, path, **kw: (str(base), path))
    monkeypatch.setattr(_ops, '_should_record_modification', lambda *a, **kw: True)
    yield base
    intents.recovery_file_bytes.cache_clear()


def large_original():
    # More bytes than the old 512 KiB cap, fewer characters: count UTF-8 bytes.
    return ('历史\r\n' * 80000 + 'unique anchor\r\n').encode()


@pytest.mark.parametrize('action', ['append', 'overwrite', 'upload', 'diff', 'insert', 'atomic-diffs', 'edit'])
def test_large_edit_preserves_history_and_exact_undo(project, action):
    original = large_original()
    target = project / 'JOURNAL.md'
    target.write_bytes(original)
    base = str(project)
    args = dict(task_id='large-file-task')
    if action == 'append':
        result = _ops.tool_write_file(base, 'JOURNAL.md', 'new evidence\r\n', append=True, **args)
        assert target.read_bytes() == original + b'new evidence\r\n'
    elif action == 'overwrite':
        result = _ops.tool_write_file(base, 'JOURNAL.md', 'replacement', **args)
    elif action == 'upload':
        result = _ops.save_uploaded_file(base, 'JOURNAL.md', b'replacement', on_conflict='overwrite', **args)
    elif action == 'diff':
        result = _ops.tool_apply_diff(base, 'JOURNAL.md', 'unique anchor', 'new anchor', **args)
    elif action == 'insert':
        result = _ops.tool_insert_content(base, 'JOURNAL.md', 'unique anchor', 'new evidence\r\n', **args)
    elif action == 'atomic-diffs':
        result = _ops.tool_apply_diffs(base, [{'path':'JOURNAL.md', 'search':'unique anchor', 'replace':'new anchor'}], atomic=True, **args)
    else:
        result = _ops.tool_edit_file(base, [{'path':'JOURNAL.md', 'operation':'replace', 'anchor':'unique anchor', 'content':'new anchor'}], **args)
    if isinstance(result, dict):
        assert result['ok'], result
    else:
        assert 'FAIL' not in result, result
    records = mods.get_modifications(base)
    assert len(records) == 1
    assert records[0]['postImage']['kind'] == 'content'
    undone = mods.undo_task_modifications(base, 'large-file-task')
    assert undone['undone'] == 1 and undone['failed'] == 0, undone
    assert target.read_bytes() == original


@pytest.mark.parametrize('phase', ['prepared', 'published'])
def test_large_prepared_intent_reopens_and_recovers_exactly(project, phase):
    original = large_original()
    target = project / 'JOURNAL.md'
    target.write_bytes(original)
    updated = original + b'evidence\n'
    stage = project / ('.tofu_atomic_' + uuid.uuid4().hex)
    with stage.open('wb') as handle:
        handle.write(updated)
        handle.flush()
        os.fsync(handle.fileno())
    session = mods._get_session_dir(str(project))
    with intents.locked_intents(session) as store:
        record = store.prepare(base_path=str(project), path='JOURNAL.md',
            original=original, post_content=updated, stage_name=stage.name, task_id='task')
    if phase == 'published':
        os.replace(stage, target)
    with intents.locked_intents(session) as store:
        assert store.read() == [record]
        assert base64.b64decode(record['original']) == original
    records = mods.get_modifications(str(project))
    with intents.locked_intents(session) as store:
        assert store.read() == []
    if phase == 'prepared':
        assert records == [] and target.read_bytes() == original
        assert not stage.exists()
    else:
        assert len(records) == 1
        undone = mods.undo_task_modifications(str(project), 'task')
        assert undone['undone'] == 1 and undone['failed'] == 0, undone
        assert target.read_bytes() == original


@pytest.mark.parametrize('which', ['original', 'post-image'])
def test_retired_recovery_budget_does_not_refuse_complete_mutation(project, which):
    original=b'x'*(4*MIB+1) if which=='original' else b'old\n'
    target=project/'JOURNAL.md';target.write_bytes(original)
    addition='new' if which=='original' else 'x'*(4*MIB)
    result=_ops.tool_write_file(str(project),'JOURNAL.md',addition,append=True,task_id='task')
    assert result['ok'],result
    assert target.read_bytes()==original+addition.encode()
    undone=mods.undo_task_modifications(str(project),'task')
    assert undone['failed']==0 and undone['undone']==1
    assert target.read_bytes()==original


def test_complete_base64_preimage_beyond_retired_budget_is_undoable(project):
    original = b'x' * (8 * MIB)
    target = project / 'JOURNAL.md'
    target.write_bytes(original)
    result = _ops.tool_write_file(str(project), 'JOURNAL.md', 'short', task_id='task')
    assert result['ok'], result
    assert mods.undo_task_modifications(str(project), 'task')['undone'] == 1
    assert target.read_bytes() == original


def test_retired_recovery_knob_is_not_a_launch_admission_budget():
    from bootstrap_pkg.resource_policy_generated import RESOURCE_FALLBACKS
    for defaults in RESOURCE_FALLBACKS.values():
        assert 'TOFU_PROJECT_WRITE_RECOVERY_MAX_MIB' not in defaults
    assert 'x-resource-budget' not in intents.contract()


def test_large_append_undo_refuses_later_user_edit(project):
    target = project / 'JOURNAL.md'
    target.write_bytes(large_original())
    result = _ops.tool_write_file(str(project), 'JOURNAL.md', 'evidence\n', append=True, task_id='task')
    assert result['ok'], result
    later = target.read_bytes() + b'user edit\n'
    target.write_bytes(later)
    undone = mods.undo_task_modifications(str(project), 'task')
    assert undone['failed'] == 1 and undone['undone'] == 0
    assert target.read_bytes() == later
    assert len(mods.get_modifications(str(project))) == 1


def test_smaller_restart_budget_preserves_pending_evidence(project, monkeypatch):
    original = b'x' * (2 * MIB)
    target = project / 'JOURNAL.md'
    target.write_bytes(original)
    updated = original + b'evidence\n'
    stage = project / ('.tofu_atomic_' + uuid.uuid4().hex)
    with stage.open('wb') as handle:
        handle.write(updated)
        handle.flush()
        os.fsync(handle.fileno())
    session = mods._get_session_dir(str(project))
    with intents.locked_intents(session) as store:
        record = store.prepare(base_path=str(project), path='JOURNAL.md', original=original,
            post_content=updated, stage_name=stage.name, task_id='task')
        evidence = Path(store.directory) / (record['changeId'] + '.json')
        before = evidence.read_bytes()
    os.replace(stage, target)
    monkeypatch.setenv('TOFU_PROJECT_WRITE_RECOVERY_MAX_MIB', '1')
    intents.recovery_file_bytes.cache_clear()
    with intents.locked_intents(session) as store:
        assert store.read()[0]['changeId']==record['changeId']
        assert intents.classify(store.read()[0])=='applied'
    assert evidence.read_bytes()==before
    assert target.read_bytes()==updated
    monkeypatch.setenv('TOFU_PROJECT_WRITE_RECOVERY_MAX_MIB', '4')
    intents.recovery_file_bytes.cache_clear()
    assert len(mods.get_modifications(str(project))) == 1
    assert mods.undo_task_modifications(str(project), 'task')['undone'] == 1
    assert target.read_bytes() == original


def test_undo_batch_budget_can_verify_one_maximum_direct_write(project, monkeypatch):
    monkeypatch.setattr(mods, 'UNDO_BATCH_CONTENT_BYTES', 128)
    result = _ops.tool_write_file(str(project), 'large.txt', 'x' * (4 * MIB), task_id='task')
    assert result['ok'], result
    undone = mods.undo_task_modifications(str(project), 'task')
    assert undone['undone'] == 1 and undone['failed'] == 0, undone
    assert not (project / 'large.txt').exists()


def test_utf8_postimage_preserves_complete_bytes_beyond_retired_budget(project):
    target=project/'JOURNAL.md';target.write_bytes(b'original')
    text='历'*(4*MIB//3+1)
    result=_ops.tool_write_file(str(project),'JOURNAL.md',text,task_id='task')
    assert result['ok'],result
    assert target.read_bytes()==text.encode()
    undone=mods.undo_task_modifications(str(project),'task')
    assert undone['failed']==0 and undone['undone']==1
    assert target.read_bytes()==b'original'
