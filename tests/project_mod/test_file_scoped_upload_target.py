"""Rename-on-upload protects its actual candidate, not only the input path."""
import os
from pathlib import Path
import threading
import time

import pytest

from lib.project_mod.file_edit_mutex import file_edit_mutex
from lib.project_mod.write_tools import _ops
from tests.project_mod.test_file_scoped_publication import project as project  # noqa: F401

pytestmark = pytest.mark.unit


def test_rename_candidate_taken_while_waiting_is_never_overwritten(project):
    target = Path(project, 'first (1).txt')
    ready, release = threading.Event(), threading.Event()
    errors, results = [], []

    def hold_candidate():
        try:
            with file_edit_mutex([os.path.realpath(target)]):
                ready.set()
                assert release.wait(5)
                target.write_text('concurrent')
        except BaseException as error:
            errors.append(error)
            ready.set()

    worker = threading.Thread(target=hold_candidate)
    uploader = threading.Thread(target=lambda: results.append(
        _ops.save_uploaded_file(project, 'first.txt', b'upload', on_conflict='rename')))
    worker.start()
    try:
        assert ready.wait(5)
        assert not errors
        # The candidate's mutex never delays an unrelated file.
        unrelated = _ops.tool_write_file(project, 'second.txt', 'other')
        assert unrelated['ok'], unrelated
        uploader.start()
        time.sleep(0.2)
        assert results == []  # waits for the edit of its own candidate
    finally:
        release.set()
        worker.join(5)
        if uploader.is_alive() or uploader.ident is not None:
            uploader.join(5)
    assert not worker.is_alive() and not uploader.is_alive()
    assert not errors
    (result,) = results
    assert result['ok'] is False, result
    assert not result.get('mutationApplied')
    assert target.read_text() == 'concurrent'
    assert Path(project, 'first.txt').read_text() == 'A'
