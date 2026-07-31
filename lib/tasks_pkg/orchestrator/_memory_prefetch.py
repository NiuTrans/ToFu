# HOT_PATH — invoked once per user turn from run_task.
# Prefer logger.debug() over logger.info(). logger.info() is reserved
# for rare, high-signal events.
"""Memory-prefetch gate — extracted from run_task (pt_03f4cdf1 slice 9).

Section 3.5 of run_task's body: the proactive, per-user-turn memory
prefetch that surfaces past lessons via BM25 → cheap-LLM precision →
``<relevant_memories>`` injection. Also stashes the sibling
``_profileConsolidateEligible`` flag the post-done spawner in
``_finalize.py`` reads — those two locals ARE the seam that used to
force this block to live inside run_task.

Never raises: a prefetch failure is advisory and logs a warning.

OFF THE CRITICAL PATH (epic pt_e92d3be4)
----------------------------------------
The rerank carries a hard 800 ms deadline, and this ran synchronously
between context-inject and the first ``dispatch_stream`` — so every turn
paid up to 800 ms (typically 200-600 ms) before the user saw a token.
``maybe_run_memory_prefetch`` now SPAWNS the work and returns immediately;
``await_memory_prefetch`` joins it just before the body is built, so the
rerank overlaps the remaining turn preparation instead of following it.

Why that is safe despite the apparent ordering dependency: the rerank
derives its query from ``messages``, which ``_inject_system_contexts``
mutates — BUT every mutation it makes to the true tail is wrapped in
``<system-reminder>``, and ``_msg_plain_text``
(``lib/memory/prefetch/_query.py``) strips exactly that wrapper. The query
text is therefore byte-identical before and after inject. That invariant is
the premise of this offload and is pinned by
``tests/test_memory_prefetch_offloaded.py::
test_query_inputs_are_invariant_across_context_inject`` — if it ever goes
red, revisit the offload rather than the assertion.

The input that is genuinely NOT ready earlier is ``has_real_tools`` /
``tool_list`` (assembled after the existing prefetch pool spawns), which is
why this is its own thread rather than a third job on that pool.
"""

from __future__ import annotations

import threading
from typing import Any

from lib.log import audit_log, get_logger
from lib.tasks_pkg.manager import append_event

logger = get_logger(__name__)

# Guards the read-modify-write on task['_checkpointUsage']. The sink can fire
# from an ABANDONED rerank worker thread, concurrently with the turn's own
# bookkeeping, so the fold must not be a bare read-modify-write.
_usage_lock = threading.Lock()

# Task states past which the finalizer has already read task['usage'] to settle
# the wallet. A bill arriving after that cannot be folded in without silently
# re-billing behind the finalizer, so it is surfaced as an audit metric instead
# of being dropped — the spend stays discoverable either way.
_SETTLED_STATES = frozenset({'done', 'error', 'aborted', 'cancelled'})

# Where the in-flight prefetch thread is parked so await_memory_prefetch can
# join it. Kept on the task (not a module global) so concurrent tasks in the
# same process never join each other's work.
_PENDING_SLOT = '_memory_prefetch_thread'

# How long the turn is willing to wait for the prefetch before proceeding
# WITHOUT the injection. Slightly above the rerank's own 800 ms deadline so a
# healthy run always lands; past that the worker is abandoned rather than
# allowed to inject into a body that is already being built.
_AWAIT_BUDGET_S = 1.2


def await_memory_prefetch(task: dict[str, Any],
                          timeout: float = _AWAIT_BUDGET_S) -> None:
    """Join the in-flight prefetch, if any, before the wire body is built.

    Bounded on purpose. The prefetch mutates ``messages`` in place, so it must
    finish BEFORE those messages are serialized — but waiting without a bound
    would just reinstate the blocking behaviour this offload removes. On
    overrun the turn proceeds with no injection: a missing advisory memory is
    strictly better than a late write into a body already on the wire.

    Idempotent, and a no-op when nothing is pending.
    """
    _t = task.pop(_PENDING_SLOT, None)
    if _t is None:
        return
    try:
        _t.join(timeout)
        if _t.is_alive():
            logger.warning(
                '[Task %s] memory prefetch exceeded its %.1fs join budget — '
                'proceeding without injection this turn',
                str(task.get('id', '?'))[:8], timeout)
    except Exception as _e:
        logger.debug('[orchestrator] prefetch join failed: %s', _e)


def make_prefetch_usage_sink(task: dict[str, Any]):
    """Return a callback that bills a memory-prefetch rerank to ``task``.

    The rerank is a real LLM call on every user turn. Its usage used to live
    only in the prefetch's own diagnostics, so it never reached the cost
    popover, the wallet or the daily report — and the WORST case was the
    timeout: ``_run_with_deadline`` abandons the worker, which stops us
    waiting but not the gateway billing, so the rounds that cost money and
    returned nothing recorded nothing.

    The usage is folded into ``_checkpointUsage`` — the same carry-forward slot
    the continue-checkpoint and turn-retry paths use, which ``_finalize_task``
    merges into the terminal ``task['usage']``. Semantically apt: the prefetch
    is billed alongside this turn but is not part of any single api_round.

    Best-effort by contract: accounting must never break a turn.
    """
    def _sink(usage):
        if not usage:
            return
        try:
            from lib.cost import merge_usage_totals
            with _usage_lock:
                if task.get('status') in _SETTLED_STATES:
                    # Landed after the finalizer settled — record it where a
                    # human can still find it rather than dropping it.
                    audit_log('memory_prefetch_usage_orphaned',
                              task_id=task.get('id', ''),
                              conv_id=task.get('convId', ''),
                              **{k: v for k, v in usage.items()
                                 if isinstance(v, (int, float))
                                 and not isinstance(v, bool)})
                    return
                task['_checkpointUsage'] = merge_usage_totals(
                    task.get('_checkpointUsage'), usage)
        except Exception as _e:
            logger.debug('[orchestrator] prefetch usage sink failed: %s', _e)

    return _sink


def maybe_run_memory_prefetch(
    *,
    task: dict[str, Any],
    cfg: dict[str, Any],
    messages: list[dict[str, Any]],
    tool_list: list[dict[str, Any]] | None,
    project_path: str | None,
    project_enabled: bool,
    memory_enabled: bool,
    has_real_tools: bool,
    injected_tool_calls: int,
) -> None:
    """Run BM25 + cheap-LLM memory prefetch, gated on eligibility.

    Skipped if any of:
      * memory toggle disabled (``memory_enabled=False``)
      * no real tools (memory tools unavailable anyway)
      * continue/resume turn (tool_history was replayed →
        ``injected_tool_calls > 0`` → not a fresh turn)

    Always stashes ``task['_profileConsolidateEligible']`` for the
    post-done profile-consolidation spawner in ``_finalize.py``.
    """
    task['_profileConsolidateEligible'] = bool(memory_enabled and has_real_tools)

    if not (memory_enabled and has_real_tools and not injected_tool_calls):
        return

    try:
        from lib.memory.prefetch import run_memory_prefetch
        # Active-tools list lets the cheap-LLM filter drop memories
        # about subsystems the user can't currently use (e.g. browser
        # memories when browser is off).
        _active_tools: list[str] = []
        for _t in (tool_list or []):
            try:
                _active_tools.append(_t['function']['name'])
            except (KeyError, TypeError) as _e_audit:
                logger.debug(
                    '[orchestrator] memory_prefetch caught %s: %s',
                    type(_e_audit).__name__, _e_audit)
                continue
        # ★ Extra workspace roots for memory scoping — recomputed
        #   locally at the (single) call site since pt_03f4cdf1 slice 3
        #   moved the prefetch pool init out of run_task. The same
        #   derivation is done inside start_prefetches for the
        #   background memory prefetch; they're deliberately
        #   independent so a future consumer can be added without
        #   threading a shared list through the call stack. Same rule:
        #   extras are projectPaths[1:] minus the primary, empty when
        #   disabled.
        _pp = project_path if project_enabled else None
        _mem_extra_paths: list[str] = []
        if project_enabled and _pp:
            _all_mem_paths = cfg.get('projectPaths') or []
            _mem_extra_paths = (
                [p for p in _all_mem_paths[1:] if p and p != _pp]
                if len(_all_mem_paths) > 1 else [])

        # ★ OFF THE CRITICAL PATH — everything above is cheap bookkeeping;
        #   the expensive part (BM25 + an 800 ms-deadline cheap-LLM rerank)
        #   now runs on its own thread. The caller returns immediately and
        #   joins via await_memory_prefetch() just before the body is built,
        #   so the rerank overlaps the rest of turn preparation.
        #
        #   All arguments are bound HERE, on the calling thread, so the
        #   worker never races the caller for them. `messages` is passed by
        #   reference by design — the injection must land on the real list —
        #   and the bounded join is what keeps that write ordered before
        #   serialization.
        _usage_sink = make_prefetch_usage_sink(task)
        _project_scope = project_path if project_enabled else None

        def _run_prefetch():
            try:
                run_memory_prefetch(
                    messages,
                    project_path=_project_scope,
                    task=task,
                    emit_event=lambda ev: append_event(task, ev),
                    active_tools=_active_tools,
                    extra_paths=_mem_extra_paths,
                    usage_sink=_usage_sink,
                )
            except Exception as _e:
                # Advisory path — a failure here must never reach the turn.
                logger.warning('[Task %s] memory prefetch failed: %s',
                               str(task.get('id', '?'))[:8], _e, exc_info=True)

        _thread = threading.Thread(
            target=_run_prefetch, daemon=True,
            name='mem-prefetch-%s' % str(task.get('id', '?'))[:8])
        task[_PENDING_SLOT] = _thread
        _thread.start()
    except Exception as _e:
        # Advisory path — never block the task on prefetch failure.
        logger.warning('[Task %s] memory prefetch failed: %s',
                       task['id'][:8], _e, exc_info=True)
