"""Demand-driven execution of canonical leased jobs.

Entry point: DurableWorkerService; policy and handlers are injected by each domain. One coordinator starts every claimed job
on a demand executor; ``max_workers`` only sizes idle thread reuse and never
caps concurrent jobs. Claims are event-driven: a read-only
``worker_job.claim_horizon`` probe precedes every exclusive
``worker_job.claim_next``, and the coordinator sleeps until that horizon, a
committed in-process producer command or a settled claim. Row principals,
leases, cancellation and recovery remain with the existing worker/repository
authorities. No per-rule thread, new queue, idle poll or polling receipt.
"""
from dataclasses import dataclass, field
import threading
import time
import uuid
from typing import assert_never

from lib.durable_worker import StorageWorkerJobStore, SynchronousWorkerRunner
from lib.identity import PrincipalContext
from lib.log import get_logger
from lib.storage.commit_events import subscribe_committed_operations
from tofu_workers.executor import DemandThreadPoolExecutor

logger = get_logger(__name__)


@dataclass
class _ClaimObservation:
    known: threading.Event = field(default_factory=threading.Event)
    claimed: bool = False
    claimable_in_ms: int | None = None


class _ObservedJobStore(StorageWorkerJobStore):
    def __init__(self, factory, observation, wake):
        super().__init__(client_factory=factory)
        self._claim_started = False
        self._observation = observation
        self._wake = wake

    def claim_next(self, **arguments):
        # The read path answers an empty or not-yet-due queue. Only a horizon
        # of 0 enters the exclusive writer, whose claim re-validates everything.
        delay = 0 if self._claim_started else self.claim_horizon(task_kinds=arguments['task_kinds'])
        if delay == 0:
            # Keep one exact acquisition through a lost ACK. Re-probing the
            # horizon could hide the already committed, process-owned claim.
            self._claim_started = True
            document = super().claim_next(**arguments)
            self._claim_started = False
        else:
            document = None
        # This is a concurrency hint; only the runner's owned fence proof can
        # authorize dispatch. A lost acknowledgement never becomes execution.
        self._observation.claimed = document is not None
        self._observation.claimable_in_ms = delay
        self._observation.known.set()
        self._wake.set()
        return document


def _next_probe(observation, now, policy):
    """Monotonic time of the next probe after one resolved claim, or None."""
    if not observation.known.is_set() or observation.claimed:
        # A failed probe retries after its backoff; a claim may have queued
        # successors at the same horizon.
        return now
    delay = observation.claimable_in_ms
    if delay is None:
        return None
    if delay == 0:
        # Another claimer won the job the horizon reported.
        return now + policy['failureBackoffMilliseconds'] / 1000
    # Monotonic waits do not advance across host suspend or follow UTC steps.
    return now + min(delay, policy['horizonReanchorMilliseconds']) / 1000


class DurableWorkerService:
    def __init__(self, *, principal, client_factory, registrations, max_workers, now_ms, policy, wake_source=None):
        self._wake_source = wake_source
        self._policy = policy = dict(policy)
        if (not isinstance(principal, PrincipalContext) or principal.kind != 'system'
                or principal.owner_user_id is not None or principal.tenant_id is not None
                or principal.scopes != frozenset({policy['controlScope']})):
            raise PermissionError('Durable worker service requires an ownerless worker control principal')
        if type(max_workers) is not int or max_workers < 1:
            raise ValueError('Durable worker idle worker hint must be a positive integer')
        self.principal = principal
        self.max_workers = max_workers
        self._factory, self._now_ms = client_factory, now_ms
        self._registrations = tuple(registrations)
        from lib.storage.worker_process import worker_process_id
        self._worker_prefix = worker_process_id(uuid.uuid4().hex,
            legacy_prefix=policy['threadNamePrefix']).rsplit(':', 1)[0] + ':'
        self._stop = threading.Event()
        self._wake = threading.Event()
        # Set by a committed producer command; cleared just before a probe is
        # submitted, so a commit racing that probe forces one more probe.
        self._claim_hint = threading.Event()
        self._lock = threading.Lock()
        self._thread = None
        self._state = 'new'
        self._active = self._high_water = 0
        # Admission has no I/O or thread effects and rejects an empty/unsafe
        # catalogue before startup can acknowledge a service.
        self._runner(_ClaimObservation()).require_claimable_task_kinds()

    def _runner(self, observation):
        policy = self._policy
        return SynchronousWorkerRunner(
            # Simultaneous acquisitions must never share a receipt identity,
            # even when the application clock has the same millisecond.
            worker_id=self._worker_prefix + uuid.uuid4().hex,
            store=_ObservedJobStore(self._factory, observation, self._wake),
            registrations=self._registrations, now_ms=self._now_ms,
            lease_ms=policy['workerLeaseMilliseconds'],
            heartbeat_interval_ms=policy['heartbeatIntervalMilliseconds'],
            control_signals=self._wake_source.signals if self._wake_source else None)

    def snapshot(self):
        with self._lock:
            return {'state': self._state, 'idleWorkers': self.max_workers,
                    'inflight': self._active, 'highWater': self._high_water}

    def start(self):
        with self._lock:
            if self._state == 'running':
                return False
            if self._state != 'new':
                raise RuntimeError('A stopped durable worker service cannot restart')
            thread = threading.Thread(target=self._coordinate,
                name=self._policy['threadNamePrefix'] + '-coordinator', daemon=True)
            self._thread, self._state = thread, 'running'
            try:
                thread.start()
            except RuntimeError:
                self._thread, self._state = None, 'new'
                raise
            return True

    def stop(self, *, timeout=None):
        with self._lock:
            if self._state == 'new':
                self._state = 'stopped'
            elif self._state == 'running':
                self._state = 'stopping'
            thread = self._thread
            self._stop.set()
            self._wake.set()
        if self._wake_source is not None:
            self._wake_source.interrupt()
        if thread is not None:
            if thread is threading.current_thread():
                raise RuntimeError('Durable worker service cannot join its coordinator from itself')
            thread.join(timeout)
            return not thread.is_alive()
        return True

    def _hint(self, _operation):
        self._claim_hint.set()
        self._wake.set()

    def _execute(self, observation):
        return self._runner(observation).run_once(shutdown_event=self._stop)

    def _finished(self, futures, now):
        """Reap done futures; return (backoff deadline, whether a claim settled)."""
        backoff_until, settled = now, False
        for future in tuple(futures):
            if not future.done():
                continue
            observation = futures.pop(future)
            try:
                result = future.result()
            except Exception as exc:
                # A claim/transport failure occurs before a handler result.
                # The durable lease remains recoverable; never fake settlement.
                logger.warning('[DurableWorker] worker execution unavailable: %s', type(exc).__name__)
                backoff_until = now + self._policy['failureBackoffMilliseconds'] / 1000
                settled = True
                continue
            settled = settled or observation.claimed
            match result.state:
                case 'succeeded' | 'failed' | 'cancelled' | 'suspended' | 'idle':
                    pass
                case 'lease_lost' | 'protocol_error' | 'terminal_unconfirmed' | 'suspension_unconfirmed' | 'settlement_unconfirmed':
                    logger.warning('[DurableWorker] worker outcome requires recovery: %s task=%s detail=%s',
                                   result.state, result.task_id or '-', result.detail or '-')
                    backoff_until = now + self._policy['failureBackoffMilliseconds'] / 1000
                case _:
                    assert_never(result.state)
        return backoff_until, settled

    def _coordinate(self):
        policy = self._policy
        futures, pending = {}, None
        due, backoff_until = 0.0, 0.0
        failed = False
        unsubscribe = lambda: None
        try:
            unsubscribe = (self._wake_source.start(self._hint) if self._wake_source else
                subscribe_committed_operations(policy['claimWakeOperations'], self._hint))
            with DemandThreadPoolExecutor(max_workers=self.max_workers,
                    thread_name_prefix=policy['threadNamePrefix']) as executor:
                try:
                    while not self._stop.is_set():
                        self._wake.clear()
                        now = time.monotonic()
                        reaped_until, settled = self._finished(futures, now)
                        backoff_until = max(backoff_until, reaped_until)
                        if pending is not None and (pending.known.is_set() or
                                not any(value is pending for value in futures.values())):
                            due, pending = _next_probe(pending, now, policy), None
                        if settled:
                            due = now
                        if (pending is None and now >= backoff_until
                                and (self._claim_hint.is_set() or (due is not None and now >= due))):
                            self._claim_hint.clear()
                            pending = _ClaimObservation()
                            future = executor.submit(self._execute, pending)
                            futures[future] = pending
                            future.add_done_callback(lambda _: self._wake.set())
                        with self._lock:
                            self._active = len(futures)
                            self._high_water = max(self._high_water, self._active)
                        wait = None
                        if pending is None:
                            target = now if self._claim_hint.is_set() else due
                            if target is not None:
                                wait = max(0.0, max(target, backoff_until) - time.monotonic())
                        self._wake.wait(wait)
                finally:
                    self._stop.set()
                    # Contexts revoke dispatch cooperatively. Joining the
                    # executor precedes storage shutdown; no detached effect.
                    executor.shutdown(wait=True, cancel_futures=True)
        except Exception:
            failed = True
            logger.exception('[DurableWorker] coordinator failed')
        finally:
            unsubscribe()
            with self._lock:
                self._active = 0
                self._state = 'failed' if failed else 'stopped'
