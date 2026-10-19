"""Streamed media temporaries retain their own cleanup and error lifecycle."""
import asyncio
from pathlib import Path

import pytest

from tests.knowledge.test_knowledge_local_publication import files, refuse_document_metadata
from tests.knowledge.test_knowledge_publication import (
    RAW, assert_published_files, prepare_operation,
    test_postcommit_failure_preserves_original_and_visuals as check_unknown_commit,
)

pytestmark = pytest.mark.unit
pytest_plugins = ('tests._knowledge_sidecar',)


def refuse_temporary_unlink(monkeypatch, failure):
    ordinary = Path.unlink

    def unlink(path, *args, **kwargs):
        if path.suffix == '.tmp':
            raise failure
        return ordinary(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'unlink', unlink)


def test_committed_media_does_not_remove_an_already_transferred_temporary(
        isolated_knowledge, tmp_path, monkeypatch):
    store = isolated_knowledge
    invoke, _ = prepare_operation(store, tmp_path, monkeypatch, 'media_create')
    failure = None
    with monkeypatch.context() as patch:
        refuse_temporary_unlink(patch, KeyboardInterrupt('temporary cleanup interrupted'))
        try:
            result = invoke()
        except BaseException as error:
            failure = error
    assert failure is None
    assert not result['duplicate']
    assert_published_files(store, 'media_create')
    assert len(files(store)) == 1


@pytest.mark.parametrize('kind', [OSError, asyncio.CancelledError])
@pytest.mark.parametrize('cleanup_refused', [False, True])
def test_media_copy_failure_owns_remaining_temporary(
        isolated_knowledge, tmp_path, monkeypatch, kind, cleanup_refused):
    store = isolated_knowledge
    invoke, _ = prepare_operation(store, tmp_path, monkeypatch, 'media_create')
    failure = kind('media copy flush failed')

    def flush(_descriptor):
        raise failure

    with monkeypatch.context() as patch:
        patch.setattr(store.os, 'fsync', flush)
        if cleanup_refused:
            refuse_temporary_unlink(patch, KeyboardInterrupt('temporary cleanup interrupted'))
        with pytest.raises(BaseException) as caught:
            invoke()
    assert caught.value is failure
    remaining = files(store)
    if cleanup_refused:
        assert len(remaining) == 1
        assert Path(next(iter(remaining))).suffix == '.tmp'
        assert next(iter(remaining.values())) == RAW
        assert any('KeyboardInterrupt' in note for note in failure.__notes__)
    else:
        assert not remaining


@pytest.mark.parametrize('kind', [TypeError, MemoryError, asyncio.CancelledError])
def test_media_metadata_failure_is_not_masked_by_consumed_temporary_cleanup(
        isolated_knowledge, tmp_path, monkeypatch, kind):
    store = isolated_knowledge
    invoke, _ = prepare_operation(store, tmp_path, monkeypatch, 'media_create')
    failure = kind('media metadata failed')
    with monkeypatch.context() as patch:
        refuse_document_metadata(store, 'media_create', failure, patch)
        refuse_temporary_unlink(patch, KeyboardInterrupt('temporary cleanup interrupted'))
        with pytest.raises(BaseException) as caught:
            invoke()
    assert caught.value is failure
    assert not files(store)


def test_duplicate_media_still_removes_its_untransferred_temporary(
        isolated_knowledge, tmp_path, monkeypatch):
    store = isolated_knowledge
    invoke, _ = prepare_operation(store, tmp_path, monkeypatch, 'media_create')
    original = invoke()
    before = files(store)
    ordinary = Path.unlink
    removed = []

    def unlink(path, *args, **kwargs):
        if path.suffix == '.tmp':
            removed.append(path.read_bytes())
        return ordinary(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, 'unlink', unlink)
        duplicate = invoke()
    assert duplicate['duplicate'] and duplicate['id'] == original['id']
    assert removed == [RAW] and files(store) == before


def test_duplicate_cleanup_failure_does_not_join_an_unrelated_callers_exception(
        isolated_knowledge, tmp_path, monkeypatch):
    store = isolated_knowledge
    invoke, _ = prepare_operation(store, tmp_path, monkeypatch, 'media_create')
    original = invoke()
    cleanup = OSError('duplicate temporary cleanup failed')
    unrelated = ValueError('already handled caller failure')
    try:
        raise unrelated
    except ValueError:
        with monkeypatch.context() as patch:
            refuse_temporary_unlink(patch, cleanup)
            with pytest.raises(OSError) as caught:
                invoke()
    assert caught.value is cleanup
    assert not getattr(unrelated, '__notes__', [])
    assert assert_published_files(store, 'media_create')['id'] == original['id']
    assert len(files(store)) == 2


@pytest.mark.parametrize('kind', ['lost_reply', 'cancel'])
def test_unknown_media_commit_retains_primary_and_original_source(
        isolated_knowledge, tmp_path, monkeypatch, kind):
    failure = None
    with monkeypatch.context() as patch:
        refuse_temporary_unlink(patch, KeyboardInterrupt('consumed temporary cleanup interrupted'))
        try:
            check_unknown_commit(isolated_knowledge, tmp_path, patch, 'media_create', kind)
        except BaseException as error:
            failure = error
    assert failure is None
