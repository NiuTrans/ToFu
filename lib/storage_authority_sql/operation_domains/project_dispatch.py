"""Owner-scoped dispatch persistence; domain admission stays in its repository."""
from lib.storage_authority_sql.operations import OperationSpec
from lib.storage_authority_sql.operations_pkg import _project_dispatch as handlers

OPERATIONS = {
    'project.dispatch.get': OperationSpec('query', False, handlers.get),
    'project.dispatch.list': OperationSpec('query', False, handlers.list_documents),
    'project.dispatch.projects': OperationSpec('query', False, handlers.projects),
    'project.dispatch.commit': OperationSpec('command', True, handlers.commit),
}
