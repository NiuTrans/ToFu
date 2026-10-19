"""Isolated real SQLite operations for paper application tests, including workers."""
from __future__ import annotations

import contextlib
import sqlite3
import threading

from lib.storage.errors import StorageError
from lib.storage_authority_sql.adapters.sqlite import SQLiteSession
from lib.storage_authority_sql.operation_registry import build_registry
from lib.storage_authority_sql.schema import initialize_schema


class TransactionClient:
    def __init__(self, path):
        self.connection = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.session = SQLiteSession(self.connection)
        initialize_schema(self.session)
        self.registry = build_registry()
        self.fail_commit = None
        self.guard = threading.RLock()

    def command(self, operation, payload, command_id):
        with self.guard:
            self.connection.execute('BEGIN IMMEDIATE')
            try:
                result = self.registry[operation].handler(self.session, payload)
                if self.fail_commit == operation:
                    self.fail_commit = None
                    raise StorageError('database_unavailable', 'Injected failure before commit')
                self.connection.commit()
                return result
            except BaseException:
                self.connection.rollback()
                raise

    def query(self, operation, payload):
        with self.guard:
            return self.registry[operation].handler(self.session, payload)


@contextlib.contextmanager
def isolated_paper_storage(tmp_path, monkeypatch):
    from lib.paper.builds import artifact_repository, document_repository
    client = TransactionClient(tmp_path / 'paper-authority.sqlite')
    monkeypatch.setattr(artifact_repository, '_storage', lambda **_: client)
    monkeypatch.setattr(document_repository, '_storage', lambda **_: client)
    try:
        yield client
    finally:
        client.connection.close()
