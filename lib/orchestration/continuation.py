"""Worker-free durable human continuation over the run's existing event log.

The journal captures effect boundaries; this owner suspends only after graph
workers have unwound, admits one owner decision through a unique event sequence,
and starts one successor execution generation without changing the durable ID.
"""
from __future__ import annotations

import hashlib
import time
import uuid

from lib.orchestration.execution_journal import read_journal
from lib.orchestration.graph import FlowEventDeliveryError


class FlowSuspended(BaseException):
    """Control transfer, deliberately not an ordinary node failure."""


def human_request(run_id, key, inputs, digest):
    from lib.orchestration._runtime_params import resolve_node_runtime_param
    node, context = inputs
    mode = resolve_node_runtime_param(node, 'mode')
    timeout = resolve_node_runtime_param(node, 'timeout_sec')
    return {
        'type': 'human_request', 'node_id': node['id'],
        'name': node.get('name') or 'Human', 'mode': mode,
        'prompt': str(resolve_node_runtime_param(node, 'prompt') or ''),
        'request_id': 'dg.' + run_id + '.' + hashlib.sha256(key.encode()).hexdigest()[:32],
        'continuation': {'key': key, 'input_hash': digest, 'context': context,
                         'expires_at': time.time() + int(timeout or 300) if mode == 'approve' else 0},
    }


def history(runs, run_id, *, keep=None):
    events, cursor = [], 0
    while True:
        page = runs.replay(run_id, cursor)
        if page is None or page.cursor_reset:
            raise FlowEventDeliveryError('Continuation history is unavailable')
        events.extend(e for e in page.events if keep is None or keep(e))
        if page.caught_up:
            return events, page.next_cursor
        if page.next_cursor <= cursor:
            raise FlowEventDeliveryError('Continuation cursor did not advance')
        cursor = page.next_cursor


def suspend(runs, run_id, journal, runtime, task_id):
    if journal.abort_requested():
        raise FlowEventDeliveryError('Cannot suspend after an uncommitted effect')
    pending = journal.pending_humans()
    if not pending:
        raise FlowEventDeliveryError('Suspension has no pending human decision')
    # All branch futures have joined before this point. Other pending gates
    # are reconstructed from the same graph after this first decision.
    request = pending[0]
    _, cursor = history(runs, run_id, keep=lambda _: False)
    if not runs.project_event(run_id, cursor, request, 'paused'):
        raise FlowEventDeliveryError('Could not persist human continuation')
    # Publish only AFTER the durable request can already accept its answer.
    runtime.append_event(task_id, {k: v for k, v in request.items() if k != 'continuation'})
    runtime.finish(task_id, result={'suspended': True, 'run_id': run_id})


def resolve(runs, runtime, resolver_provider, request_id, answer, *, owner_user_id, mode):
    from lib.orchestration.runtime_service import spawn_runtime_flow
    from lib.human_gate_contract import MAX_HUMAN_GATE_RESPONSE_LENGTH
    if mode == 'approve' and not isinstance(answer, bool):
        raise ValueError('approved must be a boolean')
    if mode == 'input' and (not isinstance(answer, str) or not answer.strip()
                            or len(answer) > MAX_HUMAN_GATE_RESPONSE_LENGTH):
        raise ValueError('Invalid human input response')
    if not request_id.startswith('dg.'):
        return None
    try:
        run_id, _ = request_id[3:].rsplit('.', 1)
    except ValueError:
        return False
    run = runs.get_execution_snapshot(run_id)
    if run is None:
        return False
    from lib.orchestration.execution_journal import JOURNAL_EVENTS
    events, cursor = history(runs, run_id, keep=lambda e:
        e.get('type') in JOURNAL_EVENTS or e.get('request_id') == request_id)
    requests = [e for e in events if e.get('type') == 'human_request'
                and e.get('request_id') == request_id and e.get('continuation')]
    if not requests:
        return False
    request = requests[-1]
    if request['mode'] != mode:
        return False
    previous = [e for e in events if e.get('decision_request_id') == request_id]
    if previous:
        return previous[-1].get('decision_answer') == answer
    if run['status'] != 'paused' or events[-1].get('request_id') != request_id:
        return False
    detail = request['continuation']
    records, parent = read_journal(events, definition=run['definition'], initial_context=run.get('input', ''))
    if parent:
        raise FlowEventDeliveryError('Continuation seed has no local authority')
    context = detail['context']
    expired = detail['expires_at'] and time.time() >= detail['expires_at']
    if mode == 'input':
        context += ('\n\n' if context else '') + f"[Human input — {request['name']}]\n{answer}"
    record = {'kind': 'human', 'input_hash': detail['input_hash'],
              'result': {'context': context, 'aborted': bool(expired or (mode == 'approve' and not answer))}}
    claim = {'type': 'execution_effect_completed', 'key': detail['key'], **record,
             'decision_request_id': request_id, 'decision_answer': answer,
             'continuation_claim': uuid.uuid4().hex}
    # Native and SQL event.project both enforce unique sequence and terminal
    # fencing. Only the insertion winner may launch, including identical replies.
    if not runs.claim_event(run_id, cursor, claim, 'running'):
        latest, _ = history(runs, run_id)
        return any(e.get('decision_request_id') == request_id
                   and e.get('decision_answer') == answer for e in latest)
    records[detail['key']] = record
    generation = 'continuation_' + claim['continuation_claim']
    try:
        spawn_runtime_flow(runtime, run['definition'],
            owner_user_id=owner_user_id, task_id=generation,
            durable_run_id=run_id, continuation=True, sequence_offset=cursor + 1,
            initial_context=run.get('input', ''), durable_runs=runs,
            recovery_records=records,
            resolved_request={'type': 'human_resolved', 'request_id': request_id,
                'node_id': request['node_id'], 'mode': mode,
                'resolution': ('cancelled' if expired else 'answered' if mode == 'input'
                               else 'approved' if answer else 'rejected')},
            subflow_resolver_provider=resolver_provider)
    except Exception as exc:
        from lib.error_envelope import make_envelope
        from lib.orchestration.durable_projection import DurableRunProjection
        error = make_envelope('worker_lost', message='Continuation worker could not start',
            detail=str(exc), source='orchestration.continuation',
            context='human decision handoff', retryable=False)
        # Fence an uncertain handoff before exposing explicit recovery. An
        # unmatched agent admission still refuses replay, even after this fence.
        DurableRunProjection(runs, run_id).record_error(error)
        try:
            runtime.finish(generation, error=error, error_context='human decision handoff')
        except Exception:
            from lib.log import get_logger
            get_logger(__name__).exception('Continuation runtime cleanup failed: %s', generation)
        raise
    return True


def expire_wait(persistence, run):
    """Settle a deadline on observation; no thread or timer per parked run.

    Two bounded tail reads avoid replaying the journal on status polling.
    The same event-sequence CAS as user decisions arbitrates deadline races.
    """
    if run is None or run.get('status') != 'paused':
        return run
    run_id = run['id']
    boundary = persistence.get_event_page(run_id, (1 << 53) - 1).next_cursor
    if not boundary:
        return run
    page = persistence.get_event_page(run_id, boundary - 1)
    if not page.caught_up or not page.events:
        return run
    request = page.events[-1]
    if request.get('continuation_expired') is not True:
        detail = request.get('continuation') or {}
        deadline = detail.get('expires_at', 0)
        if (request.get('type') != 'human_request' or request.get('mode') != 'approve'
                or not deadline or time.time() < deadline):
            return run
        event = {'type': 'human_resolved', 'request_id': request['request_id'],
                 'node_id': request['node_id'], 'mode': 'approve',
                 'resolution': 'cancelled', 'continuation_expired': True}
        if not persistence.claim_event(run_id, page.next_cursor, event, 'paused'):
            return persistence.get_run(run_id)
    # The committed expiry intent fences answers. Re-observation completes this
    # idempotent terminal transition after a crash between these two writes.
    persistence.update_status(run_id, 'aborted')
    current = persistence.get_run(run_id)
    if current and current.get('status') == 'paused':
        raise FlowEventDeliveryError('Could not settle expired human continuation')
    return current
