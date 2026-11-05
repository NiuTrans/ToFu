"""Create and reindex publish complete evidence without repeating preparation."""
from collections import Counter
from functools import wraps
import sys
from types import SimpleNamespace

import pytest

from lib.knowledge.repository import KnowledgeRepository
from tests.knowledge.test_knowledge_local_publication import files
from tests.knowledge.test_knowledge_publication import RAW, prepare_operation

pytestmark = pytest.mark.unit
pytest_plugins = ('tests._knowledge_sidecar',)


@pytest.mark.parametrize('operation', ['create', 'reindex'])
@pytest.mark.parametrize('scope', ['library', 'attachment', 'draft'])
def test_document_publication_prepares_once_and_preserves_source_identity(
        isolated_knowledge, tmp_path, monkeypatch, request, operation, scope):
    store = isolated_knowledge
    prepare_operation(store, tmp_path, monkeypatch, 'create')
    repository = KnowledgeRepository(1)
    previous = None
    if operation == 'reindex':
        initial = store.add_document(RAW, 'source.txt', user_id=1, scope=scope)
        previous = repository.document(initial['id'])
    calls = Counter()
    extraction_options, directory_options, commands = [], [], []
    def projector():  # Text extraction borrows but does not invoke this port.
        raise AssertionError('plain text must not invoke the HTML projector')

    def watch(owner, name):
        original = getattr(owner, name)

        @wraps(original)
        def counted(*args, **kwargs):
            calls[name] += 1
            if name == 'extract':
                extraction_options.append(kwargs)
            elif name == '_sync_publication_directories':
                directory_options.append(kwargs)
            elif name in ('create_document', 'replace_document'):
                commands.append(kwargs['command_id'])
            return original(*args, **kwargs)

        monkeypatch.setattr(owner, name, counted)

    for stage in ('extract', 'chunk_document', '_prepare_visual_index',
                  '_indexed_chunks', '_sync_publication_directories',
                  '_write_source', '_write_asset'):
        watch(store, stage)
    for stage in ('create_document', 'replace_document', 'document',
                  'document_by_digest'):
        watch(KnowledgeRepository, stage)

    if operation == 'create':
        result = store.add_document(
            RAW, 'source.txt', user_id=1, scope=scope,
            html_projector=projector,
            command_id=f'pipeline-create:{request.node.name}')
    else:
        result = store.reindex_document(
            previous['id'], user_id=1,
            html_projector=projector,
            command_id=f'pipeline-reindex:{request.node.name}')

    assert calls == Counter({
        'extract': 1, 'chunk_document': 1, '_prepare_visual_index': 1,
        '_indexed_chunks': 1, '_sync_publication_directories': 1,
        '_write_asset': 1,
        **({'_write_source': 1, 'create_document': 1, 'document_by_digest': 1}
           if operation == 'create' else {'replace_document': 1, 'document': 1}),
    })
    assert extraction_options == [{'html_projector': projector}]
    assert directory_options == [{
        'user_id': 1, 'source': operation == 'create', 'assets': True}]
    assert commands == [f'pipeline-{operation}:{request.node.name}']
    assert result['scope'] == scope and result['asset_count'] == 1
    assert result['chunk_count'] >= 2  # Full text plus its visual evidence.
    current = repository.document(result['id'])
    assert current['chunks'] and current['assets']
    assert (store._source_root(1) / current['stored_name']).read_bytes() == RAW
    if previous is not None:
        for field in ('id', 'sha256', 'name', 'stored_name', 'size_bytes',
                      'created_at', 'scope', 'media_metadata_json'):
            assert current[field] == previous[field]
        assert 'duplicate' not in result
    else:
        assert result['duplicate'] is False

    # A digest hit reuses the immutable original without parsing or writing it.
    before = calls.copy()
    reused = store.add_document(RAW, 'other-name.txt', user_id=1, scope=scope)
    assert reused['duplicate'] and reused['id'] == current['id']
    assert calls - before == Counter({'document_by_digest': 1})
    assert reused['name'] == 'source.txt'


@pytest.mark.parametrize(('operation', 'first', 'second', 'expected'), [
    ('create', 'warnings', 'source', MemoryError),
    ('reindex', 'index', 'warnings_json', ValueError),
    ('create', 'pages', 'media_json', RuntimeError),
])
def test_preparation_preserves_failure_precedence(
        isolated_knowledge, tmp_path, monkeypatch,
        operation, first, second, expected):
    store = isolated_knowledge
    invoke, _ = prepare_operation(store, tmp_path, monkeypatch, operation)
    before = files(store)
    failures = {'warnings': MemoryError('warning list allocation'),
                'source': OSError('source write refused'),
                'index': ValueError('index preparation refused'),
                'warnings_json': TypeError('warning serialization refused'),
                'pages': RuntimeError('page conversion refused'),
                'media_json': TypeError('media serialization refused')}
    if first == 'warnings':
        class Warnings:
            def __iter__(self):
                raise failures[first]
        original_extract = store.extract
        def extract(*args, **kwargs):
            parsed = original_extract(*args, **kwargs)
            parsed['warnings'] = Warnings()
            return parsed
        monkeypatch.setattr(store, 'extract', extract)
    elif first == 'index':
        monkeypatch.setattr(store, '_indexed_chunks',
                            lambda *_args: (_ for _ in ()).throw(failures[first]))
    else:
        class Pages:
            def __int__(self):
                raise failures[first]
        original_extract = store.extract
        def extract(*args, **kwargs):
            parsed = original_extract(*args, **kwargs)
            parsed['pages'] = Pages()
            return parsed
        monkeypatch.setattr(store, 'extract', extract)
    if second == 'source':
        monkeypatch.setattr(store, '_write_source',
                            lambda *_args, **_kwargs: (_ for _ in ()).throw(failures[second]))
    else:
        original_dumps = store.json.dumps
        def dumps(value, **kwargs):
            if ((second == 'warnings_json' and isinstance(value, list)
                    and kwargs.get('ensure_ascii') is False)
                    or (second == 'media_json' and isinstance(value, dict)
                        and value.get('media_kind') == 'document')):
                raise failures[second]
            return original_dumps(value, **kwargs)
        monkeypatch.setattr(store, 'json', SimpleNamespace(dumps=dumps))
    with pytest.raises(expected) as caught:
        invoke()
    assert caught.value is failures[first]
    assert files(store) == before


@pytest.mark.parametrize('operation', ['create', 'reindex'])
def test_interruption_at_candidate_transfer_rolls_back_before_dispatch(
        isolated_knowledge, tmp_path, monkeypatch, operation):
    store = isolated_knowledge
    invoke, _ = prepare_operation(store, tmp_path, monkeypatch, operation)
    before = files(store)
    failure = KeyboardInterrupt('interrupted at candidate transfer')
    def interrupt(*args, **kwargs):
        frame = sys._getframe(1)
        if (frame.f_code is store._prepare_document_publication.__code__
                and kwargs.get('source') == (operation == 'create')):
            raise failure
    monkeypatch.setattr(store, '_sync_publication_directories', interrupt)
    with pytest.raises(KeyboardInterrupt) as caught:
        invoke()
    assert caught.value is failure
    assert files(store) == before


@pytest.mark.parametrize('operation', ['create', 'reindex'])
def test_witness_allocation_failure_leaves_no_candidate_files(
        isolated_knowledge, tmp_path, monkeypatch, operation):
    store = isolated_knowledge
    invoke, _ = prepare_operation(store, tmp_path, monkeypatch, operation)
    before = files(store)
    failure = MemoryError('publication witness allocation failed')
    commands = []

    def command(owner, name, *args, **kwargs):
        commands.append(name)
        raise AssertionError('witness failure must precede storage dispatch')

    with monkeypatch.context() as patch:
        patch.setattr(store, 'KnowledgePublicationAttempt',
                      lambda: (_ for _ in ()).throw(failure))
        patch.setattr(KnowledgeRepository, '_command', command)
        with pytest.raises(MemoryError) as caught:
            invoke()
    assert caught.value is failure
    assert not commands
    assert files(store) == before


def test_vanished_reindex_discards_only_its_new_visual_candidates(
        isolated_knowledge, tmp_path, monkeypatch):
    store = isolated_knowledge
    invoke, _ = prepare_operation(store, tmp_path, monkeypatch, 'reindex')
    before = files(store)
    published = []

    def vanished(_repository, replacement, **kwargs):
        published.append((replacement, kwargs['publication']))
        return None

    monkeypatch.setattr(KnowledgeRepository, 'replace_document', vanished)
    assert invoke() is None
    assert len(published) == 1 and published[0][0]['assets']
    assert files(store) == before
