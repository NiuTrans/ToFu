"""Canonical input and resource budgets for automated research jobs.

The HTTP route, LLM tool handler, engine, crash recovery, and stage recipe all
enter research through different seams.  This module is their single owner for
request normalization, so a caller cannot bypass API-cost bounds by choosing a
lower-level entry point and semantically identical work shares one dedup key.

Dependencies are deliberately light: arXiv normalization is imported lazily
only when a caller supplies seed papers.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import islice
from typing import Iterable

DEFAULT_RESEARCH_IDEAS = 6
MIN_RESEARCH_IDEAS = 3
MAX_RESEARCH_IDEAS = 12
DEFAULT_RESEARCH_HARVEST_PAPERS = 20
MIN_RESEARCH_HARVEST_PAPERS = 3
MAX_RESEARCH_SEED_PAPERS = 20
MAX_RESEARCH_DIRECTION_CHARS = 2_000
MAX_RESEARCH_MODEL_CHARS = 256


@dataclass(frozen=True, slots=True)
class ResearchRequest:
    """Normalized semantic identity and bounded inputs for one research job."""

    direction: str
    lang: str
    n_ideas: int
    seed_arxiv_ids: tuple[str, ...]
    model: str

    def dedup_key(self, user_id: int) -> tuple:
        """Return the exact live-work identity, including explicit corpus."""
        return (user_id, self.direction, self.lang, self.n_ideas,
                self.seed_arxiv_ids, self.model)


def _bounded_integer(value, *, default: int, minimum: int, maximum: int,
                     label: str) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        raise TypeError(f'{label} must be an integer, not a boolean')
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise TypeError(f'{label} must be an integer') from exc
    return max(minimum, min(parsed, maximum))


def normalize_research_idea_count(value) -> int:
    """Clamp requested model output to the public 3..12 idea budget."""
    return _bounded_integer(
        value, default=DEFAULT_RESEARCH_IDEAS, minimum=MIN_RESEARCH_IDEAS,
        maximum=MAX_RESEARCH_IDEAS, label='n_ideas')


def normalize_research_harvest_count(value) -> int:
    """Clamp query-derived corpus fan-out to the same 3..20 paper envelope."""
    return _bounded_integer(
        value, default=DEFAULT_RESEARCH_HARVEST_PAPERS,
        minimum=MIN_RESEARCH_HARVEST_PAPERS,
        maximum=MAX_RESEARCH_SEED_PAPERS, label='harvest_n')


def _seed_candidates(values: Iterable) -> list:
    if isinstance(values, (str, bytes, bytearray, dict)):
        raise TypeError('seed_arxiv_ids must be an array of arXiv ids')
    try:
        candidates = list(islice(iter(values), MAX_RESEARCH_SEED_PAPERS + 1))
    except TypeError as exc:
        raise TypeError('seed_arxiv_ids must be an array of arXiv ids') from exc
    if len(candidates) > MAX_RESEARCH_SEED_PAPERS:
        raise ValueError(
            f'seed_arxiv_ids accepts at most {MAX_RESEARCH_SEED_PAPERS} papers')
    return candidates


def normalize_research_seed_arxiv_ids(values) -> tuple[str, ...]:
    """Validate, version-normalize, and de-duplicate caller-supplied seeds."""
    if values is None:
        return ()
    candidates = _seed_candidates(values)
    if not candidates:
        return ()
    from lib.paper.arxiv import normalize_arxiv_id

    normalized: list[str] = []
    seen: set[str] = set()
    for index, value in enumerate(candidates):
        arxiv_id = normalize_arxiv_id(value)
        if not arxiv_id:
            raise ValueError(
                f'seed_arxiv_ids[{index}] is not a valid arXiv id')
        if arxiv_id not in seen:
            seen.add(arxiv_id)
            normalized.append(arxiv_id)
    return tuple(normalized)


def normalize_discovered_arxiv_ids(values) -> tuple[str, ...]:
    """Bound and sanitize untrusted search-adapter ids without failing a run."""
    from lib.paper.arxiv import normalize_arxiv_id

    normalized: list[str] = []
    seen: set[str] = set()
    for value in islice(iter(values), MAX_RESEARCH_SEED_PAPERS):
        arxiv_id = normalize_arxiv_id(value)
        if arxiv_id and arxiv_id not in seen:
            seen.add(arxiv_id)
            normalized.append(arxiv_id)
    return tuple(normalized)


def normalize_research_request(direction, *, lang='en', n_ideas=None,
                               seed_arxiv_ids=None, model=None) -> ResearchRequest:
    """Return the one canonical request used for execution and deduplication."""
    if not isinstance(direction, str):
        raise TypeError('direction must be a string')
    normalized_direction = direction.strip()
    if not normalized_direction:
        raise ValueError('direction is required')
    if len(normalized_direction) > MAX_RESEARCH_DIRECTION_CHARS:
        raise ValueError(
            f'direction exceeds {MAX_RESEARCH_DIRECTION_CHARS} characters')

    if lang is None:
        normalized_lang = 'en'
    elif isinstance(lang, str):
        normalized_lang = lang.strip().lower() or 'en'
    else:
        raise TypeError('lang must be a string')
    if normalized_lang not in ('en', 'zh'):
        raise ValueError("lang must be 'en' or 'zh'")

    if model is None:
        normalized_model = ''
    elif isinstance(model, str):
        normalized_model = model.strip()
    else:
        raise TypeError('model must be a string')
    if len(normalized_model) > MAX_RESEARCH_MODEL_CHARS:
        raise ValueError(
            f'model exceeds {MAX_RESEARCH_MODEL_CHARS} characters')

    return ResearchRequest(
        direction=normalized_direction,
        lang=normalized_lang,
        n_ideas=normalize_research_idea_count(n_ideas),
        seed_arxiv_ids=normalize_research_seed_arxiv_ids(seed_arxiv_ids),
        model=normalized_model,
    )


__all__ = [
    'DEFAULT_RESEARCH_HARVEST_PAPERS', 'DEFAULT_RESEARCH_IDEAS',
    'MAX_RESEARCH_DIRECTION_CHARS', 'MAX_RESEARCH_IDEAS',
    'MAX_RESEARCH_MODEL_CHARS',
    'MAX_RESEARCH_SEED_PAPERS', 'MIN_RESEARCH_HARVEST_PAPERS',
    'MIN_RESEARCH_IDEAS', 'ResearchRequest',
    'normalize_discovered_arxiv_ids', 'normalize_research_harvest_count',
    'normalize_research_idea_count', 'normalize_research_request',
    'normalize_research_seed_arxiv_ids',
]
