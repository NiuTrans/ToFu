"""Bounded route ancestry for one dispatch; no mutation of reusable slots.

Transport invocation boundaries call start(). Settlement reads snapshot().
Candidate order, queue waits and observer delivery never manufacture attempts.
"""
import copy
import json
import time


class RouteAttemptTrace:
    def __init__(self):
        self._snapshot = {}
        self._transitions = []
        # Bounded cause for the NEXT route change: either an exception class
        # name (the previous slot failed) or a short snake_case reason such as
        # ``route_rescore`` / ``route_retry``. Never a free-form message.
        self._pending_cause = ''

    def note_cause(self, cause):
        text = str(cause or '')[:120]
        if text:
            self._pending_cause = text

    def start(self, slot):
        template = getattr(slot, 'route_snapshot', None)
        if not template:
            self._snapshot = {}
            self._transitions.clear()
            return
        previous = self._snapshot
        current = copy.deepcopy(template)
        if not all(current.get(key) for key in (
                'provider_id', 'offering_id', 'deployment_id')):
            self._snapshot = {}
            self._transitions.clear()
            return
        target = {key: current[key] for key in (
            'provider_id', 'offering_id', 'deployment_id')}
        source = ({key: previous[key] for key in target} if previous else None)
        if source != target:
            cross_model = bool(previous and previous.get('actual_model')
                               != current.get('actual_model'))
            transition = {
                'kind': ('initial' if source is None else
                         'model_fallback' if cross_model else 'provider_failover'),
                'from': source,
                'to': target,
                'reason': ('computed_v2_route' if source is None else
                           'compatible_model_fallback' if cross_model else
                           'provider_or_deployment_failover'),
            }
            if source is not None and self._pending_cause:
                # ``cause`` answers "why did the route change": the failure
                # class that killed the previous slot, or a scheduler reason
                # when nothing failed (plain re-score / same-route retry).
                transition['cause'] = self._pending_cause
            self._pending_cause = ''
            self._transitions.append(transition)
            self._transitions = self._transitions[-32:]
        current['transitions'] = copy.deepcopy(self._transitions)
        current['recorded_at'] = time.time()
        from lib.model_routing.domain import MAX_ROUTE_SNAPSHOT_BYTES
        while len(json.dumps(current, ensure_ascii=False).encode()) > MAX_ROUTE_SNAPSHOT_BYTES:
            if current['transitions']:
                current['transitions'].pop(0)
            elif current.get('degradation_reasons'):
                current['degradation_reasons'].pop()
            else:
                # Templates are already bounded; never let diagnostics alter
                # request execution if a malformed external slot bypasses it.
                current = {}
                break
        self._snapshot = current
        self._transitions = copy.deepcopy(current.get('transitions', []))

    def snapshot(self):
        return copy.deepcopy(self._snapshot)
