"""Demand-driven execution of canonical leased automation jobs.

Entry point: AutomationWorkerService. One coordinator bounds executor submissions
by the launch-probed task capacity; a single speculative claim discovers idle
work. Row principals, leases, cancellation and recovery remain with the existing
worker/repository authorities. No per-rule thread, new queue or polling receipt.
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import threading
import time
import uuid
from typing import assert_never

from lib.durable_worker import StorageWorkerJobStore, SynchronousWorkerRunner
from lib.identity import PrincipalContext
from lib.log import get_logger
from runtime_guards import MAX_TASK_CONCURRENCY
from .runtime_policy import runtime_policy

logger = get_logger(__name__)


@dataclass
class _ClaimObservation:
    known: threading.Event = field(default_factory=threading.Event)
    claimed: bool = False


class _ObservedJobStore(StorageWorkerJobStore):
    def __init__(self, factory, observation, wake):
        super().__init__(client_factory=factory, retain_claim_receipts=False)
        self._observation = observation
        self._wake = wake

    def claim_next(self, **arguments):
        document = super().claim_next(**arguments)
        # This is a concurrency hint; only the runner's owned fence proof can
        # authorize dispatch. A lost acknowledgement never becomes execution.
        self._observation.claimed = document is not None
        self._observation.known.set()
        self._wake.set()
        return document


class AutomationWorkerService:
    def __init__(self, *, principal, client_factory, registrations, max_workers, now_ms):
        policy = runtime_policy()
        if (not isinstance(principal, PrincipalContext) or principal.kind != 'system'
                or principal.owner_user_id is not None or principal.tenant_id is not None
                or principal.scopes != frozenset({policy['controlScope']})):
            raise PermissionError('Automation service requires an ownerless worker control principal')
        if type(max_workers) is not int or not 1 <= max_workers <= MAX_TASK_CONCURRENCY:
            raise ValueError('Automation capacity must be an admitted task-worker budget')
        self.principal = principal
        self.max_workers = max_workers
        self._factory, self._now_ms = client_factory, now_ms
        self._registrations = tuple(registrations)
        self._instance = uuid.uuid4().hex
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._thread = None
        self._state = 'new'
        self._active = self._high_water = 0
        # Admission has no I/O or thread effects and rejects an empty/unsafe
        # catalogue before startup can acknowledge a service.
        self._runner(_ClaimObservation()).require_claimable_task_kinds()

    def _runner(self, observation):
        policy = runtime_policy()
        return SynchronousWorkerRunner(
            worker_id='automation:' + self._instance + ':' + uuid.uuid4().hex,
            store=_ObservedJobStore(self._factory, observation, self._wake),
            registrations=self._registrations, now_ms=self._now_ms,
            lease_ms=policy['workerLeaseMilliseconds'],
            heartbeat_interval_ms=policy['heartbeatIntervalMilliseconds'])

    def snapshot(self):
        with self._lock:
            return {'state': self._state, 'capacity': self.max_workers,
                    'inflight': self._active, 'highWater': self._high_water}

    def start(self):
        with self._lock:
            if self._state == 'running':
                return False
            if self._state != 'new':
                raise RuntimeError('A stopped automation service cannot restart')
            thread = threading.Thread(target=self._coordinate,
                name=runtime_policy()['threadNamePrefix'] + '-coordinator', daemon=True)
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
        if thread is not None:
            if thread is threading.current_thread():
                raise RuntimeError('Automation service cannot join its coordinator from itself')
            thread.join(timeout)
            return not thread.is_alive()
        return True

    def _execute(self, observation):
        return self._runner(observation).run_once(shutdown_event=self._stop)

    def _finished(self, futures, now):
        backoff_until = now
        for future in tuple(futures):
            if not future.done():
                continue
            futures.pop(future)
            try:
                result = future.result()
            except Exception as exc:
                # A claim/transport failure occurs before a handler result.
                # The durable lease remains recoverable; never fake settlement.
                logger.warning('[Automation] worker execution unavailable: %s', type(exc).__name__)
                backoff_until = now + runtime_policy()['failureBackoffMilliseconds'] / 1000
                continue
            match result.state:
                case 'succeeded' | 'failed' | 'cancelled' | 'suspended' | 'idle':
                    pass
                case 'lease_lost' | 'protocol_error' | 'terminal_unconfirmed' | 'suspension_unconfirmed' | 'settlement_unconfirmed':
                    logger.warning('[Automation] worker outcome requires recovery: %s', result.state)
                    backoff_until = now + runtime_policy()['failureBackoffMilliseconds'] / 1000
                case _:
                    assert_never(result.state)
        return backoff_until

    def _coordinate(self):
        policy = runtime_policy()
        futures, pending, idle_until = {}, None, 0.0
        failed = False
        try:
            with ThreadPoolExecutor(max_workers=self.max_workers,
                    thread_name_prefix=policy['threadNamePrefix']) as executor:
                try:
                    while not self._stop.is_set():
                        self._wake.clear()
                        now = time.monotonic()
                        idle_until = max(idle_until, self._finished(futures, now))
                        if pending is not None and (pending.known.is_set() or
                                not any(value is pending for value in futures.values())):
                            if not pending.claimed:
                                idle_until = max(idle_until, now + policy['idlePollMilliseconds'] / 1000)
                            pending = None
                        if pending is None and len(futures) < self.max_workers and now >= idle_until:
                            pending = _ClaimObservation()
                            future = executor.submit(self._execute, pending)
                            futures[future] = pending
                            future.add_done_callback(lambda _: self._wake.set())
                        with self._lock:
                            self._active = len(futures)
                            self._high_water = max(self._high_water, self._active)
                        wait = max(0.0, idle_until - time.monotonic()) if idle_until > now else None
                        self._wake.wait(wait)
                finally:
                    self._stop.set()
                    # Contexts revoke dispatch cooperatively. Joining the
                    # executor precedes storage shutdown; no detached effect.
                    executor.shutdown(wait=True, cancel_futures=True)
        except Exception:
            failed = True
            logger.exception('[Automation] coordinator failed')
        finally:
            with self._lock:
                self._active = 0
                self._state = 'failed' if failed else 'stopped'
