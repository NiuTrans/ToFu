"""Evidence for goals mapped to a Coordinator request.

Responsibility: project one Coordinator request's delegated Workers into goal
evidence, and list the owner's recent requests a goal can map to. A request is
identified by the Coordinator's original delegation attempt
(``request.delegation.attemptId``) that all its Workers share.

Cost (DB load measurement 2026-11-08): a linked goal costs one bounded dispatch
source-cohort read (``ProjectDispatchRepository.request_rows``) plus one live
task listing per view; no attempt, delivery or transcript is read. Workers are
mapped from their dispatch rows alone:

  grant revoked -> cancelled; reworked by a later Worker -> cancelled (superseded);
  Coordinator review accept -> completed; review reject (final) -> cancelled
  (rejected); no execution yet -> pending (queued); live task -> in_progress
  (running); review rework -> in_progress (rework); otherwise in_progress
  (awaiting_review).

Entry points: worker_evidence, request_evidence, resolve_request, linkable_requests.
Dependencies: lib.project_dispatch.repository (request_rows, history_page),
lib.conversations.project_coordination (bindings), lib.tasks_pkg.manager
(live tasks), lib.router.instructions.authored_text (request text).
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping

from lib.log import get_logger
from lib.storage.errors import StorageError

logger = get_logger(__name__)

# The storage protocol's largest record.list page (lib/storage_authority_sql _record_list).
_LIST_PAGE = 1000
_TITLE_CHARS = 200


def _title(text: str) -> str:
    for line in (text or '').splitlines():
        line = ' '.join(line.split())
        if line:
            return line[:_TITLE_CHARS]
    return 'Worker'


def _delegated(row: Mapping) -> bool:
    return row['request'].get('profile', {}).get('mode') != 'coordinator'


def worker_evidence(rows: Iterable[Mapping], *, busy: Iterable[str] = ()) -> list[dict]:
    """Workers of one request in admission order with their evidence state and reason."""
    rows = [row for row in rows if _delegated(row)]
    busy = set(busy)
    superseded = {row['request']['reworkOf']['assignmentId'] for row in rows if row['request'].get('reworkOf')}
    workers = []
    for row in rows:
        review = (row.get('review') or {}).get('decision')
        if row['grant'].get('revoked'):
            state, reason = 'cancelled', 'cancelled'
        elif row['id'] in superseded:
            state, reason = 'cancelled', 'superseded'
        elif review == 'accept':
            state, reason = 'completed', 'accepted'
        elif review == 'reject':
            state, reason = 'cancelled', 'rejected'
        elif row.get('execution') is None:
            state, reason = 'pending', 'queued'
        elif row['request']['recipientId'] in busy:
            state, reason = 'in_progress', 'running'
        elif review == 'rework':
            state, reason = 'in_progress', 'rework'
        else:
            state, reason = 'in_progress', 'awaiting_review'
        workers.append({'assignmentId': row['id'], 'conversationId': row['request']['recipientId'],
                        'title': _title(row['request'].get('brief', '')), 'review': review,
                        'state': state, 'reason': reason})
    return workers


def _request_cohort(owner: int, project_path: str, source_attempt_id: str) -> dict:
    from lib.project_dispatch.repository import ProjectDispatchRepository
    return ProjectDispatchRepository(owner, project_path).request_rows(source_attempt_id)


def _dispatch_rows(owner: int, link: Mapping) -> tuple[list[dict], bool]:
    cohort = _request_cohort(owner, link['projectPath'], link['sourceAttemptId'])
    rows = [row for row in cohort['rows'] if row['request'].get('coordinatorId') == link['coordinatorId']]
    return rows, cohort['settled']


def resolve_request(owner: int, project_path: str, source_attempt_id: str) -> dict:
    """Link fields of the owner's request whose Workers share ``source_attempt_id``."""
    rows = [row for row in _request_cohort(owner, project_path, source_attempt_id)['rows'] if _delegated(row)]
    if not rows:
        raise ValueError('No Worker of this owner belongs to that request')
    request = rows[0]['request']
    turn_id = request['authorizationSource']['turnId']
    try:
        label = _title(_request_text(owner, request['coordinatorId'], turn_id))
    except (StorageError, ValueError, LookupError) as error:
        logger.debug('[MyDayGoals] request text unavailable: %s', error)
        label = _title(request.get('brief', ''))
    return {'projectPath': project_path, 'coordinatorId': request['coordinatorId'],
            'sourceAttemptId': source_attempt_id, 'requestTurnId': turn_id, 'label': label}


def _live_conversations(owner: int) -> frozenset[str]:
    from lib.tasks_pkg.manager import list_running_tasks
    return frozenset(entry['convId'] for entry in list_running_tasks(user_id=owner) if entry.get('convId'))


def _unavailable(error: Exception) -> str:
    code = getattr(error, 'code', None) or type(error).__name__
    return f'{code}: {error}'[:300]


def request_evidence(owner: int, link: Mapping, *,
                     read_rows: Callable[[int, Mapping], tuple[list, bool]] | None = None,
                     busy: Callable[[int], Iterable[str]] | None = None) -> dict:
    """``{available, settled, workers, reason}`` of one linked request; never raises for storage gaps."""
    from lib.project_dispatch.repository import ProjectDispatchError
    try:
        rows, settled = (read_rows or _dispatch_rows)(owner, link)
    except (StorageError, ProjectDispatchError, ValueError, PermissionError) as error:
        logger.warning('[MyDayGoals] request evidence unavailable owner=%s source=%s: %s',
                       owner, link.get('sourceAttemptId'), error)
        return {'available': False, 'settled': False, 'workers': [], 'reason': _unavailable(error)}
    try:
        running = set((busy or _live_conversations)(owner))
    except Exception as error:
        # Liveness only distinguishes running from awaiting review; rows stay the evidence.
        logger.debug('[MyDayGoals] live task projection skipped: %s', error)
        running = set()
    return {'available': True, 'settled': bool(settled), 'workers': worker_evidence(rows, busy=running),
            'reason': None}


def _bound_projects(owner: int) -> list[tuple[str, str]]:
    """``(projectPath, coordinatorId)`` of the owner's available project Coordinators."""
    from lib.conversations.project_coordination import NAMESPACE, ProjectCoordinationRepository
    from lib.router.home import is_router_home
    from lib.router.watchdog.binding import is_watchdog_location
    from lib.storage import get_storage_client
    bindings = ProjectCoordinationRepository(owner)
    prefix, after, projects = f'{bindings.user_id}:', '', []
    while True:
        rows = get_storage_client().query('record.list', {
            'namespace': NAMESPACE, 'prefix': prefix, 'after_key': after, 'limit': _LIST_PAGE})
        if not isinstance(rows, list) or len(rows) > _LIST_PAGE:
            raise StorageError('database_integrity', 'Invalid coordination binding page')
        for row in rows:
            key = row.get('key') if isinstance(row, Mapping) else None
            value = row.get('value') if isinstance(row, Mapping) else None
            if (not isinstance(key, str) or not key.startswith(prefix) or key <= after
                    or not isinstance(value, Mapping) or value.get('user_id') != bindings.user_id
                    or not isinstance(value.get('projectPath'), str)):
                raise StorageError('database_integrity', 'Coordination binding page escaped its owner')
            after = key
            project = value['projectPath']
            if is_router_home(bindings.user_id, project) or is_watchdog_location(bindings.user_id, project):
                continue
            binding = bindings.read(project)
            if binding['availability'] == 'available':
                projects.append((project, binding['conversationId']))
        if len(rows) < _LIST_PAGE:
            return projects


def _newest_rows(owner: int, project: str) -> list[dict]:
    from lib.project_dispatch.repository import ProjectDispatchRepository
    return ProjectDispatchRepository(owner, project).history_page(None)['rows']


def _request_text(owner: int, coordinator_id: str, turn_id: str) -> str:
    from lib.router.instructions import authored_text
    from lib.turn_lifecycle import get_turn
    return authored_text(get_turn(coordinator_id, turn_id, user_id=owner))


def linkable_requests(owner: int, *, projects: Callable[[int], list] | None = None,
                      newest_rows: Callable[[int, str], list] | None = None,
                      request_text: Callable[[int, str, str], str] | None = None) -> list[dict]:
    """Requests with Workers on each bound project's newest history page, newest first.

    One history page per project bounds the read; older requests are not
    offered. A project whose evidence cannot be read is skipped, never fatal.
    """
    from lib.project_dispatch.repository import ProjectDispatchError
    requests = []
    for project, coordinator_id in (projects or _bound_projects)(owner):
        try:
            rows = (newest_rows or _newest_rows)(owner, project)
        except (StorageError, ProjectDispatchError, ValueError, PermissionError) as error:
            logger.warning('[MyDayGoals] project history unavailable owner=%s project=%s: %s',
                           owner, project, error)
            continue
        groups: dict[str, dict] = {}
        for row in rows:
            request = row['request']
            source = (request.get('delegation') or {}).get('attemptId')
            if not source or not _delegated(row) or request.get('coordinatorId') != coordinator_id:
                continue
            created = int(row['grant'].get('createdAt') or 0) * 1000
            group = groups.setdefault(source, {
                'projectPath': project, 'coordinatorId': coordinator_id, 'sourceAttemptId': source,
                'requestTurnId': request['authorizationSource']['turnId'], 'createdAt': created,
                'workers': 0, '_brief': request.get('brief', '')})
            group['workers'] += 1
            group['createdAt'] = min(group['createdAt'], created)
        for group in groups.values():
            brief = group.pop('_brief')
            try:
                label = _title((request_text or _request_text)(owner, coordinator_id, group['requestTurnId']))
            except (StorageError, ValueError, LookupError) as error:
                logger.debug('[MyDayGoals] request text unavailable: %s', error)
                label = _title(brief)
            requests.append({**group, 'label': label})
    return sorted(requests, key=lambda item: -item['createdAt'])


__all__ = ['linkable_requests', 'request_evidence', 'resolve_request', 'worker_evidence']
