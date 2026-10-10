"""Boot continuity pass: automatic resume of restart-interrupted durable runs.

Design authority: ``docs/RUN_CONTINUITY.md`` (D1/D2/D6, phase 1).

Entry point: :func:`run_boot_continuity_pass`. Dependencies are injected: the
shared flow runtime port, owner-bound run/definition service factories, and
the launch-domain policy set. The pass is process authority, exactly like the
startup retire fence it follows — every successor binds the owner recorded on
the durable run row; request-time owner predicates are untouched.

Election is durable: the startup retire maintenance operation returns freshly
settled runs and pending Chat Goal handoffs from earlier generations; the
composition root hands them here once after readiness. A candidate already carrying a recovery claim is
skipped, so repeated passes never spawn twice. Refusals, resumes, deferrals
and crash-loop exhaustion land as typed ``continuity_*`` audit events on the
source run's durable stream.
"""

from __future__ import annotations

import os
import time

from lib.log import get_logger
from lib.orchestration.events import CONTINUITY_EVENT_TYPES

logger = get_logger(__name__)

EVENT_RESUMED, EVENT_REFUSED, EVENT_DEFERRED, EVENT_EXHAUSTED = (
    CONTINUITY_EVENT_TYPES)

_REFUSAL_REASON_CHARS = 300
_LINEAGE_WALK_LIMIT = 16
_RESUME_WINDOW_SECONDS = 600
_MAX_RESUMES_IN_WINDOW = 3


def auto_resume_enabled(environ=None) -> bool:
    """Operator kill switch: TOFU_CONTINUITY_AUTO_RESUME=off disables the pass."""
    env = os.environ if environ is None else environ
    return str(env.get('TOFU_CONTINUITY_AUTO_RESUME', '')
               ).strip().lower() not in ('0', 'off', 'false', 'no')


def max_resumes_per_boot(environ=None) -> int:
    """Use launch-derived task capacity unless the operator sets a boot bound.

    This bounds recovery admissions without changing normal worker dispatch.
    """
    from runtime_guards import (
        MAX_TASK_CONCURRENCY, resolve_resource_budget,
        task_concurrency_hard_ceiling,
    )

    env = os.environ if environ is None else environ
    raw = str(env.get('TOFU_CONTINUITY_AUTO_RESUME_MAX', '')).strip()
    try:
        value = int(raw) if raw else 0
    except ValueError:
        value = 0
    if value > 0:
        return min(MAX_TASK_CONCURRENCY, value)
    workers = resolve_resource_budget(
        'TOFU_AGENT_WORKERS', env, minimum=1, maximum=MAX_TASK_CONCURRENCY)
    admission = resolve_resource_budget(
        'TOFU_MAX_INFLIGHT_TASKS', env, minimum=0,
        maximum=MAX_TASK_CONCURRENCY)
    return max(1, min(workers, admission or workers,
                      task_concurrency_hard_ceiling(env)))


def _append_source_event(runs, run_id: str, event: dict) -> bool:
    """Append one audit event after the last committed source sequence."""
    cursor = 0
    while True:
        page = runs.replay(run_id, cursor)
        if page is None or page.cursor_reset:
            return False
        if page.caught_up:
            break
        if page.next_cursor <= cursor:
            return False
        cursor = page.next_cursor
    return runs.append_event(run_id, page.next_cursor, event)


def _source_claim_exists(runs, run_id: str) -> bool:
    """A committed recovery claim makes any later pass a no-op."""
    cursor = 0
    while True:
        page = runs.replay(run_id, cursor)
        if page is None or page.cursor_reset:
            return False
        if any(event.get('type') == 'execution_recovery_claim'
               for event in page.events):
            return True
        if page.caught_up or page.next_cursor <= cursor:
            return False
        cursor = page.next_cursor


def _journal_parent(runs, run_id: str) -> str:
    """First journal header's parent link; '' when absent or unreadable."""
    from lib.goal_runs.journal_codec import decode_events
    def packets():
        cursor = 0
        while True:
            page = runs.replay(run_id, cursor)
            if page is None or page.cursor_reset:
                return
            yield from page.events
            if page.caught_up or page.next_cursor <= cursor:
                return
            cursor = page.next_cursor
    for event in decode_events(packets()):
        if event.get('type') == 'execution_journal_start':
            parent = event.get('parent_run_id')
            return parent if isinstance(parent, str) else ''
    return ''


def _rapid_lineage_resumes(runs, run_id: str, *, now_ms: int) -> int:
    """Count recovery links retired inside the crash-loop window.

    Healthy restarts are days apart and never trip the guard; a boot loop
    leaves a chain of just-retired successors minutes apart. The walk is
    bounded and follows journal parent links only, never run-id naming.
    """
    window_ms = _RESUME_WINDOW_SECONDS * 1000
    count = 0
    current = run_id
    for _ in range(_LINEAGE_WALK_LIMIT):
        snapshot = runs.get_execution_snapshot(current)
        if snapshot is None:
            break
        finished = int(snapshot.get('finished_at') or 0)
        if finished and now_ms - finished <= window_ms:
            count += 1
        else:
            break
        parent = _journal_parent(runs, current)
        if not parent:
            break
        current = parent
    return count


def _resume_candidate(candidate: dict, *, runtime, definition_service_for_owner,
                      run_service_for_owner, resume_for_created_by=None) -> str:
    """Run the ordinary explicit-resume protocol for one retired run."""
    from lib.orchestration.runtime_start_service import (
        OrchestrationRuntimeStartService,
    )
    owner = int(candidate['user_id'])
    tenant = str(candidate.get('tenant_id') or '')
    callback = (resume_for_created_by or {}).get(candidate.get('created_by'))
    if callback is not None:
        return callback(run_service_for_owner(owner, tenant),
                        str(candidate['run_id']), owner_user_id=owner, tenant_id=tenant)
    service = OrchestrationRuntimeStartService(
        runtime,
        definition_service=lambda: definition_service_for_owner(owner, tenant),
        run_service=lambda: run_service_for_owner(owner, tenant),
    )
    return service.resume(str(candidate['run_id']), owner_user_id=owner)


def _retryable_recovery_failure(error: BaseException) -> bool:
    """Preserve election when a typed storage outage is wrapped by a repository."""
    from lib.storage.errors import StorageError
    from lib.orchestration.execution_journal import RecoveryDeferred
    if isinstance(error, RecoveryDeferred):
        return True
    current = error
    for _ in range(16):
        if isinstance(current, StorageError) and (
                current.retryable or current.code in {
                    'database_busy', 'database_unavailable', 'database_timeout'}):
            return True
        current = current.__cause__ or current.__context__
        if current is None:
            break
    return False


def run_boot_continuity_pass(
    *,
    candidates,
    auto_created_by=frozenset(),
    runtime=None,
    definition_service_for_owner=None,
    run_service_for_owner=None,
    resume_for_created_by=None,
    environ=None,
    now: float | None = None,
) -> dict:
    """Resume elected restart-interrupted ``auto`` runs without human action.

    Fail-soft per candidate and overall: the pass never raises, because it
    runs on the post-ready path where a failure must not wedge the process.
    Returns a small summary dict for boot logging.
    """
    summary = {'resumed': 0, 'refused': 0, 'deferred': 0,
               'exhausted': 0, 'skipped': 0}
    rows = [row for row in (candidates or []) if isinstance(row, dict)]
    if not rows or not auto_resume_enabled(environ):
        summary['skipped'] = len(rows)
        return summary
    if runtime is None:
        logger.warning('[Continuity] pass missing runtime/run-service wiring')
        summary['skipped'] = len(rows)
        return summary
    if definition_service_for_owner is None:
        from lib.orchestration.definition_service import (
            OrchestrationDefinitionService,
        )
        definition_service_for_owner = (
            lambda owner, tenant: OrchestrationDefinitionService.for_owner(
                owner, tenant_id=tenant))
    if run_service_for_owner is None:
        from lib.orchestration.run_service import OrchestrationRunService
        from lib.orchestration.sidecar_run_store import (
            SidecarOrchestrationRunStore,
        )
        run_service_for_owner = lambda owner, tenant: OrchestrationRunService(
            SidecarOrchestrationRunStore(owner, tenant_id=tenant))
    capacity = max_resumes_per_boot(environ)
    now_ms = int((time.time() if now is None else now) * 1000)
    for row in rows:
        run_id = str(row.get('run_id') or '')
        if not run_id or row.get('created_by') not in auto_created_by:
            summary['skipped'] += 1
            continue
        try:
            runs = run_service_for_owner(
                int(row['user_id']), str(row.get('tenant_id') or ''))
            if _source_claim_exists(runs, run_id):
                summary['skipped'] += 1
                continue
            if summary['resumed'] >= capacity:
                _append_source_event(runs, run_id, {
                    'type': EVENT_DEFERRED, 'reason': 'boot_capacity',
                    'capacity': capacity})
                summary['deferred'] += 1
                continue
            if _rapid_lineage_resumes(runs, run_id, now_ms=now_ms) \
                    >= _MAX_RESUMES_IN_WINDOW:
                _append_source_event(runs, run_id, {
                    'type': EVENT_EXHAUSTED,
                    'window_seconds': _RESUME_WINDOW_SECONDS,
                    'max_resumes': _MAX_RESUMES_IN_WINDOW})
                summary['exhausted'] += 1
                continue
            successor = _resume_candidate(
                row, runtime=runtime,
                definition_service_for_owner=definition_service_for_owner,
                run_service_for_owner=run_service_for_owner,
                resume_for_created_by=resume_for_created_by)
            acknowledged = _source_claim_exists(runs, run_id)
            _append_source_event(runs, run_id, {
                'type': EVENT_RESUMED if acknowledged else EVENT_DEFERRED,
                'successor_run_id': successor, 'automatic': True,
                **({} if acknowledged else {'reason': 'awaiting_successor_journal'})})
            summary['resumed'] += 1
        except Exception as exc:  # one bad candidate never blocks the others
            retryable = _retryable_recovery_failure(exc)
            event_type = EVENT_DEFERRED if retryable else EVENT_REFUSED
            reason = str(exc)[:_REFUSAL_REASON_CHARS]
            logger.warning('[Continuity] auto-resume %s run=%s: %s',
                           'deferred' if retryable else 'refused', run_id, reason)
            try:
                _append_source_event(runs, run_id, {
                    'type': event_type, 'reason': reason,
                    **({'retryable': True} if retryable else {})})
            except Exception:
                logger.warning('[Continuity] recovery decision event failed run=%s',
                               run_id, exc_info=True)
            summary['deferred' if retryable else 'refused'] += 1
    return summary


__all__ = [
    'EVENT_RESUMED', 'EVENT_REFUSED', 'EVENT_DEFERRED', 'EVENT_EXHAUSTED',
    'auto_resume_enabled', 'max_resumes_per_boot', 'run_boot_continuity_pass',
]
