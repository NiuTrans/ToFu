"""Owner-scoped logical paper documents and reading-state storage port."""
from __future__ import annotations

import uuid

from lib.identity import require_user_id


def _storage(*, write=False):
    from lib.storage import get_storage_client
    return get_storage_client(write=write)


class PaperDocumentRepository:
    def __init__(self, owner_user_id, *, client_factory=None):
        self.owner = require_user_id(owner_user_id, context='paper document owner')
        self._client = client_factory or _storage

    def _command(self, operation, payload):
        return self._client(write=True).command('paper.document.' + operation,
            {**payload, 'user_id': self.owner}, 'paper.document.' + operation + ':' + uuid.uuid4().hex)

    def _query(self, operation, payload):
        return self._client().query('paper.document.' + operation, {**payload, 'user_id': self.owner})

    def register(self, source):
        return self._command('register', {'source': source})

    def resolve(self, sources):
        return self._query('resolve', {'sources': sources})

    def get(self, document_id):
        return self._query('get', {'document_id': document_id})

    def current(self, conversation_id):
        return self._query('current', {'conversation_id': conversation_id})

    def request(self, document_id, conversation_id, request_key):
        return self._command('request', {'document_id': document_id,
                                        'conversation_id': conversation_id, 'request_key': request_key})

    def finish(self, document_id, generation, receipt, proof):
        return self._command('finish', {'document_id': document_id, 'generation': generation,
                                       'receipt': receipt, 'proof': proof})

    def abandon(self, document_id, generation):
        return self._command('abandon', {'document_id': document_id, 'generation': generation})

    def save_reading(self, document_id, state, expected_revision, *, writer_id=None, sequence=1):
        return self._command('reading', {'document_id': document_id, 'state': state,
                                        'expected_revision': expected_revision,
                                        'writer_id': writer_id or uuid.uuid4().hex, 'sequence': sequence})
