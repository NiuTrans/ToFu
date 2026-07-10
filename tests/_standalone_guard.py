"""Shared data-loss guard for STANDALONE test runners (``python tests/x.py``).

WHY
---
Many test files carry a ``if __name__ == '__main__': main()`` block so they can
be run directly (custom colour harness, not pytest). Run that way they SKIP
``tests/conftest.py`` entirely — so none of conftest's data-loss guards fire:

  * no ``_install_shim_for_collection`` → the DB layer does NOT get forced onto
    sqlite, so it freezes ``_core._BACKEND`` from the ambient env. Our ``.env``
    sets ``TOFU_DB_BACKEND=postgres`` → a bare ``python tests/x.py`` seeds/
    mutates the PRODUCTION Postgres DB (this is exactly the class of bug that
    left a stray ``requeue-test`` conversation in the live sidebar, and the
    same mechanism as the 2026-06-28 mass-deletion incident).
  * no ``pytest_configure`` keystone gate.

The durable fix is ONE helper every standalone ``main()`` calls as its first
line. It reproduces conftest's "force sqlite, not setdefault" decision, then
re-uses conftest's ``_assert_test_database`` keystone (belt-and-suspenders),
then bootstraps the fresh sqlite schema. Defined ONCE here so there is no
divergent inlined copy to rot.

Usage (first executable line of a standalone ``main()``)::

    from tests._standalone_guard import guard_standalone_db
    guard_standalone_db('test_my_thing.__main__')

Import ordering: this helper forces the sqlite env BEFORE it triggers any DB
import, so it is safe to call at the top of ``main()`` even in files that
lazily import ``lib.database`` inside their test bodies. If a file imports the
DB layer at MODULE TOP (freezing the backend before ``main()`` runs), call this
helper at module top instead — but prefer lazy DB imports.
"""

from __future__ import annotations

import os
import sys


def _force_sqlite_env() -> None:
    """Force the test process onto a throwaway sqlite DB (mirrors conftest's
    ``_install_shim_for_collection``: FORCE, not setdefault).

    Honours ``TOFU_ALLOW_PG_TESTS=1`` — the same explicit opt-in conftest uses
    — so a deliberate dedicated-test-PG run is still possible (and is then
    verified by ``_assert_test_database``)."""
    if os.environ.get('TOFU_ALLOW_PG_TESTS') == '1':
        return
    os.environ['TOFU_DB_BACKEND'] = 'sqlite'
    if not os.environ.get('TOFU_DB_PATH'):
        import tempfile
        os.environ['TOFU_DB_PATH'] = os.path.join(
            tempfile.mkdtemp(prefix='tofu-standalone-'), 'tofu-test.db')


def guard_standalone_db(context: str = 'standalone', *, init_schema: bool = True) -> None:
    """Fail-closed DB guard for a standalone ``python tests/x.py`` runner.

    1. FORCE sqlite + a throwaway ``TOFU_DB_PATH`` (unless ``TOFU_ALLOW_PG_TESTS=1``)
       — done BEFORE any DB import so ``_core._BACKEND`` can't freeze onto the
       ambient production Postgres.
    2. Re-use conftest's keystone ``_assert_test_database`` to verify the DB the
       layer ACTUALLY resolved is a safe test DB (raises ``pytest.UsageError``
       otherwise). Belt-and-suspenders on top of step 1.
    3. Bootstrap the fresh sqlite schema (``init_db``) so the runner's seeds have
       tables to write to — pytest gets this via the session-start server
       import; a standalone runner must trigger it explicitly.

    Args:
        context: A short label for the guard's diagnostics / error message.
        init_schema: Set False for runners that bootstrap the schema themselves
            (e.g. via ``reset_sqlite_for_tests`` in ``setUpClass``).

    Import note: this module is import-safe without pytest, but
    ``_assert_test_database`` lives in ``tests.conftest``; importing conftest
    also runs its (idempotent) force-sqlite shim, reinforcing step 1.
    """
    _force_sqlite_env()

    # Make ``tests`` importable when run as ``python tests/x.py`` from repo root
    # or from inside tests/. The caller's own sys.path.insert usually covers
    # this, but do it defensively so this helper is self-contained.
    _repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _repo_root not in sys.path:
        sys.path.insert(0, _repo_root)

    try:
        from tests.conftest import _assert_test_database
    except Exception:
        # Running with cwd=tests/ so ``tests`` is not a package on the path.
        from conftest import _assert_test_database  # type: ignore
    _assert_test_database(context)

    if init_schema:
        from lib.database import init_db
        init_db()
