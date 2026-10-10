"""lib/tasks_pkg/event_invariants.py — Durable task event-log invariants.

Why this exists
---------------
The persisted task event log is the authority every reconstruction path
folds from (cold SSE replay, Request Inspector evidence, turn traces). The
log's writers are guarded at construction (``build_event``) and at the
delivery seam (``check_event``), but nothing asserted the RELATIONSHIPS a
reconstruction relies on: sequence monotonicity, round open/close pairing,
tool-call settlement, model-span settlement, and the rule that every model
request left a rebuildable request snapshot ("model-visible ⟺ logged").

This module is that assertion layer. It is a pure fold over the rows
``event_log.read_events`` returns (raw event dicts are also accepted), so it
runs identically over durable rows in production diagnostics and over
recorded emissions in tests. Violations are RETURNED, never raised — the
caller decides enforcement:

* tests (``tests/events/test_event_log_invariants.py``) assert a clean log yields
  ``()`` and each broken log yields the named rule;
* the opt-in runtime audit (``TOFU_EVENT_INVARIANTS=1``,
  :func:`maybe_check_task_log`, wired at the terminal finalize seam) logs
  every violation at ERROR without ever failing a user turn.

Pairing semantics mirror the folds that consume the log
(``turn_trace.py`` occurrence pairing, settlement-exactly-once in
docs/contracts/TOOL_EXECUTION_POLICY.md):

* ``round_start`` / ``round_end`` pair sequentially by ``roundNum``; a round
  start while another is open, an end with no open round, or a roundNum
  mismatch is a violation.
* ``tool_start`` opens an occurrence of its ``toolCallId``;
  ``tool_complete`` settles the oldest open occurrence. A settle without a
  start is TOLERATED (non-streamed and replay paths may elide the
  announcement); an unsettled start is a violation.
* ``model_request_start`` / ``model_request_complete`` pair by ``spanId``;
  a complete with no open start is tolerated (the span may have opened in an
  earlier log window), an unsettled start is a violation.
* Every ``roundNum`` that carries a ``model_request_start`` must also carry
  at least one ``messages_snapshot`` with ``kind='request'`` for that round
  — otherwise the exact outbound request is not reconstructible from the
  log (``degradedReason=request-build-fallback`` territory).

``allow_open_tail=True`` drops every "still open at end of log" violation
(live or aborted tasks legitimately end mid-round) while keeping all
ordering/mismatch violations.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = [
    'InvariantViolation',
    'check_event_log_invariants',
    'maybe_check_task_log',
    'run_task_log_invariants',
]


@dataclass(frozen=True)
class InvariantViolation:
    """One broken invariant: the rule id plus concrete evidence."""

    rule: str
    detail: str


def _payload(row) -> dict:
    if not isinstance(row, dict):
        return {}
    payload = row.get('payload')
    return payload if isinstance(payload, dict) else row


def _snapshot_round_num(payload: dict):
    ref = payload.get('inspectorRef')
    if isinstance(ref, dict) and ref.get('roundNum') is not None:
        return ref.get('roundNum')
    return payload.get('roundNum')


def check_event_log_invariants(
        events, *, allow_open_tail: bool = False
) -> tuple[InvariantViolation, ...]:
    """Fold one ordered event window and return every invariant violation.

    ``events`` is the ``event_log.read_events`` row shape
    (``{'event_id': int, 'payload': dict}``) or raw event dicts. Rows
    without either shape are ignored. The fold is pure: no I/O, no clock.
    """
    violations: list[InvariantViolation] = []

    # ── Unknown durable vocabulary is a reconstruction hazard ──
    from lib.agent_core.events import EventContractError, \
        assert_registered_event_types
    try:
        assert_registered_event_types(events, consumer='event_invariants')
    except EventContractError as exc:
        violations.append(InvariantViolation('unknown-type', str(exc)))

    # ── seq monotonicity over rows that carry event_id ──
    last_event_id = None
    for row in events or []:
        if not isinstance(row, dict) or row.get('event_id') is None:
            continue
        event_id = int(row['event_id'])
        if last_event_id is not None and event_id <= last_event_id:
            violations.append(InvariantViolation(
                'seq-regression',
                f'event_id {event_id} follows {last_event_id}'))
        last_event_id = event_id

    # ── round / tool / span pairing, one sequential pass ──
    open_round: int | None = None
    open_tools: dict[str, list] = {}
    open_spans: set[str] = set()
    request_rounds: list = []
    evidence_rounds: set = set()

    for row in events or []:
        payload = _payload(row)
        type_ = payload.get('type')
        if type_ == 'round_start':
            round_num = payload.get('roundNum')
            if open_round is not None:
                violations.append(InvariantViolation(
                    'round-pairing',
                    f'round_start(roundNum={round_num}) while round '
                    f'{open_round} is still open'))
            open_round = round_num
        elif type_ == 'round_end':
            round_num = payload.get('roundNum')
            if open_round is None:
                violations.append(InvariantViolation(
                    'round-pairing',
                    f'round_end(roundNum={round_num}) with no open round'))
            elif round_num != open_round:
                violations.append(InvariantViolation(
                    'round-pairing',
                    f'round_end(roundNum={round_num}) closes open round '
                    f'{open_round}'))
            open_round = None
        elif type_ == 'tool_start':
            call_id = payload.get('toolCallId')
            if call_id:
                open_tools.setdefault(str(call_id), []).append(
                    payload.get('roundNum'))
        elif type_ == 'tool_complete':
            call_id = payload.get('toolCallId')
            if call_id and open_tools.get(str(call_id)):
                open_tools[str(call_id)].pop(0)
                if not open_tools[str(call_id)]:
                    del open_tools[str(call_id)]
        elif type_ == 'model_request_start':
            span = payload.get('spanId')
            if span:
                open_spans.add(str(span))
            request_rounds.append(payload.get('roundNum'))
        elif type_ == 'model_request_complete':
            span = payload.get('spanId')
            if span:
                open_spans.discard(str(span))
        elif (type_ == 'messages_snapshot'
              and payload.get('kind') == 'request'):
            round_num = _snapshot_round_num(payload)
            if round_num is not None:
                evidence_rounds.add(round_num)

    if not allow_open_tail:
        if open_round is not None:
            violations.append(InvariantViolation(
                'round-pairing',
                f'round {open_round} still open at end of log'))
        for call_id, occurrences in sorted(open_tools.items()):
            violations.append(InvariantViolation(
                'tool-settlement',
                f'toolCallId {call_id} has {len(occurrences)} unsettled '
                f'occurrence(s) (rounds {occurrences})'))
        for span in sorted(open_spans):
            violations.append(InvariantViolation(
                'model-span-settlement',
                f'model_request spanId {span} never completed'))

    # ── model-visible ⟺ logged: every request round keeps request evidence ──
    for round_num in request_rounds:
        if round_num is None:
            continue
        if round_num not in evidence_rounds:
            violations.append(InvariantViolation(
                'request-evidence',
                f'model_request_start at roundNum={round_num} has no '
                f"messages_snapshot(kind='request') for that round — the "
                f'outbound request is not reconstructible from the log'))

    return tuple(violations)


def run_task_log_invariants(
        task_id: str, *, allow_open_tail: bool = False
) -> tuple[InvariantViolation, ...]:
    """Read one task's durable log and check it. Raises on read failure
    (``read_events(raise_on_error=True)``) — callers decide whether the
    audit may fail soft."""
    from lib.tasks_pkg.event_log import read_events
    rows = read_events(str(task_id), raise_on_error=True)
    return check_event_log_invariants(rows, allow_open_tail=allow_open_tail)


def maybe_check_task_log(task: dict) -> None:
    """Opt-in terminal-state audit (``TOFU_EVENT_INVARIANTS=1``).

    Wired at the finalize seam after the done event is durable. Aborted
    tasks legitimately end mid-round, so they are checked with
    ``allow_open_tail=True``. The audit never raises: a broken audit must
    not fail a user turn — violations are logged at ERROR.
    """
    if os.environ.get('TOFU_EVENT_INVARIANTS') != '1':
        return
    task_id = str(task.get('id') or '')
    if not task_id:
        return
    try:
        violations = run_task_log_invariants(
            task_id, allow_open_tail=bool(task.get('aborted')))
    except Exception as exc:  # the audit itself must never fail a turn
        logger.warning('[event-invariants] audit failed for task=%s: %s',
                       task_id[:8], exc)
        return
    for violation in violations:
        logger.error('[event-invariants] task=%s %s: %s',
                     task_id[:8], violation.rule, violation.detail)
