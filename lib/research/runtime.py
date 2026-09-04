"""lib/research/runtime.py — TaskRuntime for auto-research jobs (R4).

Rides :class:`lib.production.runtime.ProductionRuntime` — the FOURTH capability
to do so (after motion-video / paper-podcast / longform-report). Per the owner
directive, this file builds NO bespoke runtime: dedup index, create-with-field-
shape, append+touch, stale sweep and id minting all come from the substrate.
It is a near-copy of longform/runtime.py by design — that similarity is the
substrate working, not duplication to refactor.

Events: ``stage`` (from the stage graph) / ``phase`` / ``final`` / ``done`` /
``error``.
"""

from __future__ import annotations

from lib.log import get_logger
from lib.production.runtime import ProductionRuntime

logger = get_logger(__name__)

_production = ProductionRuntime(
    'research', id_prefix='research', ttl=7200,
    push_channel='research', error_source='lib.research.engine',
    log_label='Research')

#: The underlying TaskRuntime discovered by the generic task API.
_research_runtime = _production.runtime


def _research_index_get(key: tuple):
    return _production.index_get(key)


def _research_index_register(key: tuple, task_id: str) -> None:
    _production.index_register(key, task_id)


def _new_research_task(task_id: str, *, direction: str, workdir: str, lang: str,
                       user_id: int, n_ideas: int = 6, conv_id: str = '',
                       seed_arxiv_ids=()):
    """Create + register a pending research task with the engine's field shape."""
    seeds = list(seed_arxiv_ids or ())
    return _production.create_task(
        task_id,
        user_id=user_id, meta={'direction': direction, 'lang': lang,
                              'n_ideas': n_ideas, 'seed_count': len(seeds)},
        fields={'direction': direction, 'workdir': workdir, 'lang': lang,
                'n_ideas': n_ideas, 'conv_id': conv_id,
                'seed_arxiv_ids': seeds})


def _claim_research_task(key: tuple, task_id: str, *, direction: str,
                         workdir: str, lang: str, user_id: int, n_ideas: int = 6,
                         conv_id: str = '', seed_arxiv_ids=()):
    """Atomic start path; resume continues to use _new_research_task."""
    seeds = list(seed_arxiv_ids or ())
    return _production.claim_task(
        key, task_id, user_id=user_id,
        meta={'direction': direction, 'lang': lang, 'n_ideas': n_ideas,
              'seed_count': len(seeds)},
        fields={'direction': direction, 'workdir': workdir, 'lang': lang,
                'n_ideas': n_ideas, 'conv_id': conv_id,
                'seed_arxiv_ids': seeds})


def _append_research_event(task, event):
    return _production.append_event(task, event)


def _cleanup_stale_research_tasks():
    return _production.cleanup_stale()


def _research_task_id():
    return _production.new_task_id()


__all__ = [
    '_production', '_research_runtime', '_research_index_get',
    '_research_index_register', '_new_research_task',
    '_claim_research_task',
    '_append_research_event', '_cleanup_stale_research_tasks',
    '_research_task_id',
]
