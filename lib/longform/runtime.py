"""lib/longform/runtime.py — TaskRuntime for long-form report tasks.

Rides :class:`lib.production.runtime.ProductionRuntime` — the dedup index,
create-with-field-shape, append+touch, stale sweep and id minting that used to
be hand-rolled here now live in the substrate (P6, driven by the P7
shared lifecycle contract in docs/modules/production.md).

Events: ``stage`` (from the stage graph) / ``phase`` / ``final`` / ``done`` /
``error``.
"""

from __future__ import annotations

from lib.log import get_logger
from lib.production.runtime import ProductionRuntime

logger = get_logger(__name__)

_production = ProductionRuntime(
    'longform-report', id_prefix='longform', ttl=3600,
    push_channel='longform', error_source='lib.longform.engine',
    log_label='Longform')

#: The underlying TaskRuntime discovered by the generic task API.
_longform_runtime = _production.runtime


def _longform_index_get(key: tuple):
    return _production.index_get(key)


def _longform_index_register(key: tuple, task_id: str) -> None:
    _production.index_register(key, task_id)


def _new_longform_task(task_id: str, *, topic: str, workdir: str, lang: str,
                       depth: str, user_id: int, conv_id: str = ''):
    """Create + register a pending long-form task with the engine's shape."""
    return _production.create_task(
        task_id,
        user_id=user_id,
        meta={'topic': topic, 'lang': lang, 'depth': depth},
        fields={'topic': topic, 'workdir': workdir, 'lang': lang,
                'depth': depth, 'conv_id': conv_id})


def _append_longform_event(task, event):
    return _production.append_event(task, event)


def _cleanup_stale_longform_tasks():
    return _production.cleanup_stale()


def _longform_task_id():
    return _production.new_task_id()


__all__ = [
    '_production', '_longform_runtime', '_longform_index_get',
    '_longform_index_register', '_new_longform_task',
    '_append_longform_event', '_cleanup_stale_longform_tasks',
    '_longform_task_id',
]
