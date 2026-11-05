"""get_router_projects: the owner's Project Routers at a glance (docs/ROUTER.md).

Responsibility: page the owner's project coordination bindings (the ordinary
``project.coordination.binding.v1`` records, keyed by owner) and project each
into passive facts: availability, active Worker and request counts from the
project's newest dispatch history page, and the latest report pointer. The
router home is not a project and is omitted. Nothing here starts, resumes or
wakes work. Pages follow the project dispatch history page bound, the
get_project_workers convention; cursors are opaque.

Entry point: router_projects(owner, arguments, *, client=None).
Dependencies: project_coordination, project_dispatch (history, continuation
worker summaries), coordinator_reports, router.home.
"""
from __future__ import annotations

from collections.abc import Mapping

from lib.storage.errors import StorageError

from .contract import validate


def _activity(owner: int, project: str, binding: dict) -> dict:
    from lib.project_dispatch.continuation import TERMINAL, worker_summaries
    from lib.project_dispatch.repository import ProjectDispatchRepository
    repository = ProjectDispatchRepository(owner, project)
    page = repository.history_page(None)
    rows = [row for row in page['rows']
            if row['request']['coordinatorId'] == binding['conversationId']
            and row['grant'].get('bindingRevision') == binding['revision']
            and row['request'].get('profile', {}).get('mode') != 'coordinator']
    active = [row for row, summary in zip(rows, worker_summaries(repository, rows))
              if summary['status'] not in TERMINAL and summary['status'] != 'cancelled']
    return {'active_workers': len(active),
            'active_requests': len({row['request']['authorizationSource']['turnId'] for row in active}),
            'workers_coverage': 'complete' if page.get('olderCursor') is None else 'newest_page'}


def _last_report(owner: int, project: str) -> dict | None:
    from lib.conversations.coordinator_reports import CoordinatorReports
    state = CoordinatorReports(owner, project).read()
    latest = state.get('latest')
    if not latest:
        return None
    return {'turn_id': latest['turnId'], 'kind': latest['kind'], 'title': latest['title'],
            'created_at': latest['createdAt'], 'unread': state['unread']}


def router_projects(owner: int, arguments: dict, *, client=None) -> dict:
    """One page of bound projects with passive activity facts."""
    from lib.conversations.project_coordination import NAMESPACE, ProjectCoordinationRepository
    from lib.project_dispatch.history import PAGE_ROWS
    from lib.router.home import is_router_home
    from lib.router.watchdog.binding import is_watchdog_location
    arguments = validate(dict(arguments), 'getRouterProjects')
    if client is None:
        from lib.storage import get_storage_client
        client = get_storage_client
    bindings = ProjectCoordinationRepository(owner, client=client)
    prefix = f'{bindings.user_id}:'
    cursor = arguments.get('after_id')
    rows = client().query('record.list', {'namespace': NAMESPACE, 'prefix': prefix,
                                          'after_key': prefix + cursor if cursor else '',
                                          'limit': PAGE_ROWS + 1})
    if not isinstance(rows, list) or len(rows) > PAGE_ROWS + 1:
        raise StorageError('database_integrity', 'Invalid coordination binding page')
    projects = []
    for row in rows[:PAGE_ROWS]:
        key = row.get('key') if isinstance(row, Mapping) else None
        value = row.get('value') if isinstance(row, Mapping) else None
        if (not isinstance(key, str) or not key.startswith(prefix) or not isinstance(value, Mapping)
                or value.get('user_id') != bindings.user_id or not isinstance(value.get('projectPath'), str)):
            raise StorageError('database_integrity', 'Coordination binding page escaped its owner')
        project = value['projectPath']
        if is_router_home(bindings.user_id, project) or is_watchdog_location(bindings.user_id, project):
            continue
        binding = bindings.read(project)
        entry = {'project_path': project, 'coordinator_conversation_id': binding['conversationId'],
                 'binding_revision': binding['revision'], 'availability': binding['availability']}
        if binding['availability'] == 'available':
            try:
                entry.update(_activity(bindings.user_id, project, binding))
                entry['last_report'] = _last_report(bindings.user_id, project)
            except (StorageError, ValueError, PermissionError) as error:
                # One damaged project must not hide the others; the gap is explicit.
                entry['activity_error'] = str(error)
        projects.append(entry)
    next_cursor = rows[PAGE_ROWS - 1]['key'][len(prefix):] if len(rows) > PAGE_ROWS else None
    return {'projects': projects, 'next_cursor': next_cursor}


__all__ = ['router_projects']
