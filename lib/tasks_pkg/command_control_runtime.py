"""Compose local command controls with the selected resident execution owner.

The manager's existing private process record locates the remote incarnation.
Application startup supplies the project explicitly; requests carry their own
owner. Reading that record neither creates tasks nor selects a storage backend.
"""
import sys

from lib.tasks_pkg.command_controls import apply_command_control
from lib.tasks_pkg.command_control_transport import CommandControlUnavailable, call

_CLIENT_KEY = 'tofu_command_controls'


class CommandControls:
    def __init__(self, *, service=None):
        self._service = service

    def execute(self, method, task_id, *, user_id, **params):
        result = apply_command_control(method, task_id, user_id=user_id, **params)
        if result is not None or self._service is None:
            return result
        record = self._service.record()
        if (not record or record.get('state') != 'ready'
                or not self._service.host_matches(record)):
            raise CommandControlUnavailable('The execution owner is unavailable')
        endpoint = record.get('commandControl')
        if (not isinstance(endpoint, dict)
                or endpoint.get('serviceId') != record.get('serviceId')):
            raise CommandControlUnavailable('The execution control incarnation differs')
        return call(endpoint, method, task_id, user_id=user_id, params=params)


def configure(app, *, process_role, environ):
    service = None
    if process_role == 'api':
        from serverctl_pkg.execution_service import ExecutionService
        project = environ.get('TOFU_PROJECT_PATH')
        if not project:
            raise RuntimeError('A separate API needs its explicit execution project')
        service = ExecutionService(project, sys.executable)
    app.extensions[_CLIENT_KEY] = CommandControls(service=service)


def execute(app, method, task_id, *, user_id, **params):
    # Apps without a resident split retain the same local owner, including
    # embedding/test compositions that do not run production background boot.
    controls = app.extensions.get(_CLIENT_KEY)
    if controls is None:
        return apply_command_control(method, task_id, user_id=user_id, **params)
    return controls.execute(method, task_id, user_id=user_id, **params)
