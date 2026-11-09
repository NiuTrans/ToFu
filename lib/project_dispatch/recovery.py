"""Retry existing project grants at known idle/recovery lifecycle events only.

Entry points: ``recover_conversation_dispatch`` resolves one owner-scoped
metadata snapshot at durable lane settlement; ``recover_project_dispatch``
accepts an already discovered owner/canonical-project scope from an existing
recovery driver. Neither discovers owners/projects, polls, constructs a human
principal, or runs on a view/GET path. Explicit continuation policies may derive
Coordinator grants through continuation.py after worker recovery. The repository
and the ordinary stable command/atomic birth guards remain the only dispatch
authority. Dependencies are injectable metadata, repository,
trusted service and existing conversation STOP watermark ports.

The existing startup driver calls recover_persisted_dispatch: one bounded
cursor pass over this grant namespace, with no poller. New versioned grants use
durable Stop withdrawal; legacy rows cannot be auto-promoted through a TTL fence.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Mapping

from lib.conversations.project_identity import normalize_project_path
from lib.identity import require_user_id
from lib.log import get_logger
from .repository import LIMITS, ProjectDispatchRepository
from .service import ProjectDispatchService

logger = get_logger(__name__)


def _stats():
    return dict(scanned=0, eligible=0, accepted=0, deferred=0, aborted=0,
                skipped=0, failed=0)


# Filters that only select another lane's pass; they say nothing about this one.
_RECOVERY_FILTERS = frozenset({'other_scope', 'other_recipient', 'not_a_dependent'})


def _recovery_skip_reason(assignment, repository, *, owner, project, recipient_id,
                          coordinator_only, affected, clock) -> str:
    """Why recovery leaves this assignment alone, or '' when it is eligible.

    Evaluated in the original order, so the restart-paused read still runs only
    for assignments that pass the cheaper checks.
    """
    request, grant = assignment['request'], assignment['grant']
    if coordinator_only or request.get('profile', {}).get('mode') == 'coordinator':
        return 'coordinator_profile'
    if grant.get('lifecycleVersion') != 1:
        return 'grant_without_durable_stop_lifecycle'
    if request['projectPath'] != project or grant['owner'] != owner:
        return 'other_scope'
    if recipient_id is not None and request['recipientId'] != recipient_id:
        return 'other_recipient'
    if affected is not None and assignment['id'] not in affected:
        return 'not_a_dependent'
    if assignment['execution'] is not None and not repository.restart_paused(assignment):
        return 'execution_not_restart_paused'
    if grant['revoked']:
        return 'grant_revoked'
    deadline = request['budget'].get('deadline')
    if deadline is not None and deadline <= clock():
        return 'deadline_passed'
    return ''


def _log_recovery_skip(assignment, owner, reason: str) -> None:
    """Leave a trace for every decision not to resume a lane that has run.

    One counter used to hide all of these; a restart left four of five
    interrupted lanes without an attempt and no line saying why. A
    restart-paused lane whose authority ended is a warning; filters and
    settled executions stay at debug so ordinary passes add no noise.
    """
    ran = assignment['execution'] is not None
    if reason in _RECOVERY_FILTERS or reason == 'execution_not_restart_paused' or not ran:
        level = logging.DEBUG
    elif reason in {'grant_revoked', 'deadline_passed'}:
        level = logging.WARNING
    else:
        level = logging.INFO
    logger.log(level, '[ProjectDispatch] recovery left assignment=%s owner=%s unresumed: %s',
               assignment.get('id'), owner, reason)


def recover_project_dispatch(owner: int, project: str, *, recipient_id=None, coordinator_only=False,
                             repository=None, service=None, prerequisite_id=None,
                             was_aborted_after=None, clock=None) -> dict:
    """One bounded retry pass over an explicitly known owner/project record.

    Idle callbacks restrict to their recipient lane; a caller in an existing
    boot driver may omit recipient_id for its already discovered project.
    Execution associations are never rebound, even if the executor failed.
    Duplicate callbacks/uncertain ACKs reuse admission and the stable command.
    """
    owner = require_user_id(owner, context='project dispatch recovery')
    if not isinstance(project, str) or not project.strip():
        raise ValueError('Project dispatch recovery requires a canonical project')
    project = normalize_project_path(project.strip())
    repository = (repository if repository is not None
                  else ProjectDispatchRepository(owner, project))
    if repository.owner != owner or repository.project != project:
        raise PermissionError('Project dispatch recovery scope differs from repository')
    service = service if service is not None else ProjectDispatchService(repository)
    if was_aborted_after is None:
        from lib.conversation_sync.pending_abort import was_pending_abort_after
        was_aborted_after = was_pending_abort_after
    clock = clock if clock is not None else time.time
    rows = repository.list()
    if repository.storage_version == 1 and len(rows) > LIMITS['assignmentsPerProject']:
        raise ValueError('Project dispatch recovery record exceeds its bounded capacity')
    # Persisted edges point backward in insertion order. One pass includes
    # transitive dependents whose own completed evidence predates this wake.
    affected = {prerequisite_id} if prerequisite_id is not None else None
    if affected is not None:
        for row in rows:
            if any(ref['assignmentId'] in affected
                   for ref in row['request'].get('dependencies', [])):
                affected.add(row['id'])
        affected.discard(prerequisite_id)
    stats = _stats()
    for assignment in rows:
        stats['scanned'] += 1
        if (assignment['execution'] is not None
                and (recipient_id is None or assignment['request']['recipientId'] == recipient_id)
                and any(item['phase'] == 'dispatch_intent' for item in assignment['modelAttempts'])):
            # Startup and lane settlement close intents whose request ended
            # without a recorded settlement; live leases keep blocking.
            try:
                repository.settle_ended_model_intents(assignment['id'])
            except Exception:
                logger.warning('[ProjectDispatch] ended intent settlement retained owner=%s '
                               'assignment=%s', owner, assignment.get('id'), exc_info=True)
        try:
            request, grant = assignment['request'], assignment['grant']
            reason = _recovery_skip_reason(
                assignment, repository, owner=owner, project=project, recipient_id=recipient_id,
                coordinator_only=coordinator_only, affected=affected, clock=clock)
            if reason:
                stats['skipped'] += 1
                _log_recovery_skip(assignment, owner, reason)
                continue
            # Preserve observed STOP beyond the runtime watermark's TTL by
            # revoking this existing grant, never by granting new authority.
            # The durable watermark also completes a Stop whose own grant
            # revocation lost its bounded CAS.
            if (any(was_aborted_after(conversation_id, owner, grant['createdAt'])
                    for conversation_id in dict.fromkeys(
                        (request['coordinatorId'], request['recipientId'])))
                    or repository.stopped(assignment)):
                repository.revoke(assignment['id'], expected_revision=grant['revision'])
                stats['aborted'] += 1
                logger.info('[ProjectDispatch] recovery withdrew assignment=%s owner=%s: '
                            'a recorded Stop postdates its grant', assignment['id'], owner)
                continue
            binding = repository.admission(assignment['id'])
            stats['eligible'] += 1
            # This service alone revalidates live grant/scope/deadline, restores
            # SystemIdentity, and births with laneidle/reject_if_human_queued.
            outcome = service.dispatch_authorized(binding)
            dispatch = outcome.get('dispatch')
            if dispatch == 'aborted':
                # STOP may race the initial probe. The shared birth seam uses
                # grant.createdAt, so retain its aborted verdict durably too.
                repository.revoke(assignment['id'], expected_revision=grant['revision'])
            if dispatch in ('accepted', 'deferred', 'aborted'):
                stats[dispatch] += 1
            else:
                raise ValueError('Trusted dispatch recovery returned no dispatch verdict')
        except Exception as error:
            # A revoked/rebound/stale or unavailable grant must not starve the
            # remaining assignments. Never mutate/replenish its budget here.
            stats['failed'] += 1
            # The watchdog drops typed refusals, so this line is their only trace;
            # anything else also keeps its traceback.
            logger.warning('[ProjectDispatch] recovery skipped unavailable grant owner=%s '
                           'assignment=%s: %s: %s', owner, assignment.get('id'),
                           type(error).__name__, error,
                           exc_info=not isinstance(error, (PermissionError, ValueError)))
            from lib.router.watchdog.signals import report_recovery_failure
            report_recovery_failure(owner, project, error, assignment=assignment)
    if any(row['request'].get('delegation', {}).get('policy', {}).get('continuation') for row in rows):
        from .continuation import recover_coordinator
        for outcome in recover_coordinator(repository, service=service):
            if outcome in stats:
                stats[outcome] += 1
    return stats


def wake_project_dependents(owner: int, project: str, assignment_id: str, *,
                            repository=None, service=None) -> dict:
    """Best-effort post-ACK notification, outside publication/repository locks.

    Notification is not authority: the existing service rechecks exact live
    prerequisites and atomic human-lane priority. Lost notifications are retried
    by ordinary settlement/startup recovery; no durable wake queue is needed.
    """
    try:
        repository = (repository if repository is not None
                      else ProjectDispatchRepository(owner, project))
        return recover_project_dispatch(owner, project, prerequisite_id=assignment_id,
            repository=repository, service=service, clock=repository.clock)
    except Exception:
        logger.warning('[ProjectDispatch] dependent wake retained for recovery '
                       'owner=%s assignment=%s', owner, assignment_id, exc_info=True)
        stats = _stats()
        stats['failed'] = 1
        return stats


def recover_conversation_dispatch(conversation_id: str, *, user_id: int,
                                  conversations=None, repository_factory=None,
                                  service_factory=None, was_aborted_after=None,
                                  clock=None, coordinator_only=False) -> dict:
    """Resolve current canonical scope from metadata, not an old task cwd.

    A missing conversation/project is inert. No transcript or owner catalog is
    loaded. This entry point is called only after the ordinary human/background
    queue drain has had priority at durable settlement.
    """
    owner = require_user_id(user_id, context='project dispatch idle recovery')
    if conversations is None:
        from lib.conversations.repository import ConversationRepository
        conversations = ConversationRepository()
    row = conversations.get(conversation_id, user_id=owner, include_messages=False)
    metadata = getattr(row, 'metadata', row)
    if not isinstance(metadata, Mapping):
        return _stats()
    settings = metadata.get('settings') or {}
    if not isinstance(settings, Mapping):
        return _stats()
    project = settings.get('canonicalProjectPath') or settings.get('projectPath')
    if not isinstance(project, str) or not project.strip():
        return _stats()
    project = normalize_project_path(project.strip())
    repository_factory = repository_factory or ProjectDispatchRepository
    service_factory = service_factory or ProjectDispatchService
    repository = repository_factory(owner, project)
    return recover_project_dispatch(owner, project, recipient_id=conversation_id,
        coordinator_only=coordinator_only,
        repository=repository, service=service_factory(repository),
        was_aborted_after=was_aborted_after, clock=clock)


__all__ = ['recover_conversation_dispatch', 'recover_project_dispatch',
           'wake_project_dependents']


def revoke_conversation_dispatch(conversation_id, *, user_id, occurred_at=None,
                                 conversations=None, repository_factory=None,
                                 lock_factory=None, stop_client=None):
    """Real Stop withdraws existing grants durably before its runtime fence expires.

    The acknowledged Stop watermark is the withdrawal: admission refuses every
    grant created before it, so a watermark failure propagates as an
    unconfirmed Stop. Revoking the referenced grant records follows it; one
    that loses its bounded CAS returns ``pending_revocation``. Settlement or
    startup recovery revokes an unstarted grant it would otherwise retry from
    the same watermark, and a repeated Stop revokes any referenced grant.

    ``occurred_at`` is the human Stop's own time. A repeated Stop of an already
    ended attempt passes its settlement time (0 when unknown): the watermark
    never moves forward, so grants created after the original Stop keep their
    authority, while the original withdrawal is still retired.
    """
    owner = require_user_id(user_id, context='project dispatch Stop')

    timestamp = time.time() if occurred_at is None else occurred_at
    from .repository import DispatchContention, record_stop
    # The stored watermark keeps the maximum Stop time; revoke against it so a
    # terminal retry dated at settlement still retires the original withdrawal.
    timestamp = record_stop(owner, conversation_id, timestamp, client=stop_client)
    outcome = {'revoked': 0, 'withdrawal': 'complete'}
    if conversations is None:
        from lib.conversations.repository import ConversationRepository
        conversations = ConversationRepository()
    snapshot = conversations.get(conversation_id, user_id=owner, include_messages=False)
    metadata = getattr(snapshot, 'metadata', snapshot)
    settings = metadata.get('settings') if isinstance(metadata, Mapping) else None
    if not isinstance(settings, Mapping):
        return outcome
    project = settings.get('canonicalProjectPath') or settings.get('projectPath')
    if not project:
        return outcome
    project = normalize_project_path(project)
    repository = (repository_factory or ProjectDispatchRepository)(owner, project)
    if lock_factory is None:
        from lib.integration_control import _repo_lock
        lock_factory = _repo_lock
    with lock_factory(project):
        try:
            for row in repository.list():
                if (row['grant']['revoked'] or row['grant']['createdAt'] > timestamp
                        or conversation_id not in (row['request']['coordinatorId'], row['request']['recipientId'])):
                    continue
                repository.revoke(row['id'], expected_revision=row['grant']['revision'])
                outcome['revoked'] += 1
        except DispatchContention:
            # Concurrent writers share the project CAS; later grants would
            # contend the same way, so they stay pending with this one.
            outcome['withdrawal'] = 'pending_revocation'
            logger.warning('[ProjectDispatch] Stop recorded; grant revocation retained for '
                           'recovery owner=%s conversation=%s revoked=%d', owner,
                           conversation_id, outcome['revoked'], exc_info=True)
    return outcome


def worker_conversation_admission(conversation_id, *, user_id, conversations=None,
                                  repository_factory=None):
    """Admission of the delegation that owns a Coordinator-created Worker conversation.

    A completion from a Worker turn that ran without its binding is still
    governed by this grant. Only Worker conversations (``_coordinatorSource``)
    qualify, so ordinary and Coordinator conversations keep their own wake.
    Returns the latest such admission or ``None``. It grants no authority; the
    caller validates the grant.
    """
    owner = require_user_id(user_id, context='project dispatch Worker conversation')
    if conversations is None:
        from lib.conversations.repository import ConversationRepository
        conversations = ConversationRepository()
    snapshot = conversations.get(conversation_id, user_id=owner, include_messages=False)
    metadata = getattr(snapshot, 'metadata', snapshot)
    settings = metadata.get('settings') if isinstance(metadata, Mapping) else None
    if not isinstance(settings, Mapping) or not settings.get('_coordinatorSource'):
        return None
    project = settings.get('canonicalProjectPath') or settings.get('projectPath')
    if not isinstance(project, str) or not project.strip():
        return None
    repository = (repository_factory or ProjectDispatchRepository)(
        owner, normalize_project_path(project.strip()))
    rows = [row for row in repository.list() if row['request']['recipientId'] == conversation_id]
    if not rows:
        return None
    return repository.admission(max(rows, key=lambda row: row['grant']['createdAt'])['id'])


def recover_persisted_dispatch(*, client=None, repository_factory=None, recovery=None):
    """One startup pass through the existing record cursor, not a scheduler.

    Four max-size grant records bound an RPC page to 16 MiB plus framing.
    Only versioned grants produced with durable Stop withdrawal can auto-start;
    legacy rows remain readable and require explicit human dispatch.
    """
    if client is None:
        from lib.storage import get_storage_client
        client = get_storage_client()
    recovery = recovery or recover_project_dispatch
    repository_factory = repository_factory or ProjectDispatchRepository
    from .repository import NAMESPACE
    cursor = ''; scopes = 0
    # One startup pass is one episode of a Router Watchdog recovery signal.
    episode = int(time.time() * 1000)
    while True:
        page = client.query('record.list', {'namespace': NAMESPACE, 'prefix': '',
                            'after_key': cursor, 'limit': 4}, deadline=30.0)
        if not isinstance(page, list) or len(page) > 4:
            raise ValueError('Invalid bounded project dispatch recovery page')
        if not page:
            break
        for item in page:
            key, value = item.get('key'), item.get('value')
            if not isinstance(key, str) or not key or key <= cursor or not isinstance(value, Mapping):
                raise ValueError('Invalid project dispatch recovery cursor/binding')
            cursor = key
            binding = value.get('binding') or {}
            owner = require_user_id(binding.get('owner'), context='persisted project dispatch discovery')
            project = normalize_project_path(binding.get('projectPath'))
            repository = repository_factory(owner, project)
            if repository.key != key:
                raise ValueError('Stored dispatch key differs from owner/project identity')
            from .segmented import FORMAT
            if value.get('format') != FORMAT:
                repository._check_record(value)
            try:
                if value.get('format') == FORMAT:
                    # A failed scope remains retryable without preventing the
                    # existing startup pass from recovering other projects.
                    from .segmented import SegmentedDispatch
                    SegmentedDispatch(repository).ensure_ready()
                recovery(owner, project, repository=repository)
                scopes += 1
            except Exception as error:
                logger.warning('[ProjectDispatch] boot recovery retained scope owner=%s project=%s',
                               owner, project, exc_info=True)
                from lib.router.watchdog.signals import report_recovery_failure
                report_recovery_failure(owner, project, error, episode=episode)
    return scopes
