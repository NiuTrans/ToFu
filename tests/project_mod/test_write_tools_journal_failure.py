"""A completed filesystem mutation must not claim durable undo on journal loss."""
import pytest
from lib.project_mod.write_tools import _ops

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('action', ['write', 'upload', 'diff', 'insert', 'diffs', 'inserts', 'edit'])
@pytest.mark.parametrize('fault', ['false', 'exception', 'missing-ack'])
def test_applied_write_reports_failed_undo_without_repeating_or_rolling_back(tmp_path, monkeypatch, action, fault):
    target = tmp_path / 'file.txt'
    target.write_text('old\n')
    monkeypatch.setattr(_ops, '_resolve_write_path', lambda base, path, **kw: str(tmp_path / path))
    monkeypatch.setattr(_ops, '_should_record_modification', lambda *a, **kw: True)
    monkeypatch.setattr(_ops, '_mod_attribution', lambda target, base, path, **kw: (base, path))
    monkeypatch.setattr(_ops, '_record_write_freshness', lambda *a, **kw: None)
    monkeypatch.setattr(_ops, '_touch_for_vscode', lambda *a: None)
    def record(*args, **kwargs):
        assert target.read_text() != 'old\n'
        # A concurrent edit after our atomic replacement must be preserved.
        target.write_text('concurrent user edit\n')
        if fault == 'exception':
            raise OSError('injected journal storage failure')
        return None if fault == 'missing-ack' else False
    monkeypatch.setattr(_ops, '_record_modification', record)
    base = str(tmp_path)
    if action == 'write': result = _ops.tool_write_file(base, 'file.txt', 'new\n')
    elif action == 'upload': result = _ops.save_uploaded_file(base, 'file.txt', b'new\n', on_conflict='overwrite')
    elif action == 'diff': result = _ops.tool_apply_diff(base, 'file.txt', 'old', 'new')
    elif action == 'insert': result = _ops.tool_insert_content(base, 'file.txt', 'old', 'new\n')
    elif action == 'diffs': result = _ops.tool_apply_diffs(base, [{'path':'file.txt', 'search':'old', 'replace':'new'}])
    elif action == 'edit': result = _ops.tool_edit_file(base, [{'path':'file.txt', 'operation':'replace', 'anchor':'old', 'content':'new'}])
    else: result = _ops.tool_insert_contents(base, [{'path':'file.txt', 'anchor':'old', 'content':'new\n'}])
    assert target.read_text() == 'concurrent user edit\n'
    if isinstance(result, dict):
        assert result['ok'] is False
        assert result['errorKind'] == 'undo-record-failed'
        assert result['mutationApplied'] is True and result['undoRecorded'] is False
        message = result['error']
    else:
        message = result
        assert 'undo-record-failed' in message
        assert '1 edits were applied without confirmed undo' in message
    assert 'already applied' in message
    assert 'Do not retry' in message


@pytest.mark.parametrize('name', ['apply_diffs', 'insert_contents', 'edit_file'])
def test_partial_journal_outcome_metadata_does_not_claim_success(name):
    from lib.tools.meta import build_project_tool_meta
    result = build_project_tool_meta(name, {'edits': [{'path': 'file.txt'}]},
        'Completed 0/1 edits (1 failed); 1 edits were applied without confirmed undo\n'
        '[1] FAIL file.txt [undo-record-failed]: File change was already applied')
    assert result['writeOk'] is False
    assert '0/1' in result['snippet']
    assert '?' not in result['snippet']
