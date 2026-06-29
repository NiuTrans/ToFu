"""Regression test for the test-DB data-loss guard (2026-06-28 incident).

WHY
---
On 2026-06-28 ~2300 real conversations were deleted from the production
Postgres DB. Root cause chain:
  1. ``pytest tests/test_e2e_smoke.py`` ran in a shell with an ambient
     ``TOFU_DB_BACKEND=postgres`` (it lives in ``.env``), which DEFEATED
     conftest's ``setdefault('TOFU_DB_BACKEND','sqlite')`` — the DB layer
     froze ``_BACKEND='pg'`` at import.
  2. The ``live_server`` fixture booted the REAL app against PRODUCTION PG.
  3. The visual-E2E ``page`` cleanup fixture did a snapshot-diff
     (``ids_after - ids_before``) and called ``deleteConversation`` for the
     diff. With an empty/untrusted baseline that diff was the ENTIRE sidebar.

The fix is defense-in-depth in ``tests/conftest.py``:
  * the conftest now FORCES sqlite (not setdefault) unless
    ``TOFU_ALLOW_PG_TESTS=1``;
  * ``_assert_test_database`` is the keystone guard called by ``flask_app`` /
    ``live_server`` — it HARD-ABORTS the session if the resolved DB is a
    non-test Postgres DB;
  * the ``page`` cleanup refuses to bulk-delete when the baseline is untrusted.

These tests pin the keystone guard's decision logic so a future refactor
can't silently re-open the hole.
"""
from __future__ import annotations

import importlib

import pytest

pytestmark = [pytest.mark.unit]

conftest = importlib.import_module('tests.conftest')


def _set_backend(monkeypatch, *, backend, dbname='tofu', db_path='/tmp/x.db'):
    """Point conftest's guard at a fake-resolved DB layer."""
    import lib.database._core as dbc
    monkeypatch.setattr(dbc, '_BACKEND', backend, raising=False)
    monkeypatch.setattr(dbc, 'PG_DBNAME', dbname, raising=False)
    monkeypatch.setattr(dbc, 'DB_PATH', db_path, raising=False)


def test_sqlite_backend_is_safe(monkeypatch):
    _set_backend(monkeypatch, backend='sqlite')
    monkeypatch.delenv('TOFU_ALLOW_PG_TESTS', raising=False)
    ok, detail = conftest._db_is_test_safe()
    assert ok, detail
    # _assert_test_database must NOT raise.
    conftest._assert_test_database('unit-sqlite')


def test_pg_production_db_is_refused(monkeypatch):
    """The exact incident config: pg backend, production DB name, no opt-in."""
    _set_backend(monkeypatch, backend='pg', dbname='tofu')
    monkeypatch.delenv('TOFU_ALLOW_PG_TESTS', raising=False)
    ok, detail = conftest._db_is_test_safe()
    assert not ok, 'pg+production must be refused'
    assert 'TOFU_ALLOW_PG_TESTS' in detail
    with pytest.raises(pytest.UsageError):
        conftest._assert_test_database('unit-pg-prod')


def test_pg_without_optin_refused_even_for_testname(monkeypatch):
    """A test-marked DB name alone is NOT enough — the explicit opt-in is
    mandatory, so an ambient postgres env can never slip through."""
    _set_backend(monkeypatch, backend='pg', dbname='tofu_test')
    monkeypatch.delenv('TOFU_ALLOW_PG_TESTS', raising=False)
    ok, _ = conftest._db_is_test_safe()
    assert not ok


def test_pg_optin_but_production_dbname_refused(monkeypatch):
    """Opt-in set but the DB is the production ``tofu`` — still refused,
    because the name carries no test marker."""
    _set_backend(monkeypatch, backend='pg', dbname='tofu')
    monkeypatch.setenv('TOFU_ALLOW_PG_TESTS', '1')
    ok, detail = conftest._db_is_test_safe()
    assert not ok, detail
    assert 'NOT test-marked' in detail


def test_pg_optin_with_testname_allowed(monkeypatch):
    """The ONLY way to run against PG: explicit opt-in + a test-marked DB."""
    _set_backend(monkeypatch, backend='pg', dbname='tofu_pytest_ci')
    monkeypatch.setenv('TOFU_ALLOW_PG_TESTS', '1')
    ok, detail = conftest._db_is_test_safe()
    assert ok, detail
    conftest._assert_test_database('unit-pg-test')


def test_sdk_e2e_boot_refuses_production_db(monkeypatch):
    """The standalone ``test_sdk_e2e._boot_real_server`` helper boots its OWN
    Hypercorn (bypassing the ``live_server`` fixture), so it must invoke the
    keystone guard itself. Pin that: against a production PG resolution it must
    raise BEFORE importing server.py / booting Hypercorn."""
    _set_backend(monkeypatch, backend='pg', dbname='tofu')
    monkeypatch.delenv('TOFU_ALLOW_PG_TESTS', raising=False)
    import importlib
    sdk_e2e = importlib.import_module('tests.test_sdk_e2e')
    # Fresh state so the early-return guard (``_STATE['app'] is not None``)
    # doesn't short-circuit before the DB check.
    monkeypatch.setitem(sdk_e2e._STATE, 'app', None)
    with pytest.raises(pytest.UsageError):
        sdk_e2e._boot_real_server()
    # The guard must fire BEFORE any server import / TemporaryDirectory.
    assert sdk_e2e._STATE['tmp'] is None, (
        '_boot_real_server proceeded past the DB guard (created a tmpdir) '
        'against a production DB — the guard is not gating the boot')


def test_sdk_parity_setup_refuses_production_db(monkeypatch):
    """``test_sdk_parity_e2e._setup_once`` imports server.py independently of
    the conftest fixtures, so it must self-guard too."""
    _set_backend(monkeypatch, backend='pg', dbname='tofu')
    monkeypatch.delenv('TOFU_ALLOW_PG_TESTS', raising=False)
    import importlib
    parity = importlib.import_module('tests.test_sdk_parity_e2e')
    monkeypatch.setitem(parity._STATE, 'app', None)
    with pytest.raises(pytest.UsageError):
        parity._setup_once()
    assert parity._STATE['tmp'] is None


def test_headless_api_setup_refuses_production_db(monkeypatch):
    """``test_e2e_headless_api._setup_once`` imports server.py via
    spec_from_file_location and builds the real app OUTSIDE the conftest
    fixtures — it must self-guard. Pin it: production PG → raises before any
    tmpdir/server import."""
    _set_backend(monkeypatch, backend='pg', dbname='tofu')
    monkeypatch.delenv('TOFU_ALLOW_PG_TESTS', raising=False)
    import importlib
    headless = importlib.import_module('tests.test_e2e_headless_api')
    monkeypatch.setitem(headless._STATE, 'app', None)
    with pytest.raises(pytest.UsageError):
        headless._setup_once()
    assert headless._STATE['tmp'] is None
