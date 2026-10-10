"""Owner-scoped, explicit recovery of a process-interrupted durable Flow.

Recovery forks an immutable history into one deterministic successor. Database
run-ID uniqueness elects exactly one launcher; request retries only return its
identity, never attach a second worker. Existing event projection fences all
receipts against terminal/aborted runs on both storage authorities.
"""
from __future__ import annotations

import hashlib

from lib.goal_runs.contract import GOAL_EXECUTION_LAUNCH_FORMATS
from lib.orchestration.errors import RunRecoveryError
from lib.orchestration.execution_journal import JOURNAL_EVENTS, RecoveryRefused, read_journal


def recovery_run_id(source_run_id: str) -> str:
    return 'recover_' + hashlib.sha256(source_run_id.encode()).hexdigest()[:40]


def _interrupted(run):
    error = run.get('error')
    return (run.get('status') == 'error' and isinstance(error, dict)
            and error.get('kind') == 'worker_lost'
            and error.get('source') in ('orchestration.startup_recovery',
                                         'orchestration.continuation'))


def load_recovery_records(runs, run_id: str):
    """Stream bounded pages and retain effect receipts only, not UI history."""
    visited = set()
    original = None
    while True:
        if run_id in visited:
            raise RunRecoveryError('Recovery history contains a cycle')
        visited.add(run_id)
        run = runs.get_execution_snapshot(run_id)
        if run is None:
            raise RunRecoveryError('Run or required recovery history not found', not_found=True)
        if not _interrupted(run):
            raise RunRecoveryError('Only a run interrupted by restart or continuation handoff can be resumed')
        if original is None:
            original = run
        elif (run.get('definition') != original.get('definition')
              or run.get('input', '') != original.get('input', '')):
            raise RunRecoveryError('Recovery ancestor definition/input changed')
        cursor, events = 0, []
        while True:
            page = runs.replay(run_id, cursor)
            if page is None or page.cursor_reset:
                raise RunRecoveryError('Recovery history disappeared during replay')
            if run_id == original['id'] and any(
                    event.get('type') == 'execution_recovery_claim' for event in page.events):
                raise RunRecoveryError('This run already has a recovery successor; missing successor history cannot be recreated')
            events.extend(event for event in page.events if event.get('type') in JOURNAL_EVENTS)
            if page.caught_up:
                break
            if page.next_cursor <= cursor:
                raise RunRecoveryError('Recovery history cursor did not advance')
            cursor = page.next_cursor
        if any(event.get('goal_execution_archive') in GOAL_EXECUTION_LAUNCH_FORMATS
               or (isinstance(event.get('launch_metadata'), dict)
                   and event['launch_metadata'].get('format') in GOAL_EXECUTION_LAUNCH_FORMATS)
               for event in events):
            raise RunRecoveryError(
                'Chat Goal recovery requires its conversation, model context and lifecycle binding')
        try:
            records, parent = read_journal(events, definition=run['definition'],
                                           initial_context=run.get('input', ''))
        except RecoveryRefused as exc:
            raise RunRecoveryError(str(exc)) from exc
        if not parent:
            return original, records
        run_id = parent


def record_recovery_claim(runs, source_id: str, successor_id: str):
    """Keep the launch claim with source history, even if its child is deleted."""
    cursor = 0
    while True:
        page = runs.replay(source_id, cursor)
        if page is None or page.cursor_reset:
            raise RunRecoveryError('Recovery source disappeared before launch')
        for event in page.events:
            if event.get('type') == 'execution_recovery_claim':
                if event.get('successor_run_id') == successor_id:
                    return
                raise RunRecoveryError('Recovery source already has a different successor')
        if page.caught_up:
            break
        if page.next_cursor <= cursor:
            raise RunRecoveryError('Recovery source cursor did not advance')
        cursor = page.next_cursor
    if not runs.append_event(source_id, page.next_cursor, {
            'type': 'execution_recovery_claim', 'successor_run_id': successor_id}):
        if recovery_claim_successor(runs, source_id) == successor_id:
            return
        raise RunRecoveryError('Could not commit recovery launch claim')


def recovery_claim_successor(runs, source_id: str) -> str:
    """Read one owner-bound immutable source claim without retaining history."""
    cursor, successor = 0, ''
    while True:
        page = runs.replay(source_id, cursor)
        if page is None or page.cursor_reset:
            raise RunRecoveryError('Recovery source history disappeared')
        for event in page.events:
            if event.get('type') == 'execution_recovery_claim':
                value = event.get('successor_run_id')
                if not isinstance(value, str) or not value or (successor and successor != value):
                    raise RunRecoveryError('Recovery source has conflicting claims')
                successor = value
        if page.caught_up:
            return successor
        if page.next_cursor <= cursor:
            raise RunRecoveryError('Recovery source cursor did not advance')
        cursor = page.next_cursor
