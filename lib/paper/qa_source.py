"""Owner-scoped stored-source resolution for repeated Paper Q&A starts.

Responsibility
--------------
Resolve an ingest-minted paper hash through ``PaperLibraryRepository`` and
retain a small reconstructible source working set. Every cache key includes
the explicit owner, and every hit rechecks owner-scoped existence so deletion
cannot revive stale source text. The content-addressed hash is the source
revision: changed content necessarily occupies another key. The generic TTL
cache makes the working set LRU-bounded, time-bounded, and reclaimable under
cgroup memory pressure.

Entry point: :func:`resolve_stored_qa_source`.
Dependencies: the Paper repository, resource policy, and ``lib.ttl_cache``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from lib.identity import require_user_id
from lib.paper.contracts import PAPER_QA_MAX_SOURCE_CHARS
from lib.paper.library_repository import PaperLibraryRepository
from lib.paper.resource_policy import (
    PAPER_QA_SOURCE_CACHE_TTL_SECONDS,
    paper_qa_source_cache_capacity,
)
from lib.paper_identity import _safe_hash_dir
from lib.ttl_cache import TTLCache


@dataclass(frozen=True, slots=True)
class PaperQASource:
    """One bounded source projection and its resolution tier."""

    text: str
    parsed_text_length: int
    tier: str


@dataclass(frozen=True, slots=True)
class _CachedPaperQASource:
    text: str
    parsed_text_length: int


RepositoryFactory = Callable[[int], Any]
_CACHE_MISS = object()


class PaperQASourceResolver:
    """Resolve owned paper bodies with metadata-validated TTL/LRU reuse."""

    def __init__(
        self,
        *,
        max_entries: int,
        ttl_seconds: float = PAPER_QA_SOURCE_CACHE_TTL_SECONDS,
        repository_factory: RepositoryFactory = PaperLibraryRepository,
    ) -> None:
        if max_entries < 1:
            raise ValueError('paper Q&A source cache requires max_entries >= 1')
        self._repository_factory = repository_factory
        self._cache = TTLCache(
            ttl_seconds,
            max_size=max_entries,
            name='paper_qa_source',
        )

    def resolve(
        self,
        owner_user_id: int,
        paper_hash: str,
    ) -> PaperQASource | None:
        """Return a bounded source only when ``owner_user_id`` still owns it."""
        owner = require_user_id(
            owner_user_id, context='paper Q&A source owner')
        canonical_hash = _safe_hash_dir(paper_hash)
        if canonical_hash is None:
            raise ValueError('paper Q&A source hash must be canonical')
        key = (owner, canonical_hash)
        repository = self._repository_factory(owner)

        cached = self._cache.get(key, _CACHE_MISS)
        if cached is not _CACHE_MISS:
            metadata = repository.identity(
                canonical_hash,
                max_text_chars=0,
                include_text_length=False,
            )
            if metadata is None:
                self._cache.invalidate(key)
                return None
            return PaperQASource(
                text=cached.text,
                parsed_text_length=cached.parsed_text_length,
                tier='memory_cache',
            )

        def load() -> _CachedPaperQASource:
            identity = repository.identity(
                canonical_hash,
                max_text_chars=PAPER_QA_MAX_SOURCE_CHARS,
            )
            source_text = identity.parsed_text.strip() if identity else ''
            if not source_text:
                raise LookupError('owned paper source is unavailable')
            return _CachedPaperQASource(
                text=source_text,
                parsed_text_length=max(
                    len(source_text), int(identity.parsed_text_length)),
            )

        try:
            loaded = self._cache.get_or_compute(key, load)
        except LookupError:
            return None
        return PaperQASource(
            text=loaded.text,
            parsed_text_length=loaded.parsed_text_length,
            tier='library',
        )

    def clear(self) -> int:
        """Drop reconstructible source text, primarily for lifecycle tests."""
        return self._cache.clear()

    def snapshot(self) -> dict[str, Any]:
        """Expose bounded cache counters without source content."""
        return self._cache.stats()


_QA_SOURCE_RESOLVER = PaperQASourceResolver(
    max_entries=paper_qa_source_cache_capacity(),
)


def resolve_stored_qa_source(
    owner_user_id: int,
    paper_hash: str,
) -> PaperQASource | None:
    """Resolve one hash through the process-wide bounded source working set."""
    return _QA_SOURCE_RESOLVER.resolve(owner_user_id, paper_hash)


__all__ = [
    'PaperQASource',
    'PaperQASourceResolver',
    'resolve_stored_qa_source',
]
