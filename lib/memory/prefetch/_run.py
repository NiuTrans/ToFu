"""lib/memory/prefetch/_run.py — Orchestration entry point.

Runs the full BM25 → cheap-LLM → inject pipeline (round 0 only, once per
user turn) and emits SSE ``memory_prefetch`` events at every stage so the
frontend can explain the background latency.
"""
from __future__ import annotations

import time

from lib.log import audit_log, get_logger

from lib.memory.prefetch._config import PREFETCH_BM25_TOP_N, PREFETCH_ENABLED
from lib.memory.prefetch._inject import inject_relevant_memories
from lib.memory.prefetch._query import (
    _build_recent_turns_text,
    _extract_current_user_request,
)
from lib.memory.prefetch._rerank import _call_cheap_reranker
from lib.memory.prefetch._shortlist import _bm25_top_n

logger = get_logger(__name__)


def run_memory_prefetch(messages: list,
                        project_path: str | None,
                        task: dict | None = None,
                        emit_event=None,
                        active_tools: list[str] | None = None,
                        extra_paths: list[str] | None = None) -> list[dict]:
    """Run the full BM25 → cheap-LLM → inject pipeline.

    Args:
        messages:     The message list for the current task; its last user
                      message will receive the <relevant_memories> block.
                      Mutated in-place.
        project_path: Project path for scoping project memories. Also passed
                      to the cheap-LLM filter (as a basename) so memories
                      tagged for unrelated projects are dropped.
        task:         The current task dict (for logging/audit).
        emit_event:   Callable(event_dict).  Used to emit SSE events.
                      Pass None to suppress frontend notifications.
        active_tools: Names of tools available this turn (e.g. ``['read_files',
                      'web_search']``).  Lets the cheap-LLM filter drop
                      memories about subsystems the user can't currently use.
                      Pass None or [] when unknown.
        extra_paths:  Additional workspace roots (multi-root session) whose
                      memories are unioned in alongside the primary root's.

    Returns:
        The list of memory dicts that were injected (empty list if none).
        Always returns a list — errors are logged + swallowed (advisory path).
    """
    if not PREFETCH_ENABLED:
        return []

    # Terminal phases whose payload should also be stashed on the task so
    # it survives into the DB-persisted message and the poll fallback.
    _TERMINAL_PHASES = {'done', 'skipped', 'failed'}

    def _emit(phase: str, **kw):
        """Emit a `memory_prefetch` SSE event + stash on task for persistence."""
        payload = {'phase': phase, **kw}
        if emit_event:
            try:
                from lib.agent_core.events import EventType, build_event
                emit_event(build_event(EventType.MEMORY_PREFETCH, **payload))
            except Exception as e:  # pragma: no cover
                logger.debug('[MemPrefetch] emit_event failed: %s', e)
        if task is not None:
            try:
                task['_memoryPrefetch'] = dict(payload)
            except (TypeError, AttributeError) as e:
                logger.debug('[MemPrefetch] could not stash payload on task: %s', e)

    t_start = time.time()
    tid = (task or {}).get('id', '?')[:8]

    try:
        from lib.memory.storage import get_eligible_memories
        memories = get_eligible_memories(project_path, extra_paths=extra_paths)
    except Exception as e:
        logger.warning('[MemPrefetch] get_eligible_memories failed: %s', e)
        _emit('failed', reason=f'load_error: {e}')
        return []

    if not memories:
        _emit('skipped', reason='no_memories')
        return []

    # ── Query construction
    #   For BM25 we keep the full last-K transcript (current request + prior
    #   turns) — wider lexical surface helps recall on the coarse stage.
    #   For the cheap-LLM rerank we split current_request out into its own
    #   section so the model can anchor on it.
    recent_turns = _build_recent_turns_text(messages)
    if not recent_turns.strip():
        _emit('skipped', reason='empty_query')
        return []
    current_request = _extract_current_user_request(messages)
    rerank_recent_turns = _build_recent_turns_text(messages,
                                                   exclude_last_user=True)

    _emit('started',
          total_memories=len(memories),
          candidate_target=PREFETCH_BM25_TOP_N)

    # ── Stage 1: BM25 coarse
    t_bm25 = time.time()
    scored = _bm25_top_n(memories, recent_turns, top_n=PREFETCH_BM25_TOP_N)
    bm25_ms = int((time.time() - t_bm25) * 1000)

    if not scored:
        logger.debug('[MemPrefetch][%s] BM25 found zero scored candidates '
                     '(memories=%d) — skipping cheap-LLM stage', tid, len(memories))
        _emit('skipped', reason='bm25_empty', bm25_ms=bm25_ms)
        return []

    candidate_indices = [i for i, _ in scored]
    _emit('bm25_done',
          candidates=len(candidate_indices),
          bm25_ms=bm25_ms,
          top_score=round(scored[0][1], 2))

    # ── Stage 2: Cheap-LLM precision filter
    _emit('rerank_started', candidates=len(candidate_indices))

    # NOTE: no timeout, no exception swallowing. If the cheap reranker
    # raises, we let it propagate — better to surface the failure than
    # silently fall back to a noisy BM25 top-K injection.
    selected_idx, diag = _call_cheap_reranker(
        memories, candidate_indices, rerank_recent_turns,
        current_request=current_request,
        project_path=project_path,
        active_tools=active_tools,
    )
    rerank_ms = diag.get('elapsed_ms', 0)

    # ── Stage 3: Inject
    selected_memories: list[dict] = [memories[i] for i in selected_idx]

    if not selected_memories:
        _emit('done',
              selected=0,
              bm25_ms=bm25_ms,
              rerank_ms=rerank_ms,
              total_ms=int((time.time() - t_start) * 1000),
              timed_out=bool(diag.get('timed_out')),
              reason=diag.get('skipped') or 'none_relevant')
        return []

    try:
        _conv_id = (task or {}).get('convId') or None
        inject_relevant_memories(messages, selected_memories,
                                 conv_id=_conv_id)
    except Exception as e:
        logger.error('[MemPrefetch] inject failed: %s', e, exc_info=True)
        _emit('failed', reason=f'inject_error: {e}')
        return []

    total_ms = int((time.time() - t_start) * 1000)
    # Send the FULL description to the frontend so the expanded provenance
    # strip can show it in full (the chip used to hard-cap at 120 chars,
    # which clipped the very text the user wants to read on expand). The
    # description is already bounded at write time (~120 chars by convention)
    # but may legitimately run longer; the chip wraps it, so no UI cap here.
    selection_summary = [
        {'name': m.get('name', ''),
         'scope': m.get('scope', ''),
         'description': m.get('description', '')}
        for m in selected_memories
    ]

    logger.info(
        '[MemPrefetch][%s] injected %d memories (from %d BM25 candidates) '
        'in %dms (bm25=%dms rerank=%dms)',
        tid, len(selected_memories), len(candidate_indices),
        total_ms, bm25_ms, rerank_ms,
    )
    audit_log('memory_prefetch',
              task_id=(task or {}).get('id', ''),
              conv_id=(task or {}).get('convId', ''),
              injected=len(selected_memories),
              bm25_candidates=len(candidate_indices),
              bm25_ms=bm25_ms,
              rerank_ms=rerank_ms,
              memory_names=[m.get('name', '') for m in selected_memories])

    _emit('done',
          selected=len(selected_memories),
          candidates=len(candidate_indices),
          bm25_ms=bm25_ms,
          rerank_ms=rerank_ms,
          total_ms=total_ms,
          memories=selection_summary)

    return selected_memories
