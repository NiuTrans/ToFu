# Incident anchor: born in commit 8aa75140 — refactor(orchestrator): pt_03f4cdf1 slice 27 — extract per-round DB-c...
# (funeral audit pt_c565a36b3e8f42e6, docs/RATCHET_AUDIT.md)
"""Wire-parity guards for pt_03f4cdf1 slice 27 — extract the per-round
DB-connection checkpoint release from _run.py's stream loop into
lib.tasks_pkg.orchestrator._db_conn_release.release_db_conn_checkpoint().

The block runs once per stream round, right before the LLM call:
run_task's pooled worker thread pins a thread-local PG connection
(_conn_semaphore slot) from its first DB op until close_thread_db()
in the terminal finally. Without this mid-loop release, a stuck LLM
call (e.g. total gateway-5xx outage rotating slots) pins a connection
for the WHOLE outage — and that semaphore is shared with the
frontend's data endpoints (/api/v1/conversations, /api/health
SELECT 1), which then can't acquire and hang ("backend alive,
frontend dead"). The connection is provably DB-idle at this point:
all per-round writes committed (db_execute_with_retry commit=True)
and the streaming-tool pool runs NO DB. Releasing here caps
connection-hold at one round; the next DB op transparently
re-acquires via get_thread_db. Best-effort — a release failure must
never break an otherwise-healthy task.

Failing-first: written BEFORE the extraction; the module/signature/
delegation guards turn RED until the leaf exists and _run.py
delegates.
"""

from __future__ import annotations

import importlib
import pathlib


ROOT = pathlib.Path(__file__).resolve().parents[1]
RUN_PY = ROOT / 'lib' / 'tasks_pkg' / 'orchestrator' / '_run.py'
LEAF_PY = ROOT / 'lib' / 'tasks_pkg' / 'orchestrator' / '_db_conn_release.py'


# ---------------------------------------------------------------------------
# 1. leaf module exists and exposes the helper by name
# ---------------------------------------------------------------------------
def test_leaf_module_exists_and_exposes_release_helper():
    """The new leaf ships a single top-level callable named
    ``release_db_conn_checkpoint`` — the seam name run_task will
    delegate to. Deleting the leaf or renaming the callable must break
    a downstream import."""
    mod = importlib.import_module(
        'lib.tasks_pkg.orchestrator._db_conn_release')
    assert hasattr(mod, 'release_db_conn_checkpoint'), (
        'lib.tasks_pkg.orchestrator._db_conn_release must export '
        'release_db_conn_checkpoint')
    assert callable(mod.release_db_conn_checkpoint)


# ---------------------------------------------------------------------------
# 2. helper signature (kw-only round_num + tid)
# ---------------------------------------------------------------------------
def test_release_helper_signature_is_keyword_only():
    """The helper takes only keyword-only ``round_num`` + ``tid`` so
    callers cannot get argument order wrong. Any drift breaks _run.py's
    call site and this test."""
    import inspect
    from lib.tasks_pkg.orchestrator._db_conn_release import (
        release_db_conn_checkpoint)
    sig = inspect.signature(release_db_conn_checkpoint)
    params = sig.parameters
    assert 'round_num' in params, 'round_num must be a parameter'
    assert 'tid' in params, 'tid must be a parameter'
    assert params['round_num'].kind == inspect.Parameter.KEYWORD_ONLY
    assert params['tid'].kind == inspect.Parameter.KEYWORD_ONLY
    # exactly two params — no task/rs needed (store lookup is internal)
    assert len(params) == 2, (
        'release_db_conn_checkpoint takes exactly round_num + tid')


# ---------------------------------------------------------------------------
# 3. _run.py imports and delegates to the extracted helper
# ---------------------------------------------------------------------------
def test_run_py_imports_release_helper():
    """_run.py imports release_db_conn_checkpoint at module scope."""
    src = RUN_PY.read_text()
    assert 'from lib.tasks_pkg.orchestrator._db_conn_release import' in src, (
        '_run.py must import the extracted release helper — expected a '
        '`from lib.tasks_pkg.orchestrator._db_conn_release import ...` '
        'line at module scope')
    assert 'release_db_conn_checkpoint' in src, (
        '_run.py must reference release_db_conn_checkpoint (either in the '
        'import or in the call site)')


def test_run_task_delegates_to_release_helper():
    """The stream loop's per-round release must be a single call to
    ``release_db_conn_checkpoint(round_num=..., tid=...)`` — no inline
    try/except body left behind."""
    src = RUN_PY.read_text()
    assert 'release_db_conn_checkpoint(' in src, (
        '_run.py must call release_db_conn_checkpoint in the stream loop')


# ---------------------------------------------------------------------------
# 4. inline body is gone from _run.py (extraction really happened)
# ---------------------------------------------------------------------------
def test_run_py_no_longer_carries_release_call_inline():
    """The inline release block's deferred store import must be gone
    from _run.py. (The string
    `get_conversation_store().release_connection()` still legitimately
    appears once — inside the slice-5 pointer comment describing what
    the _teardown.finalize_task_lane leaf does — so the guard keys on
    the import line, which only the inline block carried.)"""
    src = RUN_PY.read_text()
    assert 'from lib.agent_core.store import get_conversation_store' not in src, (
        'the inline release block (deferred store import + release call) '
        'must live in _db_conn_release.py, not _run.py')


def test_run_py_no_longer_carries_release_failure_log_inline():
    """The per-round release-failure debug log line must have moved to
    the leaf."""
    src = RUN_PY.read_text()
    assert 'per-round release_connection failed' not in src, (
        'the per-round release-failure log must live in '
        '_db_conn_release.py, not _run.py')


# ---------------------------------------------------------------------------
# 5. leaf carries the pivotal semantics (best-effort + store release)
# ---------------------------------------------------------------------------
def test_leaf_calls_conversation_store_release():
    """The leaf must call get_conversation_store().release_connection()
    — that is the entire point of the checkpoint."""
    src = LEAF_PY.read_text()
    assert 'get_conversation_store' in src, (
        'release leaf must import get_conversation_store from '
        'lib.agent_core.store')
    assert 'release_connection' in src, (
        'release leaf must call .release_connection() on the store')


def test_leaf_is_best_effort_swallowing_errors():
    """A release failure must NEVER break an otherwise-healthy task —
    the leaf wraps the release in try/except Exception and logs at
    debug level only."""
    src = LEAF_PY.read_text()
    assert 'except Exception' in src, (
        'release leaf must swallow release errors (best-effort)')
    assert 'logger.debug' in src, (
        'release leaf must log release failures at debug level, not '
        'warning/error — a stuck release is not actionable')
    assert '_conn_semaphore' in src or 'semaphore' in src, (
        'release leaf must carry the rationale comment explaining the '
        '_conn_semaphore slot pinning hazard (frontend starvation)')


def test_leaf_defers_store_import():
    """The store import stays deferred (function-scope) exactly as in
    the inline original — module-scope would pull lib.agent_core.store
    into every orchestrator import."""
    src = LEAF_PY.read_text()
    assert 'from lib.agent_core.store import get_conversation_store' in src, (
        'release leaf must import get_conversation_store (deferred, '
        'function-scope) as in the inline original')
