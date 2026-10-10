"""lib/production/heartbeat.py — worker-side liveness heartbeat (P-UX2).

The third piece of the progress-perception contract
(docs/modules/ingest_media.md §3.1): long phases (LLM script writing,
per-scene TTS, concat/mux ffmpeg runs) emit **zero events** today, so the
frontend cannot tell "working" from "dead" — and the read-side stall reaper
(:meth:`TaskRuntime.reap_if_stalled`) would false-kill them.

Usage — wrap any long phase::

    from lib.production.heartbeat import heartbeat

    with heartbeat(task, _append_podcast_event, 'script'):
        script, meta = generate_script(...)

Every ``interval`` seconds a ``{'type': 'heartbeat', 'phase', 'elapsed_s'}``
event is appended (monotonic seq + WS push + ``updated_at`` touch — the
append IS the reap-clock reset). Zero LLM cost; a 5-minute phase adds ~30
rows to the event log, which the cursor protocol digests natively.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import Callable

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['heartbeat', 'HEARTBEAT_INTERVAL']

#: Default beat cadence. The stall reaper runs at 120s = 12× this, so a
#: heartbeating phase can never be false-killed.
HEARTBEAT_INTERVAL = 10.0


@contextmanager
def heartbeat(task: dict, append_event: Callable[[dict, dict], object],
              phase: str, *, interval: float = HEARTBEAT_INTERVAL):
    """Emit a ``heartbeat`` event every ``interval``s while the block runs.

    Args:
        task: the worker's task dict (passed through to ``append_event``).
        append_event: the capability's event sink, e.g.
            ``_append_podcast_event`` / ``_append_motion_event``.
        phase: phase name carried by every beat ('script', 'render', …).
        interval: seconds between beats (first beat fires AFTER one
            interval — the phase entry event already proves liveness at t=0).

    Best-effort: a failing append is logged at debug and the beat loop keeps
    going — a heartbeat must never break the phase it watches.
    """
    stop = threading.Event()
    started = time.time()

    def _beat() -> None:
        while not stop.wait(interval):
            try:
                append_event(task, {
                    'type': 'heartbeat',
                    'phase': phase,
                    'elapsed_s': round(time.time() - started, 1),
                })
            except Exception as e:  # never let a watcher kill the watched
                logger.debug('[Production] heartbeat append failed (%s): %s',
                             phase, e)

    thread = threading.Thread(target=_beat, name=f'hb-{phase}', daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=1.0)
