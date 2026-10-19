"""Local metadata faults preserve old evidence and clean unpublished candidates."""
import asyncio
from types import SimpleNamespace

import pytest
from lib.knowledge.repository import KnowledgeRepository
from tests.knowledge.test_knowledge_publication import ASSET, RAW, prepare_operation

pytest_plugins = ('tests._knowledge_sidecar',)
pytestmark = pytest.mark.unit


def files(store):
    return {str(path): path.read_bytes()
            for root in [store._source_root(1), store._asset_root(1)]
            for path in root.glob('*')}


def refuse_document_metadata(store, operation, failure, monkeypatch):
    ordinary = store.json
    written = []
    def dumps(value, *args, **kwargs):
        selected = (isinstance(value, list) and kwargs.get('ensure_ascii') is False
                    if operation in ('create', 'reindex')
                    else isinstance(value, dict) and value.get('status') in ('pending', 'ready'))
        if selected:
            written.append(files(store))
            raise failure
        return ordinary.dumps(value, *args, **kwargs)
    monkeypatch.setattr(store, 'json', SimpleNamespace(dumps=dumps, loads=ordinary.loads))
    return written


@pytest.mark.parametrize('operation', ['create', 'reindex', 'media_create', 'media_replace'])
@pytest.mark.parametrize('kind', ['serialization', 'allocation', 'interrupt', 'cancel'])
def test_metadata_failure_rolls_back_only_unpublished_candidates(
        isolated_knowledge, tmp_path, monkeypatch, operation, kind):
    store = isolated_knowledge
    invoke, target = prepare_operation(store, tmp_path, monkeypatch, operation)
    before = files(store)
    failure = {'serialization': TypeError, 'allocation': MemoryError,
               'interrupt': KeyboardInterrupt, 'cancel': asyncio.CancelledError}[kind]('local metadata failure')
    commands = []
    original_command = KnowledgeRepository._command
    def command(owner, operation, *args, **kwargs):
        commands.append(operation)
        return original_command(owner, operation, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(KnowledgeRepository, '_command', command)
        written = refuse_document_metadata(store, operation, failure, patch)
        with pytest.raises(type(failure)) as caught:
            invoke()
    assert caught.value is failure
    assert target not in commands
    assert len(written) == 1
    candidates = {path: raw for path, raw in written[0].items() if path not in before}
    expected = {'create': [RAW, ASSET], 'reindex': [ASSET],
                'media_create': [RAW], 'media_replace': [ASSET]}[operation]
    assert sorted(candidates.values()) == sorted(expected)
    assert files(store) == before


def test_metadata_failure_keeps_primary_and_attempts_each_candidate_cleanup(
        isolated_knowledge, tmp_path, monkeypatch):
    store = isolated_knowledge
    invoke, _ = prepare_operation(store, tmp_path, monkeypatch, 'create')
    failure = TypeError('local metadata failure')
    cleanup = KeyboardInterrupt('cleanup interrupted')
    ordinary = store._unlink_files
    attempted = []
    def unlink(paths, **kwargs):
        attempted.extend(paths)
        if len(attempted) == 1:
            raise cleanup
        return ordinary(paths, **kwargs)
    with monkeypatch.context() as patch:
        refuse_document_metadata(store, 'create', failure, patch)
        patch.setattr(store, '_unlink_files', unlink)
        with pytest.raises(BaseException) as caught:
            invoke()
    assert caught.value is failure
    assert len(attempted) == len(set(attempted)) == 2
    assert attempted[0].exists() and not attempted[1].exists()
    assert any('KeyboardInterrupt' in note for note in failure.__notes__)
