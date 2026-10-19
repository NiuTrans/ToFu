"""Undo evidence preserves exact bytes; missing originals never authorize writes."""
import builtins

import pytest
from lib.project_mod.write_tools import _ops
from lib.project_mod import modifications as mods

pytestmark = pytest.mark.unit


@pytest.fixture
def project(tmp_path, monkeypatch):
    base = tmp_path / 'project'
    base.mkdir()
    monkeypatch.setattr(mods, 'SESSIONS_DIR', str(tmp_path / 'journals'))
    monkeypatch.setattr(_ops, '_resolve_write_path', lambda root, path, **kw: str(base / path))
    monkeypatch.setattr(_ops, '_mod_attribution', lambda target, root, path, **kw: (str(base), path))
    monkeypatch.setattr(_ops, '_should_record_modification', lambda *a, **kw: True)
    return base


@pytest.mark.parametrize('original', [b'old\r\ntext\r\n', b'\xff\x00old\x80'])
def test_overwrite_undo_restores_exact_original_bytes(project, original):
    target = project / 'file.txt'
    target.write_bytes(original)
    result = _ops.tool_write_file(str(project), 'file.txt', 'replacement\n', task_id='task')
    assert result['ok'], result
    undone = mods.undo_task_modifications(str(project), 'task')
    assert undone['undone'] == 1, undone
    assert target.read_bytes() == original


@pytest.mark.parametrize('action', ['write', 'upload'])
def test_unreadable_existing_file_is_not_overwritten_or_recorded_as_created(project, monkeypatch, action):
    target = project / 'file.txt'
    target.write_bytes(b'original')
    def deny(path, *args, **kwargs):
        if str(path) == str(target):
            raise PermissionError('injected preimage read failure')
        return builtins.open(path, *args, **kwargs)
    monkeypatch.setattr(_ops, 'open', deny, raising=False)
    if action == 'write':
        result = _ops.tool_write_file(str(project), 'file.txt', 'replacement', task_id='task')
    else:
        result = _ops.save_uploaded_file(str(project), 'file.txt', b'replacement', task_id='task', on_conflict='overwrite')
    assert result['ok'] is False
    assert result['errorKind'] == 'read-failed'
    assert result['mutationApplied'] is False
    assert target.read_bytes() == b'original'
    assert mods.get_modifications(str(project)) == []


@pytest.mark.parametrize('action', ['append', 'diff', 'insert', 'diffs', 'inserts', 'edit'])
def test_text_edits_refuse_lossy_decoding(project, action):
    original = b'\xffold\r\n'
    target = project / 'file.txt'
    target.write_bytes(original)
    base = str(project)
    if action == 'append': result = _ops.tool_write_file(base, 'file.txt', 'new', append=True)
    elif action == 'diff': result = _ops.tool_apply_diff(base, 'file.txt', 'old', 'new')
    elif action == 'insert': result = _ops.tool_insert_content(base, 'file.txt', 'old', 'new')
    elif action == 'diffs': result = _ops.tool_apply_diffs(base, [{'path':'file.txt', 'search':'old', 'replace':'new'}])
    elif action == 'edit': result = _ops.tool_edit_file(base, [{'path':'file.txt', 'operation':'replace', 'anchor':'old', 'content':'new'}])
    else: result = _ops.tool_insert_contents(base, [{'path':'file.txt', 'anchor':'old', 'content':'new'}])
    assert target.read_bytes() == original
    if isinstance(result, dict):
        assert result['ok'] is False and result['errorKind'] == 'read-failed'
    else:
        assert 'FAIL' in result and 'read-failed' in result
    assert mods.get_modifications(base) == []


@pytest.mark.parametrize('action', ['append', 'diff', 'insert', 'diffs', 'inserts', 'edit'])
@pytest.mark.parametrize('history_enabled', [False, True])
def test_text_edit_undo_preserves_crlf(project, monkeypatch, action, history_enabled):
    if not history_enabled:
        import lib.file_history as history
        monkeypatch.setattr(history, 'is_enabled', lambda: False)
    original = b'old\r\nuntouched\r\n'
    target = project / 'file.txt'
    target.write_bytes(original)
    base = str(project)
    if action == 'append': result = _ops.tool_write_file(base, 'file.txt', 'new\r\n', append=True, task_id='task')
    elif action == 'diff': result = _ops.tool_apply_diff(base, 'file.txt', 'old', 'new', task_id='task')
    elif action == 'insert': result = _ops.tool_insert_content(base, 'file.txt', 'old', 'new\r\n', task_id='task')
    elif action == 'diffs': result = _ops.tool_apply_diffs(base, [{'path':'file.txt', 'search':'old', 'replace':'new'}], task_id='task')
    elif action == 'edit': result = _ops.tool_edit_file(base, [{'path':'file.txt', 'operation':'replace', 'anchor':'old', 'content':'new'}], task_id='task')
    else: result = _ops.tool_insert_contents(base, [{'path':'file.txt', 'anchor':'old', 'content':'new\r\n'}], task_id='task')
    if isinstance(result, dict): assert result['ok'], result
    else: assert 'FAIL' not in result, result
    records = mods.get_modifications(base)
    if action == 'append':
        assert records[0]['originalContent'] == original.decode('utf-8')
        assert not records[0].get('originalContentB64')
    undone = mods.undo_task_modifications(base, 'task')
    assert undone['undone'] == 1, undone
    assert target.read_bytes() == original


@pytest.mark.parametrize('original,search,replacement,all_matches', [
    ('new old', 'old', 'new', False),
    ('prefix old', 'old', '', False),
    ('old old', 'old', 'new', True),
    ('one\r\ntwo\r\n', 'one\ntwo', 'changed', False),
])
def test_reverse_patch_is_exact_without_file_history(project, monkeypatch, original, search, replacement, all_matches):
    import lib.file_history as history
    monkeypatch.setattr(history, 'is_enabled', lambda: False)
    target = project / 'file.txt'
    target.write_bytes(original.encode('utf-8'))
    result = _ops.tool_apply_diff(str(project), 'file.txt', search, replacement, replace_all=all_matches, task_id='task')
    assert result['ok'], result
    undone = mods.undo_task_modifications(str(project), 'task')
    assert undone['undone'] == 1, undone
    assert target.read_bytes() == original.encode('utf-8')


@pytest.mark.parametrize('action', ['diff', 'insert'])
def test_unavailable_inverse_evidence_refuses_mutation(project, monkeypatch, action):
    from lib.project_mod.write_intents import IntentError, IntentStore
    def refuse_evidence(*args, **kwargs):
        raise IntentError('injected undo evidence persistence failure')
    monkeypatch.setattr(IntentStore, 'prepare', refuse_evidence)
    original = b'newnewnew old\r\n' + b'x' * 15
    target = project / 'file.txt'
    target.write_bytes(original)
    if action == 'diff':
        result = _ops.tool_apply_diff(str(project), 'file.txt', 'old', 'newnewnew')
    else:
        result = _ops.tool_insert_content(str(project), 'file.txt', 'old', 'added')
    assert result['ok'] is False
    assert 'injected undo evidence persistence failure' in result['error']
    assert target.read_bytes() == original
    assert mods.get_modifications(str(project)) == []
