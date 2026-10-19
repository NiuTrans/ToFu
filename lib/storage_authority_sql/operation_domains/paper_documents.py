"""Owned paper source bindings, build publication fences and reading-state CAS."""
from lib.storage_authority_sql.operations_pkg import _paper_documents as handlers
from lib.storage_authority_sql.operations_pkg._common import OperationSpec

OPERATIONS = {
    'paper.document.register': OperationSpec('command', False, handlers._paper_document_register),
    'paper.document.resolve': OperationSpec('query', False, handlers._paper_document_resolve),
    'paper.document.get': OperationSpec('query', False, handlers._paper_document_get),
    'paper.document.current': OperationSpec('query', False, handlers._paper_document_current),
    'paper.document.request': OperationSpec('command', False, handlers._paper_document_request),
    'paper.document.finish': OperationSpec('command', False, handlers._paper_document_finish),
    'paper.document.abandon': OperationSpec('command', False, handlers._paper_document_abandon),
    'paper.document.reading': OperationSpec('command', True, handlers._paper_document_reading),
}
