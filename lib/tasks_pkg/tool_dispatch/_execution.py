"""Start every admitted tool occurrence at once and collect its own receipt.

Owner decision 2026-10-28 (docs/contracts/TOOL_EXECUTION_POLICY.md,
"Concurrent tool rounds"): there is no serial lane. The pipeline admits a
round's calls; this module starts each one on the shared demand executor
immediately. A call's worker first waits for the earlier same-target calls
named by ``_ordering``, then runs the pipeline's worker-side admission
(approval, Stop, hooks) and finally its handler. Completions settle as they
arrive; replay order stays with the pipeline. Batch fields reference
invocation-local carriers; no state is copied onto the task. Ports preserve the
application's execution, collection, heartbeat, settlement and fault-injection
boundaries.
"""
from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from concurrent.futures import (
    FIRST_COMPLETED, Future, TimeoutError as _FuturesTimeoutError, wait,
)
from contextvars import Context, copy_context
from dataclasses import dataclass
from functools import partial
from typing import Any, NamedTuple

from lib.tasks_pkg.tool_dispatch._flags import (
    _invalidate_project_cache, _make_cache_key, _store_tool_result_cache_entry,
    _tool_receipt_lock,
)
from lib.tools.result_projection import TOOL_RESULT_PROJECTION_ITEMS_KEY

# A same-round write can invalidate concurrent filesystem observations.
# Do not publish their pre-write content back into the post-write cache.
_WRITE_SENSITIVE_READ_TOOLS = frozenset({
    'read_files', 'list_dir', 'search_files', 'grep_search', 'find_files',
    'inspect_image',
})

# Executors outside the registry write partition that still mutate the project.
_PROJECT_MUTATING_TOOLS = frozenset({
    'write_file', 'edit_file', 'apply_diff', 'apply_diffs', 'insert_content',
    'insert_contents', 'code_exec', 'bash_exec', 'run_command',
})

_STOPPED = 'Task aborted by user.'


class _ToolCollectionExpiry(NamedTuple):
    """An occurrence whose collector deadline expired before its receipt."""

    future: Future


class UnexecutedCall(tuple):
    """``(call_id, content, False)`` receipt of an admitted call whose handler
    never ran: worker-side admission refused it, or Stop won first."""

    verdict: str

    def __new__(cls, call_id: str, content: Any, verdict: str):
        receipt = super().__new__(cls, (call_id, content, False))
        receipt.verdict = verdict
        return receipt


@dataclass
class AdmittedCall:
    """One admitted occurrence plus the launch facts the pipeline planned.

    ``bounded`` calls keep the ordinary pool ceiling, their declared timeout
    and the Stop placeholder. Writes, ordered state and human waits are
    unbounded: no collection deadline, and after Stop the drain joins them so
    their own receipt (with any effect evidence) is kept.
    The worker records ``entered``/``deadline`` when the handler starts, so time
    spent waiting for order, approval or a human never consumes the ceiling.
    A gateway child also waits for ``sibling_predecessors``, the settlement of
    earlier sibling gateway calls of its response. ``adopt_settlement`` hands a
    published gateway occurrence to the worker submitted for it, which settles
    it when it returns, whatever the outcome; a worker still running past its
    collection deadline keeps its place.
    """

    item: tuple
    predecessors: tuple[str, ...] = ()
    bounded: bool = True
    deadline: float = math.inf
    entered: bool = False
    sibling_predecessors: tuple[Future, ...] = ()
    adopt_settlement: Callable[[], Callable[[], None] | None] | None = None


class _EntryDeadlines(Mapping):
    """Collector view of each future's current deadline."""

    def __init__(self, calls_by_future: dict[Future, AdmittedCall]):
        self._calls = calls_by_future

    def __getitem__(self, future: Future) -> float:
        return self._calls[future].deadline

    def __iter__(self):
        return iter(self._calls)

    def __len__(self) -> int:
        return len(self._calls)


@dataclass(frozen=True)
class ToolExecutionBatch:
    """Borrowed task inputs and the single invocation's mutable result carriers."""
    task: dict
    configuration: dict
    project_path: str | None
    project_enabled: bool
    tools: list | None
    model: str
    round_number: int
    task_label: str
    calls: list
    write_tools: set
    reusable_tools: set
    cache: dict
    results: dict
    verdicts: dict
    produced_cache_entries: dict
    settle: Callable[..., Any]
    # Worker-side admission: returns an UnexecutedCall refusal or None.
    admit: Callable[[tuple], Any]


@dataclass(frozen=True)
class ToolExecutionPorts:
    """Bindings supplied by the pipeline; tests and native callers share them."""
    idle_reuse_hint: Callable[..., Any]
    pool_factory: Callable[..., Any]
    start_heartbeat: Callable[..., Any]
    timeout_setting: Callable[..., Any]
    clock: Any
    submit: Callable[..., Any]
    execute_one: Callable[..., Any]
    execute_bounded: Callable[..., Any]
    settle_content: Callable[..., Any]
    canonical_content: Callable[..., Any]
    collect: Callable[..., Any]
    expire: Callable[..., Any]
    harvest: Callable[..., Any]
    drain: Callable[..., Any]
    log: Any


def _await_predecessors(task: dict, predecessors: tuple[Future, ...]) -> bool:
    """Wait for the earlier calls ``_ordering`` names; False when Stop wins first."""
    pending = {future for future in predecessors if not future.done()}
    while pending and not task.get('aborted'):
        _done, pending = wait(pending, timeout=0.1, return_when=FIRST_COMPLETED)
    return not task.get('aborted')


def _run_admitted_call(batch: ToolExecutionBatch, ports: ToolExecutionPorts,
                       call: AdmittedCall, predecessors: tuple[Future, ...],
                       timeout_seconds: float, dispatcher_context: Context):
    """Worker body: call order, worker-side admission, then the handler.

    Admission and unbounded handlers formerly ran on the dispatcher thread, so
    they run in a copy of its context (for example the swarm progress sink that
    run_command output feeds). Bounded handlers keep the bare worker context
    they always had.
    """
    tc, name, call_id, arguments, number, row, _parse_error = call.item
    if not _await_predecessors(batch.task, predecessors):
        return UnexecutedCall(call_id, _STOPPED, 'aborted')
    refusal = dispatcher_context.run(batch.admit, call.item)
    if refusal is not None:
        return refusal
    call.entered = True
    dispatcher_context.run(_publish_handler_entry, batch.task, call.item)
    handler_arguments = (
        batch.task, tc, name, call_id, arguments, number, row,
        batch.configuration, batch.project_path, batch.project_enabled)
    if call.bounded:
        call.deadline = ports.clock.monotonic() + timeout_seconds
        return ports.execute_bounded(*handler_arguments, all_tools=batch.tools)
    return dispatcher_context.run(
        ports.execute_one, *handler_arguments, all_tools=batch.tools)


def _publish_handler_entry(task: dict, item: tuple) -> None:
    """A row announced ``queued`` during streaming starts running only here.

    The status-bearing ``tool_progress`` frame is structural for the turn
    projection (``turn_lifecycle.task_event_projection_is_progress``), so the
    live row flips at the instant its handler is entered.
    """
    _tc, name, call_id, _arguments, number, row, _parse_error = item
    if not isinstance(row, dict) or row.get('status') != 'queued':
        return
    row['status'] = 'searching'
    from lib.agent_core.events import EventType, build_event
    from lib.tasks_pkg.manager import append_event
    append_event(task, build_event(
        EventType.TOOL_PROGRESS, roundNum=number, toolCallId=call_id,
        toolName=row.get('toolName') or name, status='searching'))


def _programmatic_children(calls: list[AdmittedCall]) -> bool:
    return any(
        (isinstance(call.item[0].get('caller'), dict)
         and call.item[0]['caller'].get('type') == 'program')
        or str(call.item[0].get('source') or '') in {'execute_program', 'execute_calls'}
        for call in calls)


def _publish_reusable_receipt(batch: ToolExecutionBatch, item: tuple, call_id: str,
                              name: str, content: Any, is_search: bool,
                              generation: object, has_write: bool) -> None:
    """Populate the dedup cache from one completed reusable read."""
    row = item[5]
    # A read that ran beside a sibling write may hold PRE-write bytes, and
    # this population happens after the write's invalidation already ran —
    # caching now would leave a stale entry a later FreshGate-checked hit
    # serves verbatim.
    if (not (row or {}).get('_cacheableResult', True)
            or (has_write and name in _WRITE_SENSITIVE_READ_TOOLS)):
        return
    cache_key = _make_cache_key(name, item[3])
    # For web_search / fetch_url, also cache display_results + engineBreakdown
    # from the round_entry so a dedup replay keeps the rich per-result UI
    # (e.g. batch "3 URLs" stays as 3 rows, not 1 generic row).
    display = engine_breakdown = vertical = search_diag = None
    if name in ('web_search', 'fetch_url'):
        if row and row.get('results'):
            display = row['results']
        if row:
            engine_breakdown = row.get('engineBreakdown')
            vertical = row.get('vertical') or row.get('verticals')
            # Zero-result searches: the handler stamps searchDiag on the
            # round — cache it so a later hit renders the honest diagnostic.
            search_diag = row.get('searchDiag')
    elif name in ('read_files', 'inspect_image'):
        # Preserve the FULLY-MERGED inline render descriptors (images + SVG
        # source URIs) so a dedup replay renders identically to the fresh read.
        results = (row or {}).get('results') or []
        if results and isinstance(results[0], dict) and results[0].get('imageDataUris'):
            display = results[0]['imageDataUris']
    entry = (content, is_search, 'dedup', display, engine_breakdown, vertical,
             search_diag)
    projection = row.get(TOOL_RESULT_PROJECTION_ITEMS_KEY) if row else None
    if projection is not None:
        entry += (projection,)
    # Retain this occurrence's actual publication, including a freshness
    # wrapper, before another pipeline can replace the same query receipt.
    with _tool_receipt_lock(batch.task):
        previous = batch.cache.get(cache_key)
        _store_tool_result_cache_entry(batch.task, cache_key, entry,
                                       generation=generation)
        published = batch.cache.get(cache_key)
        if published is not None and published is not previous:
            batch.produced_cache_entries[call_id] = published


def _settle_receipt(batch: ToolExecutionBatch, ports: ToolExecutionPorts,
                    item: tuple, call_id: str, content: Any) -> None:
    # SETTLE NOW, at this tool's own completion: its result is budgeted, its
    # round stamped and tool_complete emitted without waiting for siblings.
    # Screenshots settle here too; only their multimodal MESSAGE append stays
    # in the post-phase, where tool-call order lives.
    settle_arg = ports.settle_content(
        batch.task, item[5], content, batch.model, item[1], call_id)
    batch.settle(
        batch.task, item[1], call_id, item[3], item[4], item[5], settle_arg,
        idempotent_tools=batch.reusable_tools, cache=batch.cache,
        tid=batch.task_label, round_num=batch.round_number,
        canonical_content=ports.canonical_content(content))


def _record_failure(batch: ToolExecutionBatch, ports: ToolExecutionPorts,
                    item: tuple | None, call_id: str, name: str,
                    error: Exception) -> None:
    # UnknownWorkspaceRootError is the LLM's fault (bad root prefix); it is
    # already logged at WARNING at the raise site + INFO by the executor.
    unknown_root = False
    try:
        from lib.project_mod.config import UnknownWorkspaceRootError
        unknown_root = isinstance(error, UnknownWorkspaceRootError)
    except ImportError as import_error:
        ports.log.debug('[Task %s] UnknownWorkspaceRootError import failed: %s',
                        batch.task_label, import_error)
    if unknown_root:
        ports.log.info(
            '[Task %s] conv=%s Tool %s (tc_id=%s) recoverable workspace-root '
            'error returned to LLM at round %d: %s',
            batch.task_label, batch.task.get('convId', ''), name, call_id,
            batch.round_number, error)
    else:
        ports.log.error(
            '[Task %s] conv=%s Tool %s (tc_id=%s) execution failed at round %d model=%s',
            batch.task_label, batch.task.get('convId', ''), name, call_id,
            batch.round_number, batch.model, exc_info=True)
    from lib.tasks_pkg.executor._effects import tool_exception_result
    batch.results[call_id] = (tool_exception_result(
        batch.task, name, item[5] if item is not None else None, error), False)
    batch.verdicts[call_id] = 'error'


def _record_completion(batch: ToolExecutionBatch, ports: ToolExecutionPorts,
                       future: Future, futures: dict, items_by_id: dict,
                       has_write: bool) -> None:
    call_id, name, generation = futures[future]
    try:
        receipt = future.result()
        returned_id, content, is_search = receipt
        if isinstance(receipt, UnexecutedCall):
            item = items_by_id[call_id]
            batch.results[call_id] = (content, False)
            batch.verdicts[call_id] = receipt.verdict
            # Worker-side refusals already settled; Stop before entry did not.
            batch.settle(
                batch.task, name, call_id, item[3], item[4], item[5], content,
                idempotent_tools=batch.reusable_tools, cache=batch.cache,
                tid=batch.task_label, round_num=batch.round_number,
                terminal_status=receipt.verdict)
            return
        batch.results[returned_id] = (content, is_search)
        item = items_by_id.get(returned_id)
        if name in batch.reusable_tools:
            if item is not None:
                _publish_reusable_receipt(batch, item, returned_id, name, content,
                                          is_search, generation, has_write)
        elif name in batch.write_tools or name in _PROJECT_MUTATING_TOOLS:
            _invalidate_project_cache(batch.task, trigger=name)
        if item is not None:
            _settle_receipt(batch, ports, item, returned_id, content)
    except Exception as error:
        _record_failure(batch, ports, items_by_id.get(call_id), call_id, name, error)


def _collect_completions(batch: ToolExecutionBatch, ports: ToolExecutionPorts,
                         futures: dict, calls_by_future: dict, items_by_id: dict,
                         *, has_write: bool) -> bool:
    """Settle receipts as they arrive; return whether a deadline expired."""
    expired = False
    try:
        for future in ports.collect(futures, batch.task, _EntryDeadlines(calls_by_future)):
            if isinstance(future, _ToolCollectionExpiry):
                expired = True
                ports.expire(
                    batch.task, future, futures=futures, parallel_by_id=items_by_id,
                    tool_results=batch.results, tool_verdicts=batch.verdicts,
                    settle_current=batch.settle, idempotent_tools=batch.reusable_tools,
                    cache=batch.cache, tid=batch.task_label, round_num=batch.round_number)
                continue
            if future is None:
                ports.log.info('[Task %s] Abort detected during tool execution — '
                               'cancelling remaining', batch.task_label)
                for pending, (pending_id, _name, _generation) in futures.items():
                    never_started = pending.cancel()
                    if pending_id in batch.results:
                        continue
                    # An entered unbounded call is joined by the drain and
                    # keeps its own receipt and effect evidence.
                    if calls_by_future[pending].bounded or never_started:
                        batch.results[pending_id] = (_STOPPED, False)
                        batch.verdicts[pending_id] = 'aborted'
                break
            _record_completion(batch, ports, future, futures, items_by_id, has_write)
    except (TimeoutError, _FuturesTimeoutError):
        expired = True
        pending_count = sum(1 for future in futures if not future.done())
        ports.log.error(
            '[Task %s] conv=%s Tool parallel execution timeout at round %d (%d tools pending) model=%s',
            batch.task_label, batch.task.get('convId', ''), batch.round_number,
            pending_count, batch.model, exc_info=True)
        # Harvest results from futures that completed but weren't yielded
        # before the TimeoutError was raised. A published verdict always wins
        # over later worker completion.
        ports.harvest(
            futures, batch.task, batch.results, batch.verdicts,
            round_num=batch.round_number, reason='collector timeout',
            items_by_id=items_by_id)
        for future, (call_id, name, _generation) in futures.items():
            if call_id not in batch.results:
                future.cancel()
                from lib.tasks_pkg.executor._effects import tool_timeout_result
                batch.results[call_id] = (
                    tool_timeout_result(batch.task, name, items_by_id[call_id][5]), False)
                # 'error', not 'aborted': the user did not stop this tool —
                # the pool ceiling did. Effect evidence remains independent:
                # an entered worker may still be running.
                batch.verdicts[call_id] = 'error'
    return expired


def execute_admitted_tools(
    batch: ToolExecutionBatch, ports: ToolExecutionPorts,
    calls: list[AdmittedCall], *, has_write: bool,
) -> bool:
    """Start every admitted call now; return whether a collection deadline expired.

    Replay still follows provider order in the pipeline's post-phase.
    """
    if not calls:
        return False
    if batch.task.get('aborted'):
        ports.log.info('[Task %s] Skipping %d admitted tools — task aborted',
                       batch.task_label, len(calls))
        for call in calls:
            batch.results[call.item[2]] = (_STOPPED, False)
            batch.verdicts[call.item[2]] = 'aborted'
        return False

    expired = False
    pool = None
    heartbeat_stop = None
    futures: dict[Future, tuple] = {}
    calls_by_future: dict[Future, AdmittedCall] = {}
    items_by_id = {call.item[2]: call.item for call in calls}
    interrupted = False
    try:
        # Every accepted call gets a worker immediately; the hint sizes only
        # idle thread reuse of the demand executor (docs/RESOURCE_CONTROL.md).
        pool = ports.pool_factory(
            max_workers=ports.idle_reuse_hint(
                len(calls), programmatic=_programmatic_children(calls)),
            metric_pool='tool-parallel', thread_name_prefix='tool-parallel')
        # One ticker covers every call of the round. A blocking tool emits no
        # delta, so without it the SSE stream goes silent and a buffering
        # proxy idle-times-out. Ordinary ticks are graded ``_selfTick`` and do
        # not feed the reaper clocks; only ratified human waits do
        # (see _heartbeat.py).
        heartbeat_stop, _heartbeat_thread = ports.start_heartbeat(
            batch.task, [call.item for call in calls], batch.task_label)
        from lib.tools.registry import declared_tool_timeout_ms
        pool_timeout = ports.timeout_setting('TOOL_PARALLEL_TIMEOUT', 300, 1, 3600)
        futures_by_id: dict[str, Future] = {}
        for call in calls:
            # The ordinary ceiling must cover a longer declared tool wait.
            # Per-occurrence deadlines keep unrelated siblings at their own
            # ceiling, measured from the handler's entry.
            timeout_seconds = max(
                pool_timeout,
                (declared_tool_timeout_ms(
                    call.item[1], call.item[3], tool_schemas=batch.tools) or 0) / 1000.0)
            predecessors = tuple(futures_by_id[call_id] for call_id in call.predecessors
                                 if call_id in futures_by_id) + call.sibling_predecessors
            future, start_generation = ports.submit(
                pool, partial(_run_admitted_call, batch, ports, call, predecessors,
                              timeout_seconds, copy_context()))
            settle = call.adopt_settlement() if call.adopt_settlement is not None else None
            if settle is not None:
                # Runs once the worker returns, raises or is cancelled unstarted,
                # never when only its collection deadline expires.
                future.add_done_callback(lambda _done, finish=settle: finish())
            futures[future] = (call.item[2], call.item[1], start_generation)
            calls_by_future[future] = call
            futures_by_id[call.item[2]] = future
        expired = _collect_completions(batch, ports, futures, calls_by_future,
                                       items_by_id, has_write=has_write)
    except BaseException:
        interrupted = True
        raise
    finally:
        # Ownership starts at pool acquisition, before heartbeat startup.
        # All exits must release the acquired executor.
        try:
            if heartbeat_stop is not None:
                heartbeat_stop.set()
            if pool is not None and batch.task.get('aborted'):
                ports.drain(batch.task, futures)
            elif pool is not None and interrupted:
                ports.drain(batch.task, futures, items_by_id=items_by_id)
        finally:
            if pool is not None:
                # Ordinary expiry remains finite; explicit Stop and exceptional
                # dispatch exits drain their resource owners before returning.
                pool.shutdown(wait=not expired, cancel_futures=True)

    if len(batch.results) < len(batch.calls):
        # A collector may omit a terminal Future, and Stop leaves entered
        # unbounded calls to the drain. Reuse their executor receipts before
        # classifying a gap; do not enter handlers or add a new wait.
        ports.harvest(
            futures, batch.task, batch.results, batch.verdicts,
            round_num=batch.round_number, reason='collector gap',
            items_by_id=items_by_id)
    if batch.task.get('aborted') and any(
            call.entered and call.item[1] in batch.write_tools for call in calls):
        _invalidate_project_cache(batch.task, trigger='stopped write')
    return expired
