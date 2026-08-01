"""lib/swarm/scheduler.py — DAG-based streaming agent schedulers.

Extracted from master.py for modularity.

Contains:
  • StreamingScheduler — dependency-aware streaming execution (threads)
  • AsyncStreamingScheduler — asyncio wrapper around StreamingScheduler
"""

import queue
import threading
import time
from collections.abc import Callable, Generator
from concurrent.futures import ThreadPoolExecutor

from lib.log import get_logger
from lib.swarm.agent import SubAgent
from lib.swarm.liveness import ProgressBeacon
from lib.swarm.protocol import (
    SubAgentResult,
    SubAgentStatus,
    SubTaskSpec,
    compress_result,
)
from lib.swarm.rate_limiter import RateLimiter

logger = get_logger(__name__)

# Import resolve_execution_order — avoid circular import by importing at
# module level (master.py imports from scheduler.py, but
# resolve_execution_order lives in master.py which is fine since it's a
# standalone function that doesn't depend on scheduler classes).
# We use a late import inside add_specs to break any potential circularity.


class StreamingScheduler:
    """Dependency-aware streaming agent scheduler.

    Unlike wave-barrier execution, agents start **as soon as their
    dependencies complete**.  Example::

        Given: A→C, B→D  (A,B independent; C depends on A; D depends on B)
        Old:   Wave1=[A,B] → wait for BOTH → Wave2=[C,D]
        New:   A,B start together.  If A finishes first, C starts
               immediately while B is still running.

    Thread-safety: all mutable state is guarded by ``_lock``.
    Results are communicated back via ``queue.Queue`` so that
    ``run_until_idle`` can block efficiently without busy-waiting.

    The critical race-condition fix: ``_results_queue.put()`` is performed
    INSIDE ``_lock`` in ``_run_one``, and ``iter_completions`` drains the
    queue + checks idle state atomically under the same lock.  This ensures
    no result can slip through between the queue-drain and idle-check.
    """

    def __init__(self, *,
                 agent_factory: Callable[[SubTaskSpec], SubAgent],
                 rate_limiter: RateLimiter | None = None,
                 max_parallel: int = 8,
                 abort_check: Callable | None = None,
                 default_retries: int = 1,
                 on_agent_complete: Callable | None = None,
                 on_agent_start: Callable | None = None,
                 on_retry: Callable | None = None,
                 progress_beacon: ProgressBeacon | None = None):
        """
        Parameters
        ----------
        agent_factory : callable(SubTaskSpec) → SubAgent
            Creates a runnable ``SubAgent`` from a spec.
        rate_limiter : RateLimiter, optional
            If given, ``agent.run()`` is called through the limiter.
        max_parallel : int
            Thread pool size.
        abort_check : callable → bool
            Return True to stop scheduling new work.
        default_retries : int
            Default retry count when spec.max_retries is not set.
        on_agent_complete : callable(spec, result), optional
        on_agent_start : callable(spec), optional
        on_retry : callable(spec, attempt, error_msg), optional
        progress_beacon : ProgressBeacon, optional
            Shared liveness fact (see ``lib/swarm/liveness.py``). Supplied by
            the master so the scheduler, every SubAgent and the session TTL
            sweep read the SAME record of who is still producing. When omitted
            a private beacon is created, so a standalone scheduler still has a
            working stall check rather than none at all.
        """
        self._factory = agent_factory
        self._rate_limiter = rate_limiter
        self._abort_check = abort_check or (lambda: False)
        self._default_retries = default_retries
        self._on_complete = on_agent_complete
        self._on_start = on_agent_start
        self._on_retry = on_retry
        self._beacon = progress_beacon or ProgressBeacon()

        self._pool = ThreadPoolExecutor(
            max_workers=max_parallel,
            thread_name_prefix='swarm-stream',
        )

        # Internal state — guarded by _lock
        self._lock = threading.Lock()
        self._pending: list[SubTaskSpec] = []        # specs waiting for deps
        self._running: dict[str, SubTaskSpec] = {}   # id → spec currently executing
        #: id → WALL-CLOCK launch instant (``time.time()``, epoch seconds).
        #: Deliberately NOT ``monotonic()``: this value is surfaced to the
        #: browser so a reloaded swarm panel can continue each agent's live
        #: stopwatch, and a monotonic reading is meaningless off-process.
        #: Entries are released when the agent settles (see _run_one).
        self._started_at: dict[str, float] = {}
        self._completed: dict[str, tuple[SubTaskSpec, SubAgentResult]] = {}
        self._all_results: list[tuple[SubTaskSpec, SubAgentResult]] = []

        # Queue used to notify consumers of completions
        self._results_queue: queue.Queue = queue.Queue()

    # ── Public API ───────────────────────────────────

    def add_specs(self, specs: list[SubTaskSpec],
                  inject_deps: bool = True) -> list[SubTaskSpec]:
        """Add specs and immediately launch any whose deps are satisfied.

        Performs lightweight cycle detection among the new specs +
        existing pending specs before adding.  Deduplicates against
        already completed / pending / running specs by objective text
        similarity.  Raises ``ValueError`` on cycles so the caller can
        handle gracefully.

        Returns the list of specs that were actually accepted (after
        dedup).  Callers should compare against the original ``specs``
        list to detect dropped duplicates and surface them to the LLM.
        """
        from lib.swarm.planner import resolve_execution_order

        logger.info('[Scheduler] add_specs called with %d spec(s): %s',
                     len(specs), [(s.id, s.role) for s in specs])

        # ── Dedup + cycle-check + add: all under one lock to prevent
        #    TOCTOU races with _run_one threads completing concurrently.
        #    resolve_execution_order is pure (no I/O) so safe under lock.

        # Cycle check first (outside lock — pure function on new specs only)
        try:
            resolve_execution_order(specs)
        except ValueError as e:
            logger.error('[Scheduler] Cycle detected when adding specs: %s', e, exc_info=True)
            raise ValueError(f'Cannot add specs: {e}') from e

        with self._lock:
            # ── Snapshot existing objectives for dedup ──
            seen_objectives: set[str] = set()
            existing_ids: set[str] = set()
            for sid, (done_spec, _done_result) in self._completed.items():
                seen_objectives.add(done_spec.objective.strip().lower())
                existing_ids.add(sid)
            for s in self._pending:
                seen_objectives.add(s.objective.strip().lower())
                existing_ids.add(s.id)
            for s_id, running_spec in self._running.items():
                seen_objectives.add(running_spec.objective.strip().lower())
                existing_ids.add(s_id)

            deduped_specs = []
            for s in specs:
                obj_norm = s.objective.strip().lower()
                if obj_norm in seen_objectives and s.id not in existing_ids:
                    logger.debug(
                        '[Scheduler] Skipping duplicate spec %s (%s): '
                        'objective already covered',
                        s.id, s.objective[:60])
                    continue
                deduped_specs.append(s)
                seen_objectives.add(obj_norm)

            if not deduped_specs:
                logger.debug('[Scheduler] All %d specs were duplicates, nothing to add',
                             len(specs))
                return []

            if len(deduped_specs) < len(specs):
                logger.debug('[Scheduler] Deduped %d → %d specs',
                             len(specs), len(deduped_specs))

            specs = deduped_specs

            # ── Warn about unknown deps ──
            new_ids = {s.id for s in specs}
            all_known = (set(self._completed.keys())
                         | {s.id for s in self._pending}
                         | set(self._running.keys())
                         | new_ids)
            for s in specs:
                for dep_id in (s.depends_on or []):
                    if dep_id not in all_known:
                        logger.warning(
                            '[Scheduler] Spec %s depends on unknown %s, '
                            'ignoring dep', s.id, dep_id)

            for spec in specs:
                self._pending.append(spec)
            logger.debug('[Scheduler] After add: pending=%d running=%d completed=%d pending_ids=%s',
                         len(self._pending), len(self._running), len(self._completed),
                         [s.id for s in self._pending])
            self._launch_ready_locked()
            return list(specs)

    def run_until_idle(self, timeout: float | None = None) -> list[tuple[SubTaskSpec, SubAgentResult]]:
        """Block until there are no pending or running specs.

        Returns the list of ``(spec, result)`` pairs completed during
        this call (i.e. **not** all-time results, just this batch).

        ``timeout=None`` (default) = **no fixed whole-swarm budget**, the same
        contract as ``iter_completions``. This default used to be 600.0: no
        in-tree caller reaches it today, but leaving the exact constant that
        abandoned running agents sitting in a sibling entry point is how the
        defect returns the first time someone wires this method up.
        Termination is decided by the liveness beacon (genuine silence), so a
        slow-but-producing swarm is never cut off; an explicit finite
        ``timeout`` is still honoured for callers that want a hard ceiling.
        """
        logger.info('[Scheduler] run_until_idle START timeout=%s pending=%d running=%d completed=%d',
                     (f'{timeout:.0f}s' if timeout else 'liveness-governed'),
                     len(self._pending), len(self._running), len(self._completed))
        batch: list[tuple[SubTaskSpec, SubAgentResult]] = []
        deadline = (time.monotonic() + timeout) if timeout and timeout > 0 else None
        t0 = time.monotonic()

        while True:
            with self._lock:
                idle = len(self._pending) == 0 and len(self._running) == 0
            if idle:
                logger.debug('[Scheduler] run_until_idle: idle state reached')
                break

            # Liveness, not elapsed time, decides when to give up on a
            # still-running swarm (mirrors iter_completions).
            if not self._beacon.is_making_progress():
                logger.warning(
                    '[Scheduler] run_until_idle: swarm stalled — no activity '
                    'for %.0fs (stall_at=%.0fs), pending=%d running=%d',
                    self._beacon.seconds_since_activity(),
                    self._beacon.stall_timeout,
                    len(self._pending), len(self._running))
                break

            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    logger.warning('[Scheduler] run_until_idle hit the caller-'
                                   'supplied %.0fs ceiling after %.1fs, '
                                   'pending=%d running=%d', timeout,
                                   time.monotonic() - t0,
                                   len(self._pending), len(self._running))
                    break
                wait_for = min(remaining, 2.0)
            else:
                wait_for = 2.0

            try:
                item = self._results_queue.get(timeout=wait_for)
                spec, result = item
                logger.debug('[Scheduler] run_until_idle: got result agent=%s status=%s elapsed=%.1fs',
                             spec.id, result.status, time.monotonic() - t0)
                batch.append(item)
            except queue.Empty:
                if self._abort_check():
                    logger.info('[Scheduler] run_until_idle: abort requested, breaking')
                    break

        # Drain any remaining items that arrived
        drained = 0
        while not self._results_queue.empty():
            try:
                batch.append(self._results_queue.get_nowait())
                drained += 1
            except queue.Empty:
                logger.debug('[Scheduler] run_until_idle: queue empty during drain')
                break
        if drained:
            logger.debug('[Scheduler] run_until_idle: drained %d extra results from queue', drained)

        logger.info('[Scheduler] run_until_idle DONE in %.1fs, batch_size=%d',
                     time.monotonic() - t0, len(batch))
        return batch

    @property
    def is_idle(self) -> bool:
        with self._lock:
            return len(self._pending) == 0 and len(self._running) == 0

    @property
    def all_results(self) -> list[tuple[SubTaskSpec, SubAgentResult]]:
        with self._lock:
            return list(self._all_results)

    @property
    def completed_count(self) -> int:
        with self._lock:
            return len(self._completed)

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    @property
    def running_count(self) -> int:
        with self._lock:
            return len(self._running)

    def started_at_map(self) -> dict[str, float]:
        """Snapshot of ``agent_id → wall-clock launch instant`` (epoch seconds).

        Only currently-running agents appear: a settled agent's authoritative
        duration lives on its result's ``elapsed_seconds``. Consumed by
        ``master._build_agent_snapshot`` so a reloaded swarm panel can keep a
        running agent's stopwatch ticking from its REAL start instead of
        dropping the timer entirely.
        """
        with self._lock:
            return dict(self._started_at)

    def iter_completions(self, poll_interval: float = 0.5,
                         timeout: float | None = None) -> Generator:
        """Yield ``(spec, result)`` one at a time as agents complete.

        Unlike ``run_until_idle`` which blocks until everything is done,
        this is a **generator** that yields each result the moment it
        arrives.  The caller (e.g. the reactive master) can decide
        after *each* result whether to keep waiting or take action.

        Stops when the scheduler is idle (no pending + no running + queue
        empty).  Drain and idle-check are done **atomically** under
        ``_lock`` (since ``_results_queue.put()`` is also under
        ``_lock`` in ``_run_one``) so no result can slip through
        between the drain and the idle check.

        ``timeout`` defaults to ``None`` = **no fixed whole-swarm budget**.
        It used to default to 600.0, and because ``master._start_driver``
        calls this with no argument, that default WAS the ceiling on an
        entire swarm: the deadline was computed once when the generator
        started, so agents launched by a later wave inherited a clock set
        before they existed. At 600.0s the driver abandoned still-running
        agents, set ``_terminated``, shut the pool down and persisted
        ``settled:true`` — after which ``await_agents`` reported those ids
        as "will NEVER complete" and the panel painted them "no result",
        while one of them went on to finish normally 21 minutes later.

        Termination is now decided by PRODUCTION, not by elapsed time: the
        loop exits when the swarm goes silent past the beacon's stall window
        (see ``lib/swarm/liveness.py``). A caller may still pass an explicit
        finite ``timeout`` as a hard ceiling; nothing in-tree does.
        """
        deadline = (time.monotonic() + timeout) if timeout and timeout > 0 else None
        while True:
            # ── Atomic drain + idle check ────────────────────────
            drained: list[tuple[SubTaskSpec, SubAgentResult]] = []
            with self._lock:
                try:
                    while True:
                        drained.append(self._results_queue.get_nowait())
                except queue.Empty:
                    logger.debug('[Scheduler] iter_completions: drained %d results before idle check', len(drained))
                idle = (len(self._pending) == 0
                        and len(self._running) == 0)

            # Yield outside the lock so callers can call add_specs()
            for item in drained:
                spec_d, result_d = item
                logger.debug('[Scheduler] iter_completions YIELD agent=%s status=%s answer_len=%d',
                             spec_d.id, result_d.status, len(result_d.final_answer or ''))
                yield item

            if idle and not drained:
                # Truly idle and no stragglers — done.
                logger.info('[Scheduler] iter_completions: idle, no more results — stopping')
                return
            if idle and drained:
                # Got items but now idle — loop once more to confirm
                # nothing new arrived (e.g. from _launch_ready_locked
                # triggered by our completions unblocking dependents).
                continue

            # ── Not idle — block-wait for next result ─────────────
            # A running agent is only abandoned when it has gone SILENT past
            # the beacon's stall window — never merely because time passed.
            if not self._beacon.is_making_progress():
                stalled = self._beacon.stalled_agents()
                logger.warning(
                    '[StreamingScheduler] swarm stalled — no activity for '
                    '%.0fs (stall_at=%.0fs); still running=%s, quiet agents=%s',
                    self._beacon.seconds_since_activity(),
                    self._beacon.stall_timeout,
                    sorted(self._running),
                    [(a, round(s)) for a, s, _n in stalled])
                return

            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    logger.warning('[StreamingScheduler] iter_completions hit '
                                   'the caller-supplied %.0fs ceiling', timeout)
                    return
                wait_for = min(remaining, poll_interval)
            else:
                wait_for = poll_interval

            try:
                item = self._results_queue.get(timeout=wait_for)
                yield item
            except queue.Empty:
                if self._abort_check():
                    return

    def cancel_pending(self) -> list[SubTaskSpec]:
        """Remove all pending (not-yet-started) specs and return them.

        Running agents are not interrupted — only queued work is removed.
        """
        with self._lock:
            cancelled = list(self._pending)
            self._pending.clear()
            return cancelled

    def shutdown(self, wait: bool = True, timeout: float = 30.0):
        """Shut the pool down, by default WAITING for in-flight agents.

        This used to be ``shutdown(wait=False)``, which returned while agents
        were still running. Two concrete failures followed:

          * a late completion calls ``_launch_ready_locked`` to unblock its
            dependents, and submitting to a shut-down pool raises
            ``RuntimeError: cannot schedule new futures after shutdown``
            (verified on this interpreter) — killing the dependent inside
            ``_run_one``;
          * the driver's ``finally`` ran ``shutdown()`` then set
            ``_terminated``, so ``await_agents`` told the model that agents
            which were STILL EXECUTING "will NEVER complete".

        Waiting makes termination mean what it says. ``timeout`` bounds the
        wait so a wedged tool thread cannot hang teardown forever; the pool is
        then released regardless, matching the old non-blocking behaviour only
        in that genuinely-stuck corner.
        """
        if not wait:
            self._pool.shutdown(wait=False)
            return
        deadline = time.monotonic() + max(0.0, timeout)
        while self.running_count > 0 and time.monotonic() < deadline:
            time.sleep(0.05)
        still = self.running_count
        if still:
            logger.warning('[Scheduler] shutdown: %d agent(s) still running '
                           'after %.0fs — releasing pool anyway', still, timeout)
        self._pool.shutdown(wait=False)

    # ── Internal helpers ─────────────────────────────

    def _deps_satisfied(self, spec: SubTaskSpec) -> bool:
        """All depends_on IDs are in self._completed.  Caller holds _lock."""
        for dep_id in (spec.depends_on or []):
            if dep_id not in self._completed:
                return False
        return True

    def _inject_deps_locked(self, spec: SubTaskSpec):
        """Inject results from completed dependencies into spec.context.

        Caller holds _lock.
        """
        if not spec.depends_on:
            return
        dep_results = []
        for dep_id in spec.depends_on:
            if dep_id in self._completed:
                dep_spec, dep_result = self._completed[dep_id]
                dep_results.append(
                    f'[{dep_spec.role}] {dep_spec.objective[:80]}:\n'
                    f'{compress_result(dep_result.final_answer, max_chars=2000)}'
                )
        if dep_results:
            spec.context += (
                '\n\nResults from prerequisite tasks:\n'
                + '\n---\n'.join(dep_results)
            )

    def _launch_ready_locked(self):
        """Find pending specs with satisfied deps, submit to pool.

        When a spec becomes ready, dependency context is injected
        (from completed results) before submitting to the pool.

        Caller holds _lock.
        """
        if self._abort_check():
            logger.debug('[Scheduler] _launch_ready_locked skipped — abort flag set')
            return

        still_pending = []
        launched = []
        blocked = []
        for spec in self._pending:
            if self._deps_satisfied(spec):
                self._inject_deps_locked(spec)
                self._running[spec.id] = spec
                self._started_at[spec.id] = time.time()
                self._pool.submit(self._run_one, spec)
                launched.append(spec.id)
            else:
                still_pending.append(spec)
                waiting_for = [d for d in (spec.depends_on or []) if d not in self._completed]
                blocked.append((spec.id, waiting_for))
        self._pending = still_pending

        if launched:
            logger.debug('[Scheduler] Launched %d agent(s): %s', len(launched), launched)
        if blocked:
            logger.debug('[Scheduler] %d spec(s) still blocked on deps: %s', len(blocked), blocked)

    def _run_one(self, spec: SubTaskSpec):
        """Execute one agent with auto-retry.  Runs in pool thread."""
        effective_retries = spec.max_retries if spec.max_retries > 0 else self._default_retries
        result: SubAgentResult | None = None
        t0 = time.monotonic()

        logger.debug('[Scheduler] _run_one START agent=%s role=%s objective=%.80s retries=%d',
                      spec.id, spec.role, spec.objective, effective_retries)

        # Seed the liveness record the moment the agent is handed a thread, so
        # a swarm is never judged "silent" during the window between launch and
        # the agent's first token.
        self._beacon.touch(spec.id, 'agent_start')

        # Notify start
        if self._on_start:
            try:
                self._on_start(spec)
            except Exception as e:
                logger.warning('[Scheduler] on_start callback error for %s: %s', spec.id, e, exc_info=True)

        for attempt in range(1 + effective_retries):
            if self._abort_check():
                logger.warning('[Scheduler] _run_one ABORT agent=%s (abort flag)', spec.id)
                result = SubAgentResult(
                    status=SubAgentStatus.CANCELLED.value,
                    error_message='Aborted',
                )
                break

            logger.debug('[Scheduler] agent=%s attempt %d/%d', spec.id, attempt + 1, 1 + effective_retries)
            try:
                agent = self._factory(spec)

                if self._rate_limiter:
                    result = self._rate_limiter.run_agent(agent)
                else:
                    result = agent.run()

                if result.status == SubAgentStatus.COMPLETED.value:
                    result.retry_count = attempt
                    elapsed = time.monotonic() - t0
                    logger.debug('[Scheduler] _run_one DONE agent=%s status=COMPLETED '
                                 'attempt=%d elapsed=%.1fs answer_len=%d',
                                 spec.id, attempt + 1, elapsed,
                                 len(result.final_answer or ''))
                    break  # success

                # Failed — prepare for retry
                logger.warning('[Scheduler] agent=%s attempt %d FAILED status=%s err=%s',
                               spec.id, attempt + 1, result.status,
                               (result.error_message or '')[:200])
                if attempt < effective_retries:
                    if self._on_retry:
                        try:
                            self._on_retry(spec, attempt + 1, result.error_message)
                        except Exception as e:
                            logger.warning('[Scheduler] on_retry callback error for %s: %s', spec.id, e, exc_info=True)
                    spec.context += (
                        f'\n\nPrevious attempt failed with: '
                        f'{result.error_message}\n'
                        f'Please try a different approach.'
                    )
                else:
                    result.retry_count = attempt

            except Exception as exc:
                err_msg = f'{type(exc).__name__}: {exc}'
                logger.error(
                    '[StreamingScheduler] Agent %s exception '
                    '(attempt %d): %s', spec.id, attempt + 1, err_msg, exc_info=True)
                result = SubAgentResult(
                    status=SubAgentStatus.FAILED.value,
                    error_message=err_msg,
                )
                if attempt < effective_retries:
                    if self._on_retry:
                        try:
                            self._on_retry(spec, attempt + 1, err_msg)
                        except Exception as e:
                            logger.warning('[Scheduler] on_retry callback error for %s: %s', spec.id, e, exc_info=True)
                    spec.context += (
                        f'\n\nPrevious attempt failed with: {err_msg}\n'
                        f'Please try a different approach.'
                    )
                else:
                    result.retry_count = attempt

        # Record completion — put into queue INSIDE lock so that
        # iter_completions cannot see _running==0 before the item
        # is actually in the queue (race condition fix).
        elapsed_total = time.monotonic() - t0
        logger.debug('[Scheduler] _run_one FINISHED agent=%s final_status=%s elapsed=%.1fs',
                      spec.id, result.status if result else 'None', elapsed_total)
        with self._lock:
            self._completed[spec.id] = (spec, result)
            self._all_results.append((spec, result))
            self._results_queue.put((spec, result))
            self._running.pop(spec.id, None)
            # The agent has settled — its result now carries the authoritative
            # elapsed, so the live start clock is no longer needed (and must
            # not accumulate across a long-lived swarm).
            self._started_at.pop(spec.id, None)
            # Same for the liveness record: a settled agent must not keep the
            # swarm-wide "still producing" verdict alive for its siblings.
            self._beacon.forget(spec.id)
            logger.debug('[Scheduler] Queue state after agent=%s: pending=%d running=%s completed=%d',
                         spec.id, len(self._pending),
                         list(self._running.keys()), len(self._completed))
            # Unblock dependents
            self._launch_ready_locked()

        if self._on_complete:
            try:
                self._on_complete(spec, result)
            except Exception as e:
                logger.warning('[Scheduler] on_complete callback error for %s: %s', spec.id, e, exc_info=True)


# ═══════════════════════════════════════════════════════════
#  AsyncStreamingScheduler — asyncio wrapper around StreamingScheduler
# ═══════════════════════════════════════════════════════════

class AsyncStreamingScheduler:
    """Async-compatible wrapper around ``StreamingScheduler``.

    Provides an ``async for`` interface over agent completions by wrapping
    the synchronous ``StreamingScheduler`` and using ``asyncio.to_thread``
    for blocking operations.  The underlying agent execution still uses
    threads (since the LLM client is synchronous), but the control flow
    is fully ``async``/``await`` compatible.

    Usage::

        async_sched = AsyncStreamingScheduler(agent_factory=make_agent)
        await async_sched.add_specs(specs)
        async for spec, result in async_sched.iter_completions():
            print(f'{spec.role} → {result.status}')
    """

    def __init__(self, *,
                 agent_factory: Callable[[SubTaskSpec], SubAgent],
                 rate_limiter: RateLimiter | None = None,
                 max_parallel: int = 8,
                 abort_check: Callable | None = None,
                 default_retries: int = 1,
                 on_agent_complete: Callable | None = None,
                 on_agent_start: Callable | None = None,
                 on_retry: Callable | None = None,
                 progress_beacon: ProgressBeacon | None = None):
        self._sync_scheduler = StreamingScheduler(
            agent_factory=agent_factory,
            rate_limiter=rate_limiter,
            max_parallel=max_parallel,
            abort_check=abort_check,
            default_retries=default_retries,
            on_agent_complete=on_agent_complete,
            on_agent_start=on_agent_start,
            on_retry=on_retry,
            progress_beacon=progress_beacon,
        )

    async def add_specs(self, specs: list[SubTaskSpec], inject_deps: bool = True):
        """Add specs to the underlying scheduler (thread-safe, non-blocking)."""
        import asyncio
        await asyncio.to_thread(self._sync_scheduler.add_specs, specs, inject_deps)

    async def run_until_idle(self, timeout: float | None = None) -> list[tuple[SubTaskSpec, SubAgentResult]]:
        """Block asynchronously until the scheduler is idle.

        ``None`` = liveness-governed, matching the sync scheduler.
        """
        import asyncio
        return await asyncio.to_thread(self._sync_scheduler.run_until_idle, timeout)

    async def iter_completions(self, poll_interval: float = 0.5,
                                timeout: float | None = None):
        """Async generator yielding ``(spec, result)`` as agents complete.

        Internally polls the synchronous scheduler's ``_results_queue``
        with short timeouts, yielding control back to the asyncio event
        loop between polls.

        ``timeout=None`` (default) = liveness-governed, the same contract as
        the sync ``iter_completions``. It used to default to 600.0 — the exact
        constant that abandoned running agents on the sync path.
        """
        import asyncio
        deadline = (time.monotonic() + timeout) if timeout and timeout > 0 else None
        _beacon = self._sync_scheduler._beacon

        while True:
            # Drain and check idle atomically via the sync scheduler
            drained: list[tuple[SubTaskSpec, SubAgentResult]] = []
            with self._sync_scheduler._lock:
                try:
                    while True:
                        drained.append(
                            self._sync_scheduler._results_queue.get_nowait())
                except queue.Empty:
                    logger.debug('[AsyncScheduler] async iter: drained %d results before idle check', len(drained))
                idle = (len(self._sync_scheduler._pending) == 0
                        and len(self._sync_scheduler._running) == 0)

            for item in drained:
                yield item

            if idle and not drained:
                return
            if idle and drained:
                continue

            if not _beacon.is_making_progress():
                logger.warning(
                    '[AsyncScheduler] swarm stalled — no activity for %.0fs '
                    '(stall_at=%.0fs)', _beacon.seconds_since_activity(),
                    _beacon.stall_timeout)
                return

            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                wait_for = min(remaining, poll_interval)
            else:
                wait_for = poll_interval

            # Non-blocking wait: sleep briefly then try queue
            try:
                item = await asyncio.to_thread(
                    self._sync_scheduler._results_queue.get,
                    timeout=wait_for,
                )
                yield item
            except queue.Empty:
                if self._sync_scheduler._abort_check():
                    return

    async def cancel_pending(self) -> list[SubTaskSpec]:
        """Cancel pending specs asynchronously."""
        import asyncio
        return await asyncio.to_thread(self._sync_scheduler.cancel_pending)

    @property
    def is_idle(self) -> bool:
        return self._sync_scheduler.is_idle

    @property
    def all_results(self) -> list[tuple[SubTaskSpec, SubAgentResult]]:
        return self._sync_scheduler.all_results

    @property
    def completed_count(self) -> int:
        return self._sync_scheduler.completed_count

    @property
    def pending_count(self) -> int:
        return self._sync_scheduler.pending_count

    @property
    def running_count(self) -> int:
        return self._sync_scheduler.running_count

    def started_at_map(self) -> dict[str, float]:
        """Pass-through to the wrapped scheduler (plain dict read, no I/O)."""
        return self._sync_scheduler.started_at_map()

    def shutdown(self):
        """Shutdown the underlying thread pool."""
        self._sync_scheduler.shutdown()
