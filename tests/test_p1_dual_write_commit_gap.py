#!/usr/bin/env python3
"""Failing-first test for the LATENT P1 defect (board epic ``pt_7e4afe73``).

**Defect (verified 2026-07-23 by reading source, not runtime):**
``lib/chat/persistence.py::persist_conv_messages`` calls
``upsert(..., retry=True)`` which routes through ``db_execute_with_retry(...,
commit=True)`` — so the authoritative JSONB write to ``conversations.messages``
IS committed before the function returns. Immediately afterwards it calls
``dual_write_conv(db, conv_id, messages, now_ms=now_ms)`` which delegates to
``backfill_conv(db, conv_id, messages, commit=False)`` — so the mirrored
``conversation_messages`` rows are INSERTED into a new implicit transaction on
the same pooled connection but NEVER COMMITTED before the function returns.

**Live-bug status: LATENT.** The whole dual-write path is gated by
``TOFU_MESSAGES_ROWS`` (default off, see
``lib/database/messages_rows.py::rows_write_enabled``), so real traffic today
never trips it. But the moment the flag flips ON — Phase 5 read cutover — the
row mirror will silently diverge from the JSONB truth: any code path that lets
the connection return to the pool (or crashes) before another commit lands
will lose the mirror rows. The existing sibling test
``tests/test_messages_rows.py::test_dual_write_through_persist_conv_messages_when_on``
hides the bug: it manually calls ``db.commit()`` right after
``persist_conv_messages`` and then reads on the SAME connection — so both the
commit gap and the "reader sees its own uncommitted writes" quirk mask it.

**Guard status (updated 2026-07-24 — pt_7e4afe73 LANDED):** the failing-first
target became a durable regression guard when the fix landed in
``lib/chat/persistence.py::persist_conv_messages``: after the JSONB write
(already committed via ``upsert(retry=True)``) and the FTS refresh (commits
itself on SQLite), the caller now issues an explicit ``db.commit()`` if
``rows_write_enabled()`` — closing the "mirror rows sit uncommitted on the
pooled connection" gap. The tests now run by DEFAULT (no env gate); any
regression that removes the commit or re-opens the gap flips them red.
``TOFU_P1_FAILING_FIRST`` is preserved as a no-op env for backward
compatibility with legacy CI scripts.

**Why the test does not itself apply the fix:** per the owner directive (JOURNAL
续15), "the commit-point placement inside ``persist_conv_messages`` has
transaction-boundary side effects — a mid-flow ``db.commit()`` would flush any
OTHER pending writes on the pooled connection prematurely." That analysis is
epic-scope work, not test scaffolding.

**Activation:** ``TOFU_P1_FAILING_FIRST=1 python3 tests/test_p1_dual_write_commit_gap.py``
or ``pytest tests/test_p1_dual_write_commit_gap.py``.

**What the test does:** With ``TOFU_MESSAGES_ROWS=1`` set — TWO complementary
angles on the same "not-durable" invariant, because P1 has two real production
triggers, not one:

  1. ``test_dual_write_rows_are_durable_across_rollback`` (crash-half):
     ``persist_conv_messages`` → ``db.rollback()`` on the SAME connection →
     read on the same connection. Models "caller aborts / raises / hits an
     error before its own next commit". The committed JSONB write is
     unaffected; any uncommitted mirror rows vanish. Under the current
     defect: 0 rows → RED.

  2. ``test_dual_write_rows_are_durable_across_pool_return`` (pool-half):
     ``persist_conv_messages`` on the pooled thread-db → open a SEPARATE
     independent connection via ``_new_connection()`` and read
     ``conversation_messages`` from IT. This is the more common production
     path: connection returns to the pool with an uncommitted mirror hanging
     in an implicit transaction, then either (a) another borrower's
     rollback / crash drops it, or (b) PG's ``idle_in_transaction_session_timeout``
     server-side-aborts it. On SQLite (test backend) WAL isolation makes the
     separate-connection SELECT see ONLY committed writes, which is exactly
     the durability property we care about: if any borrower ever needs to see
     those mirror rows, they must be committed by the writer. Under the
     current defect: 0 rows from the second connection → RED.

Two separate assertions rather than one so a partial future fix (e.g. only
fixing the crash-half via a caller-side ``finally``) still surfaces the
pool-half regression cleanly.
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Flask→Quart shim (matches the rest of the suite).
import quart as _quart
sys.modules.setdefault('flask', _quart)

# DATA-LOSS GUARD (same rationale as tests/test_messages_rows.py's header):
# force a test DB for standalone runs so we never write against a real DB.
if __name__ == '__main__':
    from tests._standalone_guard import guard_standalone_db
    guard_standalone_db('test_p1_dual_write_commit_gap.__main__')

# The pt_7e4afe73 fix has LANDED, so the two durability tests are now
# default-runnable regression guards (previously they were skip-by-default
# failing-first targets). ``TOFU_P1_FAILING_FIRST`` is preserved as a no-op
# env for backward compatibility with legacy CI scripts that set it.
_ACTIVATION_ENV = 'TOFU_P1_FAILING_FIRST'


def _p1_active() -> bool:
    # Legacy shim — the tests now run unconditionally. Kept True so any
    # ``if _p1_active(): ...`` early-return inside test bodies is a no-op.
    return True


try:  # pytest available → use its skip API
    import pytest
except ImportError:  # pragma: no cover — standalone run without pytest
    pytest = None  # type: ignore[assignment]


def _skip_unless_p1(fn):
    # Post-fix: no-op decorator. Kept as an indirection so the test bodies
    # stay untouched — if a future defect regresses the fix, wrapping the
    # tests back into a fail-loud skipif is one line.
    return fn


# A tiny sample; the parity of the mirror is proven by
# tests/test_messages_rows.py already — this test only cares about DURABILITY.
SAMPLE_MSGS = [
    {'role': 'user', 'content': 'p1 latent', '_msgId': 'p1-m0', 'timestamp': 1},
    {'role': 'assistant', 'content': 'reply', '_msgId': 'p1-m1', 'timestamp': 2},
]


def _ensure_table():
    """Idempotent conversation_messages create (mirrors test_messages_rows.py)."""
    from lib.database import DOMAIN_CHAT, get_thread_db
    from lib.database import _core
    from lib.database._core_schema import CONVERSATION_MESSAGES, create_if_absent
    backend = getattr(_core, '_BACKEND', 'sqlite')
    if backend == 'pg':
        from lib.database._schema_pg import _table_exists
    else:
        from lib.database._schema_sqlite import _table_exists
    db = get_thread_db(DOMAIN_CHAT)
    create_if_absent(db, CONVERSATION_MESSAGES, table_exists=_table_exists)
    db.execute('CREATE INDEX IF NOT EXISTS idx_conv_msgs_conv '
               'ON conversation_messages(conv_id, seq)')
    db.commit()


def _unit(fn):
    """Attach @pytest.mark.unit when pytest is available (project convention).

    Kept as a helper (not a bare decorator at each test) so the file still
    imports cleanly in the pytest-less standalone run. Also carries
    ci_serial: these tests hold explicit cross-connection transactions, and
    under the CI parallel lane's contention the writes hit 'database is
    locked' (276a5bb unit leg) while passing uncontended.
    """
    if pytest is None:
        return fn
    return pytest.mark.unit(pytest.mark.ci_serial(fn))


@_unit
@_skip_unless_p1
def test_dual_write_rows_are_durable_across_rollback():
    """P1 (pt_7e4afe73): after persist_conv_messages returns, the mirrored
    conversation_messages rows MUST be durably committed — surviving a rollback
    of whatever transaction the caller happens to be in next.

    Currently RED: dual_write_conv calls backfill_conv(commit=False), so the
    mirror rows sit in the current implicit transaction on the same connection.
    A rollback() (or a caller-side abort / process crash before the next commit)
    drops them, and a follow-up read from ANY other connection sees zero rows —
    silent divergence from the JSONB truth.

    Guarded by ``TOFU_P1_FAILING_FIRST=1``.
    """
    if not _p1_active():
        # pytest handles this via pytestmark; the guard here is only for the
        # __main__ path (which explicitly checks _p1_active before running).
        return

    from lib.database import DOMAIN_CHAT, get_thread_db, db_execute_with_retry
    from lib.chat.persistence import persist_conv_messages

    _ensure_table()
    conv_id = 'cv-p1-latent-' + str(int(time.time() * 1000))
    os.environ['TOFU_MESSAGES_ROWS'] = '1'
    db = get_thread_db(DOMAIN_CHAT)
    try:
        # Deep-copy the messages: persist_conv_messages backfills _msgIds in
        # place. Any shared reference would leak that mutation into SAMPLE_MSGS
        # across parametrised runs.
        msgs = [dict(m) for m in SAMPLE_MSGS]
        persist_conv_messages(db, conv_id, msgs, 'p1-latent')

        # The JSONB write inside persist_conv_messages went through
        # db_execute_with_retry(commit=True), so it is durable. The mirror
        # rows, however, ride the CURRENT implicit transaction; a rollback here
        # models "the caller aborts / crashes / hits an error before its own
        # next commit". If dual_write_conv committed its own writes, the rows
        # would survive; if not, they vanish.
        db.rollback()

        # JSONB truth should be intact (invariant proven separately, asserted
        # here so the test's own diagnostics distinguish the two failure modes:
        # JSONB gone would be a totally different bug, not P1).
        conv_row = db.execute(
            'SELECT msg_count FROM conversations WHERE id=? AND user_id=1',
            (conv_id,)).fetchone()
        assert conv_row is not None, (
            'sanity: JSONB conversation row must survive the rollback '
            '(persist_conv_messages committed it via upsert(retry=True))')
        assert int(conv_row['msg_count'] if hasattr(conv_row, 'keys')
                   else conv_row[0]) == len(SAMPLE_MSGS), (
            'sanity: JSONB msg_count must match — this test is about the '
            'MIRROR, not the JSONB truth')

        # The failing-first assertion. Under the current defect this returns 0.
        cnt_row = db.execute(
            'SELECT COUNT(*) AS n FROM conversation_messages WHERE conv_id=?',
            (conv_id,)).fetchone()
        n = int(cnt_row['n'] if hasattr(cnt_row, 'keys') else cnt_row[0])
        assert n == len(SAMPLE_MSGS), (
            f'P1 defect confirmed: persist_conv_messages left {n} mirror '
            f'rows (expected {len(SAMPLE_MSGS)}) — dual_write_conv writes '
            f'through backfill_conv(commit=False), so the mirror rows sit '
            f'in the caller\'s uncommitted transaction and vanish on '
            f'rollback / crash / connection release. Fix must ensure the '
            f'mirror is committed at a safe transaction boundary '
            f'(NOT necessarily inside dual_write_conv itself — see JOURNAL '
            f'续15/续18 for the transaction-boundary caveat).'
        )
    finally:
        # Housekeeping regardless of test outcome.
        os.environ.pop('TOFU_MESSAGES_ROWS', None)
        try:
            db_execute_with_retry(
                db, 'DELETE FROM conversation_messages WHERE conv_id=?',
                (conv_id,))
            db_execute_with_retry(
                db, 'DELETE FROM conversations WHERE id=? AND user_id=1',
                (conv_id,))
            db.commit()
        except Exception:
            db.rollback()


@_unit
@_skip_unless_p1
def test_dual_write_rows_are_durable_across_pool_return():
    """P1 (pt_7e4afe73) — the pool-return / next-borrower half.

    The more common production trigger than caller-crash: after
    ``persist_conv_messages`` returns, its pooled ``db`` connection goes back
    to the shared pool with an UNCOMMITTED implicit transaction holding the
    mirror rows. Two things happen in real deployments:

      * The next borrower's ``rollback()`` or crash drops the mirror
        implicitly — the mirror rows never existed as far as any observer is
        concerned. (Multi-worker Quart is the canonical shape here.)
      * PG's ``idle_in_transaction_session_timeout`` server-side-aborts the
        transaction after N minutes of idle, dropping the mirror the same way.

    The invariant this test asserts (backend-independent): a DIFFERENT
    connection than the writer must be able to see the mirror rows. On the
    SQLite test backend WAL isolation makes uncommitted writes invisible to
    other connections; on PG multi-connection semantics behave the same. So
    querying ``conversation_messages`` from a fresh independent connection
    (via ``_new_connection()``, which bypasses both thread-local caching AND
    the pool) is the correct universal probe: if it sees the rows, they are
    truly durable; if it does not, they are still trapped in the writer's
    uncommitted transaction.

    Under the current defect: this SELECT returns 0 → RED. Guarded by
    ``TOFU_P1_FAILING_FIRST=1``.
    """
    if not _p1_active():
        return  # pytest handles this via _skip_unless_p1; __main__ gate is the guard

    from lib.database import DOMAIN_CHAT, get_thread_db, db_execute_with_retry
    from lib.database._core import _new_connection
    from lib.chat.persistence import persist_conv_messages

    _ensure_table()
    conv_id = 'cv-p1-pool-' + str(int(time.time() * 1000))
    os.environ['TOFU_MESSAGES_ROWS'] = '1'
    writer_db = get_thread_db(DOMAIN_CHAT)
    reader_db = None
    try:
        msgs = [dict(m) for m in SAMPLE_MSGS]
        persist_conv_messages(writer_db, conv_id, msgs, 'p1-pool')
        # DELIBERATELY do NOT commit or rollback on the writer connection.
        # This is the load-bearing distinction from the sibling test: we
        # model the exact production shape where the writer thread's next
        # line of code was "return" (implicit end-of-turn), leaving the
        # mirror rows suspended in an uncommitted transaction that some
        # future borrower or timeout will resolve — not the writer.

        # Independent, fresh connection: bypasses thread-local caching and
        # the pool, so it CANNOT observe uncommitted state from writer_db.
        # This is the durability probe: "does any other observer see the
        # mirror rows?".
        reader_db = _new_connection()

        # JSONB truth: committed by persist_conv_messages via upsert(retry=
        # True) → visible from the independent connection (sanity check;
        # distinguishes this bug from a completely-broken persist path).
        conv_row = reader_db.execute(
            'SELECT msg_count FROM conversations WHERE id=? AND user_id=1',
            (conv_id,)).fetchone()
        assert conv_row is not None, (
            'sanity: JSONB conversation row must be visible from an '
            'independent connection (persist_conv_messages committed it)')
        assert int(conv_row['msg_count'] if hasattr(conv_row, 'keys')
                   else conv_row[0]) == len(SAMPLE_MSGS), (
            'sanity: JSONB msg_count parity via independent connection')

        # The failing-first assertion: mirror rows must be observable from a
        # connection OTHER than the writer's. Under current defect → 0.
        cnt_row = reader_db.execute(
            'SELECT COUNT(*) AS n FROM conversation_messages WHERE conv_id=?',
            (conv_id,)).fetchone()
        n = int(cnt_row['n'] if hasattr(cnt_row, 'keys') else cnt_row[0])
        assert n == len(SAMPLE_MSGS), (
            f'P1 defect confirmed (pool-return half): a fresh independent '
            f'connection sees {n} mirror rows (expected {len(SAMPLE_MSGS)}) '
            f'immediately after persist_conv_messages returned — they are '
            f'trapped in the writer connection\'s uncommitted transaction '
            f'and will be dropped by whichever future event resolves that '
            f'transaction (next borrower rollback / caller crash / PG '
            f'idle_in_transaction_session_timeout). Fix must land the '
            f'mirror in a committed state before the writer connection '
            f'returns to the pool. See JOURNAL 续15/续18 for the '
            f'transaction-boundary caveat on WHERE to place that commit.'
        )
    finally:
        os.environ.pop('TOFU_MESSAGES_ROWS', None)
        # Close the independent reader connection first so it does not hold
        # a lock on the row we're about to clean up.
        if reader_db is not None:
            try:
                reader_db.close()
            except Exception:
                pass
        # Rollback the writer's still-uncommitted implicit transaction so
        # the cleanup writes below see a clean slate.
        try:
            writer_db.rollback()
        except Exception:
            pass
        try:
            db_execute_with_retry(
                writer_db, 'DELETE FROM conversation_messages WHERE conv_id=?',
                (conv_id,))
            db_execute_with_retry(
                writer_db, 'DELETE FROM conversations WHERE id=? AND user_id=1',
                (conv_id,))
            writer_db.commit()
        except Exception:
            writer_db.rollback()


@_unit
def test_activation_gate_metadata():
    """Meta-test: the file itself declares its RED-target status via
    ``TOFU_P1_FAILING_FIRST`` and ties itself to epic ``pt_7e4afe73``. This
    keeps the file self-documenting for whoever picks up the epic.

    Also guards that BOTH complementary durability tests remain present —
    partial deletion of one half would silently narrow the RED target.
    """
    with open(__file__, encoding='utf-8') as f:
        src = f.read()
    # Legacy env preserved (no-op post-fix).
    assert 'TOFU_P1_FAILING_FIRST' in src
    assert 'pt_7e4afe73' in src
    # File must self-document that the fix has LANDED — a future revert
    # trying to move the guard back to "skip-by-default failing-first" is
    # exactly the class of regression this meta-test exists to catch.
    assert 'LANDED' in src, (
        'test file must record that pt_7e4afe73 has LANDED (the two '
        'durability tests are now default-runnable regression guards, '
        'not failing-first RED targets)')
    # Both durability tests must be present — the crash-half and the
    # pool-return half cover different production triggers and are not
    # substitutes for one another.
    assert 'def test_dual_write_rows_are_durable_across_rollback' in src
    assert 'def test_dual_write_rows_are_durable_across_pool_return' in src


if __name__ == '__main__':
    if not _p1_active():
        print(f'SKIP (set {_ACTIVATION_ENV}=1 to run)')
        sys.exit(0)
    for name, fn in sorted(globals().items()):
        if name.startswith('test_') and callable(fn):
            try:
                fn()
                print('ok', name)
            except AssertionError as e:
                # Under the current defect the durability test is expected RED;
                # print + re-raise so the exit code still signals failure to
                # anyone who explicitly activated the run.
                print('FAIL', name)
                print(' ', str(e))
                raise
    print('ALL PASSED')
