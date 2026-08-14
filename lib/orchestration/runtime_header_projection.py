"""Pure run-header state derived from the orchestration event contract."""

from __future__ import annotations

from lib.orchestration.events import event_gate_effect, event_run_status


class RuntimeHeaderProjectionState:
    """Return only nonterminal header transitions not already projected."""

    def __init__(self):
        self._status = 'pending'
        self._gate_ids: set[str] = set()
        self._anonymous_gate_count = 0

    def consume(self, event_type: str, event: dict) -> str:
        status = event_run_status(event_type)
        effect = event_gate_effect(event_type)
        request_id = str(event.get('request_id') or '')
        if effect == 'open':
            if request_id:
                self._gate_ids.add(request_id)
            else:
                self._anonymous_gate_count += 1
        elif effect == 'close':
            closed = False
            if request_id and request_id in self._gate_ids:
                self._gate_ids.remove(request_id)
                closed = True
            elif not request_id and self._anonymous_gate_count:
                self._anonymous_gate_count -= 1
                closed = True
            if not closed or self._gate_ids or self._anonymous_gate_count:
                status = ''

        if not status or status == self._status:
            return ''
        self._status = status
        return status


__all__ = ['RuntimeHeaderProjectionState']
