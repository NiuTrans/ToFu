"""Live and durable orchestration event fan-out."""

from __future__ import annotations

from collections.abc import Callable
import threading

from lib.orchestration.durable_projection import DurableProjectionError
from lib.orchestration.events import is_durable_event
from lib.orchestration.runtime_header_projection import (
    RuntimeHeaderProjectionState,
)


class FlowEventSink:
    """Fan one engine event into live and optional durable projections.

    Durable runs intentionally omit token-level deltas; ``step_trace`` and
    ``step_complete`` are self-contained replay facts. Human gates map to the
    shared run-header lifecycle here instead of in transport adapters.
    """

    def __init__(
        self,
        live_append: Callable[[dict], int | None],
        *,
        durable_project: Callable[[int, dict, str], None] | None = None,
        persist_deltas: bool = False,
    ):
        self._live_append = live_append
        self._durable_project = durable_project
        self._persist_deltas = bool(persist_deltas)
        self._header = RuntimeHeaderProjectionState()
        self._lock = threading.Lock()

    def __call__(self, event: dict) -> None:
        with self._lock:
            seq = self._live_append(event)
            event_type = str(event.get('type') or '')
            status = self._header.consume(event_type, event)

            should_persist = self._durable_project is not None and (
                self._persist_deltas or is_durable_event(event_type)
            )
            if should_persist:
                if seq is None:
                    raise DurableProjectionError(
                        'live runtime did not assign a sequence to durable '
                        f'orchestration event {event_type!r}'
                    )
                self._durable_project(seq, event, status)
            elif status and self._durable_project is not None:
                raise DurableProjectionError(
                    'run-header lifecycle event is not durable: '
                    f'{event_type!r}'
                )


__all__ = ['FlowEventSink']
