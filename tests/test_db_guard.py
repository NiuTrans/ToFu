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
    # Also reset 'tmp': a co-scheduled earlier test may have run the boot
    # successfully and left a real TemporaryDirectory in _STATE — reading it
    # here would judge a leftover, not this call's behavior (CI red 2026-08-05).
    monkeypatch.setitem(sdk_e2e._STATE, 'tmp', None)
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
    monkeypatch.setitem(parity._STATE, 'tmp', None)
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
    monkeypatch.setitem(headless._STATE, 'tmp', None)
    with pytest.raises(pytest.UsageError):
        headless._setup_once()
    assert headless._STATE['tmp'] is None



# ─── Standalone-runner guard (tests/_standalone_guard.py) ─────────────
#
# Custom-harness / unittest test files carry ``if __name__ == '__main__':``
# blocks so they can be run as ``python tests/x.py`` — which SKIPS conftest and
# all its guards. Run that way with the ambient ``TOFU_DB_BACKEND=postgres``
# from ``.env`` they used to seed/mutate the PRODUCTION DB (this is how the
# stray ``requeue-test`` conversation reached the live sidebar). The shared
# ``guard_standalone_db`` helper is the durable fix; these tests pin it.

import ast
import glob
import os
import re
import subprocess
import sys

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_TESTS_DIR)

# A standalone runner is "DB-touching" if it can be executed as `python
# tests/x.py` (has an ``if __name__ == '__main__'`` block) AND its source
# references a DB-WRITE signature. Discovered dynamically (see below) so a NEW
# unguarded runner added months from now is policed automatically — no
# hand-maintained list to forget to append to.
_DB_WRITE_SIGNATURES = (
    'get_thread_db', 'upsert(', 'create_task', '_seed_conv',
    'persist_task_result', 'INSERT INTO', 'dispatch_next_queued',
    # tasks_pkg.manager.append_event persists to task_events via
    # _persist_before_push (manager/_events.py -> database/event_log.py).
    # Tests that stub spawn_task call the REAL append_event on synthetic
    # tasks; outside pytest (no conftest shim) that lands on the shared DB.
    'append_event',
    # Indirect drivers: tests may call the terminal/partial sync seams or the
    # event-log writer DIRECTLY (bypassing persist_task_result/append_event).
    # Today every such file is already caught by another signature or guarded
    # (strict AST re-scan 2026-07-26: 0 unguarded, population +1); these pin
    # the door against a FUTURE file that calls only the seam.
    '_sync_result_to_conversation',
    '_sync_partial_to_conversation',
    'append_persistent_event',
)

# A file is considered SAFE if it does any one of these BEFORE it can write:
#   - routes __main__ through the shared helper (guard_standalone_db)
#   - self-forces sqlite via reset_sqlite_for_tests
#   - directly assigns TOFU_DB_BACKEND = 'sqlite' (a real force, not setdefault)
#   - imports conftest under __main__ (whose import runs the force-sqlite shim)
#   - its __main__ delegates to pytest.main(...) — that re-enters pytest, so
#     conftest's force-sqlite shim + pytest_configure keystone run (safe by
#     construction, exactly like a normal `pytest tests/x.py`).
_SAFE_PATTERNS = (
    re.compile(r'guard_standalone_db'),
    re.compile(r'reset_sqlite_for_tests'),
    re.compile(r"""TOFU_DB_BACKEND['"]\]\s*=\s*['"]sqlite"""),
    re.compile(r'from\s+(?:tests\.)?conftest\s+import'),
    re.compile(r'pytest\.main\s*\('),
)

# Legitimate exemptions — files that match the DB-write heuristic but are
# provably safe by other means. Each MUST carry a reason so the exemption is
# auditable rather than a silent escape hatch. NOTE: the AST-based signature
# detection below already excludes matches that occur only inside string
# literals / comments (code fixtures), so this set stays small.
_KNOWN_EXEMPT: dict[str, str] = {
    'test_chat_flow_dispatch.py': (
        'pure in-memory unittest — builds task dicts by hand and STUBS '
        'mgr.persist_task_result to a no-op; never opens a DB connection '
        '(the persist_task_result match is the stub target, not a real write).'),
    'test_orchestration_endpoint_runner.py': (
        'pure in-memory unittest — same pattern: stubs persist_task_result to '
        'a no-op and monkeypatches the engine runner; no real DB access.'),
    'test_task_runtime.py': (
        'exercises the BARE TaskRuntime (lib/task_runtime) whose append_event '
        'is in-memory only; the task_events persist hook (_persist_before_push) '
        'is wired by tasks_pkg.manager, which this file never imports.'),
    'test_lib_orchestrator_wire_parity.py': (
        'append_event appears only as a monkeypatch TARGET (vus.append_event = '
        'lambda ...) to observe call counts — the real persister never runs.'),
    'test_paper_migration.py': (
        "routes/paper's _append_report_event feeds an in-memory report runtime; "
        'no append_persistent_event / before_push hook exists in routes/paper.'),
    'test_paper_media_ux.py': (
        '__main__ delegates to `python -m pytest <self>` — the subprocess '
        "re-enters pytest, so conftest.py's force-sqlite shim protects it."),
    'test_frontend_convview_apply_guards.py': (
        'frontend source-scan (static JS / jsdom); matches upsert( only via '
        'scanned JS symbol names — no Python DB call exists in the file.'),
}


def _has_main_block(src: str) -> bool:
    return bool(re.search(r"if\s+__name__\s*==\s*['\"]__main__['\"]", src))


def _real_code_identifiers(src: str) -> str:
    """Return only the EXECUTABLE identifier surface of *src* — attribute/name
    tokens from the AST — so DB-write signatures that appear ONLY inside string
    literals or comments (test code fixtures, docstrings, NOTE blocks) do NOT
    count as real DB access. Falls back to the raw source if it can't parse."""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return src  # be conservative: treat as touching, don't hide a real one
    toks = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            toks.append(node.id)
        elif isinstance(node, ast.Attribute):
            toks.append(node.attr)
    return '\n'.join(toks)


def _touches_db(src: str) -> bool:
    """True iff a DB-WRITE signature appears in the file's EXECUTABLE code (not
    merely in a string/comment). ``get_thread_db`` / ``create_task`` etc. are
    identifiers; ``upsert(`` / ``INSERT INTO`` are checked against raw source
    only when a bare identifier form is also present, to keep it simple."""
    ident_surface = _real_code_identifiers(src)
    for sig in _DB_WRITE_SIGNATURES:
        ident = sig.rstrip('(').split()[0]  # 'upsert(' -> 'upsert'; 'INSERT INTO' -> 'INSERT'
        if sig == 'INSERT INTO':
            # SQL literal — only meaningful if executed via a db.execute call;
            # require the raw string AND a real execute/executemany identifier.
            if 'INSERT INTO' in src and ('execute' in ident_surface):
                return True
            continue
        if ident in ident_surface:
            return True
    return False


def _is_guarded(src: str) -> bool:
    return any(p.search(src) for p in _SAFE_PATTERNS)


def _discover_db_touching_standalone_runners():
    """Return {filename: source} for every ``tests/test_*.py`` that is a
    standalone DB-touching runner (has a __main__ block + a DB-write sig in
    EXECUTABLE code). String-literal fixtures don't count (AST-filtered)."""
    found = {}
    for path in glob.glob(os.path.join(_TESTS_DIR, 'test_*.py')):
        fname = os.path.basename(path)
        try:
            src = open(path, encoding='utf-8').read()
        except OSError:
            continue
        if _has_main_block(src) and _touches_db(src):
            found[fname] = src
    return found


def test_force_sqlite_env_overrides_ambient_postgres(monkeypatch):
    """The helper's core: an ambient TOFU_DB_BACKEND=postgres (the .env value)
    is OVERRIDDEN to sqlite (force, not setdefault) + a throwaway path is set."""
    import importlib
    guard = importlib.import_module('tests._standalone_guard')
    monkeypatch.setenv('TOFU_DB_BACKEND', 'postgres')
    monkeypatch.delenv('TOFU_DB_PATH', raising=False)
    monkeypatch.delenv('TOFU_ALLOW_PG_TESTS', raising=False)
    guard._force_sqlite_env()
    assert os.environ['TOFU_DB_BACKEND'] == 'sqlite', (
        'ambient postgres was NOT forced to sqlite — setdefault trap re-opened')
    assert os.environ.get('TOFU_DB_PATH'), 'no throwaway TOFU_DB_PATH set'


def test_force_sqlite_env_honours_pg_optin(monkeypatch):
    """With the explicit opt-in the helper leaves the backend alone (the guard's
    _assert_test_database then verifies it's a test-marked DB)."""
    import importlib
    guard = importlib.import_module('tests._standalone_guard')
    monkeypatch.setenv('TOFU_DB_BACKEND', 'postgres')
    monkeypatch.setenv('TOFU_ALLOW_PG_TESTS', '1')
    guard._force_sqlite_env()
    assert os.environ['TOFU_DB_BACKEND'] == 'postgres', (
        'opt-in run should NOT be forced to sqlite')


def test_all_standalone_runners_are_guarded():
    """SELF-DISCOVERING RATCHET: enumerate every ``tests/test_*.py`` that is a
    DB-touching standalone runner (has a ``__main__`` block + a DB-write
    signature) and assert each one is guarded — via the shared helper, a
    self-force to sqlite, or a conftest import. No hand-maintained list, so a
    NEW unguarded runner added later fails THIS test automatically."""
    discovered = _discover_db_touching_standalone_runners()
    # Sanity: the scanner must actually find the population it's meant to police
    # (all the files we wired this pass + the self-forcing ones). If this drops
    # sharply, the heuristic or the glob broke — fail loudly rather than pass
    # vacuously.
    assert len(discovered) >= 13, (
        f'scanner found only {len(discovered)} DB-touching standalone runners '
        f'(expected >=13) — the discovery heuristic likely regressed: '
        f'{sorted(discovered)}')

    unguarded = []
    for fname, src in sorted(discovered.items()):
        if fname in _KNOWN_EXEMPT:
            continue
        if not _is_guarded(src):
            unguarded.append(fname)
    assert not unguarded, (
        'These DB-touching standalone runners are UNGUARDED — a bare '
        '`python tests/<file>` with ambient TOFU_DB_BACKEND=postgres could '
        'mutate the PRODUCTION DB. Add `guard_standalone_db(...)` to each '
        f'(or self-force sqlite): {unguarded}')


def test_ratchet_would_catch_an_unguarded_newcomer(tmp_path, monkeypatch):
    """NEGATIVE CONTROL: prove the self-discovering ratchet actually BITES on a
    brand-new unguarded DB-touching runner (not a vacuous pass). We synthesise
    one in a temp dir, point the scanner at it, and assert it is flagged; then
    add the guard call and assert it clears."""
    # Unguarded runner: has a __main__ block + a DB-write signature, no guard.
    bad = tmp_path / 'test_synthetic_unguarded.py'
    bad.write_text(
        "import sys\n"
        "def main():\n"
        "    db = get_thread_db('chat')\n"
        "    upsert(db, 'conversations', {'id': 'x'})\n"
        "if __name__ == '__main__':\n"
        "    main()\n",
        encoding='utf-8')
    src = bad.read_text(encoding='utf-8')
    # The predicate the ratchet uses must classify this as DB-touching+unguarded.
    assert _has_main_block(src) and _touches_db(src), 'scanner heuristic broke'
    assert not _is_guarded(src), 'unguarded synthetic runner wrongly passed'

    # Now add the shared guard → it must clear.
    good_src = src.replace(
        'def main():\n',
        'def main():\n    from tests._standalone_guard import guard_standalone_db\n'
        "    guard_standalone_db('synthetic')\n")
    assert _is_guarded(good_src), 'guarded synthetic runner wrongly flagged'


def _probe_backend_subprocess(*, run_guard: bool) -> str:
    """Run a tiny child process with ambient TOFU_DB_BACKEND=postgres that
    (optionally) calls guard_standalone_db, then prints the RESOLVED backend.
    Returns the resolved lib.database._core._BACKEND string."""
    body = (
        "import sys; sys.path.insert(0, %r)\n"
        "import quart as q, sys as _s; _s.modules['flask'] = q\n"
        % _REPO_ROOT
    )
    if run_guard:
        body += (
            "from tests._standalone_guard import guard_standalone_db\n"
            "guard_standalone_db('probe', init_schema=False)\n"
        )
    body += (
        "import lib.database._core as c\n"
        "print('BACKEND=' + str(getattr(c, '_BACKEND', '?')))\n"
    )
    env = dict(os.environ)
    env['TOFU_DB_BACKEND'] = 'postgres'      # the dangerous ambient value
    env.pop('TOFU_DB_PATH', None)
    env.pop('TOFU_ALLOW_PG_TESTS', None)
    proc = subprocess.run(
        [sys.executable, '-c', body], cwd=_REPO_ROOT, env=env,
        capture_output=True, text=True, timeout=180)
    out = proc.stdout + proc.stderr
    for line in out.splitlines():
        if line.startswith('BACKEND='):
            return line[len('BACKEND='):].strip()
    raise AssertionError(f'probe did not report a backend. output:\n{out}')


@pytest.mark.slow
def test_guard_forces_sqlite_in_subprocess_under_ambient_pg():
    """BEHAVIOURAL: a real child process with ambient postgres that calls the
    guard resolves the backend to sqlite (the production DB is protected)."""
    assert _probe_backend_subprocess(run_guard=True) == 'sqlite'


def _pg_backend_resolvable_here() -> bool:
    """True when THIS host can actually resolve a PG backend at import time.

    The double-neuter probe asserts that an ambient ``TOFU_DB_BACKEND=postgres``
    (no guard) freezes ``_BACKEND='pg'``. That only happens when the DB
    layer's PG bootstrap genuinely succeeds — which requires psycopg2 AND
    local PostgreSQL server binaries (pg_ctl/initdb). On a host without them
    (public CI), the honest resolution is the SQLite fallback and there is no
    'pg' to freeze onto — the assertion's precondition does not exist there.
    """
    try:
        import psycopg2  # noqa: F401
    except Exception:
        return False
    try:
        from lib.database._bootstrap import _pg_binaries_present
        return bool(_pg_binaries_present())
    except Exception:
        return False


@pytest.mark.slow
@pytest.mark.skipif(not _pg_backend_resolvable_here(),
                    reason='no psycopg2 / PostgreSQL server binaries on this '
                           'host — the ambient-postgres resolution path this '
                           'double-neuter asserts is unreachable (the DB layer '
                           'honestly falls back to sqlite here)')
def test_double_neuter_without_guard_resolves_pg():
    """DOUBLE-NEUTER: the SAME child process WITHOUT the guard call freezes
    _BACKEND onto the ambient postgres — proving the guard is load-bearing (it
    is what flips the resolution, not some unrelated default)."""
    assert _probe_backend_subprocess(run_guard=False) == 'pg'
