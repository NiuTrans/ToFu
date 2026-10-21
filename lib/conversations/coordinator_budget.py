"""Owner-scoped defaults and passive original-request budget presentation.

Settings apply only when a new accepted human request starts. The immutable
dispatch request receipt owns running allowances; this module never spends,
replenishes, resumes or cancels work. HTTP uses read_state/update_settings.
"""
from copy import deepcopy

from lib.project_dispatch.repository import CONTRACT, _validate_schema
from lib.identity import require_user_id
from lib.storage.errors import StorageError
from .project_identity import normalize_project_path

POLICY = CONTRACT['coordinatorBudget']


class CoordinatorBudgetSettings:
    def __init__(self, owner, project, *, client=None, bindings=None):
        self.owner = require_user_id(owner, context='Coordinator budget settings')
        self.project = normalize_project_path(project)
        if client is None:
            from lib.storage import get_storage_client
            client = get_storage_client
        self.client = client
        if bindings is None:
            from .project_coordination import ProjectCoordinationRepository
            bindings = ProjectCoordinationRepository(owner, client=client)
        self.bindings = bindings
        import hashlib
        self.key = str(self.owner) + ':' + hashlib.sha256(self.project.encode()).hexdigest()

    def read(self):
        row = self.client().query('record.get', {'namespace': POLICY['namespace'], 'key': self.key})
        if row is None:
            return {'projectPath': self.project, 'revision': 0, 'settings': deepcopy(POLICY['defaults'])}
        value = row['value']
        _validate_schema(value, 'coordinatorBudgetSettingsRecord')
        if value['owner'] != self.owner or value['projectPath'] != self.project or type(row['version']) is not int or row['version'] < 1:
            raise StorageError('database_integrity', 'Invalid owned Coordinator budget settings')
        return {'projectPath': self.project, 'revision': row['version'], 'settings': deepcopy(value['settings'])}

    def update(self, request):
        _validate_schema(request, 'coordinatorBudgetUpdate')
        if normalize_project_path(request['path']) != self.project:
            raise PermissionError('Budget settings belong to another project')
        binding = self.bindings.read(self.project)
        if (binding['availability'] != 'available' or binding['conversationId'] != request['conversationId']
                or binding['revision'] != request['bindingRevision']):
            raise PermissionError('Coordinator binding changed before budget update')
        current = self.read()
        if current['revision'] != request['expectedRevision']:
            if current['revision'] == request['expectedRevision'] + 1 and current['settings'] == request['settings']:
                return current
            raise ValueError('Coordinator budget settings changed; refresh before saving')
        value = {'projectPath': self.project, 'owner': self.owner, 'settings': deepcopy(request['settings'])}
        import hashlib, json
        identity = hashlib.sha256(json.dumps([self.owner, self.project, request], sort_keys=True).encode()).hexdigest()
        self.client(write=True).command('record.put', {'namespace': POLICY['namespace'], 'key': self.key,
            'value': value, 'expected_version': current['revision']}, 'coordinator-budget:' + identity)
        return self.read()


def read_state(owner, project, *, settings=None, repository=None):
    settings = settings or CoordinatorBudgetSettings(owner, project)
    result = {**settings.read(), 'request': None}
    if repository is None:
        from lib.project_dispatch.repository import ProjectDispatchRepository
        repository = ProjectDispatchRepository(owner, project)
    value, _ = repository._load(repository._selection(latest_budget=True))
    binding = settings.bindings.read(repository.project)
    candidates = [row for row in value.get('requestBudgets', []) if row['coordinatorId'] == binding.get('conversationId')
                  and row['bindingRevision'] == binding.get('revision')]
    if candidates:
        from lib.project_dispatch.request_budget import request_view
        latest = max(candidates, key=lambda row: row['createdAt'])
        result['request'] = request_view(repository, value, latest['id'])
    _validate_schema(result, 'coordinatorBudgetState')
    return result


def update_settings(owner, request):
    settings = CoordinatorBudgetSettings(owner, request['path'])
    settings.update(request)
    return read_state(owner, settings.project, settings=settings)
