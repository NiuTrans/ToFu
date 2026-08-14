# HOT_PATH
"""Tool-execution pipeline — approval → parallel dispatch → result-append.

The public entry-point is :func:`execute_tool_pipeline`, the big orchestrator
extracted from the inner loop of ``orchestrator.run_task``.  Also houses the
``_append_screenshot_message`` multimodal-result helper.
"""

from __future__ import annotations

import json
import os
import hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed
# concurrent.futures.TimeoutError only became an alias of builtin
# TimeoutError in 3.11 — on 3.10 it is a DISTINCT class, so
# ``except TimeoutError`` does NOT catch an as_completed/fut.result timeout
# and the pool-timeout lane never fires (3.10 CI leg, 2026-08-06).
from concurrent.futures import TimeoutError as _FuturesTimeoutError
from typing import Any

from lib.agent_core.events import EventType, build_event, now_ms
from lib.log import get_logger
from lib.model_info import model_supports_vision
from lib.tasks_pkg.compaction import (
    budget_tool_result,
    clamp_tool_result_text,
    enforce_round_aggregate_budget,
    mark_empty_result,
)
from lib.tasks_pkg.executor import _execute_tool_one, _finalize_tool_round
from lib.tasks_pkg.manager import _strip_base64_for_snapshot, append_event
from lib.tasks_pkg.tool_hooks import run_post_hooks, run_pre_hooks

from lib.tasks_pkg.tool_dispatch._approval import _handle_approval
from lib.tasks_pkg.tool_dispatch._flags import (
    _build_cache_hit_meta,
    _invalidate_project_cache,
    _make_cache_key,
    _safe_count_tokens,
    _task_partitions,
    _unpack_cache_entry,
)
from lib.tasks_pkg.tool_dispatch._heartbeat import (
    _SERIAL_BLOCKING_TOOLS,
    _execute_tool_one_pooled,
    _start_tool_heartbeat,
)

logger = get_logger(__name__)


# In-memory protocol tools whose calls are order-sensitive inside one model
# response.  They are deliberately separate from ``_write_tools``: updating a
# task-local checklist must be serial, but it is not an external mutation and
# therefore must not trigger Manual-mode write approval or project-cache
# invalidation.  Keep this list narrow; the serial lane preserves
# ``parsed_tcs`` order exactly.
_ORDERED_STATE_TOOLS = frozenset({'todo_write'})


def _blocked_multi_agent_write(
        tc: dict[str, Any], fn_name: str, write_tools: frozenset) -> str:
    """Return the non-root agent name when its write must be refused."""
    caller = tc.get('caller') if isinstance(tc, dict) else None
    if not isinstance(caller, dict) or caller.get('type') != 'multi_agent':
        return ''
    agent_name = str(caller.get('agent_name') or '')
    return (agent_name if agent_name and agent_name != '/root'
            and fn_name in write_tools else '')


def _call_id_signature(fn_name: str, fn_args: Any) -> str:
    """Stable identity for replay protection; never trusts display metadata."""
    try:
        payload = json.dumps(
            {'name': str(fn_name or ''),
             'arguments': fn_args if isinstance(fn_args, dict) else {}},
            ensure_ascii=False, sort_keys=True, separators=(',', ':'),
            default=str)
    except (TypeError, ValueError) as exc:
        logger.debug('[ToolDispatch] call signature JSON fallback: %s', exc)
        payload = f'{fn_name!s}\0{fn_args!r}'
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def _finalize_call_id_replay(
    task: dict[str, Any], fn_name: str, tc_id: str, rn: int,
    round_entry: dict[str, Any] | None, content: Any, *, status: str = 'done',
) -> None:
    """Settle the display row for a call-ID replay without executing hooks."""
    if isinstance(content, str):
        text = content
    else:
        try:
            text = json.dumps(content, ensure_ascii=False, default=str)
        except (TypeError, ValueError) as exc:
            logger.debug('[ToolDispatch] replay display serialization fallback: %s',
                         exc)
            text = str(content)
    if round_entry is not None:
        _finalize_tool_round(task, rn, round_entry, [{
            'type': 'tool_call_replay', 'toolName': fn_name,
            'content': text, 'badge': 'replayed',
            'snippet': 'Duplicate call_id — previous result reused',
        }], query_override=round_entry.get('query', fn_name))
        round_entry['toolContent'] = text
        round_entry['status'] = status if status in (
            'error', 'rejected', 'aborted') else 'done'
        round_entry['_idempotentReplay'] = True


def _bounded_tool_env_int(name: str, default: int, minimum: int,
                          maximum: int) -> int:
    raw = os.environ.get(name, '')
    try:
        value = int(raw) if raw else default
    except (TypeError, ValueError):
        logger.warning('[ToolPipeline] invalid %s=%r; using %d',
                       name, raw, default)
        value = default
    return max(minimum, min(value, maximum))


def _parallel_worker_limit(item_count: int, *, programmatic: bool = False
                           ) -> int:
    """Personal-server-safe pool size with a hard PTC concurrency ceiling."""
    configured = _bounded_tool_env_int(
        'TOOL_MAX_PARALLEL_WORKERS', 8, 1, 32)
    if programmatic:
        from lib.tools.programmatic import PROGRAMMATIC_MAX_CONCURRENT_CALLS
        configured = min(configured, PROGRAMMATIC_MAX_CONCURRENT_CALLS)
    return max(1, min(configured, max(1, int(item_count))))


def _settle_tool_result(
    task: dict[str, Any],
    fn_name: str,
    tc_id: str,
    fn_args: dict[str, Any],
    rn: int,
    round_entry: dict[str, Any] | None,
    tool_content: Any,
    *,
    idempotent_tools: frozenset,
    cache: dict,
    tid: str,
    round_num: int,
    terminal_status: str | None = None,
) -> Any:
    """Settle ONE tool call: budget its result, stamp the round, emit
    ``tool_complete``. Returns the final (budgeted) content for the message.

    ★ WHY THIS IS A FUNCTION (pt_67ffc2b7). This work used to live inline in the
    post-phase loop, which runs AFTER ``pool.shutdown(wait=True)``. That made a
    tool's completion unobservable until every SIBLING in the same round had
    also finished: in a round with a 0.05s ``read_files`` and a 40s
    ``web_search``, the fast tool's content/token chips landed 40 seconds after
    it actually returned, and the user had no way to tell which of the two was
    slow. Hoisting it into a function lets the ``as_completed`` loop settle each
    tool AT ITS OWN completion instant, while the post-phase keeps ownership of
    the one thing that genuinely must stay ordered — appending the
    ``role:'tool'`` messages in the model's ORIGINAL tool-call order.

    Everything here is PER-TOOL by construction. The round-AGGREGATE budget is
    deliberately NOT here: it needs every result to size the round, so it stays
    after the barrier and corrects an already-announced result with a
    ``tool_compacted`` event instead of delaying the first announcement.

    ``terminal_status`` (pt_ac380e3d) carries a NON-SUCCESS verdict for the
    lanes where the tool never actually ran — ``rejected`` (hallucinated call,
    pre-hook block, user pressed Reject) or ``aborted`` (user pressed Stop) —
    and, since 2026-08-06, ``error`` for the lanes where it ran but FAILED
    (raised, or cancelled by the parallel-pool timeout ceiling).
    Settling those lanes promptly is the whole point of this extension, but a
    settle that reported success would be far worse than the latency it
    removes: a write the user REFUSED would render as applied. So the verdict is
    stamped on the round AND shipped on the wire, and the client is required
    never to overwrite it (see ``stream_reducer.js``' tool_complete case).
    Without this parameter such a round would sit at ``pending_approval`` /
    ``searching`` until the end-of-task dangling sweep — i.e. spin for the rest
    of the turn.

    Idempotent: a second call for the same ``tc_id`` is a no-op (returns the
    already-settled content), so the post-phase can call it unconditionally
    without double-emitting for a tool an earlier lane already settled.
    """
    _settled = task.setdefault('_settled_tool_results', {})
    if tc_id in _settled:
        return _settled[tc_id]

    # ★ Post-tool hooks: modify/enrich result after execution.
    if isinstance(tool_content, str):
        tool_content = run_post_hooks(fn_name, fn_args, tool_content, task)

    # ★ Empty result marker: prevent models from misinterpreting
    # empty tool results as conversation end.
    if isinstance(tool_content, str):
        tool_content = mark_empty_result(fn_name, tool_content)

    # ★ Layer 0: Budget tool results before they enter context.
    # Persists oversized results to disk (inspired by Claude Code's
    # per-tool maxResultSizeChars + persistence).  Exempt tools
    # (read_files) pass through unchanged.
    # Layer 1 (micro_compact) will further compress these once
    # they fall outside the hot tail.
    _l0_pre_chars = len(tool_content) if isinstance(tool_content, str) else 0
    if round_entry and isinstance(tool_content, str):
        # Keep only the numeric pre-compaction observation; retaining another
        # full copy would defeat the context/storage savings being measured.
        _raw_tokens = _safe_count_tokens(
            tool_content, model=task.get('model', '') if task else '')
        if _raw_tokens > 0:
            round_entry['rawToolTokens'] = _raw_tokens
    if isinstance(tool_content, str):
        _conv_id = task.get('convId', '') if task else ''
        tool_content = budget_tool_result(fn_name, tool_content,
                                          tool_use_id=tc_id,
                                          conv_id=_conv_id)
    # If budget_tool_result shrank the content (persisted to disk
    # or fell back to head+tail truncation), stamp the round so
    # the frontend can flag this tool call as L0-compacted. Any
    # length reduction is a signal — budget_tool_result only
    # mutates content when it exceeds the per-tool budget.
    if (round_entry
            and isinstance(tool_content, str)
            and _l0_pre_chars > len(tool_content)):
        round_entry['compactionLayer'] = 'L0'
        round_entry['compactedFromChars'] = _l0_pre_chars
        round_entry['compactedToChars'] = len(tool_content)

    # ★ Layer 2: tool-agnostic hard ceiling — the LAST line of
    # defence. Unlike Layer 0 (budget_tool_result) this has NO
    # per-tool exemption, so it ALSO clamps read_files (which Layer 0
    # skips). Makes the "opaque blob floods context" bug class
    # unrepresentable: a relative-path PNG decoded as text, a str()'d
    # image dict, or any future leak gets clamped to a survivable
    # result instead of a fatal HTTP 400. See conv mqgfkmxy (2026-06).
    if isinstance(tool_content, str):
        _conv_id_hc = task.get('convId', '') if task else ''
        tool_content = clamp_tool_result_text(
            fn_name, tool_content, tc_id=tc_id, conv_id=_conv_id_hc)

    # ★ Sync the budgeted/offloaded form back into the dedup cache.
    # The cache entry was populated with the PRE-budget content (the
    # parallel-phase writer / the streaming prefetch injector), while
    # budgeting above only rewrote the local message copy. Left unsynced, the
    # full result (e.g. a 680 KB web_search dump) lingers in
    # ``_tool_result_cache`` — it serializes into the persisted ``raw_state``
    # (state balloon) AND is replayed verbatim on a later dedup hit,
    # re-flooding context with content the offloader had already spilled to
    # disk. Rewrite content[0] to the budgeted string, preserving the rest of
    # the entry (is_search / source / display / engine_breakdown / vertical)
    # so the rich UI-replay path is unchanged.
    if isinstance(tool_content, str) and fn_name in idempotent_tools:
        _sync_key = _make_cache_key(fn_name, fn_args)
        _cached_entry = cache.get(_sync_key)
        if (_cached_entry is not None
                and isinstance(_cached_entry, (tuple, list))
                and len(_cached_entry) >= 1
                and isinstance(_cached_entry[0], str)
                and len(_cached_entry[0]) > len(tool_content)):
            cache[_sync_key] = (tool_content, *tuple(_cached_entry)[1:])

    # ★ Emit tool_complete AFTER budgeting so that toolContent
    #   reflects the ACTUAL content given to the model (budgeted/
    #   persisted form).  Preview must show what the model sees.
    try:
        if isinstance(tool_content, str):
            tc_content_str = tool_content
        else:
            tc_content_str = json.dumps(tool_content, ensure_ascii=False)
        if len(tc_content_str) > 50000:
            tc_content_str = tc_content_str[:50000] + '\n... [truncated for continue context]'

        # ★ Persist toolContent on round_entry so checkpoint writes
        #   it to DB.  Without this, crash-recovery loses tool
        #   context and Continue rolls back ALL tool rounds
        #   (toolContent == null → incomplete).
        if round_entry:
            round_entry['toolContent'] = tc_content_str

        # ★ Per-tool token count: gives the frontend an accurate
        # measure of the cost the model actually pays for this
        # result. Falls back to 0 on backend failure; the
        # frontend then renders chars instead.
        _tc_tokens = _safe_count_tokens(tc_content_str,
                                        model=task.get('model', '') if task else '')
        if round_entry and _tc_tokens > 0:
            round_entry['toolTokens'] = _tc_tokens

        # Timing: carry the round's own clocks onto the terminal frame so the
        # row stays self-describing on a cold replay that never saw the
        # tool_start (see _finalize_tool_round for the same contract).
        #
        # ★ WRITE THEM BACK ONTO THE ROUND (pt_ac380e3d). Stamping only the
        #   EVENT is not enough: the poll lane (/api/v1/chat/poll) and every
        #   cold reload ship the whole ``toolRounds`` objects and never replay
        #   the event stream, so a lane that settled here without writing back
        #   left its round with a tStart and NO tEnd — the execution segment
        #   unresolvable exactly on the recovery paths a user takes when
        #   investigating a slow turn (and for any client with no SSE at all).
        #   ``or now_ms()`` is load-bearing in the other direction: a round that
        #   already went through ``_finalize_tool_round`` carries the REAL
        #   completion instant, and overwriting it with a later "now" would
        #   silently shrink a slow tool's measured duration toward zero.
        _t_start = (round_entry or {}).get('tStart')
        _t_end = (round_entry or {}).get('tEnd') or now_ms()
        if _t_start is None:
            _t_start = _t_end
        if round_entry is not None:
            round_entry['tStart'] = _t_start
            round_entry['tEnd'] = _t_end

        _evt = build_event(
            EventType.TOOL_COMPLETE,
            roundNum=rn,
            toolCallId=tc_id,
            toolName=fn_name,
            toolContent=tc_content_str,
            tStart=_t_start,
            tEnd=_t_end,
        )
        # ★ Terminal verdict (pt_ac380e3d). A lane that never executed the tool
        #   settles with a NON-SUCCESS verdict. Stamp it on the round so the
        #   spinner stops NOW instead of at the end-of-task dangling sweep, and
        #   carry it on the wire so the client does not promote it to 'done' —
        #   rendering a refused write as applied is strictly worse than the
        #   latency this settle removes.
        _status = terminal_status or (round_entry or {}).get('status')
        if terminal_status and round_entry is not None:
            round_entry['status'] = terminal_status
        if _status and _status not in ('done', 'searching', 'executing'):
            _evt['status'] = _status
        if _tc_tokens > 0:
            _evt['toolTokens'] = _tc_tokens
        if round_entry and round_entry.get('compactionLayer'):
            _evt['compactionLayer'] = round_entry['compactionLayer']
            _evt['compactedFromChars'] = round_entry.get('compactedFromChars')
            _evt['compactedToChars'] = round_entry.get('compactedToChars')
        append_event(task, _evt)
    except Exception as e:
        logger.warning(
            '[Task %s] tool_complete event error for tool=%s at round %d (non-fatal): %s',
            tid, fn_name, round_num, e, exc_info=True)

    _settled[tc_id] = tool_content
    task.setdefault('_tool_call_id_receipts', {})[tc_id] = {
        'signature': _call_id_signature(fn_name, fn_args),
        'name': fn_name,
        'content': tool_content,
        'status': terminal_status or (round_entry or {}).get('status') or 'done',
    }
    return tool_content


def _screenshot_display_content(model: str, tool_content: dict) -> tuple[str, bool]:
    """Resolve what a screenshot result should DISPLAY, and whether the active
    model can actually see it.

    Returns ``(display_text, is_no_vision)``.

    ★ WHY THIS IS A FUNCTION (pt_ac380e3d). The screenshot branch used to live
    entirely in the post-phase, justified as "the verdict depends on the model's
    vision capability, which is resolved later". That reasoning was WRONG:
    ``model`` is a parameter of ``execute_tool_pipeline`` and
    ``model_supports_vision(model) -> bool`` is a pure function of it, so the
    verdict is knowable the instant the tool returns. Measured consequence of
    the old placement: a zero-cost screenshot beside a 1.2s ``web_search``
    emitted its ``tool_complete`` AFTER the slow sibling — and a browser
    screenshot is one of the calls a user is most likely to read as "stuck".

    Only the MESSAGE side genuinely has to stay in the post-phase: appending the
    multimodal / placeholder ``role:'tool'`` message must follow the model's
    original tool-call order.
    """
    if model and not model_supports_vision(model):
        # Text-only model: never build an image_url block (build_body would
        # strip it later and leave a misleading "analyze it visually" text).
        # Return a truthful result so the model knows the image is unreadable
        # and stops re-rendering / re-reading images.
        custom_fallback = tool_content.get('_no_vision_fallback')
        if custom_fallback:
            return (str(custom_fallback), True)
        return (
            '[Image not shown — the current model (%s) has no vision '
            'support, so this image cannot be analyzed. Do not retry '
            'reading images; rely on text, code, and test output '
            'instead.]' % model,
            True,
        )
    return (tool_content.get('_text_fallback', '') or 'Image captured.', False)


def execute_tool_pipeline(
    task: dict[str, Any],
    parsed_tcs: list[tuple],
    cfg: dict[str, Any],
    project_path: str | None,
    project_enabled: bool,
    tool_list: list[dict] | None,
    messages: list[dict[str, Any]],
    all_search_results_text: list[str],
    round_num: int,
    model: str,
) -> bool:
    """Run the full tool-execution pipeline: approval → parallel dispatch → result append.

    Returns
    -------
    bool
        True if a tool-execution timeout occurred during this round.

    Handles three phases:

    1. **Error short-circuit** — tool calls with JSON parse errors get an
       error result returned to the LLM without execution.
    2. **Serial approval** — write operations (``write_file``, ``apply_diff``)
       and server-kill commands that require user approval are executed one
       at a time, blocking until the user approves or rejects.
    3. **Parallel execution** — all remaining tool calls run concurrently
       in a :class:`~concurrent.futures.ThreadPoolExecutor`.

    After execution, tool result messages are appended to *messages* in the
    original tool-call order, and ``tool_complete`` events are emitted.

    Parameters
    ----------
    task : dict
        Live task dict — mutated (events appended, toolRounds updated).
    parsed_tcs : list[tuple]
        7-tuples from :func:`parse_tool_calls`.
    cfg : dict
        Task configuration dict (``autoApply``, etc.).
    project_path : str
        Filesystem path to the project root.
    project_enabled : bool
        Whether project-mode tools are active.
    tool_list : list | None
        Full tool definitions (passed through to ``_execute_tool_one``).
    messages : list[dict]
        Conversation message list — tool result messages appended in-place.
    all_search_results_text : list[str]
        Accumulator for search result text — appended in-place.
    round_num : int
        Current zero-based loop round (for snapshot labels and logging).
    model : str
        Current model identifier (for logging).
    """
    tid = task['id'][:8]
    # Attendance-aware auto-apply default. A task is "attended" only when a
    # human is watching a UI that can answer the write-approval prompt (set by
    # the interactive chat routes). Headless / autonomous tasks (agent/run,
    # scheduler, autopilot) are unattended: they MUST auto-apply or they would
    # block on an approval nobody can answer. When the caller omits autoApply we
    # therefore default attended→Manual (False) and unattended→auto (True).
    _attended = bool(task.get('_attended'))
    auto_apply = cfg.get('autoApply')
    if auto_apply is None:
        auto_apply = not _attended
    tool_results = {}  # tc_id → (tool_content, is_search)
    # ★ Failure VERDICTS (2026-08-06 'silent timeout' incident, conv
    #   msh9fzvo6gmuvh). A lane that failed the tool records ONLY an error
    #   string in tool_results — the tuple's second slot is is_search, NOT a
    #   success flag — so the post-phase settle shipped tool_complete with NO
    #   status, and the client promoted the round to 'done': a get_conversation
    #   that TIMED OUT rendered as a perfectly successful card, the failure
    #   visible only in the raw debug panel. Every failure lane MUST record a
    #   terminal verdict here ('error' / 'aborted'); the post-phase settle
    #   passes it as terminal_status so it is stamped on the round AND shipped
    #   on the wire, exactly like the rejected/aborted lanes (pt_ac380e3d).
    tool_verdicts: dict[str, str] = {}  # tc_id → 'error' | 'aborted'
    _pipeline_timed_out = False
    # Per-task write/idempotent partitions (base UNION custom env flags).
    _write_tools, _idempotent_tools = _task_partitions(task)

    # ══════════════════════════════════════════
    #  Pre-phase: Serial write-approval tools
    # ══════════════════════════════════════════
    # ── Per-task dedup cache for idempotent tools ──
    # Stored on the task dict so it's scoped to one task execution.
    if '_tool_result_cache' not in task:
        task['_tool_result_cache'] = {}
    _cache = task['_tool_result_cache']
    _call_id_receipts = task.setdefault('_tool_call_id_receipts', {})
    # A recovered/continued task may have durable tool rounds but no transient
    # receipt map. Rehydrate only completed rows and only by their real call ID.
    for _row in task.get('toolRounds', []):
        if not isinstance(_row, dict) or _row.get('toolContent') is None:
            continue
        _old_id = str(_row.get('toolCallId') or '')
        _old_name = str(_row.get('toolName') or '')
        if not _old_id or not _old_name or _old_id in _call_id_receipts:
            continue
        _old_args = _row.get('toolArgs')
        if isinstance(_old_args, str):
            try:
                _old_args = json.loads(_old_args)
            except (json.JSONDecodeError, TypeError) as exc:
                logger.debug('[ToolDispatch] malformed recovered tool arguments: %s',
                             exc)
                _old_args = {}
        _call_id_receipts[_old_id] = {
            'signature': _call_id_signature(_old_name, _old_args),
            'name': _old_name, 'content': _row.get('toolContent'),
            'status': str(_row.get('status') or 'done'),
        }

    parallel_items = []
    _claimed_call_ids: dict[str, str] = {}
    _duplicate_waiters: list[tuple] = []
    for item in parsed_tcs:
        tc, fn_name, tc_id, fn_args, rn, round_entry, _parse_err = item

        _signature = _call_id_signature(fn_name, fn_args)
        _receipt = _call_id_receipts.get(tc_id)
        if isinstance(_receipt, dict):
            if (_receipt.get('signature') == _signature
                    and _receipt.get('name') == fn_name):
                _replayed = _receipt.get('content', '')
                _replay_status = str(_receipt.get('status') or 'done')
                _finalize_call_id_replay(
                    task, fn_name, tc_id, rn, round_entry, _replayed,
                    status=_replay_status)
                tool_results[tc_id] = (_replayed, False)
                if _replay_status in ('error', 'rejected', 'aborted'):
                    tool_verdicts[tc_id] = _replay_status
                logger.info('[Task %s] call_id replay HIT: %s id=%s — '
                            'previous result reused without execution',
                            tid, fn_name, tc_id[:12])
                continue
            _conflict = (
                'Tool call rejected: call_id was already used for a different '
                'tool name or arguments. Mint a new call_id and retry.')
            _finalize_call_id_replay(
                task, fn_name, tc_id, rn, round_entry, _conflict,
                status='rejected')
            tool_results[tc_id] = (_conflict, False)
            tool_verdicts[tc_id] = 'rejected'
            logger.warning('[Task %s] call_id CONFLICT rejected: %s id=%s',
                           tid, fn_name, tc_id[:12])
            continue

        # A malformed stream can repeat one call ID inside the same assistant
        # message before the first copy has produced a receipt. Execute only
        # the first; identical siblings wait for its result. A conflicting
        # sibling is rejected immediately.
        if tc_id in _claimed_call_ids:
            if _claimed_call_ids[tc_id] == _signature:
                _duplicate_waiters.append(item)
            else:
                _conflict = (
                    'Tool call rejected: duplicate call_id has conflicting '
                    'tool name or arguments. Mint a new call_id and retry.')
                _finalize_call_id_replay(
                    task, fn_name, tc_id, rn, round_entry, _conflict,
                    status='rejected')
                tool_verdicts[tc_id] = 'rejected'
            continue
        _claimed_call_ids[tc_id] = _signature

        # JSON parse failure / hallucinated-tool rejection → return error to
        # LLM, skip execution.
        if _parse_err:
            if round_entry:
                _rejected = round_entry.get('_rejected')
                _err_meta = {'type': 'error', 'content': _parse_err,
                             'toolName': fn_name}
                if _rejected:
                    # Keep the distinct 'rejected' status (don't let
                    # _finalize_tool_round flip it to 'done') and carry the
                    # descriptor onto the result meta + event so the frontend
                    # styles it as a rejected hallucination, with suggestions.
                    _err_meta['rejected'] = _rejected
                    round_entry['results'] = [_err_meta]
                    round_entry['status'] = 'rejected'
                    append_event(task, build_event(
                        EventType.TOOL_RESULT,
                        roundNum=rn,
                        toolCallId=round_entry.get('toolCallId', ''),
                        query=round_entry.get('query', fn_name),
                        results=[_err_meta],
                        status='rejected',
                        _rejected=_rejected,
                    ))
                else:
                    _finalize_tool_round(
                        task, rn, round_entry,
                        [_err_meta],
                        query_override=round_entry.get('query', fn_name),
                    )
            tool_results[tc_id] = (_parse_err, False)
            # ★ Settle NOW (pt_ac380e3d). This tool never ran, so the round is
            #   knowably finished the instant it is inspected — deferring to the
            #   post-phase made a zero-cost refusal wait for the round's slowest
            #   REAL tool. The verdict rides along so the client cannot promote
            #   a rejected hallucination to 'done'.
            _settle_tool_result(
                task, fn_name, tc_id, fn_args, rn, round_entry, _parse_err,
                idempotent_tools=_idempotent_tools, cache=_cache, tid=tid,
                round_num=round_num,
                terminal_status=(round_entry or {}).get('status') or 'rejected')
            continue

        # Native Multi-agent subagents are analysis-only in Tofu. OpenAI
        # attributes every function call to an agent; the Responses translator
        # preserves that attribution in ``caller``. Reject state-changing calls
        # from any non-root agent at the execution boundary, so the read-only
        # policy remains an authority rule even if prompt guidance is ignored.
        _agent_name = _blocked_multi_agent_write(
            tc, fn_name, _write_tools)
        if _agent_name:
            _blocked = (
                'Tool call rejected: native Multi-agent subagents are '
                'read-only. Return the finding to /root; only the root agent '
                'may request a state-changing action.')
            if round_entry:
                round_entry['_rejected'] = {
                    'kind': 'multi_agent_read_only',
                    'agent': _agent_name,
                    'tool': fn_name,
                }
            tool_results[tc_id] = (_blocked, False)
            tool_verdicts[tc_id] = 'rejected'
            _settle_tool_result(
                task, fn_name, tc_id, fn_args, rn, round_entry, _blocked,
                idempotent_tools=_idempotent_tools, cache=_cache, tid=tid,
                round_num=round_num, terminal_status='rejected')
            logger.warning('[Task %s] Rejected Multi-agent subagent write: '
                           'agent=%s tool=%s', tid, _agent_name, fn_name)
            continue

        # ── Dedup check for idempotent tools ──
        if fn_name in _idempotent_tools:
            cache_key = _make_cache_key(fn_name, fn_args)
            cached = _cache.get(cache_key)
            # ── FreshGate: never serve a STALE cached read ──
            # The streaming pre-exec/dedup cache bypasses the project-tool
            # handler, so a cached read_files/inspect_image result can be
            # arbitrarily older than the disk (sibling edit, git checkout).
            # Serving it hands the model stale bytes AND never re-stamps
            # the write-freshness token — the 're-reads never clear the
            # refusal' loop (pt_26c703c5). When a covered file moved since
            # this conversation's token, drop the entry and fall through to
            # a REAL read (which re-stamps via the handler seam).
            if cached is not None:
                try:
                    from lib.tasks_pkg.handlers._write_freshness_gate import (
                        FILE_READ_TOOLS, cached_read_is_stale,
                    )
                    if (fn_name in FILE_READ_TOOLS
                            and cached_read_is_stale(task, fn_args,
                                                     project_path)):
                        _cache.pop(cache_key, None)
                        cached = None
                        logger.info(
                            '[Task %s] conv=%s FreshGate: %s cache hit '
                            'BYPASSED — covered file changed since cached '
                            'read; re-executing',
                            tid, task.get('convId', ''), fn_name)
                except Exception as _fe:
                    logger.debug('[FreshGate] cached-read staleness check '
                                 'failed (non-fatal): %s', _fe)
            if cached is not None:
                cached_content, cached_is_search, cached_source, cached_display, cached_engine_bkdn, cached_vertical = \
                    _unpack_cache_entry(cached)
                is_prefetch = cached_source == 'prefetch'
                # Compute content length for logging without materializing
                # a massive str() for screenshot dicts (which contain base64)
                if isinstance(cached_content, dict) and cached_content.get('__screenshot__'):
                    _log_len = cached_content.get('compressedSize', 0)
                    _log_suffix = ' (image)'
                else:
                    _log_len = len(str(cached_content))
                    _log_suffix = ''
                logger.info(
                    '[Task %s] conv=%s %s HIT: %s with same args at round %d — '
                    'returning %s result (%d chars%s) instead of re-executing',
                    tid, task.get('convId', ''),
                    'PREFETCH' if is_prefetch else 'DEDUP',
                    fn_name, round_num,
                    'prefetched' if is_prefetch else 'cached',
                    _log_len, _log_suffix,
                )
                # Preserve __screenshot__ dicts as-is so the post-phase
                # can detect them and convert to image_url blocks.
                # Converting to str() would dump 800K+ of base64 text
                # directly into the context, blowing up the token count.
                if isinstance(cached_content, dict) and cached_content.get('__screenshot__'):
                    dedup_content = cached_content  # keep as dict
                else:
                    dedup_content = cached_content if isinstance(cached_content, str) else str(cached_content)
                # Update round_entry to show cached/prefetched status
                if round_entry:
                    # Use stored display_results for web_search / fetch_url if available
                    # — this preserves per-result rows (titles, URLs, snippets) in the UI
                    # instead of collapsing to a single generic meta row.
                    if cached_display and fn_name in ('web_search', 'fetch_url'):
                        extra = {}
                        if cached_engine_bkdn:
                            round_entry['engineBreakdown'] = cached_engine_bkdn
                            extra['engineBreakdown'] = cached_engine_bkdn
                        if cached_vertical:
                            # Batch web_search carries multiple verticals. The
                            # streaming prefetch path wraps them as
                            # {'batch': [...]}, and the dedup path may hand us a
                            # bare list. Both must land in the plural `verticals`
                            # field — the frontend renders that as an array;
                            # `vertical` (singular) expects one {domain, items}
                            # dict and would silently drop a list/wrapper
                            # (showing the bare "vertical: auto" badge with no card).
                            if isinstance(cached_vertical, dict) and 'batch' in cached_vertical:
                                _verts = cached_vertical.get('batch') or []
                            elif isinstance(cached_vertical, list):
                                _verts = cached_vertical
                            else:
                                _verts = None
                            if _verts is not None:
                                round_entry['verticals'] = _verts
                                extra['verticals'] = _verts
                            else:
                                round_entry['vertical'] = cached_vertical
                                extra['vertical'] = cached_vertical
                        _finalize_tool_round(
                            task, rn, round_entry, cached_display,
                            query_override=round_entry.get('query', fn_name),
                            extra_event_fields=extra or None,
                        )
                    else:
                        _meta = _build_cache_hit_meta(
                            fn_name, fn_args, cached_content, is_prefetch,
                            cached_display=cached_display,
                        )
                        _finalize_tool_round(
                            task, rn, round_entry, [_meta],
                            query_override=round_entry.get('query', fn_name),
                        )
                tool_results[tc_id] = (dedup_content, cached_is_search)
                # ★ Settle NOW (pt_ac380e3d). A cache/prefetch hit costs ZERO
                #   time: for a streaming-prefetch hit the tool already ran while
                #   the model was still emitting tokens (StreamingToolExecutor
                #   → inject_into_cache). Leaving it to the post-phase made the
                #   FASTEST class of tool in the product the one that waited
                #   longest — measured: tool_complete(cached) landed after
                #   tool_complete(slow-sibling).
                _settle_tool_result(
                    task, fn_name, tc_id, fn_args, rn, round_entry,
                    dedup_content, idempotent_tools=_idempotent_tools,
                    cache=_cache, tid=tid, round_num=round_num)
                continue

        # ── Write-approval gate (Manual mode) ──
        # The approval set is DERIVED from the per-task write partition
        # (_write_tools), so every state-mutating tool — project writes,
        # run_command, memory mutators, MCP write tools, and custom write
        # tools — is approval-eligible from a single source of truth (no second
        # hand-maintained list). Gating only ever happens for an ATTENDED task
        # (a human can answer); unattended / headless tasks never block here.
        needs_approval = (
            fn_name in _write_tools
            and _attended and not auto_apply and not task['aborted']
            and not (round_entry and round_entry.get('toolName') == 'code_exec')
        )
        # run_command is in the write partition for concurrency safety, but
        # read-only invocations (grep/ls/cat/git status/…) must NOT prompt —
        # only commands that could mutate the filesystem require approval.
        if needs_approval and fn_name == 'run_command':
            from lib.project_mod.tools import _is_destructive_command
            needs_approval = _is_destructive_command(fn_args.get('command', ''))

        if needs_approval and fn_name.startswith('browser_'):
            # Navigation/tab lifecycle are browser reads/lifecycle operations.
            # Page mutation commands need one durable domain grant; an existing
            # grant bypasses repeated per-step prompts.
            if fn_name in ('browser_navigate', 'browser_close_tab'):
                needs_approval = False
            else:
                try:
                    from lib.browser.access import browser_tool_access
                    browser_tool_access(
                        fn_name, fn_args,
                        user_id=str(task.get('_userId') or ''),
                        client_id=str(cfg.get('browserClientId') or ''))
                    needs_approval = False
                except Exception as exc:
                    logger.debug(
                        '[ToolPipeline] browser access check requires approval '
                        'for %s: %s', fn_name, exc)
                    needs_approval = True

        if needs_approval:
            approved, reject_content = _handle_approval(
                task, fn_name, fn_args, rn, round_entry, project_path,
                round_num, model, cfg=cfg)
            if not approved:
                tool_results[tc_id] = (reject_content, False)
                # ★ Settle NOW (pt_ac380e3d). The user ALREADY answered — the
                #   round is settled at that instant. Without this it sat at
                #   status='pending_approval' until the end-of-task dangling
                #   sweep, i.e. spun for the rest of the turn. The 'rejected'
                #   verdict is mandatory: a settle that reported success would
                #   render a REFUSED write as applied.
                _settle_tool_result(
                    task, fn_name, tc_id, fn_args, rn, round_entry,
                    reject_content, idempotent_tools=_idempotent_tools,
                    cache=_cache, tid=tid, round_num=round_num,
                    terminal_status='rejected')
                continue
            # Approved → fall through to normal dispatch. The item is in
            # _write_tools, so the serial write-tool phase below executes it via
            # _execute_tool_one and invalidates the project cache afterwards.
            #   (one execution path for project / run_command / memory / MCP /
            #    custom write tools.)

        # ── Abort check: skip remaining tools if user clicked Stop ──
        if task.get('aborted'):
            logger.info('[Task %s] Skipping tool %s (tc_id=%s) — task aborted', tid, fn_name, tc_id[:8])
            tool_results[tc_id] = ('Task aborted by user.', False)
            # ★ Settle NOW with an ABORTED verdict (pt_ac380e3d). Previously the
            #   round kept status='searching' until
            #   orchestrator._finalize_dangling_tool_rounds swept it at task end
            #   — so a Stop left a spinner turning on every not-yet-run tool for
            #   the remainder of the turn. 'aborted' (never 'done') is what makes
            #   the row render the static "interrupted" affordance instead.
            _settle_tool_result(
                task, fn_name, tc_id, fn_args, rn, round_entry,
                'Task aborted by user.', idempotent_tools=_idempotent_tools,
                cache=_cache, tid=tid, round_num=round_num,
                terminal_status='aborted')
            continue

        # ── Serial-dispatch for long-blocking tools ──
        _serial_cfg = _SERIAL_BLOCKING_TOOLS.get(fn_name)
        if _serial_cfg and _serial_cfg['match'](fn_args) and not task['aborted']:
            _reason = _serial_cfg['reason']
            logger.info('[Task %s] %s dispatched serially (%s) at round %d',
                        tid, fn_name, _reason, round_num)
            # Inject extra args (e.g. _parent_task) if configured
            _inject_fn = _serial_cfg.get('inject')
            if _inject_fn:
                fn_args.update(_inject_fn(task, rn))
            # ★ Heartbeat this lane too (pt_9f5a51ba). These tools block for
            #   MINUTES by design and emit no delta, so without a ticker both
            #   reaper liveness clocks go stale and the task is force-failed
            #   at TOFU_STUCK_TASK_MAX_SILENT_SECS. ``ask_human`` self-bumps
            #   (human_guidance.py) and ``timer_create`` emits per poll, but
            #   ``await_task`` does NEITHER and its own wait caps at 3600s —
            #   double the reap threshold. The module comment in _heartbeat.py
            #   claimed this lane was already covered; it was not.
            _hb_stop, _hb_thread = _start_tool_heartbeat(task, [item], tid)
            try:
                tc_id_ret, tool_content, is_search = _execute_tool_one(
                    task, tc, fn_name, tc_id, fn_args, rn, round_entry,
                    cfg, project_path, project_enabled,
                    all_tools=tool_list,
                )
            finally:
                _hb_stop.set()
            tool_results[tc_id_ret] = (tool_content, is_search)
            logger.info('[Task %s] %s serial dispatch completed at round %d '
                        '(result_len=%d)', tid, fn_name, round_num, len(str(tool_content)))
            # ★ Settle immediately (pt_67ffc2b7). These tools block for MINUTES
            #   (ask_human waits on a human; timer_create polls). Holding their
            #   settle until the post-phase meant the round they belong to could
            #   not report ANY tool as finished until the human answered.
            if not (isinstance(tool_content, dict)
                    and tool_content.get('__screenshot__')):
                _settle_tool_result(
                    task, fn_name, tc_id_ret, fn_args, rn, round_entry,
                    tool_content, idempotent_tools=_idempotent_tools,
                    cache=_cache, tid=tid, round_num=round_num)
            continue

        # ── Pre-tool hooks: validate/block/modify before execution ──
        # Inspired by Claude Code's PreToolUse hooks.
        _hook_result = run_pre_hooks(fn_name, fn_args, task)
        if _hook_result and _hook_result.action == 'block':
            logger.info('[Task %s] Pre-hook BLOCKED tool %s: %s',
                        tid, fn_name, _hook_result.message)
            _blocked_content = f'Tool blocked by pre-execution hook: {_hook_result.message}'
            # Surface the hook's recovery guidance to the model so a block is
            # an ACTIONABLE redirect (what was refused + how to proceed safely)
            # rather than a dead-end the loop can't recover from.
            _recovery = getattr(_hook_result, 'additional_context', '') or ''
            if _recovery:
                _blocked_content = f'{_blocked_content}\n\n{_recovery}'
            tool_results[tc_id] = (_blocked_content, False)
            # ★ Settle the round NOW. Without this the round stays in its
            #   'searching' start-state forever (no result, no terminal
            #   status): the live UI shows a permanent "Running…" spinner and
            #   the persisted round only gets swept to 'aborted' by the
            #   task-end dangling sweep — so an EARLY blocked tool renders as
            #   still-running even after the loop has advanced dozens of rounds
            #   past it. Emit a terminal 'rejected' result exactly like the
            #   parse-error / hallucinated-tool branch above.
            if round_entry is not None:
                _block_meta = {
                    'type': 'error',
                    'content': _blocked_content,
                    'toolName': fn_name,
                    'source': 'Blocked',
                    'snippet': _hook_result.message,
                    'badge': 'blocked',
                }
                # For run_command / code_exec, shape the meta so the frontend's
                # purpose-built "not run" terminal card renders it (⊘ blocked +
                # inline reason) — that renderer keys on meta.command / notRun.
                # A generic error meta would fall through to a plain error line.
                if fn_name in ('run_command', 'code_exec'):
                    _block_meta['command'] = fn_args.get('command') or round_entry.get('query') or ''
                    _block_meta['notRun'] = True
                    _block_meta['exitCode'] = 'not-run'
                    _block_meta['reason'] = _blocked_content
                round_entry['results'] = [_block_meta]
                round_entry['status'] = 'rejected'
                round_entry['toolContent'] = _blocked_content
                try:
                    append_event(task, build_event(
                        EventType.TOOL_RESULT,
                        roundNum=rn,
                        toolCallId=round_entry.get('toolCallId', ''),
                        query=round_entry.get('query', fn_name),
                        results=[_block_meta],
                        status='rejected',
                    ))
                except Exception as _blk_ev:
                    logger.warning(
                        '[Task %s] tool_result (pre-hook block) emit failed for '
                        'tool=%s round=%s (non-fatal): %s',
                        tid, fn_name, rn, _blk_ev, exc_info=True)
            # ★ Settle NOW (pt_ac380e3d). The hook refused the tool before it
            #   ran, so this round costs zero time. It already carries
            #   status='rejected' above; passing it through keeps the verdict on
            #   the wire so the client cannot promote a BLOCKED tool to 'done'.
            _settle_tool_result(
                task, fn_name, tc_id, fn_args, rn, round_entry,
                _blocked_content, idempotent_tools=_idempotent_tools,
                cache=_cache, tid=tid, round_num=round_num,
                terminal_status='rejected')
            continue

        parallel_items.append(item)

    # ══════════════════════════════════════════
    #  Ordered-state / write-tool serial phase (concurrency safety)
    #  Inspired by Claude Code's isConcurrencySafe partitioning:
    #  Write tools run serially to prevent filesystem race conditions.
    #  Order-sensitive task-local protocols (currently todo_write) share the
    #  ordered lane without inheriting write approval or cache invalidation.
    # ══════════════════════════════════════════
    _serial_items = [
        item for item in parallel_items
        if item[1] in _write_tools or item[1] in _ORDERED_STATE_TOOLS
    ]
    parallel_items = [
        item for item in parallel_items
        if item[1] not in _write_tools and item[1] not in _ORDERED_STATE_TOOLS
    ]
    for item in _serial_items:
        tc, fn_name, tc_id, fn_args, rn, round_entry, _pe = item
        if task.get('aborted'):
            logger.info('[Task %s] Skipping serial write tool %s — task aborted', tid, fn_name)
            tool_results[tc_id] = ('Task aborted by user.', False)
            # ★ Same abort contract as the pre-phase lane (pt_ac380e3d).
            _settle_tool_result(
                task, fn_name, tc_id, fn_args, rn, round_entry,
                'Task aborted by user.', idempotent_tools=_idempotent_tools,
                cache=_cache, tid=tid, round_num=round_num,
                terminal_status='aborted')
            continue
        logger.debug('[Task %s] Ordered serial dispatch: %s at round %d',
                     tid, fn_name, round_num)
        # ★ Heartbeat this lane (pt_9f5a51ba). ``run_command`` resolves
        #   timeout=None BY DESIGN (no ceiling, pinned by
        #   tests/test_no_backend_timeouts.py) and every non-readOnly MCP tool
        #   lands in this same write partition. Running them bare left BOTH
        #   reaper clocks silent, so the reaper's 1800s became an invisible,
        #   unconfigurable ceiling that killed the WHOLE task — measured
        #   2026-07-31: tasks 38562f78 and 31d08c82, each 1846s in
        #   run_command, process group killed, ¥22.95 + ¥12.01 of completed
        #   rounds discarded. Aliveness is proven by BEATING, never by
        #   not-timing-out.
        # ★ EVIDENCE GRADING (pt_8524e0ec, same day, same incident family):
        #   the beat must be EVIDENCE, not self-rescue. For ordinary tools
        #   this tick is marked ``_selfTick`` and keeps ONLY the transport
        #   alive; the reaper clocks are fed by real stdout chunks instead
        #   (a producing command never goes stale). A command silent >30min
        #   IS reaped now — wedged by definition; the ratified human-wait
        #   exemption covers only ask_human / await_task(wait) / timer_create.
        # External writes may block and therefore need a transport heartbeat.
        # Task-local todo transitions are bounded in-memory work; avoiding a
        # heartbeat thread on every revision keeps the progress protocol cheap.
        _hb_stop = None
        if fn_name in _write_tools:
            _hb_stop, _hb_thread = _start_tool_heartbeat(task, [item], tid)
        try:
            tc_id_ret, tool_content, is_search = _execute_tool_one(
                task, tc, fn_name, tc_id, fn_args, rn, round_entry,
                cfg, project_path, project_enabled,
                all_tools=tool_list,
            )
        finally:
            if _hb_stop is not None:
                _hb_stop.set()
        tool_results[tc_id_ret] = (tool_content, is_search)
        if fn_name in _write_tools:
            _invalidate_project_cache(_cache, trigger=fn_name)
        # ★ Settle at THIS tool's own completion (pt_67ffc2b7) — a serial write
        #   runs before the parallel pool even starts, so deferring its
        #   settle to the post-phase made it wait for every read tool in the
        #   round. Same barrier, different lane. Screenshots included
        #   (pt_ac380e3d) — see the parallel lane for why the vision verdict
        #   needs no barrier.
        if (isinstance(tool_content, dict)
                and tool_content.get('__screenshot__')):
            _shot_txt, _ = _screenshot_display_content(
                model, tool_content)
            _settle_tool_result(
                task, fn_name, tc_id_ret, fn_args, rn, round_entry,
                _shot_txt, idempotent_tools=_idempotent_tools,
                cache=_cache, tid=tid, round_num=round_num)
        else:
            _settle_tool_result(
                task, fn_name, tc_id_ret, fn_args, rn, round_entry,
                tool_content, idempotent_tools=_idempotent_tools,
                cache=_cache, tid=tid, round_num=round_num)

    # ══════════════════════════════════════════
    #  Main phase: Parallel execution (read-only tools)
    # ══════════════════════════════════════════
    if parallel_items:
        # ── Abort check before spawning parallel pool ──
        if task.get('aborted'):
            logger.info('[Task %s] Skipping %d parallel tools — task aborted', tid, len(parallel_items))
            for tc, fn_name, tc_id, fn_args, rn, round_entry, _pe in parallel_items:
                tool_results[tc_id] = ('Task aborted by user.', False)
                tool_verdicts[tc_id] = 'aborted'
            parallel_items = []  # skip the pool entirely

    if parallel_items:
        _has_program_children = any(
            isinstance(tc.get('caller'), dict)
            and tc['caller'].get('type') == 'program'
            for tc, _fn, _id, _args, _rn, _entry, _pe in parallel_items
        )
        max_workers = _parallel_worker_limit(
            len(parallel_items), programmatic=_has_program_children)
        pool = ThreadPoolExecutor(max_workers=max_workers)
        _timed_out = False
        # ── Item 3: long-tool heartbeat ──────────────────────────────────
        # A single blocking tool (a slow web_search on dead hosts, a hung MCP
        # call, a stalled browser action) emits NO delta while it runs, so the
        # SSE stream goes silent — a buffering proxy idle-times-out. This
        # daemon ticker fires every TOOL_HEARTBEAT_INTERVAL seconds while the
        # pool wait blocks, emitting a ``tool_progress`` per still-active
        # round so the UI shows "Searching… (Ns)". Fast tools finish before
        # the first tick, so they never emit a heartbeat.
        # ★ EVIDENCE GRADING (pt_8524e0ec): for ordinary tools these ticks are
        #   marked ``_selfTick`` and do NOT feed the reaper liveness clocks —
        #   liveness must come from REAL output (stdout chunks / results).
        #   Only ratified human-wait tools (ask_human / await_task(wait) /
        #   timer_create) keep the reaper exemption. See _heartbeat.py.
        _hb_stop, _hb_thread = _start_tool_heartbeat(task, parallel_items, tid)
        try:
            futures = {
                pool.submit(
                    _execute_tool_one_pooled, task,
                    tc, fn_name, tc_id, fn_args, rn, round_entry,
                    cfg, project_path, project_enabled,
                    all_tools=tool_list,
                ): (tc_id, fn_name)
                for tc, fn_name, tc_id, fn_args, rn, round_entry, _pe in parallel_items
            }
            tool_timeout = _bounded_tool_env_int(
                'TOOL_PARALLEL_TIMEOUT', 300, 1, 3600)
            try:
                for fut in as_completed(futures, timeout=tool_timeout):
                    # ── Abort check during parallel execution: cancel remaining futures ──
                    if task.get('aborted'):
                        logger.info('[Task %s] Abort detected during parallel tool execution — cancelling remaining', tid)
                        for pending_fut, (pending_id, pending_fn) in futures.items():
                            if not pending_fut.done():
                                pending_fut.cancel()
                                if pending_id not in tool_results:
                                    tool_results[pending_id] = ('Task aborted by user.', False)
                                    tool_verdicts[pending_id] = 'aborted'
                        break
                    fut_tc_id, fut_fn_name = futures[fut]
                    try:
                        ret_tc_id, tool_content, is_search = fut.result()
                        tool_results[ret_tc_id] = (tool_content, is_search)
                        # ── Populate dedup cache for idempotent tools ──
                        if fut_fn_name in _idempotent_tools:
                            # Find the matching fn_args from parallel_items
                            for _pi in parallel_items:
                                if _pi[2] == ret_tc_id:  # tc_id match
                                    _pi_cache_key = _make_cache_key(fut_fn_name, _pi[3])
                                    # For web_search / fetch_url, also cache display_results
                                    # + engineBreakdown from the round_entry for later cache hits —
                                    # this keeps the rich per-result UI even on dedup replay
                                    # (e.g. batch "3 URLs" stays as 3 rows, not 1 generic row).
                                    _pi_display = None
                                    _pi_eng_bkdn = None
                                    _pi_vert = None
                                    if fut_fn_name in ('web_search', 'fetch_url'):
                                        _pi_re = _pi[5]  # round_entry
                                        if _pi_re and _pi_re.get('results'):
                                            _pi_display = _pi_re['results']
                                        if _pi_re:
                                            _pi_eng_bkdn = _pi_re.get('engineBreakdown')
                                            _pi_vert = _pi_re.get('vertical') or _pi_re.get('verticals')
                                    elif fut_fn_name in ('read_files', 'inspect_image'):
                                        # Preserve the FULLY-MERGED inline render
                                        # descriptors (images + SVG source URIs) so a
                                        # dedup replay renders identically to the fresh
                                        # read. SVG content caches as a plain str, so
                                        # its out-of-band imageDataUris would otherwise
                                        # be lost on the second identical read.
                                        _pi_re = _pi[5]  # round_entry
                                        _pi_res = (_pi_re or {}).get('results') or []
                                        if (_pi_res and isinstance(_pi_res[0], dict)
                                                and _pi_res[0].get('imageDataUris')):
                                            _pi_display = _pi_res[0]['imageDataUris']
                                    _cache[_pi_cache_key] = (tool_content, is_search, 'dedup', _pi_display, _pi_eng_bkdn, _pi_vert)
                                    break
                        # ── Invalidate project cache after write/exec ops ──
                        elif fut_fn_name in ('write_file', 'edit_file', 'apply_diff', 'apply_diffs',
                                             'insert_content', 'insert_contents',
                                             'create_project',
                                             'code_exec', 'bash_exec', 'run_command'):
                            _invalidate_project_cache(_cache, trigger=fut_fn_name)

                        # ★ SETTLE NOW, not after the barrier (pt_67ffc2b7).
                        #   This tool is done; budget its result, stamp the
                        #   round and emit tool_complete at THIS instant. The
                        #   old code deferred all of that past
                        #   pool.shutdown(wait=True), so a fast tool's content
                        #   and token chips waited for the slowest sibling in
                        #   the round — a 2s search kept spinning for as long
                        #   as a 40s one beside it, with no way for the user to
                        #   tell them apart.
                        #
                        #   Screenshots settle here TOO (pt_ac380e3d): the
                        #   display text depends only on
                        #   model_supports_vision(model), a pure function of a
                        #   parameter we already hold, so there is nothing to
                        #   wait for. Only the multimodal MESSAGE append stays
                        #   in the post-phase, where tool-call order lives.
                        _is_shot = (isinstance(tool_content, dict)
                                    and tool_content.get('__screenshot__'))
                        for _pi in parallel_items:
                            if _pi[2] == ret_tc_id:
                                _settle_arg = tool_content
                                if _is_shot:
                                    _settle_arg, _ = _screenshot_display_content(
                                        model, tool_content)
                                _settle_tool_result(
                                    task, _pi[1], ret_tc_id, _pi[3],
                                    _pi[4], _pi[5], _settle_arg,
                                    idempotent_tools=_idempotent_tools,
                                    cache=_cache, tid=tid,
                                    round_num=round_num)
                                break
                    except Exception as e:
                        # UnknownWorkspaceRootError is the LLM's fault
                        # (bad root prefix); it's already logged at WARNING
                        # at the raise site + INFO by executor.  Do not
                        # re-log as ERROR with traceback here — just record
                        # the error for the LLM and move on.
                        _is_unknown_root = False
                        try:
                            from lib.project_mod.config import UnknownWorkspaceRootError
                            _is_unknown_root = isinstance(e, UnknownWorkspaceRootError)
                        except ImportError as _imp:
                            logger.debug('[Task %s] UnknownWorkspaceRootError '
                                         'import failed: %s', tid, _imp)
                        if _is_unknown_root:
                            logger.info(
                                '[Task %s] conv=%s Tool %s (tc_id=%s) '
                                'recoverable workspace-root error '
                                'returned to LLM at round %d: %s',
                                tid, task.get('convId', ''),
                                fut_fn_name, fut_tc_id, round_num, e)
                        else:
                            logger.error(
                                '[Task %s] conv=%s Tool %s (tc_id=%s) execution failed at round %d model=%s',
                                tid, task.get('convId', ''), fut_fn_name, fut_tc_id, round_num, model, exc_info=True)

                        tool_results[fut_tc_id] = (f'Tool execution error: {e}', False)
                        tool_verdicts[fut_tc_id] = 'error'
            except (TimeoutError, _FuturesTimeoutError):
                _timed_out = True
                _pipeline_timed_out = True
                _n_pending = sum(1 for f in futures if not f.done())
                logger.error(
                    '[Task %s] conv=%s Tool parallel execution timeout at round %d (%d tools pending) model=%s',
                    tid, task.get('convId', ''), round_num, _n_pending, model,
                    exc_info=True)

                # Harvest results from futures that completed but weren't
                # yielded by as_completed before the TimeoutError was raised.
                # Without this, completed-but-unyielded results are silently
                # lost and fall through to 'Unknown tool' in the post-phase.
                for fut, (fut_tc_id, fut_fn_name) in futures.items():
                    if fut.done():
                        if fut_tc_id not in tool_results:
                            try:
                                ret_tc_id, tool_content, is_search = fut.result()
                                tool_results[ret_tc_id] = (tool_content, is_search)
                                logger.info(
                                    '[Task %s] conv=%s Recovered completed tool %s (tc_id=%s) after timeout',
                                    tid, task.get('convId', ''), fut_fn_name, fut_tc_id)
                            except Exception as e:
                                logger.warning(
                                    '[Task %s] conv=%s Tool %s (tc_id=%s) completed with error after timeout: %s',
                                    tid, task.get('convId', ''), fut_fn_name, fut_tc_id, e)
                                tool_results[fut_tc_id] = (f'Tool execution error: {e}', False)
                                tool_verdicts[fut_tc_id] = 'error'
                    else:
                        fut.cancel()
                        tool_results[fut_tc_id] = (f'Tool execution timed out: {fut_fn_name}', False)
                        tool_verdicts[fut_tc_id] = 'error'
                        # 'error', not 'aborted': the user did not stop this
                        # tool — the pool ceiling did. The content sentinel
                        # carries the 'timed out' wording onto the rendered
                        # row's reason span.
        finally:
            # Stop the heartbeat ticker first so it can't emit after the round
            # settles (it checks round status, but stop the loop deterministically).
            _hb_stop.set()
            # On timeout use wait=False + cancel_futures=True to avoid
            # blocking indefinitely on still-running tool threads.
            # On normal completion wait=True is fine (all futures done).
            pool.shutdown(wait=not _timed_out, cancel_futures=_timed_out)

    # Identical duplicate IDs from the same assistant batch share the first
    # execution's result and settle their own UI rows now that it is known.
    for _dup in _duplicate_waiters:
        _tc, _name, _id, _args, _rn, _row, _pe = _dup
        _dup_content, _dup_search = tool_results.get(
            _id, ('Tool execution did not produce a result.', False))
        _dup_status = tool_verdicts.get(_id, 'done')
        _finalize_call_id_replay(
            task, _name, _id, _rn, _row, _dup_content,
            status=_dup_status)

    # ══════════════════════════════════════════
    #  Post-phase: Add tool messages in original order
    # ══════════════════════════════════════════
    _round_results_for_budget: list[tuple[str, str, str]] = []  # (tc_id, content, tool_name)
    _program_messages_to_settle: list[tuple[dict, str, dict]] = []
    for tc, fn_name, tc_id, fn_args, rn, round_entry, _pe in parsed_tcs:
        if tc_id in tool_results:
            tool_content, is_search = tool_results[tc_id]
        else:
            # SHOULD-NOT-HAPPEN: every dispatch branch above is expected to
            # populate tool_results[tc_id]. If a tc_id reaches here unfilled,
            # a dispatch branch silently skipped writing its result — the model
            # would get a misleading "Unknown tool" with no trace of the real
            # cause. Log it so the silent-skip is diagnosable (§2 zero-silent-failure).
            logger.warning(
                '[Task %s] conv=%s tool result missing for tool=%s tc_id=%s '
                'round=%d — no dispatch branch populated it; returning '
                'Unknown-tool fallback to LLM (should not happen)',
                tid, task.get('convId', '') if task else '',
                fn_name, tc_id, round_num)
            tool_content, is_search = (f'Unknown tool: {fn_name}', False)
            tool_verdicts[tc_id] = 'error'
        if is_search:
            all_search_results_text.append(tool_content)

        # Convert screenshot dict → image_url content block for vision models.
        # ★ The MESSAGE append is what genuinely belongs here (pt_ac380e3d):
        #   the role:'tool' message must enter the list in the model's ORIGINAL
        #   tool-call order. The tool_complete EVENT was already emitted at the
        #   tool's own completion instant by the dispatch lane; the settle below
        #   is idempotent, so it returns the cached content without re-emitting
        #   (and still does the work for a screenshot that never reached a
        #   dispatch lane at all).
        if isinstance(tool_content, dict) and tool_content.get('__screenshot__'):
            # ``model`` (the PARAMETER) is authoritative: the orchestrator passes
            # the round's resolved model and only MIRRORS it onto task['model']
            # afterwards, so reading the mirror risks a stale value on a
            # mid-turn model fallback — and the dispatch-time settle must reach
            # the same verdict as this post-phase message append, or the UI text
            # and the model's own tool message would disagree.
            _shot_txt, _no_vision = _screenshot_display_content(
                model, tool_content)
            if _no_vision:
                _tool_msg = {'role': 'tool', 'tool_call_id': tc_id,
                             'content': _shot_txt}
                if isinstance(tc.get('caller'), dict):
                    _tool_msg['caller'] = dict(tc['caller'])
                messages.append(_tool_msg)
                logger.info(
                    '[Task %s] conv=%s text-only model %s — image tool result '
                    'for tc=%s replaced with no-vision placeholder',
                    tid, task.get('convId', '') if task else '', model, tc_id)
            else:
                _append_screenshot_message(messages, tc_id, tool_content)
                if isinstance(tc.get('caller'), dict):
                    messages[-1]['caller'] = dict(tc['caller'])
            _settle_tool_result(
                task, fn_name, tc_id, fn_args, rn, round_entry, _shot_txt,
                idempotent_tools=_idempotent_tools, cache=_cache, tid=tid,
                round_num=round_num)
        else:
            # ★ Settle this tool (idempotent). Tools dispatched through the
            #   parallel pool / serial lanes already settled at their OWN
            #   completion instant — this call returns their cached content
            #   without re-emitting. Only tools that never went through a
            #   dispatch lane (dedup cache hits, approval rejections,
            #   pre-hook blocks, abort short-circuits, the missing-result
            #   fallback) actually do work here.
            #
            #   The LOOP itself must stay: it walks ``parsed_tcs``, so the
            #   ``role:'tool'`` messages enter the message list in the model's
            #   ORIGINAL tool-call order regardless of completion order. An
            #   out-of-order tool_call/tool_result pairing is a hard API error
            #   on Anthropic — that ordering is the reason this phase exists,
            #   and it is NOT what was making the UI wait.
            if not (round_entry or {}).get('_idempotentReplay'):
                tool_content = _settle_tool_result(
                    task, fn_name, tc_id, fn_args, rn, round_entry, tool_content,
                    idempotent_tools=_idempotent_tools, cache=_cache, tid=tid,
                    round_num=round_num,
                    terminal_status=tool_verdicts.get(tc_id))

            # Collect for aggregate budget check
            _round_results_for_budget.append((tc_id, tool_content, fn_name))

            _tool_msg = {'role': 'tool', 'tool_call_id': tc_id,
                         'content': tool_content}
            if isinstance(tc.get('caller'), dict):
                _tool_msg['caller'] = dict(tc['caller'])
            messages.append(_tool_msg)

        # Account the settled model-visible result once, after clamping and
        # persistence substitutions. The next round-start gate enforces the
        # configured aggregate hard ceiling before another provider call.
        try:
            from lib.task_budget import account_tool_output
            account_tool_output(task, tool_content)
        except Exception as _budget_err:
            logger.debug('[Task %s] tool-output budget accounting skipped: %s',
                         tid, _budget_err)

        caller = tc.get('caller')
        if isinstance(caller, dict) and caller.get('type') == 'program':
            _program_messages_to_settle.append((
                tc,
                tool_verdicts.get(tc_id)
                or (round_entry or {}).get('status')
                or 'done',
                messages[-1],
            ))

    # ══════════════════════════════════════════
    #  Per-round aggregate budget check
    # ══════════════════════════════════════════
    # If total tool result chars in this round exceed MAX_ROUND_TOOL_RESULTS_CHARS,
    # persist the largest non-exempt results to disk.
    # This prevents context explosion from parallel tool calls (e.g. 10 grep_search
    # calls each returning 40K chars = 400K total).
    if _round_results_for_budget:
        _agg_dict = {
            tc_id: (content, tool_name, tc_id)
            for tc_id, content, tool_name in _round_results_for_budget
            if isinstance(content, str)
        }
        _conv_id = task.get('convId', '') if task else ''
        _pre_chars_by_tc = {tc_id: len(c) for tc_id, c, _ in _round_results_for_budget
                            if isinstance(c, str)}
        _updated = enforce_round_aggregate_budget(_agg_dict, conv_id=_conv_id)
        # Apply any changes back to messages AND round_entries/toolContent
        # so Preview stays in sync with actual model content.
        for msg in messages:
            if msg.get('role') == 'tool':
                _tc_id = msg.get('tool_call_id', '')
                if _tc_id in _updated:
                    new_content, _, _ = _updated[_tc_id]
                    if new_content != msg.get('content'):
                        msg['content'] = new_content
                        # Update toolContent on the corresponding round_entry
                        for _ptc in parsed_tcs:
                            if _ptc[2] == _tc_id:  # tc_id match
                                _re = _ptc[5]  # round_entry
                                _re_rn = _ptc[4]
                                _re_fn = _ptc[1]
                                if _re:
                                    _tc_str = new_content if isinstance(new_content, str) else str(new_content)
                                    if len(_tc_str) > 50000:
                                        _tc_str = _tc_str[:50000] + '\n... [truncated for continue context]'
                                    _re['toolContent'] = _tc_str
                                    # Stamp aggregate-budget compaction
                                    _pre = _pre_chars_by_tc.get(_tc_id, 0)
                                    _post = len(_tc_str)
                                    if _pre > _post:
                                        _re['compactionLayer'] = 'L0'
                                        _re['compactedFromChars'] = _pre
                                        _re['compactedToChars'] = _post
                                        _re['toolTokens'] = _safe_count_tokens(
                                            _tc_str, model=task.get('model', '') if task else '')
                                        try:
                                            append_event(task, build_event(
                                                EventType.TOOL_COMPACTED,
                                                roundNum=_re_rn,
                                                toolCallId=_tc_id,
                                                toolName=_re_fn,
                                                compactionLayer='L0',
                                                compactedFromChars=_pre,
                                                compactedToChars=_post,
                                                toolTokens=_re.get('toolTokens', 0),
                                                compactedContent=_tc_str,
                                            ))
                                            # Diagnostic — see matching log in
                                            # compaction.py:_stamp_l1 for the rationale.
                                            logger.info(
                                                '[L0] tool_compacted emitted: tc_id=%s '
                                                'tool=%s round=%s %dch→%dch (-%.0f%%)',
                                                _tc_id[:12] if _tc_id else '?',
                                                _re_fn or '?', _re_rn,
                                                _pre, _post,
                                                (1 - _post / _pre) * 100 if _pre else 0,
                                            )
                                        except Exception as _ev_err:
                                            logger.warning(
                                                '[L0] tool_compacted SSE emit failed: '
                                                'tc_id=%s tool=%s round=%s err=%s',
                                                _tc_id[:12] if _tc_id else '?',
                                                _re_fn or '?', _re_rn, _ev_err)
                                break

    # Measure the exact content that the next Responses request will replay.
    # Aggregate L0 compaction above can replace large results, so measuring in
    # the per-call loop would overstate bytes and report false truncations.
    if _program_messages_to_settle:
        from lib.tasks_pkg.orchestrator._programmatic import (
            settle_programmatic_call,
        )
        for _ptc, _status, _tool_message in _program_messages_to_settle:
            settle_programmatic_call(
                task, _ptc, _status, content=_tool_message.get('content'))

    # Emit snapshot AFTER tool results appended — WIRE-FORM view (same single
    # source of truth as the orchestrator's pre-LLM and final snapshots), so
    # the panel reflects exactly what the model will receive next round. Runs
    # on an independent copy via apply_wire_sanitize (does not mutate messages).
    try:
        from lib.tasks_pkg.wire_messages import apply_wire_sanitize
        _wire = apply_wire_sanitize(
            messages, conv_id=task.get('convId', ''),
            provider_id=task.get('provider_id') or '')
        snapshot = _strip_base64_for_snapshot(_wire)
        snap_evt = build_event(
            EventType.MESSAGES_SNAPSHOT,
            # Request Inspector contract: post-tool mirror, NOT an LLM request.
            kind='state',
            model=model,
            roundNum=round_num + 1,
            label=f'Round {round_num + 1} 工具结果后 · {len(snapshot)}条',
            messages=snapshot,
            contextManifest=list(task.get('_contextManifest') or []),
        )
        if tool_list:
            snap_evt['tools'] = tool_list
        append_event(task, snap_evt)
    except Exception:
        logger.warning(
            '[Task %s] messages_snapshot post-tool failed at round %d model=%s',
            tid, round_num + 1, model, exc_info=True)

    return _pipeline_timed_out


def _append_screenshot_message(messages, tc_id, tool_content):
    """Convert a screenshot dict into a multimodal tool message and append it.

    Parameters
    ----------
    messages : list[dict]
        Conversation messages — appended in-place.
    tc_id : str
        The tool_call_id to associate with the result message.
    tool_content : dict
        Screenshot dict with keys ``dataUrl``, ``format``, ``originalSize``,
        ``compressedSize``, ``compressionApplied``.
    """
    # A multi-image batch (read_files of several images) carries every image
    # in ``images``; otherwise treat the dict itself as the single image.
    img_dicts = tool_content.get('images') or [tool_content]

    def _data_url_parts(img):
        du = img.get('dataUrl', '')
        if du.startswith('data:'):
            header, b64 = du.split(',', 1)
            return header.split(':')[1].split(';')[0], b64
        return f'image/{img.get("format", "png")}', du

    content_blocks = []
    for img in img_dicts:
        media_type, b64_data = _data_url_parts(img)
        content_blocks.append({
            'type': 'image_url',
            'image_url': {'url': f'data:{media_type};base64,{b64_data}'},
        })

    # Use custom text description if provided (e.g. image gen results),
    # otherwise fall back to the generic screenshot description.
    text_desc = tool_content.get('_text_fallback')
    if not text_desc:
        fmt = tool_content.get('format', 'png')
        orig_size = tool_content.get('originalSize', 0)
        comp_size = tool_content.get('compressedSize', 0)
        size_info = f'{comp_size:,} bytes'
        if tool_content.get('compressionApplied') and orig_size:
            size_info = f'{orig_size:,} → {comp_size:,} bytes (compressed)'
        text_desc = (
            f'📸 Screenshot captured ({fmt}, {size_info}). '
            f'The image above shows the current visible area of the page. '
            f'Analyze it visually.'
        )
    if len(img_dicts) > 1:
        text_desc = f'{len(img_dicts)} images loaded above.\n{text_desc}'

    content_blocks.append({'type': 'text', 'text': text_desc})

    messages.append({
        'role': 'tool',
        'tool_call_id': tc_id,
        'content': content_blocks,
    })
