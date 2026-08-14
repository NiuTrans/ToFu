"""Contract tests for the synchronous data-layer write boundary."""

from __future__ import annotations

import pytest


pytestmark = pytest.mark.unit


@pytest.fixture()
def fresh_db(tmp_path):
    from lib.database import _core as core
    snapshot = core.reset_sqlite_for_tests(str(tmp_path / 'write-tx.db'))
    try:
        yield core, core._new_sqlite_connection()
    finally:
        core.restore_db_state(snapshot)


def test_write_transaction_commits_success_and_rolls_back_failure(fresh_db):
    core, db = fresh_db
    try:
        db.execute('CREATE TABLE write_tx_probe (id INTEGER PRIMARY KEY)')
        db.commit()

        with core.write_transaction(db, label='successful unit'):
            db.execute('INSERT INTO write_tx_probe VALUES (1)')
        assert db.raw.in_transaction is False
        assert db.execute(
            'SELECT id FROM write_tx_probe ORDER BY id').fetchall()[0][0] == 1

        with pytest.raises(ValueError, match='abort unit'):
            with core.write_transaction(db, label='failing unit'):
                db.execute('INSERT INTO write_tx_probe VALUES (2)')
                raise ValueError('abort unit')
        assert db.raw.in_transaction is False
        assert [row[0] for row in db.execute(
            'SELECT id FROM write_tx_probe ORDER BY id').fetchall()] == [1]
    finally:
        db.close()


def test_nested_write_transaction_rolls_back_only_inner_savepoint(fresh_db):
    core, db = fresh_db
    try:
        db.execute('CREATE TABLE nested_tx_probe (id INTEGER PRIMARY KEY)')
        db.commit()

        with core.write_transaction(db, label='outer'):
            db.execute('INSERT INTO nested_tx_probe VALUES (1)')
            with pytest.raises(RuntimeError, match='inner failure'):
                with core.write_transaction(db, label='inner'):
                    db.execute('INSERT INTO nested_tx_probe VALUES (2)')
                    raise RuntimeError('inner failure')
            db.execute('INSERT INTO nested_tx_probe VALUES (3)')

        assert [row[0] for row in db.execute(
            'SELECT id FROM nested_tx_probe ORDER BY id').fetchall()] == [1, 3]
    finally:
        db.close()


def test_write_transaction_rejects_unreserved_existing_sqlite_snapshot(
        fresh_db):
    core, db = fresh_db
    try:
        db.execute('CREATE TABLE invalid_tx_probe (id INTEGER PRIMARY KEY)')
        db.commit()
        db.raw.execute('BEGIN')
        db.raw.execute('SELECT COUNT(*) FROM invalid_tx_probe').fetchone()

        with pytest.raises(core.SQLiteWriteDisciplineError,
                           match='without reserving the writer'):
            with core.write_transaction(db, label='invalid nested boundary'):
                pass
        db.rollback()
    finally:
        db.close()


def test_transaction_internal_helper_contract_fails_loudly(fresh_db):
    core, db = fresh_db
    try:
        with pytest.raises(core.SQLiteWriteDisciplineError,
                           match='requires write_transaction'):
            core.assert_write_transaction(db, label='probe helper')

        with core.write_transaction(db, label='outer helper owner'):
            assert core.assert_write_transaction(
                db, label='probe helper') is db

        with pytest.raises(core.SQLiteWriteDisciplineError,
                           match='requires write_transaction'):
            core.assert_write_transaction(db, label='probe helper')
    finally:
        db.close()


def test_canonical_write_requires_server_or_explicit_maintenance_role(
        fresh_db, monkeypatch):
    core, db = fresh_db
    from lib.database import sqlite_owner

    try:
        sqlite_owner.release_owner()
        # Treat this isolated fixture as canonical for the authorization check;
        # no production path or data is touched.
        monkeypatch.setattr(core, '_DEFAULT_DB_FILE', db._pool_path)
        monkeypatch.delenv('TOFU_SERVER_PROCESS', raising=False)

        with pytest.raises(sqlite_owner.SQLiteOwnershipError,
                           match='restricted'):
            with core.write_transaction(db, label='ambient canonical write'):
                db.execute(
                    "INSERT INTO schema_meta(key,value) VALUES ('role-probe','x')")

        with sqlite_owner.maintenance_write_authority('unit role probe'):
            with core.write_transaction(db, label='authorized canonical write'):
                db.execute(
                    "INSERT INTO schema_meta(key,value) VALUES ('role-probe','ok')")
        assert db.execute(
            "SELECT value FROM schema_meta WHERE key='role-probe'"
        ).fetchone()[0] == 'ok'
    finally:
        sqlite_owner.release_owner()
        db.close()
