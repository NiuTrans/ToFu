"""Parked demand carriers wake only for a job they can take or a retirement."""
import _thread
import collections
import threading
import time
from concurrent.futures import wait

import pytest

from tofu_workers import DemandExecutor
from tofu_workers import executor as kernel

pytestmark = pytest.mark.unit


class _ParkingCounter:
    """Count carriers blocked on their private slot and every wake-up from it."""

    def __init__(self):
        self._lock = threading.Lock()
        self.parked = 0
        self.wakeups = 0

    def instrument(self, monkeypatch):
        park = kernel._Carrier.park

        def counted_park(carrier):
            with self._lock:
                self.parked += 1
            try:
                return park(carrier)
            finally:
                with self._lock:
                    self.parked -= 1
                    self.wakeups += 1

        monkeypatch.setattr(kernel._Carrier, 'park', counted_park)


def _instrumented_pool(monkeypatch, **options):
    counter = _ParkingCounter()
    counter.instrument(monkeypatch)
    pool = DemandExecutor(queue_capacity=0, max_abandoned_workers=1, **options)
    return pool, counter


def _parked(counter):
    return counter.parked


def _wakeups(counter):
    return counter.wakeups


def _await(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, 'executor did not reach the expected state'
        time.sleep(0.001)


def _park_carriers(pool, conditions, count):
    rendezvous = threading.Barrier(count + 1)
    warm = [pool.submit(rendezvous.wait, 5) for _ in range(count)]
    rendezvous.wait(5)
    for future in warm:
        future.result(5)
    _await(lambda: _parked(conditions) == count)


def test_submission_wakes_one_parked_carrier_and_completion_wakes_none(monkeypatch):
    idle = 64
    pool, conditions = _instrumented_pool(
        monkeypatch, max_workers=idle, thread_name_prefix='targeted-wake')
    try:
        _park_carriers(pool, conditions, idle)
        before = _wakeups(conditions)
        assert pool.submit(lambda: 'one').result(5) == 'one'
        _await(lambda: _parked(conditions) == idle)
        woken = _wakeups(conditions) - before
        print({'parked_carriers': idle, 'wakeups_per_submission': woken})
        assert woken == 1
        assert pool.scheduling_snapshot()['residentThreads'] == idle
    finally:
        pool.shutdown(wait=True)


def test_resize_retires_parked_surplus_with_one_wakeup_each(monkeypatch):
    idle = 16
    pool, conditions = _instrumented_pool(
        monkeypatch, max_workers=idle, thread_name_prefix='targeted-retire')
    try:
        _park_carriers(pool, conditions, idle)
        before = _wakeups(conditions)
        pool.resize(1)
        _await(lambda: pool.scheduling_snapshot()['residentThreads'] == 1)
        assert _parked(conditions) == 1
        assert _wakeups(conditions) - before == idle - 1
        assert pool.submit(lambda: 'kept').result(5) == 'kept'
    finally:
        pool.shutdown(wait=True)


def test_spare_carrier_admission_does_not_wait_for_another_physical_start(monkeypatch):
    prefix = 'spare-carrier'
    pool, conditions = _instrumented_pool(
        monkeypatch, max_workers=8, thread_name_prefix=prefix)
    release_two, release_last = threading.Event(), threading.Event()
    start_entered, start_release = threading.Event(), threading.Event()
    stalled, admitted = [], threading.Event()
    real_start = threading.Thread.start

    def controlled_start(thread):
        if thread.name == f'{prefix}_3':
            start_entered.set()
            start_release.wait(5)
        return real_start(thread)

    try:
        _park_carriers(pool, conditions, 3)
        busy = [pool.submit(release_two.wait, 5) for _ in range(2)]
        busy.append(pool.submit(release_last.wait, 5))
        _await(lambda: pool.scheduling_snapshot()['active'] == 3)
        monkeypatch.setattr(threading.Thread, 'start', controlled_start)
        submitter = threading.Thread(
            target=lambda: stalled.append(pool.submit(lambda: 'stalled')))
        submitter.start()
        assert start_entered.wait(2), 'fourth job must need a physical start'
        release_two.set()
        _await(lambda: _parked(conditions) == 2)

        def admit():
            if pool.submit(lambda: 'spare').result(5) == 'spare':
                admitted.set()
        spare = threading.Thread(target=admit)
        spare.start()
        assert admitted.wait(1), 'spare carrier waited behind a stalled Thread.start()'
        assert not start_release.is_set()
    finally:
        start_release.set()
        release_two.set()
        release_last.set()
        submitter.join(5)
        if 'spare' in locals():
            spare.join(5)
        pool.shutdown(wait=True)
    assert stalled[0].result(1) == 'stalled'
    assert all(future.result(1) for future in busy)


class _CountingRLock:
    """Real RLock that counts acquisitions per thread, Condition re-entry included."""

    def __init__(self):
        self._lock = _thread.RLock()
        self.acquisitions = collections.Counter()

    def acquire(self, blocking=True, timeout=-1):
        acquired = self._lock.acquire(blocking, timeout)
        if acquired:
            self.acquisitions[threading.get_ident()] += 1
        return acquired

    def release(self):
        self._lock.release()

    __enter__ = acquire

    def __exit__(self, *exc_info):
        self.release()

    def _is_owned(self):
        return self._lock._is_owned()

    def _release_save(self):
        return self._lock._release_save()

    def _acquire_restore(self, state):
        self._lock._acquire_restore(state)
        self.acquisitions[threading.get_ident()] += 1


def _carrier_idle(pool):
    # Due only after the carrier's post-callback hold, which also parks it.
    return pool.idle_retirement_snapshot(1e-9)['due']


def test_parked_carrier_runs_and_resolves_its_job_while_the_scheduler_lock_is_held():
    pool = DemandExecutor(max_workers=1, queue_capacity=0, max_abandoned_workers=1,
                          thread_name_prefix='direct-handoff')
    try:
        assert pool.submit(lambda: 'warm').result(5) == 'warm'
        _await(lambda: _carrier_idle(pool))
        ran = threading.Event()
        # Held across the submission (the lock is re-entrant), so the carrier
        # can never win it between admission and claim.
        with pool._lifecycle_lock:
            future = pool.submit(ran.set)
            assert ran.wait(2), 'the woken carrier needed the scheduler lock to claim its job'
            assert future.result(2) is None
        _await(lambda: _carrier_idle(pool))
        assert pool.resource_snapshot()['completed'] == 2
    finally:
        pool.shutdown(wait=True)


def test_parked_round_trip_takes_one_scheduler_hold_per_side(monkeypatch):
    # Every executor lock is created here; Futures made later keep real locks.
    locks = []

    def counting_rlock():
        locks.append(_CountingRLock())
        return locks[-1]

    def holds():
        total = collections.Counter()
        for lock in locks:
            total.update(lock.acquisitions)
        return total

    with monkeypatch.context() as construction:
        construction.setattr(threading, 'RLock', counting_rlock)
        pool = DemandExecutor(max_workers=1, queue_capacity=0, max_abandoned_workers=1,
                              thread_name_prefix='lock-budget')
    submitter, jobs, submit_holds = threading.get_ident(), 200, 0
    try:
        assert pool.submit(lambda: 'warm').result(5) == 'warm'
        _await(lambda: _carrier_idle(pool))
        baseline = holds()
        for _ in range(jobs):
            before = holds()[submitter]
            assert pool.submit(int).result(5) == 0
            submit_holds += holds()[submitter] - before
            _await(lambda: _carrier_idle(pool))
        carrier_holds = sum(count - baseline[ident] for ident, count in holds().items()
                            if ident != submitter)
        print({'submitter_holds_per_job': submit_holds / jobs,
               'carrier_holds_per_job': carrier_holds / jobs})
        assert submit_holds + carrier_holds <= 2 * jobs
        assert pool.resource_snapshot()['completed'] == jobs + 1
    finally:
        pool.shutdown(wait=True)


def test_free_carrier_takes_back_a_hand_off_its_parked_carrier_has_not_picked_up(monkeypatch):
    # The gate stands in for a woken carrier still waiting for the GIL: its
    # job sits in its inbox until a carrier that is free right now runs it.
    pool = DemandExecutor(max_workers=2, queue_capacity=0, max_abandoned_workers=1,
                          thread_name_prefix='reclaim')
    park = kernel._Carrier.park
    armed, reached, gate = threading.Event(), threading.Event(), threading.Event()
    delayed = []

    def delayed_park(carrier):
        if armed.is_set() and not delayed:
            delayed.append(carrier.thread.name)
            reached.set()
            gate.wait(5)
        return park(carrier)

    monkeypatch.setattr(kernel._Carrier, 'park', delayed_park)
    busy_started, release_busy = threading.Event(), threading.Event()

    def occupy():
        busy_started.set()
        release_busy.wait(5)
        return threading.current_thread().name

    try:
        rendezvous = threading.Barrier(3)
        warm = [pool.submit(rendezvous.wait, 5) for _ in range(2)]
        rendezvous.wait(5)
        for future in warm:
            future.result(5)
        _await(lambda: _carrier_idle(pool))
        busy = pool.submit(occupy)
        # Picked up, so the next hold cannot take it back from its carrier.
        assert busy_started.wait(5)
        armed.set()
        assert pool.submit(int).result(5) == 0
        assert reached.wait(5), 'the other carrier never parked again'
        handed = pool.submit(lambda: threading.current_thread().name)
        release_busy.set()
        busy_name = busy.result(5)
        assert handed.result(5) == busy_name != delayed[0]
        gate.set()
        _await(lambda: _carrier_idle(pool))
        snapshot = pool.scheduling_snapshot()
        assert snapshot['residentThreads'] == 2 and snapshot['active'] == 0
        assert pool.resource_snapshot()['completed'] == 5
    finally:
        release_busy.set()
        gate.set()
        pool.shutdown(wait=True)


def test_carrier_whose_hand_off_was_taken_back_runs_outstanding_work_when_it_wakes(monkeypatch):
    # Gates stand in for woken carriers still waiting for the GIL. A carrier
    # whose hand-off was taken back is already awake: once it runs, it takes
    # a hand-off still waiting for its own carrier instead of parking again.
    pool = DemandExecutor(max_workers=3, queue_capacity=0, max_abandoned_workers=1,
                          thread_name_prefix='robbed-claims')
    park, reclaim = kernel._Carrier.park, kernel._Carrier.reclaim
    armed, gates_lock, reached = threading.Event(), threading.Lock(), threading.Semaphore(0)
    gates, robbed = {}, []

    def gated_park(carrier):
        if armed.is_set():
            with gates_lock:
                gate = gates.get(carrier.thread.name)
                first = gate is None
                if first:
                    gate = gates[carrier.thread.name] = threading.Event()
            if first:
                reached.release()
                # Outlasts the probe's wait below; the test always opens it.
                gate.wait(10)
        return park(carrier)

    def recorded_reclaim(carrier):
        item = reclaim(carrier)
        if item is not None:
            robbed.append(carrier.thread.name)
        return item

    monkeypatch.setattr(kernel._Carrier, 'park', gated_park)
    monkeypatch.setattr(kernel._Carrier, 'reclaim', recorded_reclaim)
    busy_started, release_busy = threading.Event(), threading.Event()
    blocker_started, release_blocker = threading.Event(), threading.Event()

    def occupy(started, release):
        started.set()
        # Outlasts the probe's wait below; the test always releases it.
        release.wait(10)
        return threading.current_thread().name

    def open_gates():
        armed.clear()
        with gates_lock:
            for gate in gates.values():
                gate.set()

    try:
        rendezvous = threading.Barrier(4)
        warm = [pool.submit(rendezvous.wait, 5) for _ in range(3)]
        rendezvous.wait(5)
        for future in warm:
            future.result(5)
        _await(lambda: _carrier_idle(pool))
        busy = pool.submit(occupy, busy_started, release_busy)
        assert busy_started.wait(5)
        armed.set()
        # Both other carriers run one job each, then park behind their gates.
        pair = threading.Barrier(2)
        for future in [pool.submit(pair.wait, 5) for _ in range(2)]:
            future.result(5)
        assert reached.acquire(timeout=5) and reached.acquire(timeout=5)
        blocker = pool.submit(occupy, blocker_started, release_blocker)
        probe = pool.submit(lambda: threading.current_thread().name)
        release_busy.set()
        # The free carrier takes back the older hand-off and blocks in it.
        assert blocker_started.wait(5)
        free_carrier = busy.result(5)
        _await(lambda: robbed)
        with gates_lock:
            gates[robbed[0]].set()
        done, _ = wait([probe], timeout=5)
        assert done, 'the robbed carrier parked while a hand-off waited for its carrier'
        assert probe.result() == robbed[0]
        open_gates()
        release_blocker.set()
        assert blocker.result(5) == free_carrier != robbed[0]
        _await(lambda: _carrier_idle(pool))
        snapshot = pool.scheduling_snapshot()
        assert snapshot['residentThreads'] == 3 and snapshot['active'] == 0
        assert pool.resource_snapshot()['completed'] == 8
    finally:
        open_gates()
        release_busy.set()
        release_blocker.set()
        pool.shutdown(wait=True)


class _HandOffCounter:
    """Count job and retirement hand-offs to parked carriers."""

    def __init__(self):
        self._lock = threading.Lock()
        self.jobs = 0
        self.retirements = 0

    def instrument(self, monkeypatch):
        hand_off = kernel._Carrier.hand_off

        def counted_hand_off(carrier, item):
            with self._lock:
                if item is None:
                    self.retirements += 1
                else:
                    self.jobs += 1
            return hand_off(carrier, item)

        monkeypatch.setattr(kernel._Carrier, 'hand_off', counted_hand_off)


def test_burst_on_parked_carriers_wakes_each_carrier_once_per_hand_off(monkeypatch):
    idle, jobs = 8, 256
    pool, conditions = _instrumented_pool(
        monkeypatch, max_workers=idle, thread_name_prefix='burst-wakeups')
    hand_offs = _HandOffCounter()
    hand_offs.instrument(monkeypatch)
    try:
        _park_carriers(pool, conditions, idle)
        woken_before = _wakeups(conditions)
        jobs_before, retired_before = hand_offs.jobs, hand_offs.retirements
        futures = [pool.submit(int) for _ in range(jobs)]
        assert [future.result(5) for future in futures] == [0] * jobs

        def accounted():
            handed = hand_offs.jobs - jobs_before
            retired = hand_offs.retirements - retired_before
            return _wakeups(conditions) - woken_before == handed + retired

        # Every hand-off, including one taken back, wakes its carrier once.
        _await(lambda: accounted() and _carrier_idle(pool))
        print({'jobs': jobs, 'job_hand_offs': hand_offs.jobs - jobs_before,
               'retirement_hand_offs': hand_offs.retirements - retired_before,
               'wakeups': _wakeups(conditions) - woken_before})
        assert hand_offs.jobs - jobs_before <= jobs
        assert pool.resource_snapshot()['completed'] == idle + jobs
    finally:
        pool.shutdown(wait=True)


def test_burst_on_parked_carriers_starts_every_job_while_take_backs_block():
    idle, jobs = 4, 32
    pool = DemandExecutor(max_workers=idle, queue_capacity=0, max_abandoned_workers=1,
                          thread_name_prefix='burst-starts')
    lock, started, all_started = threading.Lock(), [0], threading.Event()

    def job(index):
        with lock:
            started[0] += 1
            if started[0] == jobs:
                all_started.set()
        if index % 2:
            # A carrier that took this job back blocks in it; no other job
            # may wait behind it.
            all_started.wait(5)
        return index

    try:
        rendezvous = threading.Barrier(idle + 1)
        warm = [pool.submit(rendezvous.wait, 5) for _ in range(idle)]
        rendezvous.wait(5)
        for future in warm:
            future.result(5)
        _await(lambda: _carrier_idle(pool))
        futures = [pool.submit(job, index) for index in range(jobs)]
        assert all_started.wait(5), 'a job waited behind a blocked carrier'
        assert [future.result(5) for future in futures] == list(range(jobs))
    finally:
        all_started.set()
        pool.shutdown(wait=True)


def _gated_parked_carrier(monkeypatch, pool):
    """Park the pool's one carrier behind a gate, before it reads its inbox.

    The gate stands in for a woken carrier still waiting for the GIL: a job
    handed to it sits in its inbox, and no carrier has entered it yet.
    """
    park = kernel._Carrier.park
    armed, reached, gate = threading.Event(), threading.Event(), threading.Event()

    def gated_park(carrier):
        if armed.is_set():
            reached.set()
            gate.wait(5)
        return park(carrier)

    monkeypatch.setattr(kernel._Carrier, 'park', gated_park)
    armed.set()
    assert pool.submit(lambda: 'warm').result(5) == 'warm'
    assert reached.wait(5), 'the carrier never parked'
    _await(lambda: _carrier_idle(pool))
    return gate


@pytest.mark.parametrize('cancel', ['future', 'cancel_task', 'shutdown'])
def test_a_handed_off_job_stays_cancellable_until_its_carrier_takes_it(monkeypatch, cancel):
    # Stop/abort owners cancel work that has not started. A hand-off only
    # commits a carrier to the job; entering it is the carrier's act.
    pool = DemandExecutor(max_workers=1, queue_capacity=0, max_abandoned_workers=1,
                          thread_name_prefix=f'handed-cancel-{cancel}')
    ran = threading.Event()
    gate = _gated_parked_carrier(monkeypatch, pool)
    try:
        handed = pool.submit_task('handed-job', ran.set)
        assert not handed.running() and not handed.done()
        if cancel == 'future':
            assert handed.cancel()
        elif cancel == 'cancel_task':
            assert pool.cancel_task('handed-job')
        else:
            pool.shutdown(wait=False, cancel_futures=True)
        assert handed.cancelled()
        # The accounting ends with the cancellation, not when the carrier wakes.
        assert pool.metric_snapshot()['active'] == 0
        gate.set()
        _await(lambda: _carrier_idle(pool) or cancel == 'shutdown')
        assert wait([handed], timeout=5).done == {handed}
        assert not ran.is_set()
        if cancel != 'shutdown':
            assert pool.submit(lambda: 'reused').result(5) == 'reused'
            _await(lambda: _carrier_idle(pool))
            assert pool.resource_snapshot()['completed'] == 2
            assert pool.scheduling_snapshot()['active'] == 0
    finally:
        gate.set()
        pool.shutdown(wait=True)


def test_a_taken_back_hand_off_that_was_cancelled_never_runs(monkeypatch):
    # The free carrier that takes a cancelled hand-off back skips it and
    # serves the next job instead.
    pool = DemandExecutor(max_workers=2, queue_capacity=0, max_abandoned_workers=1,
                          thread_name_prefix='reclaim-cancelled')
    park = kernel._Carrier.park
    armed, reached, gate = threading.Event(), threading.Event(), threading.Event()
    delayed = []

    def delayed_park(carrier):
        if armed.is_set() and not delayed:
            delayed.append(carrier.thread.name)
            reached.set()
            gate.wait(5)
        return park(carrier)

    monkeypatch.setattr(kernel._Carrier, 'park', delayed_park)
    busy_started, release_busy, ran = threading.Event(), threading.Event(), threading.Event()

    def occupy():
        busy_started.set()
        release_busy.wait(5)
        return threading.current_thread().name

    try:
        rendezvous = threading.Barrier(3)
        warm = [pool.submit(rendezvous.wait, 5) for _ in range(2)]
        rendezvous.wait(5)
        for future in warm:
            future.result(5)
        _await(lambda: _carrier_idle(pool))
        busy = pool.submit(occupy)
        assert busy_started.wait(5)
        armed.set()
        assert pool.submit(int).result(5) == 0
        assert reached.wait(5), 'the other carrier never parked again'
        cancelled = pool.submit(ran.set)
        assert cancelled.cancel()
        queued = pool.submit(lambda: threading.current_thread().name)
        release_busy.set()
        busy_name = busy.result(5)
        assert queued.result(5) in {busy_name, delayed[0]}
        gate.set()
        _await(lambda: _carrier_idle(pool))
        assert not ran.is_set()
        snapshot = pool.scheduling_snapshot()
        assert snapshot['residentThreads'] == 2 and snapshot['active'] == 0
        assert pool.resource_snapshot()['completed'] == 5
    finally:
        release_busy.set()
        gate.set()
        pool.shutdown(wait=True)
