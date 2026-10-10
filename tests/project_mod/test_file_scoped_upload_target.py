"""Rename-on-upload protects its actual candidate, not only the input path."""
from pathlib import Path
import threading

import pytest

from lib.project_mod.workspace_lease import workspace_file_write_lease
from lib.project_mod.write_tools import _ops
from tests.project_mod.test_file_scoped_publication import project as project  # noqa: F401

pytestmark = pytest.mark.unit


def test_busy_rename_candidate_does_not_get_overwritten_or_block_other_files(project, monkeypatch):
    monkeypatch.setenv('TOFU_WORKSPACE_TOOL_WAIT_SECONDS', '0')
    target = Path(project, 'first (1).txt')
    ready, release = threading.Event(), threading.Event()
    errors = []

    def hold_candidate():
        try:
            with workspace_file_write_lease(target):
                ready.set()
                assert release.wait(5)
        except BaseException as error:
            errors.append(error)
            ready.set()

    worker = threading.Thread(target=hold_candidate)
    worker.start()
    try:
        assert ready.wait(5)
        assert not errors
        result = _ops.save_uploaded_file(project, 'first.txt', b'upload', on_conflict='rename')
        assert result['ok'] is False, result
        assert not result.get('mutationApplied')
        assert not target.exists()
        assert Path(project, 'first.txt').read_text() == 'A'
        unrelated = _ops.tool_write_file(project, 'second.txt', 'other')
        assert unrelated['ok'], unrelated
    finally:
        release.set()
        worker.join(5)
        assert not worker.is_alive()
        assert not errors
    result = _ops.save_uploaded_file(project, 'first.txt', b'upload', on_conflict='rename')
    assert result['ok'], result
    assert Path(result['path']).read_bytes() == b'upload'


def test_candidate_occupied_between_selection_and_admission_is_not_clobbered(project, monkeypatch):
    select = _ops._dedupe_target
    intercepted = []

    def competing_creation(path):
        candidate = select(path)
        if not intercepted:
            Path(candidate).write_bytes(b'other-writer')
            intercepted.append(candidate)
        return candidate

    monkeypatch.setattr(_ops, '_dedupe_target', competing_creation)
    result = _ops.save_uploaded_file(project, 'first.txt', b'upload', on_conflict='rename')
    assert len(intercepted) == 1
    assert Path(intercepted[0]).read_bytes() == b'other-writer'
    assert Path(project, 'first.txt').read_bytes() == b'A'
    if result['ok']:
        assert result['path'] != intercepted[0]
        assert Path(result['path']).read_bytes() == b'upload'
    else:
        assert not result.get('mutationApplied')
