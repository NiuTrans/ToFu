"""Bounded I/O and allocation contracts for memory BM25 retrieval.

Search historically scored only the first 2,000 body characters but loaded
every complete file and retained every corpus token. These tests pin identical
ranking, lazy prefix reads, selected-only prefetch hydration, unsafe-link
rejection, and a measured allocation ceiling for the streaming scorer.
"""

from __future__ import annotations

import io
import math
import os
import tracemalloc
from pathlib import Path

import pytest

from lib.memory.contracts import (
    MEMORY_FRONTMATTER_READ_MAX_CHARS,
    MEMORY_SEARCH_BODY_MAX_CHARS,
)


pytestmark = pytest.mark.unit


@pytest.fixture()
def isolated_memory_store(tmp_path, monkeypatch):
    import lib.memory.storage._dirs as storage_dirs
    import lib.memory.storage._files as memory_files

    data_dir = tmp_path / 'data'
    project_dir = tmp_path / 'project'
    memory_dir = project_dir / '.tofu' / 'memories'
    memory_dir.mkdir(parents=True)
    (project_dir / '.tofu' / 'skills').mkdir(parents=True)
    monkeypatch.setenv('TOFU_DATA_DIR', str(data_dir))
    monkeypatch.setattr(
        storage_dirs, '_server_data_dir', lambda: str(data_dir))
    storage_dirs._migrated_roots.clear()
    storage_dirs._server_store_migrated = False
    memory_files._metadata_cache.clear()
    yield project_dir
    memory_files._metadata_cache.clear()
    storage_dirs._migrated_roots.clear()
    storage_dirs._server_store_migrated = False


def _reference_corpus_scores(query, memories):
    """The pre-streaming formula, retained only as an independent oracle."""
    from lib.memory.relevance import BM25_B, BM25_K1, _build_memory_doc, _tokenize

    query_terms = set(_tokenize(query))
    documents = [
        _build_memory_doc(memory, include_body=True)
        for memory in memories
    ]
    document_lengths = [len(document) for document in documents]
    document_count = len(documents)
    average_length = sum(document_lengths) / document_count
    document_frequency = {
        term: sum(term in document for document in documents)
        for term in query_terms
    }
    scored = []
    for index, (document, document_length) in enumerate(
            zip(documents, document_lengths)):
        term_frequency = {}
        for token in document:
            if token in query_terms:
                term_frequency[token] = term_frequency.get(token, 0) + 1
        score = 0.0
        for term in query_terms:
            frequency = term_frequency.get(term, 0)
            if not frequency:
                continue
            frequency_in_documents = document_frequency[term]
            inverse_document_frequency = math.log(
                (document_count - frequency_in_documents + 0.5)
                / (frequency_in_documents + 0.5)
                + 1.0
            )
            numerator = frequency * (BM25_K1 + 1)
            denominator = frequency + BM25_K1 * (
                1 - BM25_B
                + BM25_B * document_length / average_length
            )
            score += inverse_document_frequency * numerator / denominator
        scored.append((score, index))
    scored.sort(key=lambda row: (-row[0], row[1]))
    return [(score, memories[index]['id']) for score, index in scored]


def test_streaming_corpus_scores_match_the_previous_formula():
    from lib.memory.relevance._search import _score_corpus

    memories = [
        {
            'id': 'exact', 'name': 'ParserState rollback',
            'description': 'lib/parser/state.py recovery convention',
            'tags': ['parser', 'rollback'],
            'body': 'needle evidence ' * 300,
        },
        {
            'id': 'cjk', 'name': '中文海报',
            'description': '中文字符渲染 guardrail',
            'tags': ['image'], 'body': 'needle 中文证据',
        },
        {
            'id': 'tie-a', 'name': 'unrelated',
            'description': '', 'tags': [], 'body': '',
        },
        {
            'id': 'tie-b', 'name': 'unrelated',
            'description': '', 'tags': [], 'body': '',
        },
    ]
    query = 'needle ParserState rollback 中文'

    expected = _reference_corpus_scores(query, memories)
    actual = [
        (score, memory['id'])
        for score, memory in _score_corpus(
            query, iter(memories), include_body=True)
    ]

    assert [memory_id for _, memory_id in actual] == [
        memory_id for _, memory_id in expected]
    assert [score for score, _ in actual] == pytest.approx(
        [score for score, _ in expected], rel=0, abs=1e-12)


def test_streaming_scorer_peak_allocation_stays_below_one_megabyte():
    from lib.memory.relevance._score import _score_token_documents

    def token_documents():
        for _index in range(300):
            yield ['needle', 'nonmatch'] * 2_000

    tracemalloc.start()
    scores = _score_token_documents('needle', token_documents())
    _current_bytes, peak_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert len(scores) == 300
    assert peak_bytes < 1_000_000, (
        f'streaming BM25 retained {peak_bytes:,} bytes; corpus tokens may have '
        'become resident again')


class _TrackedTextFile:
    def __init__(self, wrapped, reads):
        self._wrapped = wrapped
        self._reads = reads

    def read(self, size=-1):
        self._reads.append(('read', size))
        return self._wrapped.read(size)

    def readline(self, size=-1):
        self._reads.append(('readline', size))
        return self._wrapped.readline(size)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return self._wrapped.__exit__(*args)

    def __getattr__(self, name):
        return getattr(self._wrapped, name)


def test_search_reads_only_the_existing_two_thousand_character_body_window(
        isolated_memory_store, monkeypatch):
    import lib.memory.storage._files as memory_files
    from lib.memory.relevance import search_memories

    memory_path = (
        isolated_memory_store / '.tofu' / 'memories' / 'legacy-large.md')
    body = (
        'headneedle verified prefix\n'
        + 'x' * (MEMORY_SEARCH_BODY_MAX_CHARS + 50_000)
        + '\ntailonlyneedle outside ranking window\n'
    )
    memory_path.write_text(
        '---\nname: Large legacy memory\n'
        'description: metadata without either marker\n'
        'tags: [streaming]\nenabled: true\n---\n' + body,
        encoding='utf-8',
    )

    real_open = open
    reads = []

    def tracked_open(filepath, *args, **kwargs):
        wrapped = real_open(filepath, *args, **kwargs)
        if os.path.abspath(filepath) == os.path.abspath(memory_path):
            return _TrackedTextFile(wrapped, reads)
        return wrapped

    monkeypatch.setattr(memory_files, 'open', tracked_open, raising=False)

    found = search_memories(
        'headneedle', project_path=str(isolated_memory_store))
    missed = search_memories(
        'tailonlyneedle', project_path=str(isolated_memory_store))

    assert 'Large legacy memory' in found
    assert 'Large legacy memory' not in missed
    assert ('read', -1) not in reads
    bounded_body_reads = [size for operation, size in reads
                          if operation == 'read']
    assert bounded_body_reads
    assert max(bounded_body_reads) <= MEMORY_SEARCH_BODY_MAX_CHARS


def test_unclosed_frontmatter_read_stops_at_metadata_budget(monkeypatch):
    import lib.memory.storage._files as memory_files

    source = io.StringIO('---\n' + 'x' * 1_000_000)

    class _InspectableText(io.StringIO):
        def close(self):
            pass

    source = _InspectableText(source.getvalue())
    monkeypatch.setattr(
        memory_files, 'open', lambda *_args, **_kwargs: source,
        raising=False)

    with pytest.raises(ValueError, match='frontmatter exceeds'):
        memory_files._read_memory_source(
            '/synthetic.md', include_body=False)
    assert source.tell() <= MEMORY_FRONTMATTER_READ_MAX_CHARS + 1


def test_oversized_frontmatter_record_fails_soft(tmp_path):
    import lib.memory.storage._files as memory_files

    path = tmp_path / 'oversized-frontmatter.md'
    path.write_text(
        '---\ndescription: ' + 'x' * MEMORY_FRONTMATTER_READ_MAX_CHARS,
        encoding='utf-8',
    )

    assert memory_files._memory_from_file(
        str(path), scope='project', include_body=False) is None


def test_storage_record_normalizes_legacy_single_tag(tmp_path):
    import lib.memory.storage._files as memory_files

    path = tmp_path / 'single-tag.md'
    path.write_text(
        '---\nname: legacy\ndescription: scalar tag\ntags: solo\n---\nbody',
        encoding='utf-8',
    )

    memory = memory_files._memory_from_file(
        str(path), scope='project', include_body=False)
    assert memory['tags'] == ['solo']


def test_memory_directory_rejects_symlinked_files(
        isolated_memory_store, tmp_path):
    from lib.memory.storage import list_memories

    outside = tmp_path / 'outside.md'
    outside.write_text(
        '---\nname: Foreign memory\n'
        'description: must not cross the storage boundary\n---\nsecret',
        encoding='utf-8',
    )
    link = isolated_memory_store / '.tofu' / 'memories' / 'linked.md'
    try:
        link.symlink_to(outside)
        outside_package = tmp_path / 'outside-package'
        outside_package.mkdir()
        (outside_package / 'SKILL.md').write_text(
            '---\nname: Foreign package\n'
            'description: must not cross the package boundary\n---\nsecret',
            encoding='utf-8',
        )
        package_link = (
            isolated_memory_store / '.tofu' / 'skills' / 'linked-package')
        package_link.symlink_to(outside_package, target_is_directory=True)
        linked_skill_dir = (
            isolated_memory_store / '.tofu' / 'skills' / 'linked-skill-file')
        linked_skill_dir.mkdir()
        (linked_skill_dir / 'SKILL.md').symlink_to(
            outside_package / 'SKILL.md')
    except (OSError, NotImplementedError):
        pytest.skip('symlinks unavailable on this platform')

    memories = list_memories(
        str(isolated_memory_store), scope='project', include_body=False)

    ids = {memory['id'] for memory in memories}
    assert {'linked', 'linked-package', 'linked-skill-file'}.isdisjoint(ids)


def test_prefetch_loads_body_only_for_selected_metadata(
        isolated_memory_store, monkeypatch):
    import lib.memory.storage._files as memory_files
    from lib.memory.prefetch import run_memory_prefetch
    from lib.memory.storage import create_memory

    project_path = str(isolated_memory_store)
    selected_memory = create_memory(
        name='ParserState rollback',
        description='ParserState rollback in lib/parser/state.py',
        body='critical selected body evidence',
        tags=['parser'], scope='project', project_path=project_path,
    )
    create_memory(
        name='Typography palette',
        description='font colors and spacing',
        body='unrelated body must stay unread',
        tags=['design'], scope='project', project_path=project_path,
    )

    original_read = memory_files._read_memory_source
    bounded_body_paths = []

    def recorded_read(filepath, *, include_body=True, body_char_limit=None):
        if include_body and body_char_limit is not None:
            bounded_body_paths.append((filepath, body_char_limit))
        return original_read(
            filepath,
            include_body=include_body,
            body_char_limit=body_char_limit,
        )

    monkeypatch.setattr(memory_files, '_read_memory_source', recorded_read)
    task = {'id': 'bounded-prefetch', 'convId': 'conv'}
    selected = run_memory_prefetch(
        [{'role': 'user',
          'content': 'fix ParserState in lib/parser/state.py'}],
        project_path,
        task=task,
    )

    assert [memory['id'] for memory in selected] == [selected_memory['id']]
    assert selected[0]['body'] == 'critical selected body evidence'
    assert bounded_body_paths == [(selected_memory['filepath'], 6_000)]


def test_memory_hint_requests_metadata_only(monkeypatch):
    import lib.memory.injection as injection

    captured = {}

    def fake_eligible(
        project_path,
        extra_paths=None,
        *,
        include_body=True,
        record_view='complete',
    ):
        captured.update(
            project_path=project_path,
            extra_paths=extra_paths,
            include_body=include_body,
            record_view=record_view,
        )
        return [{'id': 'exists'}]

    monkeypatch.setattr(injection, 'get_eligible_memories', fake_eligible)

    assert injection.build_memory_context('/project', ['/extra'])
    assert captured == {
        'project_path': '/project',
        'extra_paths': ['/extra'],
        'include_body': False,
        'record_view': 'retrieval',
    }


def test_memory_hint_reuses_known_prefetch_availability(monkeypatch):
    import lib.memory.injection as injection

    monkeypatch.setattr(
        injection,
        'get_eligible_memories',
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError('known availability rebuilt the memory corpus')),
    )

    assert injection.build_memory_context(
        '/project', known_available=True)
    assert injection.build_memory_context(
        '/project', known_available=False) is None


def test_empty_prefetch_query_skips_even_metadata_scan(monkeypatch):
    import lib.memory.storage as storage
    from lib.memory.prefetch import run_memory_prefetch

    monkeypatch.setattr(
        storage,
        'get_eligible_memories',
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError('empty prefetch query scanned memory metadata')),
    )

    assert run_memory_prefetch(
        [{'role': 'user', 'content': '   '}],
        '/project',
        task={'id': 'empty-prefetch'},
    ) == []


def test_prefetch_drops_candidate_that_changed_before_hydration(monkeypatch):
    import lib.memory.storage as storage
    from lib.memory.prefetch import run_memory_prefetch

    candidate = {
        'id': 'parser',
        'name': 'ParserState rollback',
        'description': 'ParserState rollback in lib/parser/state.py',
        'tags': ['parser'],
        'body': '',
        'scope': 'project',
        'filepath': '/project/parser.md',
    }
    changed = {
        **candidate,
        'name': 'Typography palette',
        'description': 'font colors and spacing',
        'tags': ['design'],
        'body': 'new unrelated content',
    }
    monkeypatch.setattr(
        storage, 'get_eligible_memories',
        lambda *_args, **_kwargs: [candidate])
    monkeypatch.setattr(
        storage, 'load_eligible_memories',
        lambda *_args, **_kwargs: [changed])

    selected = run_memory_prefetch(
        [{'role': 'user',
          'content': 'fix ParserState in lib/parser/state.py'}],
        '/project',
        task={'id': 'prefetch-race'},
    )

    assert selected == []
