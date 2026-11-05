"""Exact reuse of the tokenized prefetch corpus across BM25 shortlist calls.

The cached index must score bit-for-bit like the streaming BM25 core, be keyed
by the exact source-ordered document texts (so any changed, added, removed or
reordered entry rebuilds it), share one build among concurrent callers, and
retain only the most recent corpus.
"""

from __future__ import annotations

import random
import threading
import time

import pytest

from lib.memory.prefetch._config import PREFETCH_BM25_TOP_N as _TOP_N


pytestmark = pytest.mark.unit

_WAIT_SECONDS = 10.0
_VOCABULARY = [
    'memory', 'lock', 'convoy', 'startup', 'scan', 'prefetch', 'bm25',
    'storage', 'flask', 'migration', 'snake_case', 'kebab-case', 'the', 'and',
    '中文海报', '缓存', 'gil', 'thread', 'x', 'lib/memory/storage/_dirs.py',
]


@pytest.fixture(autouse=True)
def empty_corpus_slot(monkeypatch):
    import lib.memory.prefetch._shortlist as shortlist

    monkeypatch.setattr(shortlist, '_corpus_index_slot', None)


def _memory(rng: random.Random, index: int) -> dict:
    def words(count):
        return ' '.join(rng.choice(_VOCABULARY) for _ in range(count))

    return {
        'id': f'memory-{index}',
        'name': words(rng.randint(0, 4)),
        'description': words(rng.randint(0, 12)),
        'tags': [rng.choice(_VOCABULARY) for _ in range(rng.randint(0, 3))],
    }


def _queries(rng: random.Random) -> list[str]:
    return [
        '',
        'the and',
        'no-such-token anywhere',
        'memory lock convoy memory',
        '缓存 中文',
        'lib/memory/storage/_dirs.py startup',
    ] + [
        ' '.join(rng.choice(_VOCABULARY) for _ in range(rng.randint(1, 8)))
        for _ in range(20)
    ]


def _reference_top_n(memories, query, top_n):
    from lib.memory.relevance import _build_memory_doc
    from lib.memory.relevance._score import _score_token_documents

    scored = [
        (index, score)
        for index, score in _score_token_documents(
            query, (_build_memory_doc(memory) for memory in memories))
        if score > 0
    ]
    scored.sort(key=lambda row: -row[1])
    return scored[:top_n]


@pytest.mark.parametrize('seed', range(8))
def test_index_scores_exactly_like_the_streaming_core(seed):
    from lib.memory.relevance import _tokenize
    from lib.memory.relevance._score import (
        TokenCorpusIndex,
        _score_token_documents,
    )

    rng = random.Random(seed)
    documents = [
        ' '.join(rng.choice(_VOCABULARY) for _ in range(rng.randint(0, 20)))
        for _ in range(rng.randint(1, 60))
    ]
    index = TokenCorpusIndex(_tokenize(document) for document in documents)

    for query in _queries(rng):
        assert index.score(query) == _score_token_documents(
            query, (_tokenize(document) for document in documents))


@pytest.mark.parametrize('seed', range(4))
def test_shortlist_matches_the_uncached_ranking(seed):
    from lib.memory.prefetch._shortlist import _bm25_top_n

    rng = random.Random(seed)
    memories = [_memory(rng, index) for index in range(80)]
    for query in _queries(rng):
        for top_n in (1, 5, 200):
            assert _bm25_top_n(memories, query, top_n=top_n) == (
                _reference_top_n(memories, query, top_n))


def _count_index_builds(monkeypatch) -> list[int]:
    import lib.memory.prefetch._shortlist as shortlist

    builds: list[int] = []
    original = shortlist.TokenCorpusIndex

    class CountedIndex(original):
        def __init__(self, token_documents):
            builds.append(1)
            super().__init__(token_documents)

    monkeypatch.setattr(shortlist, 'TokenCorpusIndex', CountedIndex)
    return builds


def test_unchanged_corpus_is_tokenized_once(monkeypatch):
    from lib.memory.prefetch._shortlist import _bm25_top_n

    builds = _count_index_builds(monkeypatch)
    rng = random.Random(11)
    memories = [_memory(rng, index) for index in range(40)]
    relisted = [dict(memory, tags=list(memory['tags'])) for memory in memories]

    first = _bm25_top_n(memories, 'memory lock convoy')
    second = _bm25_top_n(relisted, 'startup scan prefetch')

    assert builds == [1]
    assert first == _reference_top_n(memories, 'memory lock convoy', _TOP_N)
    assert second == _reference_top_n(
        relisted, 'startup scan prefetch', _TOP_N)


@pytest.mark.parametrize('change', [
    'rename', 'describe', 'retag', 'add', 'remove', 'reorder'])
def test_any_changed_entry_rebuilds_the_index(monkeypatch, change):
    from lib.memory.prefetch._shortlist import _bm25_top_n

    builds = _count_index_builds(monkeypatch)
    memories = [
        {'id': 'a', 'name': 'memory lock', 'description': 'convoy scan',
         'tags': ['startup']},
        {'id': 'b', 'name': 'flask migration', 'description': 'memory',
         'tags': ['storage']},
        {'id': 'c', 'name': 'gil thread', 'description': 'lock memory',
         'tags': []},
    ]
    query = 'memory lock storage convoy'
    assert _bm25_top_n(memories, query) == _reference_top_n(
        memories, query, _TOP_N)
    changed = [dict(memory, tags=list(memory['tags'])) for memory in memories]
    if change == 'rename':
        changed[1]['name'] = 'lock lock lock'
    elif change == 'describe':
        changed[0]['description'] = 'unrelated words'
    elif change == 'retag':
        changed[2]['tags'] = ['convoy']
    elif change == 'add':
        changed.append({'id': 'd', 'name': 'convoy', 'description': 'lock',
                        'tags': []})
    elif change == 'remove':
        changed.pop(0)
    else:
        changed.reverse()

    assert _bm25_top_n(changed, query) == _reference_top_n(
        changed, query, _TOP_N)
    assert builds == [1, 1]


def test_concurrent_identical_corpora_share_one_build(monkeypatch):
    import lib.memory.prefetch._shortlist as shortlist
    from lib.memory.prefetch._shortlist import _bm25_top_n

    builds: list[int] = []
    entered = threading.Event()
    release = threading.Event()
    original = shortlist.TokenCorpusIndex

    class SlowIndex(original):
        def __init__(self, token_documents):
            builds.append(1)
            entered.set()
            assert release.wait(_WAIT_SECONDS)
            super().__init__(token_documents)

    monkeypatch.setattr(shortlist, 'TokenCorpusIndex', SlowIndex)
    rng = random.Random(5)
    memories = [_memory(rng, index) for index in range(30)]
    results: list = []

    def rank():
        results.append(_bm25_top_n(
            [dict(memory) for memory in memories], 'memory lock'))

    threads = [threading.Thread(target=rank) for _ in range(6)]
    threads[0].start()
    assert entered.wait(_WAIT_SECONDS)
    for thread in threads[1:]:
        thread.start()
    deadline = time.monotonic() + _WAIT_SECONDS
    while shortlist._corpus_index_reads.snapshot()['queuedCallers'] != 5:
        assert time.monotonic() < deadline
        time.sleep(0.001)
    release.set()
    for thread in threads:
        thread.join(_WAIT_SECONDS)

    assert builds == [1]
    assert results == [_reference_top_n(memories, 'memory lock', _TOP_N)] * 6


def test_only_the_latest_corpus_is_retained(monkeypatch):
    from lib.memory.prefetch._shortlist import _bm25_top_n

    builds = _count_index_builds(monkeypatch)
    first = [{'id': 'a', 'name': 'memory', 'description': '', 'tags': []}]
    second = [{'id': 'b', 'name': 'lock', 'description': '', 'tags': []}]

    _bm25_top_n(first, 'memory')
    _bm25_top_n(second, 'lock')
    _bm25_top_n(second, 'lock')
    assert builds == [1, 1]
    _bm25_top_n(first, 'memory')  # Evicted by the newer corpus.
    assert builds == [1, 1, 1]
