"""Passive original-request usage projection for the human interface.

Coordinator requests have no spending ceiling, so there are no defaults to
configure. This module reads the latest request account of the current
Coordinator binding and projects combined Coordinator + Worker usage. It never
spends, resumes or cancels work. HTTP uses read_state.
"""
from lib.project_dispatch.repository import _validate_schema
from .project_identity import normalize_project_path


def read_state(owner, project, *, repository=None, bindings=None):
    project = normalize_project_path(project)
    result = {'projectPath': project, 'request': None}
    if repository is None:
        from lib.project_dispatch.repository import ProjectDispatchRepository
        repository = ProjectDispatchRepository(owner, project)
    if bindings is None:
        from .project_coordination import ProjectCoordinationRepository
        bindings = ProjectCoordinationRepository(owner, client=repository.client)
    value, _ = repository._load(repository._selection(latest_budget=True))
    binding = bindings.read(repository.project)
    candidates = [row for row in value.get('requestBudgets', []) if row['coordinatorId'] == binding.get('conversationId')
                  and row['bindingRevision'] == binding.get('revision')]
    if candidates:
        from lib.project_dispatch.request_budget import request_view
        latest = max(candidates, key=lambda row: row['createdAt'])
        result['request'] = request_view(repository, value, latest['id'])
    _validate_schema(result, 'coordinatorUsageState')
    return result
