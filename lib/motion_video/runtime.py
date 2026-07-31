"""lib/motion_video/runtime.py — TaskRuntime for motion-video tasks.

Rides :class:`lib.production.runtime.ProductionRuntime` — the dedup index,
create-with-field-shape, append+touch, stale sweep and id minting that used to
be hand-rolled here (and, near-verbatim, in every other production capability)
now live in the substrate (P6, driven by the P7 measurement in
docs/PRODUCTION_PIPELINE_DESIGN.md §9).

Background video generation (SRT/topic → storyboard → narrate → render →
concat → mux) with a dedup index so a second identical request joins the
in-flight task instead of regenerating; events: ``phase`` / ``scene_done`` /
``scene_authored`` / ``final`` / ``done`` / ``error`` / ``aborted``.
"""

from __future__ import annotations

from lib.log import get_logger
from lib.production.runtime import ProductionRuntime

logger = get_logger(__name__)

_production = ProductionRuntime(
    'motion-video', id_prefix='motion', ttl=3600, push_channel='motion',
    error_source='routes.api_v1.motion', log_label='MotionVideo',
    stall_timeout=120)

#: The underlying TaskRuntime — what ``routes/api_v1/tasks.py::_registries()``
#: discovers, and what existing call sites expect from this name.
_motion_runtime = _production.runtime
_motion_tasks = _production.tasks
_motion_tasks_lock = _production.lock
#: (srt_sha, voice, alignment, aspect, narration, quality, burn_in) -> task_id
_motion_dedup_index = _production.dedup_index


def _motion_index_get(key: tuple):
    """Return a live task_id for the dedup key, pruning stale entries."""
    return _production.index_get(key)


def _motion_index_register(key: tuple, task_id: str) -> None:
    _production.index_register(key, task_id)


def _new_motion_task(task_id: str, *, srt_path: str, workdir: str,
                     voice: str, speed, alignment: str, narration: bool,
                     quality: str, parallel: int, width: int, height: int,
                     scenes_path: str = ''):
    """Create + register a pending motion task with the engine's field shape."""
    return _production.create_task(
        task_id,
        meta={'srt_path': srt_path, 'voice': voice, 'alignment': alignment,
              'narration': narration, 'quality': quality,
              'aspect': f'{width}x{height}'},
        fields={
            'srt_path': srt_path,
            'scenes_path': scenes_path,
            'workdir': workdir,
            'voice': voice,
            'speed': speed,
            'alignment': alignment,
            'narration': narration,
            'quality': quality,
            'parallel': parallel,
            'width': width,
            'height': height,
        })


def _append_motion_event(task, event):
    """Append one event (monotonic seq + WS push)."""
    return _production.append_event(task, event)


def _cleanup_stale_motion_tasks():
    """Drop finished/error/aborted tasks past TTL; prune dedup entries."""
    return _production.cleanup_stale()


def _motion_task_id():
    return _production.new_task_id()


__all__ = [
    '_production',
    '_motion_runtime',
    '_motion_tasks',
    '_motion_tasks_lock',
    '_motion_dedup_index',
    '_motion_index_get',
    '_motion_index_register',
    '_new_motion_task',
    '_append_motion_event',
    '_cleanup_stale_motion_tasks',
    '_motion_task_id',
]
