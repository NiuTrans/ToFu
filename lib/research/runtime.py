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

#: The underlying TaskRuntime — what ``routes/api_v1/tasks.py::_registries()``
#: discovers (so the generic /api/v1/tasks/* poll+abort serve this capability
#: with ZERO bespoke routes), and what legacy call sites expect.
_research_runtime = _production.runtime
_research_tasks = _production.tasks
_research_tasks_lock = _production.lock
_research_dedup_index = _production.dedup_index


def _research_index_get(key: tuple):
    return _production.index_get(key)


def _research_index_register(key: tuple, task_id: str) -> None:
    _production.index_register(key, task_id)


def _new_research_task(task_id: str, *, direction: str, workdir: str, lang: str,
                       n_ideas: int = 6, conv_id: str = ''):
    """Create + register a pending research task with the engine's field shape."""
    return _production.create_task(
        task_id,
        meta={'direction': direction, 'lang': lang, 'n_ideas': n_ideas},
        fields={'direction': direction, 'workdir': workdir, 'lang': lang,
                'n_ideas': n_ideas, 'conv_id': conv_id})


def _append_research_event(task, event):
    return _production.append_event(task, event)


def _cleanup_stale_research_tasks():
    return _production.cleanup_stale()


def _research_task_id():
    return _production.new_task_id()


__all__ = [
    '_production', '_research_runtime', '_research_tasks',
    '_research_tasks_lock', '_research_dedup_index', '_research_index_get',
    '_research_index_register', '_new_research_task',
    '_append_research_event', '_cleanup_stale_research_tasks',
    '_research_task_id',
]
