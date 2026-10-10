"""Durable replacement ordering and truthful post-rename failure outcomes."""
import os
import stat

import pytest
from lib.project_mod.write_tools import _ops

pytestmark = pytest.mark.unit


def test_replacement_flushes_final_mode_and_parent(tmp_path, monkeypatch):
    target = tmp_path / 'file.txt'
    target.write_text('old')
    target.chmod(0o640)
    observed = []
    fsync = os.fsync
    def sync(fd):
        mode = os.fstat(fd).st_mode
        observed.append('directory' if stat.S_ISDIR(mode) else stat.S_IMODE(mode))
        return fsync(fd)
    monkeypatch.setattr(os, 'fsync', sync)
    _ops._atomic_write_bytes(str(target), b'new')
    assert observed == [0o640, 'directory']
    assert target.read_bytes() == b'new'


@pytest.mark.parametrize('action', ['write', 'upload', 'diff', 'insert', 'diffs', 'inserts', 'edit'])
def test_directory_flush_failure_is_applied_not_retryable(tmp_path, monkeypatch, action):
    target = tmp_path / 'file.txt'
    target.write_text('old\n')
    monkeypatch.setattr(_ops, '_resolve_write_path', lambda base, path, **kw: str(tmp_path / path))
    monkeypatch.setattr(_ops, '_should_record_modification', lambda *a, **kw: False)
    fsync = os.fsync
    directory_fds = []
    def fail_directory(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode) and target.read_bytes() != b'old\n':
            directory_fds.append(fd)
            raise OSError('injected directory flush failure')
        return fsync(fd)
    monkeypatch.setattr(os, 'fsync', fail_directory)
    base = str(tmp_path)
    if action == 'write': result = _ops.tool_write_file(base, 'file.txt', 'new\n')
    elif action == 'upload': result = _ops.save_uploaded_file(base, 'file.txt', b'new\n', on_conflict='overwrite')
    elif action == 'diff': result = _ops.tool_apply_diff(base, 'file.txt', 'old', 'new')
    elif action == 'insert': result = _ops.tool_insert_content(base, 'file.txt', 'old', 'new\n')
    elif action == 'diffs': result = _ops.tool_apply_diffs(base, [{'path':'file.txt', 'search':'old', 'replace':'new'}])
    elif action == 'edit': result = _ops.tool_edit_file(base, [{'path':'file.txt', 'operation':'replace', 'anchor':'old', 'content':'new'}])
    else: result = _ops.tool_insert_contents(base, [{'path':'file.txt', 'anchor':'old', 'content':'new\n'}])
    assert target.read_text() != 'old\n'
    assert len(directory_fds) == 1
    with pytest.raises(OSError): os.fstat(directory_fds[0])
    assert not list(tmp_path.glob('.tofu_atomic_*'))
    if isinstance(result, dict):
        assert result['ok'] is False
        assert result['mutationApplied'] is True and result['durabilityConfirmed'] is False
        assert result['errorKind'] == 'write-durability-unconfirmed'
        message = result['error']
    else:
        message = result
        assert 'write-durability-unconfirmed' in message
    assert 'already applied' in message and 'Do not retry' in message
    assert 'NOT lost' not in message  # Must not suggest moving a stale salvage over the file.
