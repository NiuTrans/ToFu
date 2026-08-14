"""Persisted SSE event log — durable Last-Event-ID replay.

Every event that goes through ``manager.append_event`` is mirrored into the
``task_events`` table.  This decouples event replay from in-memory task
state, so SSE reconnection survives:

  * task removal by ``cleanup_old_tasks`` (1h threshold)
  * server restart
  * cross-process readers (when a future deployment fans tasks across
    multiple Flask workers)

Two read paths exist on the SSE handler:

  1. **Hot path** — when the task is still in ``tasks`` dict, replay reads
     directly from ``task['events']`` (no DB hit, lower latency).
  2. **Cold path** — when the task is gone (cleanup or restart), the SSE
     handler falls back to ``read_events`` here.

Pruning and legacy snapshot compaction run on one bounded maintenance daemon.
They never execute on an SSE producer thread; each delete transaction is
bounded by a fixed number of event rows.

Note on persistence semantics (2026-08-07, docs/STORAGE_REDESIGN.md §4):
every event — including each delta — is still persisted as its OWN row at
its real cursor, so exact-cursor cold replay is unchanged. What changed is
the COMMIT cadence: rows go through a write-behind batch lane (one writer
thread, burst commits) instead of one commit per row, because FUSE charges
per IO operation, not per byte (measured 20x). A process crash loses at
most the unflushed tail (≤0.3 s of rebuildable replay events — owner-
approved); terminal frames (done/error/aborted/interrupted) are
durability-ACKED before ``append_persistent_event`` returns, so
durable-before-visible holds for every frame a reconnect can anchor to.
``TOFU_EVENT_BATCH=0`` restores the legacy one-commit-per-row path;
``flush_pending`` drains the lane (bounded wait).
"""

import atexit
import json
import os
import queue
import threading
import time

from lib.database import (
    DOMAIN_CHAT,
    db_execute_with_retry,
    json_dumps_pg,
    pooled_db,
    write_transaction,
)
from lib.log import get_logger
from lib.storage_projection import (
    project_event_usage_for_storage,
    project_usage_container_for_storage,
    sanitize_usage_for_persist,
)

logger = get_logger(__name__)

# 6 hours — generous enough to span any realistic SSE reconnect window
# (page refresh, network blip, proxy timeout) for a finished task.
EVENT_TTL_MS = 6 * 3600 * 1000

# ── Tiered retention (docs/DEBUG_PANEL_REDESIGN.md §10.4) ──
# The 6h window above exists ONLY to serve SSE reconnects, and it used to
# take the Request Inspector's data down with it: a task from two hours ago
# already read "event log expired". Structural events (the request payloads
# + their usage/round markers) are what the inspector renders, so they get a
# 30-day tier. This is only affordable BECAUSE snapshots are now stored as
# deltas (§10) — measured 31.5x smaller across 493 real rounds. Order matters:
# never extend retention before the delta projection is in place.
STRUCTURAL_EVENT_TYPES = (
    'messages_snapshot', 'round_usage', 'round_start', 'round_end',
)
# The event types are source-code constants, not input.  Keep the predicate
# literal in SQL so PostgreSQL can prove it implies the matching partial-index
# predicate even if a driver later switches from custom to generic plans.
_STRUCTURAL_TYPES_SQL = ','.join(
    "'" + event_type.replace("'", "''") + "'"
    for event_type in STRUCTURAL_EVENT_TYPES
)
STRUCTURAL_TTL_MS = 30 * 24 * 3600 * 1000

# How many tasks one compaction pass may rewrite. Kept small: this runs on
# the maintenance thread and remains bounded to avoid IO bursts.
_COMPACT_MAX_TASKS = 2

# Maintenance runs on one dedicated daemon rather than randomly on an SSE
# producer.  The old sampling hook could make an arbitrary token append execute
# a multi-minute DELETE, creating exactly the frontend stalls the event log is
# meant to survive.  Row-key batches are a hard upper bound: limiting task_ids
# was not a bound at all because one task can own hundreds of thousands of rows.
# ``task_results`` stores a second, often very large recovery copy of a turn.
# It used to have no lifecycle at all.  Keep external/deleted-conversation
# results for a month (same as the Request Inspector structural-event tier),
# and keep results whose authoritative conversation still exists for a much
# more conservative 90 days.  Running rows are never eligible.
def _env_seconds(name, default, minimum):
    try:
        return max(minimum, float(os.environ.get(name, default)))
    except (TypeError, ValueError):
        logger.warning('[EventLog] invalid %s=%r; using %ss',
                       name, os.environ.get(name), default)
        return float(default)


def _env_days_ms(name, default, minimum):
    try:
        days = max(float(minimum), float(os.environ.get(name, default)))
    except (TypeError, ValueError):
        logger.warning('[EventLog] invalid %s=%r; using %s days',
                       name, os.environ.get(name), default)
        days = float(default)
    return int(days * 24 * 3600 * 1000)


def _env_int(name, default, minimum, maximum):
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        logger.warning('[EventLog] invalid %s=%r; using %s',
                       name, os.environ.get(name), default)
        value = int(default)
    return max(minimum, min(maximum, value))


_PRUNE_BATCH_ROWS = _env_int('TOFU_EVENT_PRUNE_BATCH_ROWS', 25, 10, 10_000)
# Task-anchored selection is ~25 ms for 2,000 rows, but the DELETE itself still
# took 2-10 seconds on the measured FUSE PG data directory: every row updates
# three indexes and may retire TOAST data. Even 100-row batches reached 3.3s
# under concurrent FUSE pressure, so cap the default transaction at 25 rows.
# Sixteen separately committed batches retain a 400-row/cycle catch-up rate;
# the loop then rests for the maintenance interval, and steady state normally
# needs fewer than one batch. Operators on local SSDs can raise the env knob.
_PRUNE_MAX_BATCHES = _env_int('TOFU_EVENT_PRUNE_BATCHES', 16, 1, 64)
_RESULT_PRUNE_BATCH_ROWS = _env_int(
    'TOFU_TASK_RESULT_PRUNE_BATCH_ROWS', 25, 10, 1_000)
# Results can each own MiB-sized TOAST values, so do not let the event backlog's
# accelerated batch count amplify result deletion I/O in the same cycle.
_RESULT_PRUNE_MAX_BATCHES = _env_int(
    'TOFU_TASK_RESULT_PRUNE_BATCHES', 4, 1, 16)
_MAINTENANCE_INTERVAL_S = _env_seconds(
    'TOFU_EVENT_MAINTENANCE_INTERVAL', 15, 5)
_COMPACTION_INTERVAL_S = _env_seconds(
    'TOFU_EVENT_COMPACTION_INTERVAL', 900, 60)
_ORPHAN_PRUNE_INTERVAL_S = _env_seconds(
    'TOFU_EVENT_ORPHAN_PRUNE_INTERVAL', 6 * 3600, 300)
_ORPHAN_RESULT_TTL_MS = _env_days_ms(
    'TOFU_TASK_RESULT_ORPHAN_TTL_DAYS', 30, 7)
_LINKED_RESULT_TTL_MS = _env_days_ms(
    'TOFU_TASK_RESULT_LINKED_TTL_DAYS', 90, 30)
# New SQLite authorities use auto_vacuum=INCREMENTAL.  Reclaim at most 256
# pages (~1 MiB at the default page size) per compaction interval and only
# after at least 16 MiB is free.  This prevents the historical pattern where
# a deletion-heavy personal database remained multi-GiB forever, without
# introducing the exclusive whole-file rewrite and startup stall of VACUUM.
# On legacy SQLite files whose auto_vacuum mode is NONE this is a read-only
# no-op; an existing database is never silently converted at startup.
_SQLITE_VACUUM_PAGES = _env_int(
    'TOFU_SQLITE_INCREMENTAL_VACUUM_PAGES', 256, 0, 16_384)
_SQLITE_VACUUM_MIN_FREE_PAGES = _env_int(
    'TOFU_SQLITE_INCREMENTAL_VACUUM_MIN_FREE_PAGES', 4096, 1, 1_000_000)
_SQLITE_VACUUM_BUDGET_MS = _env_int(
    'TOFU_SQLITE_INCREMENTAL_VACUUM_BUDGET_MS', 250, 10, 5_000)


# ═══════════════════════════════════════════════════════════════════════════
#  Write-behind batch lane (docs/STORAGE_REDESIGN.md §4)
# ═══════════════════════════════════════════════════════════════════════════
_EVENT_BATCH_ENABLED = os.environ.get(
    'TOFU_EVENT_BATCH', '1').strip().lower() not in ('0', 'false', 'no')
_EVENT_BATCH_WINDOW_S = 0.3
_EVENT_BATCH_MAX_ROWS = 500
_EVENT_QUEUE_MAX = 100_000
_TERMINAL_FLUSH_TIMEOUT_S = 5.0
_TERMINAL_EVENT_TYPES = ('done', 'error', 'aborted', 'interrupted')

_EVENT_Q = queue.Queue(maxsize=_EVENT_QUEUE_MAX)
_WRITER_LOCK = threading.Lock()
_WRITER_THREAD = None
_WRITER_STOP = threading.Event()
_MAINTENANCE_LOCK = threading.Lock()
_MAINTENANCE_THREAD = None
_MAINTENANCE_STOP = threading.Event()
_TICKET_LOCK = threading.Lock()
_TICKET_NEXT = 0
_FLUSH_COND = threading.Condition(_TICKET_LOCK)
_FLUSHED_TICKET = 0
# Per-ticket resolution closes two correctness holes a scalar high-water mark
# cannot represent:
#   * queue-full falls back synchronously OUT OF ORDER (ticket N can commit
#     while N-1 is still queued);
#   * a failed write is not a successful durability acknowledgement.
# Resolved committed entries are compacted as the contiguous prefix advances;
# failures are retained in a tiny set until observed so they can never be
# mistaken for commits after compaction.
_TICKET_STATUS = {}       # ticket -> 'pending' | 'committed' | 'failed'
_TICKET_FAILURES = set()
# Producer-side shadow of every not-yet-committed row (ticket → row). The
# PRODUCER registers before enqueueing and the writer unregisters only after
# commit (or drop), so read_events sees a row at every point of its
# lifecycle with NO pull-window hole: shadow ∪ committed is complete.
_PENDING_SHADOW = {}
# Approximate gauges (no lock — good enough for the acceptance metric):
# rows in, rows out, commits, losses.
_STATS = {'enqueued': 0, 'flushed_rows': 0, 'commits': 0,
          'dropped': 0, 'sync_writes': 0, 'flush_errors': 0,
          'queue_full': 0, 'maintenance_cycles': 0,
          'maintenance_errors': 0, 'sqlite_vacuum_pages': 0}


class EventDurabilityError(RuntimeError):
    """A terminal event could not be proven durable before client visibility."""


def get_batch_stats():
    """Approximate batch-lane gauges (acceptance metric, STORAGE_REDESIGN §8)."""
    return dict(_STATS)


def _next_ticket():
    global _TICKET_NEXT
    with _FLUSH_COND:
        _TICKET_NEXT += 1
        _TICKET_STATUS[_TICKET_NEXT] = 'pending'
        return _TICKET_NEXT


def _current_ticket():
    with _TICKET_LOCK:
        return _TICKET_NEXT


def _advance_resolved_prefix_locked():
    """Compact the contiguous resolved prefix; caller holds _FLUSH_COND."""
    global _FLUSHED_TICKET
    while True:
        tk = _FLUSHED_TICKET + 1
        status = _TICKET_STATUS.get(tk)
        if status not in ('committed', 'failed'):
            break
        _TICKET_STATUS.pop(tk, None)
        if status == 'failed':
            _TICKET_FAILURES.add(tk)
        _FLUSHED_TICKET = tk


def _resolve_tickets(tickets, status):
    if status not in ('committed', 'failed'):
        raise ValueError('invalid ticket resolution: %s' % status)
    with _FLUSH_COND:
        for tk in tickets:
            # A synchronous terminal retry can upgrade a prior failure.
            if status == 'committed':
                _TICKET_FAILURES.discard(tk)
            if tk > _FLUSHED_TICKET:
                _TICKET_STATUS[tk] = status
        _advance_resolved_prefix_locked()
        _FLUSH_COND.notify_all()


def _ticket_result_locked(ticket):
    if ticket in _TICKET_FAILURES:
        return 'failed'
    if ticket <= _FLUSHED_TICKET:
        return 'committed'
    return _TICKET_STATUS.get(ticket, 'pending')


def _wait_ticket(ticket, timeout):
    """Block until `ticket` commits or fails; True means committed only."""
    if ticket <= 0:
        return True
    with _FLUSH_COND:
        ok = _FLUSH_COND.wait_for(
            lambda: _ticket_result_locked(ticket) != 'pending', timeout)
        result = _ticket_result_locked(ticket) if ok else 'pending'
        if result == 'failed':
            # Failure is an edge-triggered result for its direct waiter. Keep
            # it long enough that compaction cannot turn it into a commit, then
            # consume it so an atexit flush does not re-report a handled test /
            # synchronous fallback failure forever.
            _TICKET_FAILURES.discard(ticket)
    if not ok:
        logger.warning('[EventLog] flush ack for ticket=%d not seen within %.1fs '
                       '(flushed=%d) — proceeding with durability unconfirmed',
                       ticket, timeout, _FLUSHED_TICKET)
        return False
    if result != 'committed':
        logger.error('[EventLog] ticket=%d resolved FAILED — not durability-acked',
                     ticket)
        return False
    return True


def _mark_flushed(max_ticket):
    """Backward-compatible batch helper: resolve pending tickets through N."""
    with _FLUSH_COND:
        tickets = [tk for tk, status in _TICKET_STATUS.items()
                   if tk <= max_ticket and status == 'pending']
    _resolve_tickets(tickets, 'committed')


def _wait_through(ticket, timeout):
    """Wait until every ticket allocated through `ticket` has resolved."""
    if ticket <= 0:
        return True
    with _FLUSH_COND:
        ok = _FLUSH_COND.wait_for(lambda: _FLUSHED_TICKET >= ticket, timeout)
        failures = [tk for tk in _TICKET_FAILURES if tk <= ticket]
        for tk in failures:
            _TICKET_FAILURES.discard(tk)
    if not ok:
        logger.warning('[EventLog] flush-through ticket=%d timed out after %.1fs '
                       '(resolved_through=%d)', ticket, timeout, _FLUSHED_TICKET)
        return False
    if failures:
        logger.error('[EventLog] flush-through ticket=%d contains %d failed row(s)',
                     ticket, len(failures))
        return False
    return True


def _upsert_row(db, row):
    """One DO-NOTHING upsert (NO commit) + the duplicate-seq collision canary.

    ON CONFLICT (task_id, event_id) DO NOTHING because the composite PK
    guarantees idempotency on retry — but a real duplicate (caller minted the
    same seq twice for different events) WOULD silently drop data, so a
    rowcount of 0 raises the canary. retry=False is REQUIRED —
    upsert(retry=True) routes through db_execute_with_retry which returns
    None, destroying cur.rowcount; DO-NOTHING rowcount semantics (insert→1,
    duplicate→0) are verified identical on PG and sqlite3.
    """
    from lib.database import assert_write_transaction
    from lib.database._core_schema import TASK_EVENTS, upsert
    assert_write_transaction(db, label='task event append')
    cur = upsert(
        db, TASK_EVENTS, row,
        conflict_cols=['task_id', 'event_id'],
        insert_cols=['task_id', 'event_id', 'ts_ms', 'type', 'payload'],
        update_cols=[],  # DO NOTHING — append-only event log
        commit=False, retry=False,
    )
    if getattr(cur, 'rowcount', 1) == 0:
        # Either an exact retry (harmless — same row already there) or two
        # distinct events colliding on event_id (DATA LOSS).  We can't cheaply
        # distinguish, but a non-zero rate is the canary.
        logger.warning('[EventLog] event_id collision on task=%s event_id=%d type=%s — '
                       'ON CONFLICT DO NOTHING dropped the row.  If this is not a retry, the '
                       'caller minted a duplicate seq and cold replay will be missing this '
                       'event.', row['task_id'][:8], int(row['event_id']), row['type'])


def _write_row_sync(db, row):
    """Legacy synchronous lane: one committed upsert per row (kill switch /
    writer unavailable / queue full). Never raises — a transient DB blip must
    not abort the SSE stream; the WARNING makes the cold-replay hole visible.
    """
    try:
        with write_transaction(db, label='task-event-sync-write'):
            _upsert_row(db, row)
        _STATS['sync_writes'] += 1
        return True
    except Exception as e:
        logger.warning('[EventLog] persist event failed for task=%s type=%s: %s',
                       row['task_id'][:8], row['type'], e)
        return False


def _write_row_one_shot(row):
    try:
        with pooled_db(DOMAIN_CHAT) as db:
            return _write_row_sync(db, row)
    except Exception as e:
        logger.warning('[EventLog] one-shot connection failed task=%s type=%s: %s',
                       row['task_id'][:8], row['type'], e)
        return False


def _flush_batch(db, rows):
    """Write `rows` in ONE commit. Payloads arrive pre-serialized, so a failure
    here is DB-level; the writer's retry re-runs the whole batch, which
    ON CONFLICT DO NOTHING makes idempotent."""
    with write_transaction(db, label='task-event-batch-write'):
        for row in rows:
            _upsert_row(db, row)


def _ensure_writer():
    """Lazily spawn the single writer thread. None → caller uses the sync lane."""
    global _WRITER_THREAD
    t = _WRITER_THREAD
    if t is not None and t.is_alive():
        return t
    with _WRITER_LOCK:
        if _WRITER_THREAD is not None and _WRITER_THREAD.is_alive():
            return _WRITER_THREAD
        try:
            _WRITER_STOP.clear()
            t = threading.Thread(target=_writer_loop, name='event-log-writer',
                                 daemon=True)
            t.start()
        except Exception as e:
            logger.warning('[EventLog] writer thread spawn failed — sync lane: %s', e)
            return None
        _WRITER_THREAD = t
        _ensure_maintenance()
        return t


def _ensure_maintenance():
    """Lazily start the single prune/compaction daemon."""
    global _MAINTENANCE_THREAD
    t = _MAINTENANCE_THREAD
    if t is not None and t.is_alive():
        return t
    with _MAINTENANCE_LOCK:
        if _MAINTENANCE_THREAD is not None and _MAINTENANCE_THREAD.is_alive():
            return _MAINTENANCE_THREAD
        try:
            _MAINTENANCE_STOP.clear()
            t = threading.Thread(
                target=_maintenance_loop, name='event-log-maintenance',
                daemon=True)
            t.start()
        except Exception as e:
            logger.warning('[EventLog] maintenance thread spawn failed: %s', e)
            return None
        _MAINTENANCE_THREAD = t
        return t


def start_storage_maintenance():
    """Start retention/compaction even when no new task event arrives.

    Historically the daemon was only started by ``append_persistent_event``.
    A freshly restarted but otherwise idle personal server therefore never
    drained an existing event backlog — exactly when low-impact maintenance is
    most useful.  This idempotent public entry point lets server startup arm the
    same lazy singleton without exposing thread construction details.
    """
    return _ensure_maintenance()


def stop_storage_maintenance(timeout=3.0):
    """Request maintenance shutdown and wait for its DB work to release.

    The daemon may be between bounded prune transactions when shutdown starts.
    Merely setting its event lets the database pool/SQLite ownership teardown
    race that in-flight transaction.  Join for a bounded interval so normal
    exits release the checked-out connection first without making shutdown
    unbounded when the filesystem or database is wedged.
    """
    _MAINTENANCE_STOP.set()
    t = _MAINTENANCE_THREAD
    if t is None or t is threading.current_thread():
        return True
    try:
        wait_s = max(0.0, float(timeout))
    except (TypeError, ValueError):
        logger.debug('[EventLog] invalid maintenance shutdown timeout %r; using 3s',
                     timeout)
        wait_s = 3.0
    t.join(wait_s)
    if t.is_alive():
        logger.warning('[EventLog] maintenance thread did not stop within %.1fs',
                       wait_s)
        return False
    return True


def _maintenance_loop():
    """Run bounded storage maintenance completely off producer threads."""
    last_compact = time.monotonic()
    # Orphan discovery is an anti-join over the entire event-key population
    # when no orphans exist. It is repair work, not ordinary terminal-row
    # retention, so never run that 10 GiB proof every 15 seconds.
    last_orphan_prune = time.monotonic()
    # A short initial delay lets schema bootstrap and the first user-visible
    # event finish before backlog work begins.
    if _MAINTENANCE_STOP.wait(min(10.0, _MAINTENANCE_INTERVAL_S)):
        return
    while not _MAINTENANCE_STOP.is_set():
        try:
            with pooled_db(DOMAIN_CHAT) as db:
                now = time.monotonic()
                include_orphans = (
                    now - last_orphan_prune >= _ORPHAN_PRUNE_INTERVAL_S)
                _opportunistic_prune(db, include_orphans=include_orphans)
                if include_orphans:
                    last_orphan_prune = now
                _prune_terminal_task_results(db)
                now = time.monotonic()
                if now - last_compact >= _COMPACTION_INTERVAL_S:
                    _opportunistic_compact(db)
                    _sqlite_incremental_vacuum(db)
                    last_compact = now
                _STATS['maintenance_cycles'] += 1
        except Exception as e:
            _STATS['maintenance_errors'] += 1
            logger.warning('[EventLog] maintenance cycle failed: %s', e)
        _MAINTENANCE_STOP.wait(_MAINTENANCE_INTERVAL_S)


def _sqlite_incremental_vacuum(db):
    """Return a tiny bounded free-page slice on eligible SQLite authorities.

    This intentionally shares the existing low-frequency storage-maintenance
    daemon instead of adding another timer/thread.  It cannot affect
    PostgreSQL, and legacy SQLite files stay untouched unless they were
    explicitly created with incremental auto-vacuum support.
    """
    if _SQLITE_VACUUM_PAGES <= 0:
        return 0
    try:
        from lib.database.sqlite_maintenance import incremental_vacuum
        reclaimed = incremental_vacuum(
            db,
            max_pages=_SQLITE_VACUUM_PAGES,
            min_free_pages=_SQLITE_VACUUM_MIN_FREE_PAGES,
            budget_ms=_SQLITE_VACUUM_BUDGET_MS,
            # Injection keeps the existing deterministic wall-budget test and
            # makes the scheduling subsystem own policy, not raw DB access.
            monotonic=time.monotonic,
        )
        _STATS['sqlite_vacuum_pages'] += reclaimed
        return reclaimed
    except Exception as exc:
        # Retention and event durability must not depend on optional space
        # reclamation.  The next low-frequency cycle can try again.
        logger.warning('[EventLog] SQLite incremental vacuum deferred: %s', exc)
        return 0


def _writer_loop():
    """Drain _EVENT_Q in bursts; ONE commit per burst.

    A dequeued batch is NEVER acknowledged or dropped on a transient DB
    failure. It remains shadow-visible and is retried with bounded backoff.
    This is what makes a terminal ticket a real durability acknowledgement.
    """
    while not (_WRITER_STOP.is_set() and _EVENT_Q.empty()):
        try:
            pending = []
            try:
                pending.append(_EVENT_Q.get(timeout=_EVENT_BATCH_WINDOW_S))
            except queue.Empty:
                pass
            if pending:
                # Group-commit window: keep collecting until the window
                # expires or the batch fills, so SPARSE streams batch too
                # (an immediate drain-then-flush degenerates to one commit
                # per row the moment appends arrive slower than the drain).
                first = time.monotonic()
                while len(pending) < _EVENT_BATCH_MAX_ROWS:
                    remaining = _EVENT_BATCH_WINDOW_S - (time.monotonic() - first)
                    if remaining <= 0:
                        break
                    try:
                        pending.append(_EVENT_Q.get(timeout=remaining))
                    except queue.Empty:
                        break
            if not pending:
                continue
            rows = [r for _, r in pending]
            flushed = False
            backoff = 0.1
            while not flushed:
                try:
                    with pooled_db(DOMAIN_CHAT) as db:
                        _flush_batch(db, rows)
                    flushed = True
                except Exception as e:
                    _STATS['flush_errors'] += 1
                    logger.warning('[EventLog] batch flush failed (%d rows): %s — '
                                   'retaining shadow + retrying in %.1fs',
                                   len(rows), e, backoff)
                    time.sleep(backoff)
                    backoff = min(2.0, backoff * 2)
            _STATS['flushed_rows'] += len(rows)
            _STATS['commits'] += 1
            # Unregister only after the commit, so readers never see a row in
            # neither the shadow nor the database.
            with _TICKET_LOCK:
                for tk, _ in pending:
                    _PENDING_SHADOW.pop(tk, None)
            _resolve_tickets([tk for tk, _ in pending], 'committed')
        except Exception as e:
            logger.error('[EventLog] writer loop iteration failed: %s', e, exc_info=True)
            if pending:
                # Recover an unexpected Python-side failure through independent
                # idempotent one-shot writes.  Merely marking the tickets failed
                # left their shadow rows resident forever and could strand an
                # already-committed terminal event without an ACK.
                committed = []
                failed = []
                for tk, row in pending:
                    if _write_row_one_shot(row):
                        committed.append(tk)
                    else:
                        failed.append(tk)
                with _TICKET_LOCK:
                    for tk in committed + failed:
                        _PENDING_SHADOW.pop(tk, None)
                if committed:
                    _resolve_tickets(committed, 'committed')
                if failed:
                    _STATS['dropped'] += len(failed)
                    _resolve_tickets(failed, 'failed')
            time.sleep(0.5)


def stop_event_writer(timeout=3.0):
    """Flush and stop the batch writer within a bounded shutdown window.

    The writer used to be an immortal daemon. It could keep a DB checkout and
    emit logs after pytest/server logging teardown, while production shutdown
    raced database-pool destruction. Stop accepting an idle lifetime here;
    ``_ensure_writer`` remains restartable for tests or embedded app cycles.
    """
    global _WRITER_THREAD
    t = _WRITER_THREAD
    if t is None or t is threading.current_thread():
        return True
    try:
        wait_s = max(0.0, float(timeout))
    except (TypeError, ValueError):
        logger.debug('[EventLog] invalid writer shutdown timeout %r; using 3s',
                     timeout)
        wait_s = 3.0
    deadline = time.monotonic() + wait_s
    if not _wait_through(_current_ticket(), max(0.0, deadline - time.monotonic())):
        logger.warning('[EventLog] writer tail did not flush before shutdown')
        return False
    _WRITER_STOP.set()
    t.join(max(0.0, deadline - time.monotonic()))
    if t.is_alive():
        logger.warning('[EventLog] writer thread did not stop within %.1fs', wait_s)
        return False
    with _WRITER_LOCK:
        if _WRITER_THREAD is t:
            _WRITER_THREAD = None
    return True


def _flush_lane_at_exit():
    """atexit: give the writer a bounded window to land the queued tail."""
    try:
        stop_storage_maintenance(timeout=3.0)
        stop_event_writer(timeout=3.0)
    except Exception as e:
        logger.debug('[EventLog] atexit flush wait failed: %s', e)


atexit.register(_flush_lane_at_exit)


def _row_payload_to_json(payload):
    """Serialize a payload dict for storage; tolerant of non-dict events.

    Uses ``json_dumps_pg`` so NUL bytes (``\\x00`` / ``\\u0000``) are stripped
    before the row hits the ``task_events.payload`` JSONB column — PostgreSQL's
    JSONB parser rejects ``\\u0000`` escapes, which would otherwise make the
    INSERT raise and silently drop the event (e.g. a ``messages_snapshot``
    carrying binary image data) from cold replay.
    """
    try:
        return json_dumps_pg(payload)
    except (TypeError, ValueError) as e:
        logger.debug('[EventLog] payload serialize failed: %s', e)
        return json.dumps({'type': 'error', 'detail': 'unserializable'})


def _usage_without_wire_diagnostics(usage):
    """Return ``usage`` without private ``_wire_*`` keys, copy-on-change."""
    return sanitize_usage_for_persist(usage)


def _project_usage_container_for_storage(container):
    """Project ``usage`` and ``apiRounds[].usage`` on one mapping."""
    return project_usage_container_for_storage(container)


def _project_usage_diagnostics_for_storage(event):
    """Drop wire diagnostics from every known persisted event usage shape.

    ``_wire_*`` fields are sizeable, transient cache-debugging structures.
    They are consumed synchronously by the cache/accounting pipeline before
    events are emitted; neither durable SSE replay nor the Request Inspector
    reads them.  Persisting them made the structural tier unexpectedly larger
    than the token/accounting data it exists to retain (measured: 1.34 GiB in
    49,495 ``round_usage`` rows).  Terminal ``done`` events also carry the
    complete ``apiRounds`` array: just 100 recent rows consumed 214 MiB, so a
    round-usage-only filter merely moved the cyclic bloat to another event.

    Cover the durable shapes used by the app:
      * top-level ``event.usage`` (round_usage + terminal summaries), and
      * ``event.apiRounds[].usage`` (done/error/recovery summaries).
      * the same fields inside terminal ``committedMessage`` and autopilot
        ``parentMessage`` containers.

    Never mutate ``event`` or nested mappings: the caller still sends the
    original object to live SSE consumers. Unknown public fields remain
    forward-compatible and copy cost is zero when there is nothing to strip.
    """
    return project_event_usage_for_storage(event)


def _project_round_usage_for_storage(event):
    """Compatibility seam for tests/extensions that called the old helper."""
    if not isinstance(event, dict) or event.get('type') != 'round_usage':
        return event
    return _project_usage_diagnostics_for_storage(event)


def append_persistent_event(task_id, event_id, event):
    """Persist one event to the task_events table immediately.

    Every event (including deltas) is written as its own row on arrival.
    No in-memory buffering — a process crash never loses persisted state.

    This function MUST be cheap — it runs on every SSE delta.  The normal path
    only serializes and enqueues; database maintenance runs on its own daemon.
    """
    if not task_id:
        return
    # ── Reject None event_id explicitly ──
    # The legacy fallback path in ``manager.append_event`` (when a task is
    # not registered in TaskRuntime) historically used ``seq=None``.  Letting
    # that flow through here would either crash on ``int(None)`` or insert
    # a NULL primary-key column, so we log loudly and skip — cold replay
    # would silently drop these events otherwise.
    if event_id is None:
        logger.warning('[EventLog] Refusing to persist event with event_id=None for task=%s '
                       'type=%s — caller (likely manager.append_event legacy fallback) bypassed '
                       'TaskRuntime sequencing. Cold replay would have a hole here.',
                       task_id[:8], (event or {}).get('type', '?'))
        return
    etype = (event or {}).get('type', '')
    now = time.time()

    # ── Snapshot delta projection (docs/DEBUG_PANEL_REDESIGN.md §10) ──
    # messages_snapshot rows were 92.4% of this table's bytes because every
    # round re-stored the WHOLE messages array plus a byte-identical tools
    # array (measured: 123.2 MB for one 167-round task; 1.9 MB as deltas).
    # We project ONLY the row that gets persisted — the ``event`` object the
    # caller pushes to SSE subscribers is never touched, so live rendering is
    # byte-identical. Rebuild happens server-side on read
    # (snapshot_delta.rebuild_snapshots), so no consumer sees the delta form.
    # Best-effort: a projection failure falls back to storing the full row.
    row_event = _project_usage_diagnostics_for_storage(event)
    if etype == 'messages_snapshot':
        try:
            from lib.tasks_pkg.snapshot_delta import get_projector
            row_event = get_projector().project(task_id, row_event)
        except Exception as e:
            logger.warning('[EventLog] snapshot delta projection failed for '
                           'task=%s (storing full row): %s', task_id[:8], e)
            row_event = event

    # ── Persist: batch lane (default) or legacy synchronous lane ──
    # The row is built AND the payload serialized here on the producer
    # thread, so the writer thread never pays the projection/JSON cost.
    row = {'task_id': task_id, 'event_id': event_id, 'ts_ms': int(now * 1000),
           'type': etype or 'unknown', 'payload': _row_payload_to_json(row_event)}
    if _EVENT_BATCH_ENABLED and _ensure_writer() is not None:
        tk = _next_ticket()
        with _TICKET_LOCK:
            _PENDING_SHADOW[tk] = row
        try:
            _EVENT_Q.put_nowait((tk, row))
            _STATS['enqueued'] += 1
        except queue.Full:
            _STATS['queue_full'] += 1
            logger.warning('[EventLog] batch queue FULL (cap %d) — writing task=%s '
                           'event_id=%s synchronously', _EVENT_QUEUE_MAX,
                           task_id[:8], event_id)
            committed = _write_row_one_shot(row)
            with _TICKET_LOCK:
                _PENDING_SHADOW.pop(tk, None)
            _resolve_tickets([tk], 'committed' if committed else 'failed')
            if not committed:
                _STATS['dropped'] += 1
                if etype in _TERMINAL_EVENT_TYPES:
                    _wait_ticket(tk, 0)
                    raise EventDurabilityError(
                        'terminal event could not be persisted from the '
                        'queue-full synchronous lane')
        else:
            # Durable-before-visible for terminal frames: a reconnect may
            # anchor to done/error/aborted — block until THAT row is flushed.
            if etype in _TERMINAL_EVENT_TYPES:
                if not _wait_ticket(tk, _TERMINAL_FLUSH_TIMEOUT_S):
                    # The writer may be retrying a connection-specific failure.
                    # One independent synchronous checkout often succeeds. The
                    # upsert is idempotent if the writer committed in the race.
                    if _write_row_one_shot(row):
                        with _TICKET_LOCK:
                            _PENDING_SHADOW.pop(tk, None)
                        _resolve_tickets([tk], 'committed')
                    else:
                        raise EventDurabilityError(
                            'terminal event was not durable before timeout; '
                            'withholding terminal push')
    else:
        if not _write_row_one_shot(row) and etype in _TERMINAL_EVENT_TYPES:
            raise EventDurabilityError(
                'terminal event failed on synchronous persistence lane')
    # The maintenance daemon is lazy so installations that never use durable
    # task events pay no thread/connection cost. It never runs on this producer.
    _ensure_maintenance()

    # Read-your-writes for the Request Inspector's read cache: this task's
    # cached event rows are now stale. The cache's TTL bounds staleness in
    # wall-clock terms, but a live task that appends a round and is polled
    # immediately after must see it — so drop the entry at the write.
    try:
        from lib.tasks_pkg.request_inspector import invalidate_task_cache
        invalidate_task_cache(task_id)
    except Exception as e:
        logger.debug('[EventLog] inspector cache invalidation skipped: %s', e)



def flush_pending(task_id):
    """Drain the batch lane up to NOW (bounded wait); no-op when disabled.

    ``manager.append_event`` calls this on the terminal 'done' frame. The
    terminal row itself is already durability-acked inside
    ``append_persistent_event``; this wait additionally lands any earlier
    non-terminal rows still queued, so a finished task's ENTIRE log is
    durable before the stream closes. ``task_id`` is accepted for API
    compatibility — the lane is global, so the wait covers every task.
    """
    if not _EVENT_BATCH_ENABLED:
        return
    try:
        return _wait_through(_current_ticket(), _TERMINAL_FLUSH_TIMEOUT_S)
    except Exception as e:
        logger.debug('[EventLog] flush_pending wait failed (non-fatal): %s', e)
        return False


def pending_event_rows(task_id, since_event_id=None):
    """Lane-shadow rows for `task_id` in FULL row shape, ticket order.

    Each entry: {'event_id', 'type', 'ts_ms', 'payload' (parsed dict)} —
    the shape the Request Inspector's structural read needs. Snapshots the
    producer-side shadow (registered BEFORE enqueue, dropped only after
    commit), so a row is visible at every point of its lifecycle.
    Best-effort — a snapshot failure degrades to [].
    """
    if not (_EVENT_BATCH_ENABLED and _WRITER_THREAD is not None):
        return []
    try:
        with _TICKET_LOCK:
            items = list(_PENDING_SHADOW.items())
    except Exception as e:
        logger.debug('[EventLog] pending snapshot failed: %s', e)
        return []
    rows = []
    for _tk, row in items:
        if row.get('task_id') != task_id:
            continue
        try:
            eid = int(row.get('event_id'))
        except (TypeError, ValueError) as exc:
            logger.debug('[EventLog] pending shadow has invalid event_id %r: %s',
                         row.get('event_id'), exc)
            continue
        if since_event_id is not None and eid <= int(since_event_id):
            continue
        try:
            payload = json.loads(row['payload'])
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            logger.debug('[EventLog] pending shadow payload decode failed '
                         'event_id=%s: %s', eid, exc)
            payload = {'type': row.get('type') or 'unknown'}
        rows.append({'event_id': eid, 'type': row.get('type') or 'unknown',
                     'ts_ms': int(row.get('ts_ms') or 0), 'payload': payload})
    return rows


def _pending_rows_for(task_id, since_event_id=None):
    """read_events-shaped projection of :func:`pending_event_rows`."""
    return [{'event_id': r['event_id'], 'payload': r['payload']}
            for r in pending_event_rows(task_id, since_event_id)]


def read_events(task_id, since_event_id=None, limit=10000):
    """Read persisted events for a task, ordered by event_id.

    Lane-aware (STORAGE_REDESIGN §4): rows still queued/in-flight in the
    batch lane are MERGED over the DB rows (dedup by event_id, re-sorted),
    so callers — SSE cold replay, cold fold — always see every appended
    event, never just the committed prefix.

    Args:
        task_id: task identifier.
        since_event_id: if set, returns only events with event_id > N.
        limit: maximum rows to return (defensive cap).

    Returns:
        list of dicts: [{'event_id': N, 'type': ..., 'payload': {...}}, ...]
    """
    if not task_id:
        return []
    # Snapshot the lane shadow BEFORE the DB read: a row committed (and
    # shadow-popped) WHILE our DB query runs is still in this snapshot —
    # taking the shadow after the DB read would let a reader straddle the
    # writer's commit→pop and see the row in NEITHER store (measured in the
    # durable-before-visible suite: fold lagging by one burst).
    pending = _pending_rows_for(task_id, since_event_id)
    try:
        with pooled_db(DOMAIN_CHAT) as db:
            if since_event_id is not None:
                rows = db.execute(
                    'SELECT event_id, type, payload FROM task_events '
                    'WHERE task_id=? AND event_id>? ORDER BY event_id ASC LIMIT ?',
                    (task_id, int(since_event_id), int(limit))
                ).fetchall()
            else:
                rows = db.execute(
                    'SELECT event_id, type, payload FROM task_events '
                    'WHERE task_id=? ORDER BY event_id ASC LIMIT ?',
                    (task_id, int(limit))
                ).fetchall()
    except Exception as e:
        logger.warning('[EventLog] read failed for task=%s: %s', task_id[:8], e)
        return []
    out = []
    for r in rows:
        try:
            payload_raw = r['payload'] if 'payload' in r.keys() else r[2]
        except Exception as e:
            logger.debug('[EventLog] row.keys() unavailable, falling back to positional access: %s', e)
            payload_raw = r[2]
        if isinstance(payload_raw, dict):
            payload = payload_raw
        else:
            try:
                payload = json.loads(payload_raw or '{}')
            except (TypeError, ValueError, json.JSONDecodeError) as _e_audit:
                # WARNING (was DEBUG): an unparseable payload row means cold
                # replay silently degrades this event to {'type': ...} — a
                # data-integrity degradation, same severity as the persist-side
                # 'persist event failed' warning above.
                logger.warning('[EventLog] read_events: unparseable payload row for task=%s, '
                               'degrading to type-only: %s', task_id[:8], _e_audit)
                payload = {'type': r['type'] if 'type' in r.keys() else r[1]}
        try:
            eid = int(r['event_id'] if 'event_id' in r.keys() else r[0])
        except Exception as e:
            logger.debug('[EventLog] row missing event_id, dropping: %s', e)
            continue
        out.append({'event_id': eid, 'payload': payload})
    if pending:
        seen = {e['event_id'] for e in out}
        out.extend(e for e in pending if e['event_id'] not in seen)
        out.sort(key=lambda e: e['event_id'])
        if len(out) > int(limit):
            out = out[:int(limit)]
    return out


def has_terminal_event(task_id):
    """Return True if a 'done' event has been persisted for this task."""
    if not task_id:
        return False
    try:
        with pooled_db(DOMAIN_CHAT) as db:
            row = db.execute(
                "SELECT 1 FROM task_events WHERE task_id=? AND type='done' LIMIT 1",
                (task_id,)
            ).fetchone()
        return bool(row)
    except Exception as e:
        logger.debug('[EventLog] has_terminal_event failed for task=%s: %s', task_id[:8], e)
        return False



def latest_event_ts(task_id):
    """MAX(ts_ms) across a task's persisted events, or None.

    Liveness oracle for the poll layer (pt_a21cd6eb ③-2): a task whose event
    log is STILL GROWING has a live worker somewhere, even when the in-memory
    registry lost it (the 2026-08-01 evaporation family) — the stale-
    checkpoint "absent = crashed" flip must not fire on fresh events.
    """
    if not task_id:
        return None
    try:
        with pooled_db(DOMAIN_CHAT) as db:
            # event_id is monotonic per task and leads the composite PK, so a
            # backward index probe is O(log n). MAX(ts_ms) scanned every event
            # owned by a long-running task because no (task_id, ts_ms) index
            # exists — this liveness probe runs every few seconds.
            row = db.execute(
                'SELECT ts_ms AS mx FROM task_events WHERE task_id=? '
                'ORDER BY event_id DESC LIMIT 1',
                (task_id,)
            ).fetchone()
        mx = row['mx'] if row else None
        return int(mx) if mx else None
    except Exception as e:
        logger.debug('[EventLog] latest_event_ts failed for task=%s: %s', task_id[:8], e)
        return None


def _opportunistic_compact(db):
    """Compact a few tasks' leftover FULL snapshot rows into delta form.

    Why this exists (and is not just the one-shot migration): the write-path
    projection in :func:`append_persistent_event` only shrinks rows that THIS
    process writes. Until every serving process runs that code, full rows keep
    arriving — and the offline migration is a point-in-time sweep, so the gap
    re-opens the moment it finishes (measured: +519 MB between two checks).
    This hook lets any process running this build heal the backlog
    continuously, so the table converges WITHOUT a coordinated restart.

    Reuses ``_migrate_snapshot_deltas.migrate_task`` VERBATIM so there is ONE
    implementation of the verify-then-write contract (§11): project → rebuild
    → compare byte-for-byte → write only on an exact match, else leave that
    task untouched. Never raises.
    """
    try:
        import importlib.util
        import os
        script = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__)))),
            'tests', '_migrate_snapshot_deltas.py')
        if not os.path.exists(script):
            return
        spec = importlib.util.spec_from_file_location('_snap_migrate', script)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception as e:
        logger.debug('[EventLog] compaction helper unavailable: %s', e)
        return

    try:
        tasks = mod._tasks_with_full_rows(db, limit=_COMPACT_MAX_TASKS)
    except Exception as e:
        logger.debug('[EventLog] compaction scan failed: %s', e)
        return
    if not tasks:
        return

    healed = failed = 0
    for tid in tasks:
        try:
            rep = mod.migrate_task(db, tid)
        except Exception as e:
            logger.debug('[EventLog] compaction of task=%s raised: %s',
                         str(tid)[:8], e)
            failed += 1
            continue
        if rep.get('status') == 'ok':
            healed += 1
        elif rep.get('status') == 'FAILED':
            failed += 1
            logger.warning('[EventLog] compaction REFUSED task=%s (rows left '
                           'untouched): %s', str(tid)[:8], rep.get('reason'))
    if healed or failed:
        logger.info('[EventLog] Delta-compacted %d task(s) of leftover full '
                    'snapshot rows (%d refused)', healed, failed)


def _prune_selected_rows(db, select_sql, params, label, *,
                         short_batch_is_exhaustive=True):
    """Delete at most a fixed number of event *rows* per committed batch.

    Selection is deliberately a separate read statement. A selector may need
    to prove that a historical/orphan tier is empty; embedding that scan in a
    ``DELETE`` acquires SQLite's writer lane *before* the scan and blocks every
    foreground writer for its whole duration (14.4s reproduced on production).
    We fetch at most 25 immutable primary keys, then delete them with ONE
    row-value statement. This keeps the old O(batches), not O(rows), command
    count while moving all potentially slow discovery outside the write
    transaction. Concurrent maintenance is harmless: event rows are immutable
    and the exact-key delete is idempotent.
    """
    total = 0
    for _ in range(_PRUNE_MAX_BATCHES):
        try:
            selected = db.execute(
                select_sql, (*params, _PRUNE_BATCH_ROWS)).fetchall()
        except Exception as e:
            logger.debug('[EventLog] %s key selection failed: %s', label, e)
            break
        if not selected:
            break

        keys = []
        for row in selected:
            if hasattr(row, 'keys'):
                keys.append((row['task_id'], row['event_id']))
            else:
                keys.append((row[0], row[1]))
        placeholders = ','.join(['(?,?)'] * len(keys))
        delete_sql = (
            'DELETE FROM task_events WHERE (task_id, event_id) IN ('
            + placeholders + ')')
        flat_keys = tuple(value for key in keys for value in key)
        try:
            cur = db_execute_with_retry(
                db, delete_sql, flat_keys,
                return_cursor=True)
            deleted = int(getattr(cur, 'rowcount', -1) or 0)
        except Exception as e:
            logger.debug('[EventLog] %s batch delete failed: %s', label, e)
            break
        if deleted < 0:
            # Both production wrappers report DELETE rowcount.  An unknown
            # value from a future adapter must stop here: assuming a full
            # batch would defeat the transaction bound, while retrying could
            # spin on an already-empty selector.
            logger.warning('[EventLog] %s delete returned unknown rowcount; '
                           'stopping this bounded pass', label)
            break
        if deleted == 0:
            break
        total += deleted
        if len(keys) < _PRUNE_BATCH_ROWS and short_batch_is_exhaustive:
            break
    return total


def _opportunistic_prune(db, *, include_orphans=True):
    """Delete stale task_events rows in two passes, both bounded by EVENT_TTL_MS.

    Pass 1 (terminal tasks): rows whose ``task_id`` JOINs a ``task_results``
    row in a terminal status with ``completed_at`` older than the TTL. This
    is the normal lifecycle reaper — uses ``task_results.completed_at`` as the
    authoritative terminal timestamp.

    Pass 2 (ORPHANED rows): rows whose ``task_id`` has NO ``task_results`` row
    at all and whose own ``task_events.ts_ms`` is older than the TTL. Pass 1
    structurally cannot see these — its JOIN drops any row without a matching
    ``task_results`` entry — so without Pass 2 they would never be reaped
    (permanent litter). Orphans arise whenever something runs the tool
    executor on a task dict whose id is not registered in the chat
    TaskRuntime and which never writes a task_results row: e.g. the 2026-06-28
    timer-poll-proxy collision bug left ~160 orphaned ``(tmr_*, 0/1)`` rows
    (the first, successful write of each colliding pair). The ``ts_ms <
    cutoff`` age guard is the safety mechanism — it guarantees we never reap
    events of a legitimately in-flight unregistered task, since any single
    poll's lifetime and the SSE-reconnect window are far under EVENT_TTL_MS.
    This also future-proofs the reaper against any new orphaned-id writer.
    """
    cutoff = int((time.time() * 1000) - EVENT_TTL_MS)
    structural_cutoff = int((time.time() * 1000) - STRUCTURAL_TTL_MS)
    _struct_ph = ','.join(['?'] * len(STRUCTURAL_EVENT_TYPES))

    # ── Pass 1: terminal tasks (JOIN task_results), deleted in bounded batches ──
    # TIERED (§10.4): this pass reaps the STREAMING NOISE (delta / phase /
    # tool_progress / …) at the 6h SSE-reconnect horizon but SPARES the
    # structural events the Request Inspector renders; those are reaped by
    # pass 1b at the 30-day horizon. Previously this deleted every row of an
    # eligible task, which is why a 2-hour-old task showed "log expired".
    # PostgreSQL has a partial timestamp index containing ONLY non-structural
    # events (idx_task_events_stream_ts).  This selector must start from that
    # tier.  The former "pick one terminal task, then probe its events" shape
    # looked bounded but was not: once compaction had removed every streaming
    # row, PostgreSQL merge-scanned all 251k surviving structural rows every
    # 15 seconds merely to prove absence (185k buffers / ~111 ms measured).
    # Starting from the partial tier makes the steady-state empty proof a
    # handful of index pages, while SQLite uses the same correct SQL with its
    # ordinary timestamp index. A short batch is globally exhaustive now.
    if getattr(db, 'dialect', '') == 'sqlite':
        # The time-first plan scans every old orphan event before discovering
        # that no terminal task_result owns it. Force the compact task-first
        # covering indexes on SQLite; PostgreSQL keeps its measured time-first
        # partial-index plan below.
        streaming_selector = (
            'SELECT te.task_id, te.event_id FROM task_results tr '
            'INDEXED BY idx_task_terminal_retention '
            'CROSS JOIN task_events te '
            'INDEXED BY idx_task_events_stream_task_ts '
            "WHERE tr.status IN ('done','error','aborted','interrupted') "
            'AND tr.completed_at IS NOT NULL AND tr.completed_at < ? '
            'AND te.task_id=tr.task_id AND te.ts_ms < ? '
            f'AND te.type NOT IN ({_STRUCTURAL_TYPES_SQL}) '
            'ORDER BY te.ts_ms ASC LIMIT ?')
    else:
        streaming_selector = (
            'SELECT te.task_id, te.event_id FROM task_events te '
            'JOIN task_results tr ON tr.task_id = te.task_id '
            'WHERE te.ts_ms < ? '
            f'AND te.type NOT IN ({_STRUCTURAL_TYPES_SQL}) '
            "AND tr.status IN ('done','error','aborted','interrupted') "
            'AND tr.completed_at IS NOT NULL AND tr.completed_at < ? '
            'ORDER BY te.ts_ms ASC LIMIT ?')
    total = _prune_selected_rows(
        db, streaming_selector,
        (cutoff, cutoff),
        'streaming prune')
    if total > 0:
        logger.info('[EventLog] Pruned %d stale streaming event row(s) '
                    '(cutoff=%d, structural events spared)', total, cutoff)

    # ── Pass 1b: STRUCTURAL events past the 30-day tier (§10.4) ──
    # Same batched-commit shape; only the horizon and the type filter differ.
    total = _prune_selected_rows(
        db,
        "SELECT te.task_id, te.event_id FROM task_events te "
        "JOIN task_results tr ON tr.task_id = te.task_id "
        "WHERE te.ts_ms < ? "
        f"  AND te.type IN ({_struct_ph}) "
        "  AND tr.status IN ('done','error','aborted','interrupted') "
        "  AND tr.completed_at IS NOT NULL AND tr.completed_at < ? "
        "ORDER BY te.ts_ms ASC LIMIT ?",
        (structural_cutoff, *STRUCTURAL_EVENT_TYPES, structural_cutoff),
        'structural prune')
    if total > 0:
        logger.info('[EventLog] Pruned %d structural event row(s) past the '
                    '30-day tier (cutoff=%d)', total, structural_cutoff)

    # ── Pass 2: orphaned rows (no task_results row), aged out by own ts_ms ──
    # Same batched-commit strategy. Deletes by the row's own primary key
    # (task_id, event_id) picked in bounded chunks so the correlated NOT EXISTS
    # subquery never runs against the whole table in one un-committable statement.
    # This is deliberately infrequent in the daemon (default every 6h): when
    # there are NO orphans, proving absence touches the old event population.
    # Keep the public/direct call default enabled for repair tools and tests.
    # Split the two age tiers instead of an OR: the OR forced PostgreSQL into a
    # parallel full-table scan even when the 30-day arm matched zero rows.
    if include_orphans:
        total = 0
        total += _prune_selected_rows(
            db,
            "SELECT te.task_id, te.event_id FROM task_events te "
            "WHERE te.ts_ms < ? AND te.type NOT IN (%s) "
            "  AND NOT EXISTS (SELECT 1 FROM task_results tr "
            "                  WHERE tr.task_id = te.task_id) "
            "ORDER BY te.ts_ms ASC LIMIT ?" % _STRUCTURAL_TYPES_SQL,
            (cutoff,),
            'streaming orphan prune')
        total += _prune_selected_rows(
            db,
            "SELECT te.task_id, te.event_id FROM task_events te "
            "WHERE te.ts_ms < ? AND te.type IN (%s) "
            "  AND NOT EXISTS (SELECT 1 FROM task_results tr "
            "                  WHERE tr.task_id = te.task_id) "
            "ORDER BY te.ts_ms ASC LIMIT ?" % _struct_ph,
            (structural_cutoff, *STRUCTURAL_EVENT_TYPES),
            'structural orphan prune')
        if total > 0:
            logger.info('[EventLog] Pruned %d orphaned event row(s) with no '
                        'task_results (stream cutoff=%d, structural cutoff=%d)',
                        total, cutoff, structural_cutoff)


def _prune_terminal_task_results(db):
    """Bound retention for terminal recovery rows without risking live tasks.

    ``task_results`` contains full content/thinking/tool state and had no
    retention policy, even after its conversation disappeared.  That made it
    a multi-gigabyte duplicate store on a personal installation.  Two passes
    deliberately distinguish rows with no authoritative conversation from
    rows still linked to one; only terminal rows with a real ``completed_at``
    are eligible. Candidate discovery stays outside the writer transaction,
    exactly like event-row cleanup: both relation gates contain anti-joins and
    must never make SQLite hold its global writer lane while proving absence.
    """
    now_ms = int(time.time() * 1000)
    terminal = "('done','error','aborted','interrupted')"
    passes = (
        ('orphan task-result prune', now_ms - _ORPHAN_RESULT_TTL_MS,
         'NOT EXISTS (SELECT 1 FROM conversations c WHERE c.id=tr.conv_id)'),
        ('linked task-result prune', now_ms - _LINKED_RESULT_TTL_MS,
         'EXISTS (SELECT 1 FROM conversations c WHERE c.id=tr.conv_id)'),
    )
    grand_total = 0
    for label, cutoff, relation_gate in passes:
        select_sql = (
            'SELECT tr.task_id FROM task_results tr '
            f'WHERE tr.status IN {terminal} '
            'AND tr.completed_at IS NOT NULL AND tr.completed_at < ? '
            # Delete the recovery row only after every event retention tier
            # has drained. Otherwise the next statement loses its indexed
            # task_results JOIN anchor and turns the remaining events into an
            # orphan backlog that the intentionally-infrequent anti-join will
            # not revisit for hours.
            'AND NOT EXISTS (SELECT 1 FROM task_events te '
            '                WHERE te.task_id=tr.task_id) '
            f'AND {relation_gate} '
            'ORDER BY tr.completed_at ASC LIMIT ?'
        )
        deleted_total = 0
        for _ in range(_RESULT_PRUNE_MAX_BATCHES):
            try:
                selected = db.execute(
                    select_sql,
                    (cutoff, _RESULT_PRUNE_BATCH_ROWS)).fetchall()
            except Exception as e:
                logger.debug('[EventLog] %s key selection failed: %s',
                             label, e)
                break
            if not selected:
                break
            task_ids = tuple(
                row['task_id'] if hasattr(row, 'keys') else row[0]
                for row in selected)
            delete_sql = (
                'DELETE FROM task_results WHERE task_id IN ('
                + ','.join(['?'] * len(task_ids)) + ')')
            try:
                cur = db_execute_with_retry(
                    db, delete_sql, task_ids,
                    return_cursor=True)
                deleted = int(getattr(cur, 'rowcount', -1) or 0)
            except Exception as e:
                logger.debug('[EventLog] %s batch delete failed: %s', label, e)
                break
            if deleted <= 0:
                if deleted < 0:
                    logger.warning('[EventLog] %s returned unknown rowcount; '
                                   'stopping this bounded pass', label)
                break
            deleted_total += deleted
            if len(task_ids) < _RESULT_PRUNE_BATCH_ROWS:
                break
        if deleted_total:
            grand_total += deleted_total
            logger.info('[EventLog] Pruned %d terminal task_results row(s) '
                        'via %s (cutoff=%d)', deleted_total, label, cutoff)
    return grand_total
