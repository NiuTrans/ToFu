"""Bounded, owner-safe stored-source reuse for Paper Q&A starts."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import pytest

from lib.paper.contracts import PAPER_QA_MAX_SOURCE_CHARS
from lib.paper.library_repository import PaperIdentity
from lib.paper.qa_source import PaperQASourceResolver
from lib.paper.resource_policy import paper_qa_source_cache_capacity


pytestmark = pytest.mark.unit

HASH_A = 'a' * 32
HASH_B = 'b' * 32


@dataclass
class _RepositoryState:
    rows: dict[tuple[int, str], str]
    calls: list[tuple[int, str, int | None, bool]]


class _Repository:
    def __init__(self, owner_user_id: int, state: _RepositoryState) -> None:
        self.owner_user_id = owner_user_id
        self.state = state

    def identity(
        self,
        paper_hash: str,
        *,
        max_text_chars: int | None = None,
        include_text_length: bool = True,
    ) -> PaperIdentity | None:
        self.state.calls.append(
            (
                self.owner_user_id,
                paper_hash,
                max_text_chars,
                include_text_length,
            ))
        text = self.state.rows.get((self.owner_user_id, paper_hash))
        if text is None:
            return None
        projected = text if max_text_chars is None else text[:max_text_chars]
        return PaperIdentity(
            title='Paper',
            arxiv_id='',
            parsed_text=projected,
            parsed_text_length=len(text) if include_text_length else 0,
        )


def _resolver(
    state: _RepositoryState,
    *,
    max_entries: int = 2,
) -> PaperQASourceResolver:
    return PaperQASourceResolver(
        max_entries=max_entries,
        repository_factory=lambda owner: _Repository(owner, state),
    )


def test_repeat_start_revalidates_owner_without_reloading_body():
    text = '# Paper\n' + 'evidence ' * 100
    state = _RepositoryState({(7, HASH_A): text}, [])
    resolver = _resolver(state)

    first = resolver.resolve(7, HASH_A)
    second = resolver.resolve(7, HASH_A)

    assert first and first.text == text.strip() and first.tier == 'library'
    assert second and second.text == text.strip()
    assert second.tier == 'memory_cache'
    assert state.calls == [
        (7, HASH_A, PAPER_QA_MAX_SOURCE_CHARS, True),
        (7, HASH_A, 0, False),
    ]


def test_concurrent_cold_starts_share_one_full_source_projection():
    state = _RepositoryState({(7, HASH_A): 'shared source'}, [])
    resolver = _resolver(state)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(
            lambda _index: resolver.resolve(7, HASH_A), range(2)))

    assert [result.text for result in results] == [
        'shared source', 'shared source']
    assert sum(
        max_chars == PAPER_QA_MAX_SOURCE_CHARS
        for _owner, _paper_hash, max_chars, _include_length in state.calls
    ) == 1


def test_same_hash_is_isolated_by_explicit_owner():
    state = _RepositoryState({
        (7, HASH_A): 'owner seven source',
        (8, HASH_A): 'owner eight source',
    }, [])
    resolver = _resolver(state)

    assert resolver.resolve(7, HASH_A).text == 'owner seven source'
    assert resolver.resolve(8, HASH_A).text == 'owner eight source'
    assert resolver.resolve(7, HASH_A).text == 'owner seven source'
    assert state.calls == [
        (7, HASH_A, PAPER_QA_MAX_SOURCE_CHARS, True),
        (8, HASH_A, PAPER_QA_MAX_SOURCE_CHARS, True),
        (7, HASH_A, 0, False),
    ]


def test_deleted_source_never_revives_and_new_content_uses_a_new_hash():
    state = _RepositoryState({(7, HASH_A): 'old source'}, [])
    resolver = _resolver(state)
    assert resolver.resolve(7, HASH_A).text == 'old source'

    del state.rows[(7, HASH_A)]
    assert resolver.resolve(7, HASH_A) is None

    state.rows[(7, HASH_B)] = 'replacement source is longer'
    replaced = resolver.resolve(7, HASH_B)
    assert replaced and replaced.text == 'replacement source is longer'
    assert replaced.tier == 'library'


def test_entry_capacity_is_lru_bounded_and_observable():
    state = _RepositoryState({
        (7, HASH_A): 'source a',
        (7, HASH_B): 'source b',
    }, [])
    resolver = _resolver(state, max_entries=1)

    assert resolver.resolve(7, HASH_A)
    assert resolver.resolve(7, HASH_B)
    assert resolver.resolve(7, HASH_A)

    snapshot = resolver.snapshot()
    assert snapshot['size'] == 1
    assert snapshot['max_size'] == 1
    assert snapshot['size_evicts'] == 2
    assert [call[2] for call in state.calls] == [
        PAPER_QA_MAX_SOURCE_CHARS,
        PAPER_QA_MAX_SOURCE_CHARS,
        PAPER_QA_MAX_SOURCE_CHARS,
    ]


def test_resource_override_stays_below_consumer_hard_ceiling():
    assert paper_qa_source_cache_capacity({
        'TOFU_DEPLOYMENT_MODE': 'distributed',
        'TOFU_PAPER_QA_SOURCE_CACHE_CAPACITY': '999999',
    }) == 32


def test_hash_only_start_removes_the_full_source_wire_cost():
    common = {
        'question': 'What is the main result?',
        'paper_hash': HASH_A,
        'lang': 'en',
        'history': [],
        'title': 'Representative paper',
    }
    full_bytes = len(json.dumps(
        {**common, 'paper_text': 'x' * PAPER_QA_MAX_SOURCE_CHARS},
        ensure_ascii=False,
        separators=(',', ':'),
    ).encode())
    hash_only_bytes = len(json.dumps(
        common,
        ensure_ascii=False,
        separators=(',', ':'),
    ).encode())

    assert hash_only_bytes == 143
    assert full_bytes - hash_only_bytes == 1_000_016
    assert hash_only_bytes / full_bytes < 0.0002


def test_resolver_rejects_implicit_owner_and_noncanonical_hash():
    resolver = _resolver(_RepositoryState({}, []))
    with pytest.raises(ValueError, match='numeric user_id'):
        resolver.resolve(None, HASH_A)
    with pytest.raises(ValueError, match='canonical'):
        resolver.resolve(7, 'not-a-hash')
