"""Retry existing project grants at known idle/recovery lifecycle events only.

Entry points: ``recover_conversation_dispatch`` resolves one owner-scoped
metadata snapshot at durable lane settlement; ``recover_project_dispatch``
accepts an already discovered owner/canonical-project scope from an existing
recovery driver. Neither discovers owners/projects, polls, constructs a human
principal, or runs on a view/GET path. Explicit continuation policies may derive
finite Coordinator grants through continuation.py after worker recovery. The repository's
128-item record and the ordinary stable command/atomic birth guards remain the
only dispatch authority. Dependencies are injectable metadata, repository,
trusted service and existing conversation STOP watermark ports.

The existing startup driver calls recover_persisted_dispatch: one bounded
cursor pass over this grant namespace, with no poller. New versioned grants use
durable Stop withdrawal; legacy rows cannot be auto-promoted through a TTL fence.
"""
from __future__ import annotations

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
        try:
            request, grant = assignment['request'], assignment['grant']
            if (coordinator_only or request.get('profile', {}).get('mode') == 'coordinator'
                    or grant.get('lifecycleVersion') != 1 or request['projectPath'] != project or grant['owner'] != owner
                    or recipient_id is not None and request['recipientId'] != recipient_id
                    or affected is not None and assignment['id'] not in affected
                    or assignment['execution'] is not None or grant['revoked']
                    or request['budget']['deadline'] <= clock()):
                stats['skipped'] += 1
                continue
            # Preserve observed STOP beyond the runtime watermark's TTL by
            # revoking this existing grant, never by granting new authority.
            if any(was_aborted_after(conversation_id, owner, grant['createdAt'])
                   for conversation_id in dict.fromkeys(
                       (request['coordinatorId'], request['recipientId']))):
                repository.revoke(assignment['id'], expected_revision=grant['revision'])
                stats['aborted'] += 1
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
        except Exception:
            # A revoked/rebound/stale or unavailable grant must not starve the
            # remaining assignments. Never mutate/replenish its budget here.
            stats['failed'] += 1
            logger.debug('[ProjectDispatch] recovery skipped unavailable grant owner=%s '
                         'assignment=%s', owner, assignment.get('id'), exc_info=True)
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
    """Real Stop withdraws existing grants durably before its runtime fence expires."""
    owner = require_user_id(user_id, context='project dispatch Stop')

    timestamp = time.time() if occurred_at is None else occurred_at
    from .repository import record_stop
    record_stop(owner, conversation_id, timestamp, client=stop_client)
    if conversations is None:
        from lib.conversations.repository import ConversationRepository
        conversations = ConversationRepository()
    snapshot = conversations.get(conversation_id, user_id=owner, include_messages=False)
    metadata = getattr(snapshot, 'metadata', snapshot)
    settings = metadata.get('settings') if isinstance(metadata, Mapping) else None
    if not isinstance(settings, Mapping):
        return 0
    project = settings.get('canonicalProjectPath') or settings.get('projectPath')
    if not project:
        return 0
    project = normalize_project_path(project)
    repository = (repository_factory or ProjectDispatchRepository)(owner, project)
    if lock_factory is None:
        from lib.integration_control import _repo_lock
        lock_factory = _repo_lock
    revoked = 0
    with lock_factory(project):
        for row in repository.list():
            if (row['grant']['revoked'] or row['grant']['createdAt'] > timestamp
                    or conversation_id not in (row['request']['coordinatorId'], row['request']['recipientId'])):
                continue
            repository.revoke(row['id'], expected_revision=row['grant']['revision'])
            revoked += 1
    return revoked


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
            except Exception:
                logger.warning('[ProjectDispatch] boot recovery retained scope owner=%s project=%s',
                               owner, project, exc_info=True)
    return scopes
