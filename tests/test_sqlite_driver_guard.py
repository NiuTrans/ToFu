"""Runtime boundary for dynamically loaded raw SQLite callers.

NEUTER anchor: an unguarded plugin connection can bypass the repository's
transaction and migration authority while touching the canonical database.
"""

from __future__ import annotations

import sqlite3
import threading

import pytest


pytestmark = pytest.mark.unit


@pytest.fixture()
def driver_guard(tmp_path):
    from lib.database import sqlite_driver_guard as guard

    guard.uninstall_sqlite_driver_guard_for_tests()
    canonical = tmp_path / 'canonical.db'
    db = sqlite3.connect(canonical)
    db.execute('CREATE TABLE probe (value TEXT)')
    db.commit()
    db.close()
    guard.install_sqlite_driver_guard(canonical)
    try:
        yield guard, canonical
    finally:
        guard.uninstall_sqlite_driver_guard_for_tests()


def test_raw_path_and_uri_to_canonical_are_denied(driver_guard):
    guard, canonical = driver_guard

    with pytest.raises(guard.SQLiteDriverBoundaryError, match='data layer'):
        sqlite3.connect(canonical)
    with pytest.raises(guard.SQLiteDriverBoundaryError, match='data layer'):
        sqlite3.dbapi2.connect(canonical.as_uri() + '?mode=ro', uri=True)


def test_data_layer_capability_opens_canonical_but_unrelated_db_is_free(
        driver_guard, tmp_path):
    guard, canonical = driver_guard

    with guard.allow_sqlite_driver_connection('unit repository open'):
        db = sqlite3.connect(canonical)
    try:
        assert db.execute('SELECT COUNT(*) FROM probe').fetchone()[0] == 0
    finally:
        db.close()

    unrelated = sqlite3.connect(tmp_path / 'plugin-private.db')
    unrelated.close()


def test_registered_auxiliary_authority_is_denied_but_capability_opens_it(
        driver_guard, tmp_path):
    guard, _canonical = driver_guard
    auxiliary = tmp_path / 'knowledge.sqlite3'
    guard.register_sqlite_driver_authority(auxiliary)

    with pytest.raises(guard.SQLiteDriverBoundaryError, match='data layer'):
        sqlite3.connect(auxiliary)
    with guard.allow_sqlite_driver_connection('auxiliary repository open'):
        db = sqlite3.connect(auxiliary)
    db.close()


def test_connection_capability_is_thread_local(driver_guard):
    guard, canonical = driver_guard
    errors = []

    def _other_thread():
        try:
            sqlite3.connect(canonical)
        except BaseException as exc:
            errors.append(exc)

    with guard.allow_sqlite_driver_connection('main thread only'):
        thread = threading.Thread(target=_other_thread)
        thread.start()
        thread.join(timeout=5)
    assert not thread.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], guard.SQLiteDriverBoundaryError)


def test_core_factory_retains_capability_under_installed_guard(driver_guard):
    guard, canonical = driver_guard
    from lib.database import _core as core
    from lib.database._schema_sqlite import _SCHEMA_VERSION

    snapshot = core.reset_sqlite_for_tests(str(canonical))
    try:
        db = core._new_sqlite_connection()
        try:
            assert db.execute(
                "SELECT value FROM schema_meta WHERE key='_schema_version'"
            ).fetchone()[0] == str(_SCHEMA_VERSION)
        finally:
            db.close()
        with pytest.raises(guard.SQLiteDriverBoundaryError):
            sqlite3.connect(canonical)
    finally:
        core.restore_db_state(snapshot)
