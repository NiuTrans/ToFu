"""Candidate writers and extracted visuals preserve failure/cleanup ownership."""
import asyncio
from pathlib import Path

import pytest

from lib.knowledge.asset_spool import OwnedAssetSequence
from tests.knowledge.test_knowledge_local_publication import files
from tests.knowledge.test_knowledge_publication import prepare_operation

pytestmark = pytest.mark.unit
pytest_plugins = ('tests._knowledge_sidecar',)


@pytest.mark.parametrize('writer', ['_write_source', '_write_asset'])
def test_successful_rename_does_not_depend_on_removing_a_missing_temporary(
        isolated_knowledge, monkeypatch, writer):
    ordinary = Path.unlink

    def refuse_temporary(path, *args, **kwargs):
        if path.suffix == '.tmp':
            raise KeyboardInterrupt('unnecessary temporary cleanup interrupted')
        return ordinary(path, *args, **kwargs)

    failure = None
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'unlink', refuse_temporary)
        try:
            path = getattr(isolated_knowledge, writer)(b'evidence', 'candidate.bin', user_id=1)
        except BaseException as error:
            failure = error
    assert failure is None
    assert path.read_bytes() == b'evidence'
    assert list(path.parent.iterdir()) == [path]


@pytest.mark.parametrize('writer', ['_write_source', '_write_asset'])
@pytest.mark.parametrize('kind', [OSError, asyncio.CancelledError])
@pytest.mark.parametrize('cleanup_refused', [False, True])
def test_writer_failure_owns_temporary_cleanup(
        isolated_knowledge, monkeypatch, writer, kind, cleanup_refused):
    store = isolated_knowledge
    failure = kind('file flush failed')
    ordinary = Path.unlink

    def flush(_descriptor):
        raise failure

    def unlink(path, *args, **kwargs):
        if cleanup_refused and path.suffix == '.tmp':
            raise KeyboardInterrupt('temporary cleanup interrupted')
        return ordinary(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(store.os, 'fsync', flush)
        patch.setattr(Path, 'unlink', unlink)
        with pytest.raises(BaseException) as caught:
            getattr(store, writer)(b'evidence', 'candidate.bin', user_id=1)
    assert caught.value is failure
    remaining = files(store)
    if cleanup_refused:
        assert any('KeyboardInterrupt' in note for note in failure.__notes__)
        assert len(remaining) == 1 and Path(next(iter(remaining))).suffix == '.tmp'
    else:
        assert not remaining


def own_extracted_assets(store, monkeypatch, close_failure, *, count=1):
    extract, close = store.extract, OwnedAssetSequence.close
    owned, closed = [], []

    def parse(*args, **kwargs):
        parsed = extract(*args, **kwargs)
        spool = OwnedAssetSequence()
        for index in range(count):
            spool.append(dict(parsed['assets'][0], caption=f'visual {index}'))
        parsed['assets'] = spool
        owned.append(spool)
        return parsed

    def close_owned(spool):
        close(spool)
        if any(item is spool for item in owned):
            closed.append(id(spool))
            raise close_failure

    monkeypatch.setattr(store, 'extract', parse)
    monkeypatch.setattr(OwnedAssetSequence, 'close', close_owned)
    return owned, closed


@pytest.mark.parametrize('operation', ['create', 'reindex'])
@pytest.mark.parametrize('kind', [OSError, KeyboardInterrupt])
def test_visual_close_failure_rolls_back_candidates_and_is_not_repeated(
        isolated_knowledge, tmp_path, monkeypatch, operation, kind):
    store = isolated_knowledge
    invoke, _ = prepare_operation(store, tmp_path, monkeypatch, operation)
    before = files(store)
    failure = kind('visual spool close failed')
    with monkeypatch.context() as patch:
        owned, closed = own_extracted_assets(store, patch, failure)
        with pytest.raises(BaseException) as caught:
            invoke()
    assert caught.value is failure
    assert len(owned) == len(closed) == 1 and owned[0]._closed
    assert files(store) == before


def test_visual_preparation_keeps_primary_and_attempts_all_file_and_spool_cleanup(
        isolated_knowledge, tmp_path, monkeypatch):
    store = isolated_knowledge
    invoke, _ = prepare_operation(store, tmp_path, monkeypatch, 'create')
    primary = ValueError('second visual projection failed')
    secondary = asyncio.CancelledError('spool cleanup cancelled')
    project, unlink = store.proxy_text, store._unlink_files
    attempts, calls = [], []

    def project_visual(name, asset):
        if asset['caption'] == 'visual 1':
            raise primary
        return project(name, asset)

    def unlink_files(paths, **kwargs):
        attempts.extend(paths)
        calls.append(True)
        if len(calls) == 1:
            raise KeyboardInterrupt('first candidate cleanup interrupted')
        return unlink(paths, **kwargs)

    with monkeypatch.context() as patch:
        owned, closed = own_extracted_assets(store, patch, secondary, count=2)
        patch.setattr(store, 'proxy_text', project_visual)
        patch.setattr(store, '_unlink_files', unlink_files)
        with pytest.raises(BaseException) as caught:
            invoke()
    assert caught.value is primary
    assert len(owned) == len(closed) == 1 and owned[0]._closed
    assert len(attempts) == len(set(attempts)) == 2
    assert attempts[0].exists() and not attempts[1].exists()
    assert any('KeyboardInterrupt' in note for note in primary.__notes__)
    assert any('CancelledError' in note for note in primary.__notes__)


@pytest.mark.parametrize('operation', ['create', 'reindex'])
def test_chunk_failure_keeps_primary_when_untransferred_spool_close_fails(
        isolated_knowledge, tmp_path, monkeypatch, operation):
    store = isolated_knowledge
    invoke, _ = prepare_operation(store, tmp_path, monkeypatch, operation)
    before = files(store)
    primary = ValueError('chunk preparation failed')

    def chunk(_text):
        raise primary

    with monkeypatch.context() as patch:
        owned, closed = own_extracted_assets(store, patch, asyncio.CancelledError('close cancelled'))
        patch.setattr(store, 'chunk_document', chunk)
        with pytest.raises(BaseException) as caught:
            invoke()
    assert caught.value is primary
    assert len(owned) == len(closed) == 1 and owned[0]._closed
    assert files(store) == before
    assert any('CancelledError' in note for note in primary.__notes__)
