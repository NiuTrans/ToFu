"""The shared paper test authority bootstraps atomically and closes on failure."""
from __future__ import annotations

from contextlib import closing
import sqlite3
import time
from types import SimpleNamespace

import pytest

from tests.support import paper_storage

pytestmark = pytest.mark.unit


@pytest.fixture
def connections(monkeypatch):
    connect = sqlite3.connect
    opened, statements = [], []

    def tracked(*args, **kwargs):
        connection = connect(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        opened.append(connection)
        return connection

    monkeypatch.setattr(paper_storage, 'sqlite3',
                        SimpleNamespace(connect=tracked, Row=sqlite3.Row))
    yield opened, statements, connect
    for connection in opened:
        connection.close()


def test_fixture_bootstrap_commits_one_complete_transaction(
        tmp_path, monkeypatch, connections):
    observed = []

    def initialize(session):
        observed.append(session.connection.in_transaction)
        session.execute('CREATE TABLE fixture_probe (value TEXT)')
        session.execute("INSERT INTO fixture_probe VALUES ('complete')")

    monkeypatch.setattr(paper_storage, 'initialize_schema', initialize)
    monkeypatch.setattr(paper_storage, 'build_registry', lambda: {})
    path = tmp_path / 'authority.sqlite'
    client = paper_storage.TransactionClient(path)
    assert observed == [True]
    assert not client.connection.in_transaction
    assert client.connection.execute('SELECT value FROM fixture_probe').fetchone()[0] == 'complete'
    _, statements, connect = connections
    assert statements.count('BEGIN IMMEDIATE') == 1
    assert statements.count('COMMIT') == 1
    client.connection.close()
    with closing(connect(path)) as reopened:
        assert reopened.execute('SELECT value FROM fixture_probe').fetchone() == ('complete',)


@pytest.mark.parametrize('failure_stage', ['schema', 'registry'])
@pytest.mark.parametrize('error_type', [RuntimeError, KeyboardInterrupt])
def test_failed_fixture_bootstrap_rolls_back_and_closes_its_connection(
        tmp_path, monkeypatch, connections, failure_stage, error_type):
    failure = error_type('injected bootstrap failure')

    def initialize(session):
        session.execute('CREATE TABLE fixture_partial (value TEXT)')
        session.execute("INSERT INTO fixture_partial VALUES ('unpublished')")
        if failure_stage == 'schema':
            raise failure

    def registry():
        raise failure

    monkeypatch.setattr(paper_storage, 'initialize_schema', initialize)
    monkeypatch.setattr(paper_storage, 'build_registry', registry)
    path = tmp_path / 'authority.sqlite'
    with pytest.raises(error_type) as caught:
        paper_storage.TransactionClient(path)
    assert caught.value is failure
    opened, _, connect = connections
    assert len(opened) == 1
    with closing(connect(path)) as reopened:
        assert reopened.execute(
            "SELECT name FROM sqlite_master WHERE name='fixture_partial'"
        ).fetchall() == []
    with pytest.raises(sqlite3.ProgrammingError, match='closed'):
        opened[0].execute('SELECT 1')


def test_real_fixture_schema_records_bootstrap_receipt(
        tmp_path, connections, record_property):
    started = time.perf_counter()
    client = paper_storage.TransactionClient(tmp_path / 'real-authority.sqlite')
    elapsed = time.perf_counter() - started
    _, statements, _ = connections
    assert client.connection.execute(
        "SELECT meta_value FROM storage_meta WHERE meta_key='schema_version'"
    ).fetchone()
    assert not client.connection.in_transaction
    record_property('bootstrap_seconds', elapsed)
    record_property('sql_statements', len(statements))
    record_property('explicit_begin_statements', statements.count('BEGIN IMMEDIATE'))
    record_property('explicit_commit_statements', statements.count('COMMIT'))
