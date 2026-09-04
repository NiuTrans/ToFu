"""SQLite enterprise backend: one writer, fair priority queue, read-only pool."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
import faulthandler
import os
from pathlib import Path
import queue
import sqlite3
import threading
import time
from typing import Any
from datetime import datetime, timezone
import uuid

from lib.storage.errors import StorageError
from lib.storage.startup_control import StartupProgressCallback
from lib.storage_metric_policy import bounded_storage_metric_sample_capacity
from lib.log import get_logger
from lib.storage_sidecar.adapters.base import Backend, Operation, receipt_cacheable
from lib.storage_sidecar.backup_policy import (
    capacity_preflight,
    cleanup_job_artifacts,
    job_manifest_path,
    prune_verified_backups,
    reclaim_stale_job_artifacts,
    write_job_manifest,
)
from lib.storage_sidecar.config import SidecarConfig
from lib.storage_sidecar.preflight import run_filesystem_preflight
from lib.storage_sidecar.receipt_codec import (
    COMMAND_RECEIPT_LOOKUP_SQL,
    command_receipt_identity_v2,
    decode_command_receipt_lookup,
    encode_receipt_response,
)
from lib.storage_sidecar.schema import deferred_index_statements, initialize_schema
from lib.storage_sidecar.turn_projection_cache import TurnProjectionCache
from lib.storage_sidecar.durability import (
    fsync_directory, fsync_file, sha256_file, write_json_durable,
)


_SQLITE_BUSY = {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
_SQLITE_UNAVAILABLE = {sqlite3.SQLITE_IOERR, sqlite3.SQLITE_CANTOPEN, sqlite3.SQLITE_FULL}
_SQLITE_INTEGRITY = {sqlite3.SQLITE_CONSTRAINT, sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB}
_PRIORITY_CYCLE = ('user',) * 8 + ('event',) * 2 + ('maintenance',)
logger = get_logger('tofu.storage.sidecar.sqlite')

# Queue-acquisition fast-fail budget by priority lane. User-facing writes
# fail fast so an HTTP request never hangs behind a long transaction;
# background lanes (event/maintenance) may wait for their fair queue slot
# under load instead of being cancelled — the caller's deadline still bounds
# the total wait either way. The previous hardcoded 2s cap ignored the
# caller's deadline entirely and produced "Storage writer acquisition timed
# out" floods whenever one long transaction held the writer (2026-08-17,
# online conversation migration on a 45ms-fsync filesystem).
_ACQUIRE_CAP_S = {'user': 2.0, 'event': 10.0, 'maintenance': 30.0}


def _verify_readonly_backup(path: Path, deadline_at: float) -> None:
    """Run integrity_check without allowing last-close WAL side effects."""
    connection = sqlite3.connect(
        f'{path.as_uri()}?mode=ro', uri=True, isolation_level=None)
    try:
        connection.execute('PRAGMA query_only=ON')
        setter = getattr(connection, 'setconfig', None)
        no_close_checkpoint = getattr(
            sqlite3, 'SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE', None)
        if setter is not None and no_close_checkpoint is not None:
            setter(no_close_checkpoint, 1)

        def enforce_deadline() -> int:
            return 1 if time.monotonic() >= deadline_at else 0

        connection.set_progress_handler(enforce_deadline, 10_000)
        try:
            result = connection.execute('PRAGMA integrity_check').fetchone()[0]
        except sqlite3.OperationalError as exc:
            if time.monotonic() >= deadline_at:
                raise StorageError(
                    'database_timeout', 'SQLite backup verification deadline expired',
                    True, 100) from exc
            raise
        if result != 'ok':
            raise StorageError(
                'database_integrity', 'SQLite backup verification failed')
    finally:
        connection.close()

# Group-commit bounds.  ``_MAX_BATCH_JOBS`` caps one queue drain so deadlines
# re-evaluate between batches; ``_SEGMENT_BUDGET_S`` soft-caps the execution
# time of one commit segment so a slow op (e.g. a retention prune) can only
# delay the jobs already executed alongside it by a bounded amount — the rest
# of the batch continues in a fresh segment.  Batching is purely an
# optimization: any batch-level failure falls back to the per-job
# ``_transaction`` path, so worst-case semantics equal the pre-batch design.
_MAX_BATCH_JOBS = 64
_SEGMENT_BUDGET_S = 0.25


def _deferred_index_name(statement: str) -> str:
    """Return the declared name from our constrained CREATE INDEX grammar."""
    tokens = statement.split()
    if (len(tokens) < 7
            or [token.upper() for token in tokens[:5]]
            != ['CREATE', 'INDEX', 'IF', 'NOT', 'EXISTS']
            or tokens[6].upper() != 'ON'):
        raise RuntimeError(
            'deferred index statements must use CREATE INDEX IF NOT EXISTS')
    return tokens[5]


class SQLiteSession:
    backend = 'sqlite'

    def __init__(
        self,
        connection: sqlite3.Connection,
        turn_projection_cache: TurnProjectionCache | None = None,
    ) -> None:
        self.connection = connection
        self.turn_projection_cache = turn_projection_cache

    def lock_key(self, namespace: str, key: str) -> None:
        # The backend's single physical writer already serializes every key.
        del namespace, key

    def index_exists(self, index_name: str) -> bool:
        return self.fetch_one(
            "SELECT 1 AS present FROM sqlite_schema "
            "WHERE type='index' AND name=?",
            (str(index_name),),
        ) is not None

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        cursor = self.connection.execute(sql, params)
        return max(0, int(cursor.rowcount))

    def execute_many_exact(
        self, sql: str, params: Sequence[tuple[Any, ...]],
    ) -> int:
        """Execute a bounded DML batch that must match every input row."""
        if not params:
            return 0
        cursor = self.connection.executemany(sql, params)
        affected = max(0, int(cursor.rowcount))
        if affected != len(params):
            raise StorageError(
                'database_conflict',
                'Bulk mutation did not affect every expected row',
            )
        return affected

    def fetch_one(self, sql: str, params: tuple[Any, ...] = ()):
        row = self.connection.execute(sql, params).fetchone()
        return dict(row) if row is not None else None

    def fetch_all(self, sql: str, params: tuple[Any, ...] = ()):
        return [dict(row) for row in self.connection.execute(sql, params).fetchall()]

    def fetch_one_for_update_skip_locked(
        self, sql: str, params: tuple[Any, ...] = (),
    ):
        """Select one claim candidate inside the serialized writer txn.

        SQLite has one physical writer, so the surrounding ``BEGIN
        IMMEDIATE`` already provides the exclusion represented by PostgreSQL's
        row lock.  Keeping this adapter method preserves one operation body for
        both backends.
        """
        return self.fetch_one(sql, params)


@dataclass(slots=True)
class _ReadSlot:
    connection: sqlite3.Connection
    created_at: float
    last_used_at: float


@dataclass(slots=True)
class _WriteJob:
    operation: Operation
    deadline_at: float
    priority: str
    operation_name: str = ''
    transaction_timeout_s: float | None = None
    started: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)
    result: Any = None
    error: BaseException | None = None
    cancelled: bool = False
    # Group-commit scratch slot: the op's return value between SAVEPOINT
    # release and the segment's COMMIT (the job resolves only on commit).
    result_pending: Any = None
    # Raw jobs run on the writer thread WITHOUT a transaction wrapper (the
    # fastpath shipper's TRUNCATE checkpoint cannot execute inside one).
    raw: bool = False


class _BatchFailed(Exception):
    """A batch-level failure rolled back the outer transaction.

    Carries the mapped StorageError for retry classification; every job the
    batch had not yet resolved must be retried (as a batch) or fall back to
    its own ``_transaction`` — their work was never committed.
    """

    def __init__(self, mapped: StorageError) -> None:
        super().__init__(mapped.message)
        self.mapped = mapped


class _FairWriter:
    # A single writer thread is an availability liability: one transaction
    # stuck inside an uninterruptible syscall (fsync on a stalled FUSE mount
    # under cgroup memory pressure — measured 2026-08-18, writes wedged for
    # 30+ minutes until a manual restart) blocks EVERY lane forever while
    # clients time out at acquisition with no sidecar-side trace of what
    # holds the writer. The watchdog closes that hole: interrupt first,
    # hard-exit for the process so the supervisor's proven auto-restart
    # recovers the authority (WAL keeps each transaction all-or-nothing).
    #
    # The second availability liability is THROUGHPUT collapse, not wedge:
    # with one fsync per commit on a network filesystem (45ms healthy,
    # seconds under cgroup pressure), the writer's ceiling is 1/fsync_latency
    # while demand scales with agents × frame rate (measured 2026-08-20:
    # queue pinned at user:3-5/event:1-5 for 15+ minutes; every lane timing
    # out at acquisition with no single transaction overrunning — the
    # watchdog correctly never fired). Group commit closes that hole: the
    # writer drains its queue into one transaction with per-job SAVEPOINT
    # isolation, so N logical writes pay ONE fsync and throughput scales
    # with backlog exactly when the backlog exists.
    def __init__(
        self,
        connection: sqlite3.Connection,
        transaction_timeout_s: float,
        *,
        stall_grace_s: float = 15.0,
        hard_kill_s: float = 60.0,
        watchdog_interval_s: float = 1.0,
        queue_capacity: int = 64,
        max_batch_jobs: int = _MAX_BATCH_JOBS,
        segment_budget_s: float = _SEGMENT_BUDGET_S,
        metric_sample_capacity: int | None = None,
        turn_projection_cache: TurnProjectionCache | None = None,
    ) -> None:
        self._connection = connection
        self._transaction_timeout_s = transaction_timeout_s
        self._stall_grace_s = max(0.05, float(stall_grace_s))
        self._hard_kill_s = max(self._stall_grace_s + 0.05, float(hard_kill_s))
        self._watchdog_interval_s = max(0.05, float(watchdog_interval_s))
        if queue_capacity <= 0:
            raise ValueError('queue_capacity must be positive')
        self.queue_capacity = int(queue_capacity)
        self._max_batch_jobs = max(1, int(max_batch_jobs))
        self._segment_budget_s = max(0.0, float(segment_budget_s))
        self._turn_projection_cache = turn_projection_cache
        self._queues = {name: deque() for name in {'user', 'event', 'maintenance'}}
        self._condition = threading.Condition()
        self._stop = False
        self._cycle_index = 0
        self._thread = threading.Thread(
            target=self._run, name='storage-sqlite-writer', daemon=True)
        self.metrics = {
            'submitted': 0, 'completed': 0, 'failed': 0, 'timed_out': 0,
            'queue_rejections': 0, 'cancelled_before_start': 0,
            'write_admission_rejections': 0,
            'max_queue_depth': 0, 'transaction_retries': 0,
            'stall_interrupts': 0,
            'batches': 0, 'batched_jobs': 0, 'max_batch_size': 0,
            'batch_fallbacks': 0,
        }
        self._current_lock = threading.Lock()
        self._current: dict[str, Any] | None = None
        self._last_stall: dict[str, Any] | None = None
        self._last_acquisition_warning = 0.0
        # Fast-path shipper hook: invoked after every successful COMMIT on
        # the writer thread so the shadow tracks the front in near-real-time.
        self._on_commit: Any = None
        # Checked before retaining a job and again immediately before BEGIN.
        # The writer-side check is authoritative for work queued before a
        # resource threshold changed. Raw shipper checkpoints bypass it so
        # pressure can never deadlock the operation that releases pressure.
        self._write_admission_hook: Callable[[], None] | None = None
        # Commit-latency ground truth (seconds), sampled for the metrics
        # surface — this is how the fast path's measured win stays honest.
        self._metric_sample_capacity = bounded_storage_metric_sample_capacity(
            metric_sample_capacity)
        self._commit_latencies: deque[float] = deque(
            maxlen=self._metric_sample_capacity)
        self._latency_lock = threading.Lock()
        self._watchdog_stop = threading.Event()
        self._watchdog = threading.Thread(
            target=self._watchdog_loop, name='storage-sqlite-watchdog',
            daemon=True)
        self._thread.start()
        self._watchdog.start()

    def _set_current(self, job: _WriteJob | None) -> None:
        with self._current_lock:
            if job is None:
                self._current = None
                return
            now = time.monotonic()
            self._current = {
                'operation': job.operation_name,
                'priority': job.priority,
                'phase': 'begin',
                'started_at': now,
                'phase_started_at': now,
                'deadline_at': job.deadline_at,
                'interrupted': False,
                'token': id(job),
            }

    def _transaction_budget_s(self, job: _WriteJob) -> float:
        return (
            self._transaction_timeout_s
            if job.transaction_timeout_s is None
            else float(job.transaction_timeout_s)
        )

    def _set_current_batch(self, jobs: list['_WriteJob']) -> None:
        # The watchdog/metrics surface reads one "current" record; a batch
        # reports the EARLIEST member deadline (that is what bounds the
        # commit) and the most latency-sensitive lane present, so stall
        # detection stays as strict as the strictest member.
        lane_rank = {'user': 0, 'event': 1, 'maintenance': 2}
        names = [job.operation_name or 'unknown' for job in jobs]
        label = ','.join(names)[:120]
        with self._current_lock:
            now = time.monotonic()
            self._current = {
                'operation': f'batch:{len(jobs)}({label})',
                'priority': min((job.priority for job in jobs),
                                key=lambda name: lane_rank.get(name, 3)),
                'phase': 'begin',
                'started_at': now,
                'phase_started_at': now,
                'deadline_at': min(job.deadline_at for job in jobs),
                'interrupted': False,
                'batch_size': len(jobs),
                'token': id(jobs),
            }

    def _arm_current_job(self, job: '_WriteJob', deadline_at: float) -> None:
        # The watchdog thread keys ``_connection.interrupt()`` off the single
        # ``_current`` record.  ``_set_current_batch`` publishes the EARLIEST
        # member deadline once, but members commit at different times — once
        # the early-deadline job is done, the executing job must be measured
        # by its OWN deadline (mirroring the per-job progress-handler arming
        # in _commit_batch_once), or the watchdog interrupts an innocent
        # long-budget job and _map_sqlite_error mislabels the interrupt as a
        # non-retryable internal error.
        with self._current_lock:
            if self._current is None:
                return
            now = time.monotonic()
            self._current['operation'] = job.operation_name or 'unknown'
            self._current['priority'] = job.priority
            self._current['phase'] = 'begin'
            self._current['started_at'] = now
            self._current['phase_started_at'] = now
            self._current['deadline_at'] = deadline_at
            self._current['interrupted'] = False
            self._current['token'] = id(job)

    def _set_phase(self, phase: str) -> None:
        """Publish the writer's blocking boundary for watchdog diagnosis."""
        with self._current_lock:
            if self._current is None:
                return
            self._current['phase'] = phase
            self._current['phase_started_at'] = time.monotonic()

    def current_job(self) -> dict[str, Any] | None:
        with self._current_lock:
            if self._current is None:
                return None
            current = dict(self._current)
        current.pop('token', None)
        return current

    def last_stall(self) -> dict[str, Any] | None:
        with self._current_lock:
            return dict(self._last_stall) if self._last_stall else None

    def watchdog_policy(self) -> dict[str, float]:
        return {
            'stall_grace_s': self._stall_grace_s,
            'hard_kill_s': self._hard_kill_s,
        }

    def queue_depths(self) -> dict[str, int]:
        with self._condition:
            return {name: len(q) for name, q in self._queues.items()}

    def set_write_admission_hook(
        self,
        hook: Callable[[], None] | None,
    ) -> None:
        """Install the backend-owned, fail-closed resource admission fence."""
        with self._condition:
            self._write_admission_hook = hook

    def _write_admission_error(self) -> StorageError | None:
        with self._condition:
            hook = self._write_admission_hook
        if hook is None:
            return None
        try:
            hook()
        except StorageError as exc:
            return exc
        except Exception:
            logger.exception('SQLite write admission hook failed')
            return StorageError(
                'database_unavailable',
                'Storage write admission check failed',
                True,
                100,
            )
        return None

    def _record_write_admission_rejection(self) -> None:
        with self._condition:
            self.metrics['write_admission_rejections'] += 1

    def _watchdog_loop(self) -> None:
        while not self._watchdog_stop.wait(self._watchdog_interval_s):
            if not self._thread.is_alive():
                # The writer thread is the only consumer of the queues; its
                # death would wedge every future submit. Nothing in-process
                # can be trusted to recover a half-dead connection state.
                logger.critical(
                    'SQLite writer thread has exited; hard-exiting the sidecar '
                    'so the supervisor restarts the authority')
                self._hard_exit('writer-thread-dead')
                return
            with self._current_lock:
                current = dict(self._current) if self._current else None
            if current is None:
                continue
            now = time.monotonic()
            overrun = now - current['deadline_at']
            if overrun <= self._stall_grace_s:
                continue
            if not current['interrupted']:
                with self._current_lock:
                    if (self._current is not None
                            and self._current.get('token') == current.get('token')):
                        self._current['interrupted'] = True
                        self._current['interrupted_at'] = now
                        self._last_stall = {
                            'operation': current['operation'] or 'unknown',
                            'priority': current['priority'],
                            'phase': current.get('phase') or 'unknown',
                            'observed_at_ms': int(time.time() * 1000),
                            'held_s': round(now - current['started_at'], 3),
                            'phase_s': round(
                                now - current.get('phase_started_at', now), 3),
                            'overrun_s': round(overrun, 3),
                        }
                    else:
                        continue
                self.metrics['stall_interrupts'] += 1
                logger.error(
                    'SQLite writer transaction stalled %.0fs past its deadline '
                    '(op=%s priority=%s phase=%s, held %.0fs phase_held=%.0fs) '
                    '— interrupting SQLite; commit/fsync is not interruptible '
                    'and will force a bounded sidecar restart if it does not return',
                    overrun, current['operation'] or 'unknown',
                    current['priority'], current.get('phase') or 'unknown',
                    now - current['started_at'],
                    now - current.get('phase_started_at', now))
                try:
                    self._connection.interrupt()
                except sqlite3.Error as exc:
                    logger.debug('SQLite interrupt on stalled writer failed: %s', exc)
            if overrun > self._hard_kill_s:
                with self._current_lock:
                    still_current = (
                        self._current is not None
                        and self._current.get('token') == current.get('token'))
                if not still_current:
                    continue
                logger.critical(
                    'SQLite writer still stalled %.0fs past deadline after '
                    'interrupt (op=%s priority=%s phase=%s) — hard-exiting the sidecar; '
                    'the supervisor auto-restarts it and WAL recovery keeps '
                    'the committed prefix',
                    overrun, current['operation'] or 'unknown',
                    current['priority'], current.get('phase') or 'unknown')
                self._hard_exit('writer-stalled-past-hard-kill')
                return

    def _hard_exit(self, reason: str) -> None:
        # Overridable seam for tests; production dumps every thread's stack
        # to stderr (inherited into the server console log) and exits with a
        # distinctive code so the supervisor's crash callback fires.
        try:
            faulthandler.dump_traceback()
        except Exception as exc:
            logger.debug('faulthandler dump_traceback failed during hard exit: %s', exc)
        os._exit(75)

    def _pop_fair(self) -> _WriteJob | None:
        # Caller must hold self._condition.
        for _ in range(len(_PRIORITY_CYCLE)):
            name = _PRIORITY_CYCLE[self._cycle_index]
            self._cycle_index = (self._cycle_index + 1) % len(_PRIORITY_CYCLE)
            if self._queues[name]:
                return self._queues[name].popleft()
        for q in self._queues.values():
            if q:
                return q.popleft()
        return None

    def _take(self) -> _WriteJob | None:
        with self._condition:
            while not self._stop and not any(self._queues.values()):
                self._condition.wait()
            if self._stop and not any(self._queues.values()):
                return None
            return self._pop_fair()

    def _take_batch(self) -> list[_WriteJob] | None:
        # Block for the first job, then drain everything already queued (in
        # the same fair cycle order) so the whole backlog commits with ONE
        # fsync.  No artificial coalescing delay: when the writer is the
        # bottleneck the backlog exists; when it is not, a single-job batch
        # commits immediately — added idle latency would be pure cost.
        first = self._take()
        if first is None:
            return None
        batch = [first]
        with self._condition:
            while len(batch) < self._max_batch_jobs:
                nxt = self._pop_fair()
                if nxt is None:
                    break
                batch.append(nxt)
        return batch

    def _resolve(
        self,
        job: _WriteJob,
        result: Any = None,
        error: BaseException | None = None,
    ) -> None:
        # A defensive failure path may resolve a job before its normal
        # execution site arms ``started``. Wake acquisition waiters in that
        # case so they receive the real classified error immediately instead
        # of manufacturing a second acquisition timeout.
        job.started.set()
        job.result = result
        job.error = error
        if error is None:
            self.metrics['completed'] += 1
        else:
            self.metrics['failed'] += 1
            logger.warning(
                'SQLite writer job failed (op=%s priority=%s): %s: %.200s',
                job.operation_name or 'unknown', job.priority,
                type(error).__name__, error)
        job.done.set()

    def _resolve_acquisition_timeout(self, job: _WriteJob) -> None:
        job.error = StorageError(
            'database_timeout', 'Storage writer acquisition timed out', True, 25)
        if not job.cancelled:
            self.metrics['timed_out'] += 1
        job.started.set()
        job.done.set()

    def _resolve_batch_wait_timeout(self, job: _WriteJob) -> None:
        # Distinct from acquisition timeout: the job WAS acquired, then
        # expired queued behind batch-mates / a rolled-back batch attempt.
        # Keeping the labels separate protects stall diagnosis (the
        # acquisition metric was the key evidence in the 2026-08-20
        # postmortem — conflating the two would silently re-blur it).
        job.error = StorageError(
            'database_timeout', 'Storage writer batch wait timed out', True, 25)
        if not job.cancelled:
            self.metrics['timed_out'] += 1
        job.started.set()
        job.done.set()

    def _run(self) -> None:
        while True:
            batch = self._take_batch()
            if batch is None:
                return
            survivors = []
            for job in batch:
                if job.cancelled or time.monotonic() >= job.deadline_at:
                    self._resolve_acquisition_timeout(job)
                else:
                    survivors.append(job)
            if not survivors:
                continue
            # Raw jobs (shipper checkpoints) must NEVER enter a batch: the
            # shared BEGIN IMMEDIATE would make the checkpoint a no-op.
            raw_jobs = [job for job in survivors if job.raw]
            survivors = [job for job in survivors if not job.raw]
            for job in raw_jobs:
                job.started.set()
                self._set_current(job)
                try:
                    self._resolve(job, result=self._transaction(job))
                except BaseException as exc:
                    self._resolve(job, error=exc)
                finally:
                    self._set_current(None)
            if not survivors:
                continue
            if len(survivors) == 1:
                job = survivors[0]
                job.started.set()
                self._set_current(job)
                try:
                    self._resolve(job, result=self._transaction(job))
                except BaseException as exc:
                    # BaseException, not Exception: a SystemExit/
                    # KeyboardInterrupt raised inside a job must fail THAT
                    # job, never kill the writer thread (its death would
                    # wedge every lane forever).
                    self._resolve(job, error=exc)
                finally:
                    self._set_current(None)
            else:
                try:
                    self._run_batch(survivors)
                except BaseException as exc:
                    # Same invariant as the single-job path: a failure must
                    # resolve ITS jobs, never kill the writer thread.
                    for job in survivors:
                        if not job.done.is_set():
                            self._resolve(job, error=exc)

    def _run_batch(self, jobs: list[_WriteJob]) -> None:
        self.metrics['batches'] += 1
        self.metrics['batched_jobs'] += len(jobs)
        self.metrics['max_batch_size'] = max(
            self.metrics['max_batch_size'], len(jobs))
        self._set_current_batch(jobs)
        pending = list(jobs)
        attempts = 0
        try:
            while pending:
                try:
                    self._commit_batch_once(pending)
                    return
                except _BatchFailed as failure:
                    mapped = failure.mapped
                    # Every not-yet-resolved job in ``pending`` was rolled
                    # back; nothing was committed.  (Jobs already resolved
                    # by their savepoint — per-job errors — must NOT
                    # re-run.)  Expired jobs resolve as batch-wait
                    # timeouts; the rest either retry as a batch (same
                    # busy/unavailable policy as the single-job path) or
                    # degrade to their own transactions — worst case equals
                    # the pre-batch design exactly.
                    still_open = []
                    for job in pending:
                        if job.done.is_set():
                            continue
                        if job.cancelled or time.monotonic() >= job.deadline_at:
                            self._resolve_batch_wait_timeout(job)
                        else:
                            still_open.append(job)
                    if (mapped.retryable
                            and mapped.code in {
                                'database_busy', 'database_unavailable'}
                            and attempts < 3 and still_open):
                        attempts += 1
                        self.metrics['transaction_retries'] += 1
                        time.sleep(0.01 * (2 ** attempts))
                        pending = still_open
                        continue
                    self.metrics['batch_fallbacks'] += 1
                    logger.warning(
                        'SQLite batch commit failed (%s); falling back to '
                        'per-job transactions for %d job(s)',
                        mapped.message, len(still_open))
                    for job in still_open:
                        if job.cancelled or time.monotonic() >= job.deadline_at:
                            self._resolve_batch_wait_timeout(job)
                            continue
                        # The fallback runs single-job transactions — arm the
                        # watchdog with THIS job's deadline exactly like the
                        # non-batch path does, or a stale batch record could
                        # interrupt it mid-transaction.
                        job.started.set()
                        self._set_current(job)
                        try:
                            self._resolve(job, result=self._transaction(job))
                        except BaseException as exc:
                            self._resolve(job, error=exc)
                        finally:
                            self._set_current(None)
                    return
        finally:
            self._set_current(None)

    def _commit_batch_once(self, jobs: list[_WriteJob]) -> None:
        """Run ``jobs`` in one or more commit segments, one fsync per segment.

        Per-job SAVEPOINTs give each job its own atomicity and error
        isolation inside the shared transaction.  Raises _BatchFailed when a
        segment cannot commit; every not-yet-resolved job is then the
        caller's retry/fallback problem.
        """
        batch_start = time.monotonic()
        # The progress handler is re-armed PER JOB with ``min(job.deadline_at,
        # segment_start + transaction_timeout)``: one shared handler armed with
        # the batch's earliest deadline used to interrupt every later job once
        # that earliest deadline passed — jobs with minutes of budget left
        # failed mid-op and were mislabeled ``database_timeout`` (2026-08-20
        # audit).  Per-job arming keeps the segment watchdog intact while a
        # job can only ever be interrupted by its OWN deadline.
        segment_deadline = batch_start + self._transaction_budget_s(jobs[0])
        self._connection.set_progress_handler(
            lambda: 1 if time.monotonic() >= segment_deadline else 0, 1000)
        segment_start = batch_start
        segment_work: list[_WriteJob] = []
        in_transaction = False
        try:
            for job in jobs:
                now = time.monotonic()
                if job.cancelled or now >= job.deadline_at:
                    self._resolve_batch_wait_timeout(job)
                    continue
                if in_transaction and (
                        job.priority == 'maintenance'
                        or now - segment_start >= self._segment_budget_s):
                    # Lane-boundary / time-budget commit: background work and
                    # overlong segments must not extend the latency of the
                    # jobs already executed in this segment.
                    self._commit_segment(segment_work, segment_deadline)
                    segment_work = []
                    in_transaction = False
                    now = time.monotonic()
                    if job.cancelled or now >= job.deadline_at:
                        self._resolve_batch_wait_timeout(job)
                        continue
                # ``submit``'s acquisition cap measures time until THIS job
                # owns the writer, not time until a whole drained batch was
                # reserved. Marking every batch member started up front (or
                # before committing its predecessor segment) let a user
                # command sit behind slow batch-mates until its full RPC
                # deadline (16.4s in the 2026-08-23 incident), defeating the
                # documented 2s fail-fast boundary.
                job.started.set()
                if not in_transaction:
                    admission_error = self._write_admission_error()
                    if admission_error is not None:
                        self._record_write_admission_rejection()
                        self._resolve(job, error=admission_error)
                        continue
                    segment_deadline = now + self._transaction_budget_s(job)
                    self._arm_current_job(job, segment_deadline)
                    self._set_phase('begin')
                    self._connection.set_progress_handler(
                        lambda: 1 if time.monotonic() >= segment_deadline else 0,
                        1000)
                    try:
                        self._connection.execute('BEGIN IMMEDIATE')
                    except BaseException as exc:
                        raise _BatchFailed(
                            _map_sqlite_error(exc, segment_deadline)) from exc
                    in_transaction = True
                    segment_start = time.monotonic()
                segment_deadline = min(
                    job.deadline_at,
                    segment_start + self._transaction_budget_s(job),
                )
                self._connection.set_progress_handler(
                    lambda: 1 if time.monotonic() >= segment_deadline else 0,
                    1000)
                self._arm_current_job(job, segment_deadline)
                self._set_phase('execute')
                try:
                    self._connection.execute('SAVEPOINT tofu_gc')
                    try:
                        result = job.operation(SQLiteSession(
                            self._connection, self._turn_projection_cache))
                    except BaseException as op_exc:
                        try:
                            self._set_phase('rollback')
                            self._connection.execute('ROLLBACK TO tofu_gc')
                            self._connection.execute('RELEASE tofu_gc')
                        except sqlite3.Error as sp_exc:
                            raise _BatchFailed(_map_sqlite_error(
                                sp_exc, segment_deadline)) from op_exc
                        mapped = _map_sqlite_error(op_exc, segment_deadline)
                        if (mapped.retryable and mapped.code in {
                                'database_busy', 'database_unavailable'}):
                            # The single-job path retries these (3× backoff);
                            # resolving here would permanently fail a
                            # recoverable write — diverging from the
                            # documented worst-case-equals-per-job contract.
                            # Defer to the batch-level retry/fallback, which
                            # re-runs this job with the same policy.
                            raise _BatchFailed(mapped) from op_exc
                        self._resolve(job, error=mapped)
                        continue
                    if time.monotonic() >= job.deadline_at:
                        self._set_phase('rollback')
                        self._connection.execute('ROLLBACK TO tofu_gc')
                        self._connection.execute('RELEASE tofu_gc')
                        self._resolve(job, error=StorageError(
                            'database_timeout',
                            'Storage transaction exceeded its watchdog',
                            True, 25))
                        continue
                    self._connection.execute('RELEASE tofu_gc')
                except _BatchFailed:
                    raise
                except BaseException as exc:
                    # Statement-level failure outside the savepoint guard
                    # (e.g. watchdog interrupt between statements): the
                    # transaction's integrity is no longer assured.
                    raise _BatchFailed(
                        _map_sqlite_error(exc, segment_deadline)) from exc
                job.result_pending = result  # resolved only when the segment commits
                segment_work.append(job)
            if in_transaction:
                self._commit_segment(segment_work, segment_deadline)
                in_transaction = False
        finally:
            if in_transaction:
                try:
                    self._set_phase('rollback')
                    self._connection.rollback()
                except sqlite3.Error as rollback_error:
                    logger.debug('SQLite batch rollback failed: %s',
                                 type(rollback_error).__name__)
            self._connection.set_progress_handler(None, 0)

    def _commit_segment(
        self,
        segment_work: list[_WriteJob],
        segment_deadline: float,
    ) -> None:
        if time.monotonic() >= segment_deadline:
            raise _BatchFailed(StorageError(
                'database_timeout', 'Storage transaction exceeded its watchdog',
                True, 25))
        commit_started = time.monotonic()
        try:
            self._set_phase('commit')
            self._connection.commit()
        except BaseException as exc:
            raise _BatchFailed(
                _map_sqlite_error(exc, segment_deadline)) from exc
        self._note_commit(commit_started)
        self._set_phase('post_commit')
        for job in segment_work:
            self._resolve(job, result=job.result_pending)
            job.result_pending = None

    def _note_commit(self, started_at: float) -> None:
        with self._latency_lock:
            self._commit_latencies.append(time.monotonic() - started_at)
        if self._on_commit is not None:
            try:
                self._on_commit()
            except Exception:
                # The hook is an availability optimization (shadow shipping);
                # it must never fail the commit that already landed.
                logger.debug('SQLite on-commit hook failed', exc_info=True)

    def commit_latency_stats(self) -> dict[str, float | int]:
        with self._latency_lock:
            recent_samples = tuple(self._commit_latencies)
        # Prometheus/support snapshots must not hold the writer's observation
        # lock while sorting reconstructible history; commits remain the
        # higher-authority path.
        samples = sorted(recent_samples)
        if not samples:
            return {
                'sample_capacity': self._metric_sample_capacity,
                'samples': 0,
                'p50_ms': 0.0,
                'p95_ms': 0.0,
                'max_ms': 0.0,
            }
        import math as _math
        return {
            'sample_capacity': self._metric_sample_capacity,
            'samples': len(samples),
            'p50_ms': round(samples[len(samples) // 2] * 1000, 3),
            'p95_ms': round(
                samples[max(0, _math.ceil(len(samples) * 0.95) - 1)] * 1000, 3),
            'max_ms': round(samples[-1] * 1000, 3),
        }

    def _transaction(self, job: _WriteJob) -> Any:
        if not job.raw:
            admission_error = self._write_admission_error()
            if admission_error is not None:
                self._record_write_admission_rejection()
                raise admission_error
        attempts = 0
        while True:
            transaction_deadline = min(
                job.deadline_at,
                time.monotonic() + self._transaction_budget_s(job),
            )
            self._connection.set_progress_handler(
                lambda: 1 if time.monotonic() >= transaction_deadline else 0,
                1000,
            )
            try:
                if job.raw:
                    self._set_phase('execute')
                    return job.operation(SQLiteSession(
                        self._connection, self._turn_projection_cache))
                self._set_phase('begin')
                self._connection.execute('BEGIN IMMEDIATE')
                self._set_phase('execute')
                result = job.operation(SQLiteSession(
                    self._connection, self._turn_projection_cache))
                if time.monotonic() >= transaction_deadline:
                    raise StorageError(
                        'database_timeout', 'Storage transaction exceeded its watchdog',
                        True, 25)
                commit_started = time.monotonic()
                self._set_phase('commit')
                self._connection.commit()
                self._note_commit(commit_started)
                self._set_phase('post_commit')
                return result
            except BaseException as exc:
                # Rollback precedes classification and every possible retry.
                try:
                    self._set_phase('rollback')
                    self._connection.rollback()
                except sqlite3.Error as rollback_error:
                    logger.debug('SQLite rollback after failed transaction failed: %s',
                                 type(rollback_error).__name__)
                mapped = _map_sqlite_error(exc, transaction_deadline)
                if (mapped.retryable and mapped.code in {'database_busy', 'database_unavailable'}
                        and attempts < 3 and time.monotonic() + 0.02 < job.deadline_at):
                    attempts += 1
                    self.metrics['transaction_retries'] += 1
                    time.sleep(0.01 * (2 ** attempts))
                    continue
                raise mapped from exc
            finally:
                self._connection.set_progress_handler(None, 0)

    def submit(
        self,
        operation: Operation,
        priority: str,
        deadline_at: float,
        operation_name: str = '',
        *,
        raw: bool = False,
        transaction_timeout_s: float | None = None,
    ) -> Any:
        if priority not in self._queues:
            raise StorageError('database_protocol_error', 'Invalid storage priority')
        if not self._thread.is_alive():
            # A dead writer thread would otherwise make every submit wait out
            # its acquisition cap forever with no trace. Fail loudly instead;
            # the watchdog also hard-exits the process so the supervisor
            # restarts the authority.
            raise StorageError(
                'database_unavailable', 'Storage writer thread has exited', True, 100)
        with self._condition:
            if self._stop:
                raise StorageError(
                    'database_unavailable', 'Storage writer is stopping', True, 100)
        if not raw:
            admission_error = self._write_admission_error()
            if admission_error is not None:
                self._record_write_admission_rejection()
                raise admission_error
        if transaction_timeout_s is not None and not (
            0.05 <= float(transaction_timeout_s) <= 300.0
        ):
            raise StorageError(
                'database_protocol_error',
                'Invalid storage transaction timeout override',
            )
        job = _WriteJob(
            operation=operation, deadline_at=deadline_at, priority=priority,
            operation_name=operation_name, raw=raw,
            transaction_timeout_s=transaction_timeout_s)
        with self._condition:
            if self._stop:
                raise StorageError(
                    'database_unavailable', 'Storage writer is stopping', True, 100)
            depth = sum(len(items) for items in self._queues.values())
            if depth >= self.queue_capacity:
                self.metrics['queue_rejections'] += 1
                raise StorageError(
                    'database_busy',
                    'Storage writer queue is full',
                    True,
                    25,
                )
            self._queues[priority].append(job)
            depth += 1
            self.metrics['submitted'] += 1
            self.metrics['max_queue_depth'] = max(self.metrics['max_queue_depth'], depth)
            self._condition.notify()
        acquire_wait = max(0.0, min(
            _ACQUIRE_CAP_S.get(priority, 2.0), deadline_at - time.monotonic()))
        if not job.started.wait(acquire_wait):
            # The caller no longer owns a useful write. Remove the job while
            # it is still queued so its operation closure and decoded RPC
            # payload become reclaimable immediately. If the writer already
            # drained it into a local batch, the cancellation bit preserves
            # the existing pre-execution/commit fence.
            removed_before_start = False
            with self._condition:
                job.cancelled = True
                try:
                    self._queues[priority].remove(job)
                    removed_before_start = True
                except ValueError:
                    pass
                self.metrics['timed_out'] += 1
                if removed_before_start:
                    self.metrics['cancelled_before_start'] += 1
                self._condition.notify_all()
            now = time.monotonic()
            if now - self._last_acquisition_warning >= 5.0:
                self._last_acquisition_warning = now
                current = self.current_job()
                if current is not None:
                    current['held_s'] = round(now - current['started_at'], 1)
                logger.warning(
                    'SQLite writer acquisition timed out after %.1fs '
                    '(priority=%s op=%s queue=%s held_by=%s) — the writer is '
                    'occupied; see stall diagnostics above',
                    acquire_wait, priority, operation_name or 'unknown',
                    self.queue_depths(), current)
            raise StorageError(
                'database_timeout', 'Storage writer acquisition timed out', True, 25)
        remaining = max(0.0, deadline_at - time.monotonic())
        if not job.done.wait(remaining):
            raise StorageError(
                'database_timeout', 'Storage command deadline expired', True, 25)
        if job.error is not None:
            raise job.error
        return job.result

    def close(self) -> None:
        self._watchdog_stop.set()
        with self._condition:
            self._stop = True
            self._condition.notify_all()
        self._watchdog.join(timeout=2)
        self._thread.join(timeout=10)
        self._connection.close()


def _map_sqlite_error(exc: BaseException, deadline_at: float) -> StorageError:
    if isinstance(exc, StorageError):
        return exc
    if not isinstance(exc, sqlite3.Error):
        return StorageError('database_internal', 'Storage operation failed')
    raw_code = getattr(exc, 'sqlite_errorcode', 0) or 0
    code = int(raw_code) & 0xFF
    if code == sqlite3.SQLITE_INTERRUPT and time.monotonic() >= deadline_at:
        return StorageError('database_timeout', 'Storage transaction timed out', True, 25)
    if code in _SQLITE_BUSY:
        return StorageError('database_busy', 'Storage writer is busy', True, 25)
    if code in _SQLITE_UNAVAILABLE:
        return StorageError('database_unavailable', 'SQLite storage is unavailable', True, 100)
    if code in _SQLITE_INTEGRITY:
        return StorageError('database_integrity', 'SQLite integrity constraint failed')
    return StorageError('database_internal', 'SQLite operation failed')


class SQLiteBackend(Backend):
    name = 'sqlite'

    def __init__(
        self,
        config: SidecarConfig,
        *,
        startup_progress: StartupProgressCallback | None = None,
    ) -> None:
        self.config = config
        self._startup_progress = startup_progress
        self._writer: _FairWriter | None = None
        self._deferred_schema_missing: tuple[str, ...] = ()
        self._read_pool: queue.Queue[_ReadSlot] = queue.Queue(config.read_pool_size)
        self._closed = False
        self._preflight: dict[str, Any] = {}
        self._metrics = {'queries': 0, 'query_failures': 0, 'pool_rotations': 0}
        self._turn_projection_cache = TurnProjectionCache(
            config.turn_projection_cache_mib * 1024 * 1024)
        # Fast-path authority (see lib/storage_sidecar/fastpath.py): when a
        # measured-local filesystem wins decisively, the write front opens
        # THERE and the shipper keeps a durable shadow on the data dir.
        self._authority_path = config.sqlite_path
        self._authority_uuid = ''
        self._fastpath_decision: Any = None
        self._preflight_fastpath: dict[str, Any] = {'active': False}
        self._authority_storage_class = 'unknown'
        self._authority_filesystem_type = 'unknown'
        self._shipper: Any = None
        self._anchor_connection: sqlite3.Connection | None = None
        self._backup_lock = threading.Lock()
        self._turn_search_projection: Any = None
        self._turn_search_projection_error = ''

    def _connect(self, *, query_only: bool) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self._authority_path,
            timeout=self.config.acquire_timeout_s,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA busy_timeout=2000')
        connection.execute('PRAGMA foreign_keys=ON')
        if self._fastpath_active:
            self._arm_no_checkpoint_on_close(connection)
        if query_only:
            connection.execute('PRAGMA query_only=ON')
        else:
            mode = connection.execute('PRAGMA journal_mode=WAL').fetchone()[0]
            if str(mode).lower() != 'wal':
                connection.close()
                raise StorageError(
                    'database_unavailable', 'SQLite WAL mode is unavailable')
            connection.execute('PRAGMA synchronous=FULL')
            # SQLite otherwise gives this sole cross-domain writer only 2 MiB
            # of page cache.  On a large/FUSE authority that turns a small
            # indexed UPSERT into avoidable random reads.  The launch-time
            # resource profile supplies a bounded MiB budget; negative
            # cache_size means KiB rather than a page-count guess.
            writer_cache_kib = self.config.sqlite_writer_cache_mib * 1024
            connection.execute(f'PRAGMA cache_size=-{writer_cache_kib}')
            if self._fastpath_active:
                # The shipper owns EVERY checkpoint; an automatic one would
                # break the shadow's byte-prefix invariant.
                connection.execute('PRAGMA wal_autocheckpoint=0')
            else:
                connection.execute('PRAGMA wal_autocheckpoint=4096')
        return connection

    @property
    def _fastpath_active(self) -> bool:
        return self._authority_path != self.config.sqlite_path

    def diagnostic_locator(self) -> dict[str, Any]:
        """Describe the active file without exposing the private RPC token."""
        locator: dict[str, Any] = {
            'format': 'tofu.storage-locator/v1',
            'backend': self.name,
            'authority_path': str(self._authority_path.resolve()),
            'configured_path': str(self.config.sqlite_path.resolve()),
            'fastpath_active': self._fastpath_active,
        }
        decision = self._fastpath_decision
        if self._fastpath_active and decision is not None and decision.shadow_dir:
            locator['shadow_dir'] = str(Path(decision.shadow_dir).resolve())
        return locator

    @staticmethod
    def _arm_no_checkpoint_on_close(connection: sqlite3.Connection) -> None:
        # A last-connection close would otherwise checkpoint the WAL outside
        # the shipper's control.  Python 3.12+ exposes the dbconfig; the
        # anchor connection held for the backend's lifetime covers drivers
        # that predate it (a still-open connection means no close-checkpoint).
        setter = getattr(connection, 'setconfig', None)
        flag = getattr(sqlite3, 'SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE', None)
        if setter is not None and flag is not None:
            try:
                setter(flag, 1)
            except sqlite3.Error:
                logger.debug('NO_CKPT_ON_CLOSE rejected by driver')

    def _scratch_recovery_preflight(self) -> None:
        path = self._authority_path.parent / '.storage-wal-preflight.sqlite3'
        for suffix in ('', '-wal', '-shm'):
            path.with_name(path.name + suffix).unlink(missing_ok=True)
        try:
            connection = sqlite3.connect(path, isolation_level=None)
            connection.execute('PRAGMA journal_mode=WAL')
            connection.execute('PRAGMA synchronous=FULL')
            connection.execute('CREATE TABLE durability_probe(value TEXT NOT NULL)')
            connection.execute('BEGIN IMMEDIATE')
            connection.execute('INSERT INTO durability_probe(value) VALUES (?)', ('committed',))
            connection.commit()
            connection.close()
            reopened = sqlite3.connect(path)
            row = reopened.execute('SELECT value FROM durability_probe').fetchone()
            integrity = reopened.execute('PRAGMA integrity_check').fetchone()[0]
            reopened.close()
            if row != ('committed',) or integrity != 'ok':
                raise StorageError(
                    'database_integrity', 'SQLite WAL recovery preflight failed')
        finally:
            for suffix in ('', '-wal', '-shm'):
                path.with_name(path.name + suffix).unlink(missing_ok=True)

    def start(self) -> dict[str, Any]:
        report = run_filesystem_preflight(self.config.data_dir)
        from lib.storage_sidecar import fastpath
        decision = fastpath.decide(self.config.data_dir)
        self._fastpath_decision = decision
        if decision.active:
            self._authority_path = fastpath.reconcile(
                decision,
                self.config.sqlite_path,
                startup_progress=self._startup_progress,
            )
            report_fastpath = {
                'active': self._fastpath_active,
                'reason': decision.reason,
                'benchmark': decision.benchmark,
                'local_dir': str(decision.local_dir or ''),
            }
            if self._fastpath_active:
                logger.info('[fastpath] write front at %s (shadow: %s)',
                            self._authority_path, decision.shadow_dir)
            else:
                # Split-brain guard inside reconcile refused the front.
                report_fastpath['reason'] = 'reconcile refused; classic path'
        else:
            report_fastpath = {
                'active': False, 'reason': decision.reason,
                'benchmark': decision.benchmark,
            }
        self._preflight_fastpath = report_fastpath
        if self._fastpath_active:
            from lib.storage_sidecar.storage_capabilities import describe_mount

            authority_mount = describe_mount(self._authority_path.parent)
            self._authority_storage_class = authority_mount.storage_class
            self._authority_filesystem_type = authority_mount.filesystem_type
        else:
            self._authority_storage_class = report.storage_class
            self._authority_filesystem_type = report.filesystem_type
        self._scratch_recovery_preflight()
        writer_connection = self._connect(query_only=False)
        try:
            # Arm incremental auto-vacuum on a FRESH authority.  The pragma
            # only persists through a VACUUM (SQLite ≥3.53 semantics), which
            # is instant while the database is table-less; an existing
            # authority skips this entirely and keeps its current mode — the
            # prod authority was born INCREMENTAL by the migrator.  Without
            # the mode, ``system.reclaim`` can never return deleted pages to
            # the filesystem (2026-08-20 postmortem: the sidecar era had no
            # page reclamation at all).
            authority_is_fresh = writer_connection.execute(
                    "SELECT 1 FROM sqlite_schema WHERE type='table' "
                    'LIMIT 1').fetchone() is None
            if authority_is_fresh:
                writer_connection.execute('PRAGMA auto_vacuum=INCREMENTAL')
                writer_connection.execute('VACUUM')
            writer_connection.execute('BEGIN IMMEDIATE')
            startup_session = SQLiteSession(writer_connection)
            initialize_schema(startup_session)
            deferred_statements = deferred_index_statements('sqlite')
            if authority_is_fresh:
                # Optional indexes are effectively free while every table is
                # empty. Install them under the one startup transaction, before
                # the runtime writer exists, so a fresh authority is complete
                # without introducing a second writer connection.
                for statement in deferred_statements:
                    startup_session.execute(statement)
                logger.info(
                    '[deferred-schema] installed %d index(es) on fresh authority',
                    len(deferred_statements))
            else:
                # CREATE INDEX takes SQLite's sole write lock for the entire
                # table scan. Starting that DDL on a populated, 400+ GiB
                # authority after advertising sidecar readiness starved boot
                # recovery until Hypercorn killed the process, and the next
                # boot repeated the same work forever. Existing authorities
                # therefore report missing performance indexes but never build
                # them automatically; an explicit offline maintenance window
                # owns that potentially hours-long operation.
                self._deferred_schema_missing = tuple(
                    name
                    for statement in deferred_statements
                    for name in (_deferred_index_name(statement),)
                    if startup_session.fetch_one(
                        "SELECT 1 FROM sqlite_schema "
                        "WHERE type='index' AND name=?",
                        (name,),
                    ) is None
                )
                if self._deferred_schema_missing:
                    logger.warning(
                        '[deferred-schema] skipped automatic DDL on established '
                        'authority; missing=%s',
                        ','.join(self._deferred_schema_missing))
            startup_session.execute(
                'INSERT INTO storage_meta(meta_key, meta_value) VALUES (?, ?) '
                'ON CONFLICT(meta_key) DO UPDATE SET meta_value = excluded.meta_value',
                ('__startup_read_write_probe__', 'ok'),
            )
            probe = startup_session.fetch_one(
                'SELECT meta_value FROM storage_meta WHERE meta_key = ?',
                ('__startup_read_write_probe__',),
            )
            if probe is None or probe['meta_value'] != 'ok':
                raise StorageError(
                    'database_integrity', 'SQLite read/write startup probe failed')
            startup_session.execute(
                'DELETE FROM storage_meta WHERE meta_key = ?',
                ('__startup_read_write_probe__',),
            )
            # The authority lineage uuid anchors fastpath split-brain
            # detection: the local front, the durable shadow, and this row
            # must always agree (minted once, never rewritten).
            authority = startup_session.fetch_one(
                'SELECT meta_value FROM storage_meta WHERE meta_key = ?',
                ('authority_uuid',),
            )
            if authority is None:
                self._authority_uuid = uuid.uuid4().hex
                startup_session.execute(
                    'INSERT INTO storage_meta(meta_key, meta_value) '
                    'VALUES (?, ?)',
                    ('authority_uuid', self._authority_uuid),
                )
            else:
                self._authority_uuid = str(authority['meta_value'])
            # Do not run PRAGMA integrity_check in the boot-critical path.  It
            # scans the entire authority and can exceed the supervisor's
            # bounded startup window by minutes once tofu.db reaches tens of
            # gigabytes.  Filesystem durability, scratch WAL recovery, schema
            # initialization, and the real transactional round-trip above are
            # the bounded readiness gates.  The exhaustive scan remains
            # available through ``system.integrity_check`` / storage_certify.
            writer_connection.commit()
        except BaseException:
            writer_connection.rollback()
            writer_connection.close()
            raise
        now = time.monotonic()
        try:
            for _ in range(self.config.read_pool_size):
                self._read_pool.put(_ReadSlot(self._connect(query_only=True), now, now))
        except BaseException:
            while not self._read_pool.empty():
                self._read_pool.get_nowait().connection.close()
            writer_connection.close()
            raise
        self._writer = _FairWriter(
            writer_connection,
            self.config.transaction_timeout_s,
            stall_grace_s=self.config.writer_stall_grace_s,
            hard_kill_s=self.config.writer_hard_kill_s,
            queue_capacity=self.config.sqlite_writer_queue_capacity,
            turn_projection_cache=self._turn_projection_cache,
        )
        if self._fastpath_active:
            # Keep one connection open for the backend's lifetime so a
            # read-pool rotation can never become the last close and trigger
            # an uncontrolled close-checkpoint (belt-and-braces alongside
            # NO_CKPT_ON_CLOSE).
            self._anchor_connection = self._connect(query_only=True)
            from lib.storage_sidecar.shipper import WalShipper
            self._shipper = WalShipper(
                self._authority_path,
                self._fastpath_decision.shadow_dir,
                authority_uuid=self._authority_uuid,
                checkpoint_fn=self._checkpoint_for_shipper,
                checkpoint_deadline_fn=self._checkpoint_for_shipper,
                wal_budget_max_bytes=(
                    self.config.fastpath_wal_rebase_max_mib * 1024 ** 2),
            )
            self._shipper.start()
            self._writer.set_write_admission_hook(
                self._shipper.assert_write_admitted)
            self._writer._on_commit = self._shipper.notify_commit
            # The local half of the split-brain guard: reconcile() compares
            # this against the shadow manifest before trusting either side.
            fastpath.write_local_manifest(self._authority_path.parent, {
                'authority_uuid': self._authority_uuid,
                'shadow_dir': str(self._fastpath_decision.shadow_dir),
            })
        self._preflight = report.as_dict()
        try:
            from lib.storage_sidecar.turn_search_projection import (
                LocalSQLiteTurnSearchTarget,
                TurnSearchProjectionRuntime,
            )

            projection = TurnSearchProjectionRuntime(
                self,
                LocalSQLiteTurnSearchTarget(
                    # SidecarConfig.__post_init__ resolves the direct-constructor
                    # default; from_environment supplies a host-local path.
                    self.config.turn_search_projection_dir,
                    self.config.turn_search_projection_max_mib * 1024 * 1024,
                ),
                backfill_delay_s=self.config.turn_search_backfill_delay_s,
            )
            projection.start()
            self._turn_search_projection = projection
        except BaseException as exc:
            # This store is reconstructible. Its absence degrades search only;
            # it can never revoke authority readiness or roll back a user write.
            self._turn_search_projection_error = type(exc).__name__
            logger.exception(
                '[turn-search] local projection failed to start; '
                'conversation search is degraded')
        return self.health()

    def _checkpoint_for_shipper(self, deadline_at: float | None = None) -> None:
        """Run a TRUNCATE checkpoint on the writer thread (raw, no wrapping
        transaction) and raise if the WAL could not be fully checkpointed."""
        if self._writer is None:
            raise StorageError('database_unavailable', 'SQLite writer is not ready', True, 100)

        def op(session: SQLiteSession) -> None:
            row = session.fetch_one('PRAGMA wal_checkpoint(TRUNCATE)')
            busy = int(next(iter(row.values()))) if row else 1
            if busy:
                raise StorageError(
                    'database_busy', 'Fastpath checkpoint waited out readers', True, 100)

        checkpoint_deadline = time.monotonic() + 120.0
        if deadline_at is not None:
            checkpoint_deadline = min(checkpoint_deadline, float(deadline_at))
        self._writer.submit(
            op, 'maintenance', checkpoint_deadline,
            operation_name='fastpath.checkpoint', raw=True)

    def _acquire_read(self, deadline_at: float) -> _ReadSlot:
        timeout = max(0.0, min(
            self.config.acquire_timeout_s, deadline_at - time.monotonic()))
        try:
            slot = self._read_pool.get(timeout=timeout)
        except queue.Empty as exc:
            raise StorageError(
                'database_timeout', 'Storage read pool acquisition timed out', True, 25,
            ) from exc
        now = time.monotonic()
        if (now - slot.created_at >= self.config.max_lifetime_s
                or now - slot.last_used_at >= self.config.idle_lifetime_s):
            slot.connection.close()
            slot = _ReadSlot(self._connect(query_only=True), now, now)
            self._metrics['pool_rotations'] += 1
        return slot

    def query(
        self, operation_name: str, operation: Operation, deadline_at: float,
    ) -> Any:
        if operation_name == 'conversation.search':
            if self._turn_search_projection is None:
                raise StorageError(
                    'database_unavailable',
                    'Conversation search projection is unavailable',
                    True,
                    250,
                )
            return self._turn_search_projection.query(operation, deadline_at)
        slot = self._acquire_read(deadline_at)
        slot.connection.set_progress_handler(
            lambda: 1 if time.monotonic() >= deadline_at else 0, 1000)
        try:
            slot.connection.execute('BEGIN')
            result = operation(SQLiteSession(
                slot.connection, self._turn_projection_cache))
            slot.connection.rollback()
            self._metrics['queries'] += 1
            return result
        except BaseException as exc:
            try:
                slot.connection.rollback()
            except sqlite3.Error as rollback_error:
                logger.debug('SQLite read rollback failed: %s',
                             type(rollback_error).__name__)
            self._metrics['query_failures'] += 1
            raise _map_sqlite_error(exc, deadline_at) from exc
        finally:
            slot.connection.set_progress_handler(None, 0)
            slot.last_used_at = time.monotonic()
            self._read_pool.put(slot)

    def command(
        self,
        operation_name: str,
        payload_digest: str,
        command_id: str | None,
        priority: str,
        operation: Operation,
        deadline_at: float,
        *,
        receipt_required: bool,
        transaction_timeout_s: float | None = None,
    ) -> Any:
        if receipt_required and (
                not isinstance(command_id, str)
                or not command_id
                or len(command_id) > 200):
            raise StorageError(
                'database_protocol_error', 'A valid command_id is required')
        receipt_identity = (
            command_receipt_identity_v2(
                command_id, operation_name, payload_digest)
            if receipt_required else None
        )

        # The Python watchdog can interrupt SQLite VM opcodes, but it cannot
        # pre-empt a filesystem call already executing inside one
        # ``incremental_vacuum(1)`` statement.  On BeeGFS/NFS/FUSE that single
        # page move has exceeded the entire interactive writer budget.  Make
        # the capability decision before queueing anything on the sole writer;
        # the explicit offline compactor remains the safe whole-file path.
        if operation_name == 'system.reclaim':
            from lib.storage_sidecar.reclaim_policy import online_reclaim_allowed

            if not online_reclaim_allowed(self._authority_storage_class):
                return {
                    'reclaimed': 0,
                    'offline_required': True,
                    'reason_code': 'unsupported_storage_topology',
                    'reason': (
                        'automatic SQLite page relocation is disabled on '
                        f'{self._authority_storage_class}'
                    ),
                    'storage_class': self._authority_storage_class,
                    'filesystem_type': self._authority_filesystem_type,
                    'authority_path': str(self._authority_path),
                }

        def transactional(session: SQLiteSession) -> Any:
            if receipt_required:
                assert receipt_identity is not None
                command_key, request_digest = receipt_identity
                found, replay = decode_command_receipt_lookup(
                    session.fetch_all(
                        COMMAND_RECEIPT_LOOKUP_SQL,
                        (
                            operation_name, payload_digest, command_id,
                            operation_name, request_digest, command_key,
                        ),
                    )
                )
                if found:
                    return replay
            response = operation(session)
            if receipt_required and receipt_cacheable(response):
                assert receipt_identity is not None
                command_key, request_digest = receipt_identity
                encoded = encode_receipt_response(response)
                session.execute(
                    'INSERT INTO storage_command_receipts_v2('
                    'command_key, operation, request_digest, response_json, '
                    'committed_at_ms) '
                    'VALUES (?, ?, ?, ?, ?)',
                    (command_key, operation_name, request_digest, encoded,
                     int(time.time() * 1000)),
                )
            return response

        if self._writer is None:
            raise StorageError('database_unavailable', 'SQLite writer is not ready', True, 100)
        result = self._writer.submit(
            transactional,
            priority,
            deadline_at,
            operation_name=operation_name,
            transaction_timeout_s=transaction_timeout_s,
        )
        if self._turn_search_projection is not None:
            self._turn_search_projection.wake()
        return result

    def health(self) -> dict[str, Any]:
        result = {
            'ready': not self._closed and self._writer is not None,
            'backend': self.name,
            'protocol': 'storage.v1',
            'preflight': self._preflight,
        }
        if self._turn_search_projection is not None:
            result['turn_search_projection'] = (
                self._turn_search_projection.status())
        elif self._turn_search_projection_error:
            result['turn_search_projection'] = {
                'state': 'unavailable',
                'error_type': self._turn_search_projection_error,
            }
        return result

    def metrics(self) -> dict[str, Any]:
        writer = dict(self._writer.metrics) if self._writer else {}
        if self._writer is not None:
            writer['queue_capacity'] = self.config.sqlite_writer_queue_capacity
            current = self._writer.current_job()
            if current is not None:
                now = time.monotonic()
                current['held_s'] = round(now - current['started_at'], 1)
                current['phase_held_s'] = round(
                    now - current.get('phase_started_at', now), 1)
                current.pop('started_at', None)
                current.pop('deadline_at', None)
                current.pop('phase_started_at', None)
                current.pop('interrupted_at', None)
            writer['current'] = current
            writer['last_stall'] = self._writer.last_stall()
            writer['queue_depths'] = self._writer.queue_depths()
            writer['commit_latency'] = self._writer.commit_latency_stats()
        fastpath_status = dict(self._preflight_fastpath)
        if self._shipper is not None:
            fastpath_status['shipper'] = self._shipper.status()
        return {
            'backend': self.name,
            'sqlite_version': sqlite3.sqlite_version,
            'read_pool_size': self._read_pool.qsize(),
            'read_pool_capacity': self.config.read_pool_size,
            'writer_cache_mib': self.config.sqlite_writer_cache_mib,
            'turn_projection_cache': self._turn_projection_cache.stats(),
            'writer_watchdog': (
                self._writer.watchdog_policy()
                if self._writer is not None else {
                    'stall_grace_s': self.config.writer_stall_grace_s,
                    'hard_kill_s': self.config.writer_hard_kill_s,
                }),
            'queries': dict(self._metrics),
            'writer': writer,
            'fastpath': fastpath_status,
            'deferred_schema': {
                'automatic_build': 'fresh-authority-only',
                'missing_indexes': list(self._deferred_schema_missing),
            },
            'turn_search_projection': (
                self._turn_search_projection.status()
                if self._turn_search_projection is not None else {
                    'state': 'unavailable',
                    'error_type': self._turn_search_projection_error,
                }
            ),
        }

    def integrity_check(self, deadline_at: float) -> dict[str, Any]:
        def check(session: SQLiteSession):
            row = session.fetch_one('PRAGMA integrity_check')
            # PRAGMA result column name is driver-defined; use the sole value.
            value = next(iter(row.values())) if row else ''
            return {'ok': value == 'ok', 'result': value}

        return self.query('system.integrity_check', check, deadline_at)

    def backup(self, deadline_at: float) -> dict[str, Any]:
        if not self._backup_lock.acquire(blocking=False):
            raise StorageError(
                'database_busy', 'A SQLite backup is already running', True, 1000)
        try:
            return self._backup_locked(deadline_at)
        finally:
            self._backup_lock.release()

    def _backup_locked(self, deadline_at: float) -> dict[str, Any]:
        backups = self.config.data_dir / 'backups'
        backups.mkdir(parents=True, exist_ok=True)
        reclaimed = reclaim_stale_job_artifacts(backups)
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        target = backups / f'storage-sqlite-{stamp}-{uuid.uuid4().hex[:8]}.sqlite3'
        temporary = backups / f'.{target.name}.tmp-{uuid.uuid4().hex}'
        if target.exists():
            raise StorageError('database_conflict', 'Backup target already exists')
        if self._shipper is not None:
            return self._backup_fastpath_locked(
                backups,
                target,
                temporary,
                reclaimed=reclaimed,
                deadline_at=deadline_at,
            )
        slot = self._acquire_read(deadline_at)
        destination = None
        try:
            page_count = int(slot.connection.execute('PRAGMA page_count').fetchone()[0])
            page_size = int(slot.connection.execute('PRAGMA page_size').fetchone()[0])
            capacity = capacity_preflight(backups, page_count * page_size)
            write_job_manifest(
                temporary,
                source=self._authority_path,
                state='copying',
                extra={'estimated_bytes': capacity['estimated_bytes']},
            )
            destination = sqlite3.connect(temporary, isolation_level=None)
            def progress(_status, remaining, total):
                if time.monotonic() >= deadline_at:
                    raise StorageError(
                        'database_timeout', 'SQLite backup deadline expired', True, 100)
                if remaining == 0:
                    write_job_manifest(
                        temporary,
                        source=self._authority_path,
                        state='verifying',
                        extra={
                            'estimated_bytes': capacity['estimated_bytes'],
                            'total_pages': int(total),
                        },
                    )

            slot.connection.backup(
                destination, pages=4096, progress=progress, sleep=0.01)
            result = destination.execute('PRAGMA integrity_check').fetchone()[0]
            if result != 'ok':
                raise StorageError('database_integrity', 'SQLite backup verification failed')
            destination.close()
            destination = None
            fsync_file(temporary)
            size = temporary.stat().st_size
            checksum = sha256_file(temporary, deadline_at)
            os.replace(temporary, target)
            fsync_directory(backups)
            manifest = {
                'format': 'tofu.storage-backup.v1',
                'backend': self.name,
                'created_at': datetime.now(timezone.utc).isoformat(),
                'artifact': target.name,
                'bytes': size,
                'sha256': checksum,
                'integrity': 'ok',
            }
            manifest_path = target.with_name(target.name + '.manifest.json')
            write_json_durable(manifest_path, manifest)
            pruned = prune_verified_backups(backups, preserve=target)
            return {
                'ok': True,
                'backup': str(target.relative_to(self.config.project_root)),
                'manifest': str(manifest_path.relative_to(self.config.project_root)),
                'bytes': size,
                'sha256': checksum,
                'estimated_bytes': capacity['estimated_bytes'],
                'recovery_copy_budget_bytes': capacity[
                    'recovery_copy_budget_bytes'],
                'retained_recovery_bytes': capacity[
                    'retained_recovery_bytes'],
                'projected_recovery_bytes': capacity[
                    'projected_recovery_bytes'],
                'same_volume_rollback_bytes': capacity[
                    'same_volume_rollback_bytes'],
                'reclaimed_temp_artifacts': reclaimed,
                'pruned': pruned,
            }
        except BaseException:
            cleanup_job_artifacts(temporary)
            if target.exists() and not target.with_name(
                    target.name + '.manifest.json').exists():
                target.unlink(missing_ok=True)
            raise
        finally:
            if destination is not None:
                destination.close()
            slot.last_used_at = time.monotonic()
            self._read_pool.put(slot)
            job_manifest_path(temporary).unlink(missing_ok=True)

    def _backup_fastpath_locked(
        self,
        backups: Path,
        target: Path,
        temporary: Path,
        *,
        reclaimed: int,
        deadline_at: float,
    ) -> dict[str, Any]:
        """Back up a fastpath front through one stable shipper generation."""
        estimated_bytes = self._authority_path.stat().st_size
        capacity = capacity_preflight(
            backups,
            estimated_bytes,
            allow_verified_rotation=True,
        )
        budget_rotation_required = bool(
            capacity['budget_rotation_required'])
        retire_verified_artifacts = {
            str(name) for name in capacity['retire_verified_artifacts']
        }
        if budget_rotation_required:
            logger.info(
                '[backup] recovery-copy peak exceeds budget; publishing one '
                'verified hard-link replacement before retiring %d old backup(s)',
                len(retire_verified_artifacts),
            )
        write_job_manifest(
            temporary,
            source=self._authority_path,
            state='copying',
            extra={
                'estimated_bytes': capacity['estimated_bytes'],
                'budget_rotation_required': budget_rotation_required,
            },
        )
        published = False
        try:
            try:
                if budget_rotation_required:
                    pinned = self._shipper.pin_checkpointed_snapshot_for_backup(
                        temporary,
                        deadline_at=deadline_at,
                        require_hardlink=True,
                    )
                else:
                    pinned = self._shipper.pin_checkpointed_snapshot_for_backup(
                        temporary,
                        deadline_at=deadline_at,
                    )
            except TimeoutError as exc:
                raise StorageError(
                    'database_timeout', str(exc), True, 100) from exc
            write_job_manifest(
                temporary,
                source=self._authority_path,
                state='verifying',
                extra={
                    'estimated_bytes': capacity['estimated_bytes'],
                    'snapshot_generation': pinned['generation'],
                    'copy_strategy': pinned['copy_strategy'],
                    'recovery_point_at': pinned['recovery_point_at'],
                },
            )
            _verify_readonly_backup(temporary, deadline_at)
            fsync_file(temporary)
            size = temporary.stat().st_size
            checksum = sha256_file(temporary, deadline_at)
            recovery_point_at = datetime.fromtimestamp(
                float(pinned['recovery_point_at']),
                tz=timezone.utc,
            ).isoformat()
            os.replace(temporary, target)
            fsync_directory(backups)
            manifest = {
                'format': 'tofu.storage-backup.v1',
                'backend': self.name,
                'created_at': datetime.now(timezone.utc).isoformat(),
                'artifact': target.name,
                'bytes': size,
                'sha256': checksum,
                'integrity': 'ok',
                'source_mode': 'fastpath-checkpointed-shadow',
                'snapshot_generation': pinned['generation'],
                'copy_strategy': pinned['copy_strategy'],
                'recovery_point_at': recovery_point_at,
            }
            manifest_path = target.with_name(target.name + '.manifest.json')
            write_json_durable(manifest_path, manifest)
            published = True
            pruned = prune_verified_backups(
                backups,
                preserve=target,
                retire_names=retire_verified_artifacts,
            )
            return {
                'ok': True,
                'backup': str(target.relative_to(self.config.project_root)),
                'manifest': str(
                    manifest_path.relative_to(self.config.project_root)),
                'bytes': size,
                'sha256': checksum,
                'estimated_bytes': capacity['estimated_bytes'],
                'recovery_copy_budget_bytes': capacity[
                    'recovery_copy_budget_bytes'],
                'retained_recovery_bytes': capacity[
                    'retained_recovery_bytes'],
                'projected_recovery_bytes': capacity[
                    'projected_recovery_bytes'],
                'peak_projected_recovery_bytes': capacity[
                    'peak_projected_recovery_bytes'],
                'same_volume_rollback_bytes': capacity[
                    'same_volume_rollback_bytes'],
                'budget_rotation_required': budget_rotation_required,
                'budget_retired_backups': len(retire_verified_artifacts),
                'reclaimed_temp_artifacts': reclaimed,
                'pruned': pruned,
                'source_mode': manifest['source_mode'],
                'snapshot_generation': pinned['generation'],
                'copy_strategy': pinned['copy_strategy'],
                'recovery_point_at': recovery_point_at,
            }
        finally:
            if not published:
                cleanup_job_artifacts(temporary)
                if target.exists() and not target.with_name(
                        target.name + '.manifest.json').exists():
                    target.unlink(missing_ok=True)
            job_manifest_path(temporary).unlink(missing_ok=True)

    def baseline(self, deadline_at: float) -> dict[str, Any]:
        def collect(session: SQLiteSession):
            rows = session.fetch_all(
                "SELECT name, type, sql FROM sqlite_master "
                "WHERE type IN ('table', 'index') AND name NOT LIKE 'sqlite_%' "
                'ORDER BY type, name')
            tables = []
            for row in rows:
                if row['type'] != 'table':
                    continue
                if time.monotonic() >= deadline_at:
                    raise StorageError(
                        'database_timeout', 'Storage baseline deadline expired',
                        True, 100)
                identifier = str(row['name']).replace('"', '""')
                count = session.fetch_one(
                    f'SELECT COUNT(*) AS count FROM "{identifier}"')
                tables.append({'name': row['name'], 'rows': int(count['count'])})
            version = session.fetch_one(
                'SELECT meta_value FROM storage_meta WHERE meta_key = ?',
                ('schema_version',))
            return {
                'backend': self.name,
                'schema_version': int(version['meta_value']) if version else None,
                'tables': tables,
                'indexes': [row['name'] for row in rows if row['type'] == 'index'],
            }

        result = self.query('system.baseline', collect, deadline_at)
        result['database_bytes'] = self._authority_path.stat().st_size
        return result

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._turn_search_projection is not None:
            self._turn_search_projection.close()
            self._turn_search_projection = None
        self._turn_projection_cache.clear()
        if self._writer is not None:
            self._writer.close()
            self._writer = None
        if self._shipper is not None:
            # After the writer drains, the WAL tail is stable — ship it so a
            # graceful stop leaves a fully-current shadow.
            self._shipper.stop()
            self._shipper = None
        while not self._read_pool.empty():
            self._read_pool.get_nowait().connection.close()
        if self._anchor_connection is not None:
            try:
                self._anchor_connection.close()
            except sqlite3.Error as exc:
                logger.debug('SQLite anchor connection close failed: %s', exc)
            self._anchor_connection = None


__all__ = ['SQLiteBackend', 'SQLiteSession']
