"""lib/slides/runtime.py — TaskRuntime for slide-deck jobs.

Rides :class:`lib.production.runtime.ProductionRuntime` exactly like
longform does (docs/modules/production.md): dedup index,
create-with-field-shape, append+touch, stale sweep, id minting.

Events: ``stage`` / ``phase`` / ``page_authored`` / ``final`` / ``done`` /
``error``.
"""

from __future__ import annotations

from lib.log import get_logger
from lib.production.runtime import ProductionRuntime

logger = get_logger(__name__)

_production = ProductionRuntime(
    'slides-deck', id_prefix='slides', ttl=3600,
    push_channel='slides', error_source='lib.slides.engine',
    log_label='Slides')

#: The underlying TaskRuntime — what routes/api_v1/tasks.py::_registries()
#: discovers.
_slides_runtime = _production.runtime


def _slides_index_get(key: tuple):
    return _production.index_get(key)


def _slides_index_register(key: tuple, task_id: str) -> None:
    _production.index_register(key, task_id)


def _new_slides_task(task_id: str, *, topic: str, workdir: str, lang: str,
                     style: str, max_pages: int, size: tuple,
                     user_id: int, conv_id: str = '', model: str = '',
                     creative_mode: str = 'director'):
    return _production.create_task(
        task_id,
        user_id=user_id,
        meta={'topic': topic, 'lang': lang, 'style': style,
              'max_pages': max_pages, 'model': model,
              'creative_mode': creative_mode},
        fields={'topic': topic, 'workdir': workdir, 'lang': lang,
                'style': style, 'max_pages': max_pages, 'size': tuple(size),
                'conv_id': conv_id, 'model': model,
                'creative_mode': creative_mode})


def _claim_slides_task(key: tuple, task_id: str, *, topic: str,
                       workdir: str, lang: str, style: str, max_pages: int,
                       size: tuple, user_id: int, conv_id: str = '',
                       model: str = '', creative_mode: str = 'director'):
    """Atomically join or create one topic-deck task."""
    return _production.claim_task(
        key, task_id, user_id=user_id,
        meta={'topic': topic, 'lang': lang, 'style': style,
              'max_pages': max_pages, 'model': model,
              'creative_mode': creative_mode},
        fields={'topic': topic, 'workdir': workdir, 'lang': lang,
                'style': style, 'max_pages': max_pages, 'size': tuple(size),
                'conv_id': conv_id, 'model': model,
                'creative_mode': creative_mode})


def _append_slides_event(task, event):
    return _production.append_event(task, event)


def _cleanup_stale_slides_tasks():
    return _production.cleanup_stale()


def _slides_task_id():
    return _production.new_task_id()


__all__ = [
    '_production', '_slides_runtime', '_slides_index_get',
    '_slides_index_register', '_new_slides_task', '_append_slides_event',
    '_claim_slides_task', '_cleanup_stale_slides_tasks', '_slides_task_id',
]
