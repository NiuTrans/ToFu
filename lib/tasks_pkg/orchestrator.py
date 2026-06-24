# HOT_PATH — functions in this module are called per-request.
# Prefer logger.debug() over logger.info(). logger.info() is reserved
# for rare, high-signal events (e.g. content-filter injection, per-round diagnostics).
"""Task orchestrator — main run_task loop coordinating LLM calls and tool execution.

Also exposes ``_run_single_turn()`` — a reusable primitive that executes one
full LLM-tool cycle (setup → tool loop → finalization) on an existing task
dict.  ``endpoint.py`` uses it to drive the outer work→review→revise loop.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any

from lib.log import get_logger, set_req_id
from lib.protocols import BodyBuilder

logger = get_logger(__name__)

from lib.llm import build_body as _build_body_impl

build_body: BodyBuilder = _build_body_impl  # type: explicit protocol binding
from lib.llm import AbortedError
from lib.tasks_pkg.attachments import compute_turn_attachments, inject_attachments
from lib.tasks_pkg.cache_tracking import (
    cleanup_stale_cache_states,
    detect_cache_break,
    get_session_cache_stats,
    log_round_cache_stats,
    release_ttl_latch,
    sort_tool_results,
)
from lib.agent_core.events import EventType, build_event
from lib.tasks_pkg.compaction import run_compaction_pipeline
from lib.tasks_pkg.executor import (
    _generate_tool_summary,
)
from lib.tasks_pkg.llm_fallback import _llm_call_with_fallback
from lib.tasks_pkg.manager import (
    _strip_base64_for_snapshot,
    append_event,
    checkpoint_task_partial,
    persist_task_result,
    stream_llm_response,
)
from lib.tasks_pkg.message_builder import inject_tool_history
from lib.tasks_pkg.model_config import (
    _assemble_tool_list,
    _resolve_model_config,
)
from lib.tasks_pkg.stream_handler import analyse_stream_result
from lib.tasks_pkg.system_context import (
    _inject_system_contexts,
    _disabled_prompt_blocks,
    inject_search_addendum_to_user,
)
from lib.tasks_pkg.server_message_store import (
    rebuild_messages_with_history as _rebuild_messages_with_history,
    save_messages as _save_messages_to_store,
    estimate_token_overhead as _estimate_token_overhead,
)
from lib.tasks_pkg.tool_dispatch import (
    emit_tool_exec_phase,
    execute_tool_pipeline,
    parse_tool_calls,
    tool_label,
)


# ── Suspicious-completion detection ────────────────────────────────────────
def _check_suspicious_completion(task, last_finish_reason, _loop_exit_reason,
                                  tool_call_happened, round_num, model,
                                  assistant_msg=None):
    """Check for suspicious completion patterns and return a list of reason strings.

    Returns an empty list if the completion looks normal.  Also emits
    appropriate warning logs for each detected suspicion.
    """
    tid = task['id'][:8]
    _content_len = len(task.get('content') or '')
    _thinking_len = len(task.get('thinking') or '')
    _elapsed = time.time() - task.get('created_at', time.time())

    suspicion_reasons = []

    if _content_len == 0 and _thinking_len == 0 and not task.get('error') and not task.get('aborted'):
        suspicion_reasons.append('empty_content_and_thinking_no_error')

    if last_finish_reason == 'stop' and tool_call_happened and _content_len < 50:
        suspicion_reasons.append(f'short_content_after_tool_calls({_content_len}chars)')

    if _loop_exit_reason == 'max_rounds_exhausted':
        suspicion_reasons.append('loop_fell_through_max_rounds')
        _tc_count = len((assistant_msg or {}).get('tool_calls', []))
        logger.warning('[%s] conv=%s ⚠️ MAX TOOL ROUNDS EXHAUSTED: ran %d rounds without model stopping. '
                       'last_finish_reason=%s final_content=%dchars tool_calls_in_last_round=%d '
                       'model=%s. Consider increasing max_tool_rounds or investigating infinite tool loop.',
                       tid, task.get('convId', ''), round_num + 1, last_finish_reason, _content_len, _tc_count, model)

    if last_finish_reason is None:
        suspicion_reasons.append('finish_reason_is_None')
        logger.error('[%s] ❓ finish_reason is None — stream_llm_response likely never returned normally. '
                     'loop_exit=%s error=%s', tid, _loop_exit_reason, task.get('error') or 'none')

    if _elapsed < 1.0 and _content_len == 0:
        suspicion_reasons.append(f'completed_too_fast({_elapsed:.1f}s)_with_no_content')

    if suspicion_reasons:
        logger.warning(
            '[Orchestrator] Task %s conv=%s ⚠️ SUSPICIOUS COMPLETION detected! '
            'Reasons: %s. '
            'This task may have stopped prematurely but appears as "completed" to the user.',
            tid, task.get('convId', ''), ', '.join(suspicion_reasons)
        )

    return suspicion_reasons


# ── JSON repair for truncated / malformed LLM tool-call arguments ──────────
# Canonical implementation lives in lib.utils.repair_json.
# Re-exported here for backward compatibility.
from lib.utils import repair_json as _repair_json  # noqa: F401


def _emit_tool_round_phase(task, assistant_msg, round_num):
    """Emit a 'phase' event describing the current tool round for the frontend."""
    if round_num == 0:
        append_event(task, build_event(EventType.PHASE, phase='llm_thinking', detail='Generating response…', round=1))
    else:
        tool_names = [tc['function']['name'] for tc in assistant_msg.get('tool_calls', [])]
        unique_names = list(dict.fromkeys(tool_names))
        labeled = [tool_label(n) for n in unique_names]
        summary = ', '.join(labeled)
        append_event(task, build_event(
            EventType.PHASE, phase='llm_thinking',
            detail=f'Analyzing results and planning next step… (round {round_num+1})',
            toolContext=summary,
            round=round_num + 1,
        ))


# Realistic ceiling for genuine message JSON/role framing overhead in a
# round's cache `write`. A round's residual (write − toolResults − prevOutput)
# is at most a few hundred tokens of real framing; anything far above this is
# NOT framing — it is the conversation CONTEXT being written to cache. The
# ceiling is applied UNCONDITIONALLY (not just on cache-break rounds): the
# excess is attributed to `recacheBody` (re-billed waste) on a break round and
# to `contextWrite` (legitimate first-time caching) otherwise, so the round-1
# prefix warm-up is never mislabeled as tens of thousands of tokens of
# "message framing".
_ENVELOPE_MAX_TOKENS = 800

# Minimum cache_read drop (vs the previous round) that the write-breakdown
# treats as re-billed body (`recacheBody`) rather than first-time context
# (`contextWrite`). A drop is direct, percentage-independent evidence that
# already-cached body was NOT read back and is being re-written inside `write`.
# It is deliberately independent of detect_cache_break's 5%-relative WARNING
# gate, which (correctly, to avoid banner noise) stays silent on a small-percent
# but real-cost drop — e.g. a 4.9k re-bill on a 135k read is only ~3.6% so
# api_break never fires, and the excess used to be mislabeled "first-time
# context, not waste". Matches the project's _MIN_CACHE_MISS_TOKENS floor.
_READ_DROP_WASTE_TOKENS = 2000


def _compute_write_breakdown(task: dict[str, Any], api_rounds: list,
                             round_num: int) -> dict[str, int] | None:
    """Decompose a round's prompt-cache ``write`` into exact sub-items.

    A round's ``cache_write_tokens`` is the new context cached since the
    previous cached point. It is NOT what the model generated this round; it
    is composed of three parts, each computed here from REAL recorded numbers
    (never a hand-labeled lump):

    * ``toolResults`` — the tool RESULTS fed back into the prefix = the sum of
      ``toolTokens`` over the tool rounds whose ``llmRound`` is the PREVIOUS
      LLM iteration (``round_num - 1``); these are the per-tool token counts
      the ptool-panel badges show (``_safe_count_tokens`` of each result).
    * ``prevOutput`` — the previous API round's assistant output (model text +
      reasoning + serialized ``tool_call`` argument blocks), read from that
      round's recorded ``usage`` (``completion_tokens``/``output_tokens`` +
      ``reasoning_tokens``/``thinking_tokens``).
    * ``contextWrite`` — conversation CONTEXT (system prompt, tool definitions,
      history messages) written to cache for the FIRST time. Dominant on
      round 1 (the prefix warm-up), and on any round that appends a large fresh
      chunk of context. This is the unavoidable, non-wasteful cost of warming
      the cache — the next round reads it back. Recognized by ``cache_read``
      holding or GROWING vs the previous round (the new context is added on top
      of a still-cached prefix).
    * ``recacheBody`` — context we ALREADY paid to cache being re-billed because
      the server didn't read it back (genuine waste; see ``recacheCause``).
      Recognized EITHER by a confirmed ``cacheBreak`` flag OR — independently of
      that banner-level alarm — by ``cache_read`` DROPPING vs the previous round
      (``readDrop`` ≥ ``_READ_DROP_WASTE_TOKENS``): a drop is direct evidence
      that already-cached body fell out and is now inside ``write``. The
      ``detect_cache_break`` 5%-relative gate stays silent on a small-percent
      but real-cost drop (e.g. 4.9k re-billed on a 135k read ≈ 3.6%), so relying
      on it alone mislabeled that waste as benign ``contextWrite``. When the
      excess exceeds the read drop, only the drop is ``recacheBody`` and the
      remainder is genuine new ``contextWrite``; the two split the excess.
    * ``envelope`` — the message JSON/role framing overhead, the residual
      ``write - prevOutput - toolResults - (contextWrite|recacheBody)`` capped
      at ``_ENVELOPE_MAX_TOKENS``. By construction the sub-items sum to EXACTLY
      ``write``.

    Args:
        task: Live task dict (read-only here) — used for ``toolRounds``.
        api_rounds: The per-LLM-round usage list. ``api_rounds[-1]`` is the
            round just recorded (round ``round_num + 1``); ``api_rounds[-2]``
            is the previous round (round ``round_num``), whose output became
            part of this round's write.
        round_num: Zero-based orchestrator loop index of the CURRENT iteration.

    The residual above ``_ENVELOPE_MAX_TOKENS`` is ALWAYS context, not framing
    — a 64k round-1 "envelope" (the symptom that motivated this) is the whole
    system+tools+history prefix being cached for the first time. It is split
    into ``contextWrite`` (first-time caching, no break) or ``recacheBody``
    (re-billed body, on a break round); ``envelope`` keeps only a realistic
    framing allowance. All sub-items still sum to ``write``.

    Returns:
        ``{'write', 'toolResults', 'prevOutput', 'contextWrite', 'recacheBody',
        'envelope', 'recacheCause', 'capped'}`` token dict, or ``None`` when this round
        wrote no cache (nothing to decompose) or the inputs are missing.
        Returning ``None`` keeps the frontend on its plain inflow line rather
        than printing a meaningless breakdown.
    """
    try:
        if not api_rounds:
            return None
        _cur = api_rounds[-1]
        if not isinstance(_cur, dict):
            return None
        _u = _cur.get('usage') or {}
        write = int(_u.get('cache_write_tokens')
                    or _u.get('cache_creation_input_tokens') or 0)
        if write <= 0:
            return None

        # (a) tool results that flowed into THIS round's prefix: the tools that
        #     ran in the previous LLM iteration (llmRound == round_num - 1).
        tool_results = 0
        _prev_llm_round = round_num - 1
        for _r in (task.get('toolRounds') or []):
            if not isinstance(_r, dict):
                continue
            if _r.get('llmRound') == _prev_llm_round:
                _tt = _r.get('toolTokens')
                if isinstance(_tt, (int, float)) and _tt > 0:
                    tool_results += int(_tt)

        # (b) previous API round's output tokens (text + reasoning + tool_call
        #     args). The previous round is api_rounds[-2]; fall back to 0 when
        #     this is the first recorded round (no predecessor).
        prev_output = 0
        if len(api_rounds) >= 2 and isinstance(api_rounds[-2], dict):
            _pu = api_rounds[-2].get('usage') or {}
            prev_output = int(_pu.get('completion_tokens') or _pu.get('output_tokens') or 0) \
                + int(_pu.get('reasoning_tokens') or _pu.get('thinking_tokens') or 0)

        # (b2) cache_read delta vs the previous round. A DROP means part of the
        #     previously-cached prefix was not read back this round and is being
        #     re-billed inside `write` — direct, percentage-independent evidence
        #     of waste. This is what distinguishes a large write that is genuine
        #     first-time context (read held/grew) from one that is re-cached
        #     body (read fell), WITHOUT relying on detect_cache_break's
        #     5%-relative warning gate.
        cur_read = int(_u.get('cache_read_tokens')
                       or _u.get('cache_read_input_tokens') or 0)
        prev_read = 0
        if len(api_rounds) >= 2 and isinstance(api_rounds[-2], dict):
            _pru = api_rounds[-2].get('usage') or {}
            prev_read = int(_pru.get('cache_read_tokens')
                            or _pru.get('cache_read_input_tokens') or 0)
        read_drop = max(0, prev_read - cur_read)

        # (c) envelope = the genuine residual. INVARIANT: the three sub-items
        #     MUST sum to exactly `write` (the whole point — a breakdown that
        #     doesn't add up is worse than none). prev_output / tool_results
        #     are counted with a DIFFERENT (output-side / local) tokenizer than
        #     the provider's input-side `cache_write_tokens`, so they can
        #     legitimately overshoot `write`. When that happens we must NOT
        #     print components that exceed the total. Resolve by treating
        #     `write` as ground truth and capping the measured components to it
        #     in priority order (tool results first — they're the most directly
        #     attributable and match the ptool badges, then prev output), with
        #     the envelope absorbing whatever is left. This keeps
        #     toolResults + prevOutput + envelope == write ALWAYS.
        tool_results = min(tool_results, write)
        prev_output = min(prev_output, write - tool_results)
        residual = write - tool_results - prev_output  # always >= 0 now

        # (d) Split the residual. On a NORMAL round the residual is just the
        #     message JSON/role framing overhead (tens of tokens/message) — a
        #     few hundred tokens. On a CACHE-BREAK round the residual is huge
        #     (tens of thousands) because the conversation BODY between the
        #     static prefix and the tail was re-cached: a prefix-byte mutation
        #     or a breakpoint advance re-billed the whole body uncached. Lumping
        #     that into "envelope" is the lie the user caught (36.5k of
        #     "structure"). So when this round carries a cacheBreak signal,
        #     attribute the bulk to `recacheBody` and leave only a realistic
        #     framing allowance as `envelope`. The four sub-items still sum to
        #     EXACTLY `write` (recacheBody is the residual minus the allowance).
        # Framing is fundamentally BOUNDED, so cap envelope unconditionally and
        # attribute the excess by cause: on a cache-break round it is body we
        # already cached being re-billed (`recacheBody`, waste); otherwise it is
        # context cached for the first time (`contextWrite`, e.g. the round-1
        # prefix warm-up — legitimate, not framing). Exactly one of the two is
        # ever non-zero. The five sub-items still sum to EXACTLY `write`.
        _cache_break = _cur.get('cacheBreak')
        recache_body = 0
        context_write = 0
        envelope = residual
        if residual > _ENVELOPE_MAX_TOKENS:
            envelope = _ENVELOPE_MAX_TOKENS
            excess = residual - envelope
            if _cache_break:
                # Confirmed break (detect_cache_break fired) → the whole excess
                # is re-billed body.
                recache_body = excess
            elif read_drop >= _READ_DROP_WASTE_TOKENS:
                # No banner-level break, but cache_read fell vs the previous
                # round: at least `read_drop` tokens of already-cached body are
                # being re-written (waste). Attribute that to recacheBody and
                # only the remainder — genuinely new context — to contextWrite.
                recache_body = min(excess, read_drop)
                context_write = excess - recache_body
            else:
                # Read held or grew → the excess is fresh context cached for the
                # first time (e.g. the round-1 prefix warm-up). Legitimate.
                context_write = excess

        return {
            'write': write,
            'toolResults': tool_results,
            'prevOutput': prev_output,
            'contextWrite': context_write,
            'recacheBody': recache_body,
            'envelope': envelope,
            # How far cache_read fell vs the previous round (0 if it held/grew).
            # Lets the frontend explain a recacheBody term that the banner-level
            # detector did not flag.
            'readDrop': read_drop,
            # The cache-break cause string (if any) that drove the re-cache —
            # lets the frontend tie the recacheBody term to the 缓存失效 line.
            # When recacheBody is driven by a sub-threshold read drop (no formal
            # break), synthesize a cause so the term is never left unexplained.
            'recacheCause': (
                _cache_break if isinstance(_cache_break, dict)
                else ({'no_cache_reuse':
                       f'cache_read 较上一轮下降 {read_drop} tok（已缓存正文被重新计费）'}
                      if recache_body > 0 else {})
            ),
            # True when the measured components had to be capped because they
            # exceeded `write` (output-side vs input-side tokenizer mismatch).
            # The frontend can note the figures are approximate in this case.
            'capped': (tool_results + prev_output) >= write and residual == 0,
        }
    except Exception as e:
        logger.debug('write-breakdown compute failed: %s', e)
        return None


def derive_round_modified_files(task: dict, project_path: str | None,
                                project_paths: list[str] | None) -> tuple[list[dict], int, bool]:
    """Build this round's authoritative file-change list from the journal.

    The modifications journal is keyed per-root (``session_dir =
    md5(base_path)``), so a write to an EXTRA workspace root lands in THAT
    root's journal — not the primary's.  Scanning only ``project_path``
    (the primary) makes extra-root edits invisible, which in turn lets the
    project-global file-history side-channel seed ``modifiedFileList`` with
    a CONCURRENT conversation's edit instead of this round's real edits.

    This helper scans the primary root PLUS every extra root in
    ``project_paths[1:]``, keeps only modifications stamped with THIS
    task's id (falling back to a start-timestamp filter for legacy mods),
    and returns ``(file_list, count, used_ts_fallback)``.  Because each mod
    is taskId-stamped at write time, the result is conversation-isolated
    and cannot leak across conversations.

    Args:
        task: The task dict (needs ``id``, ``convId``, ``created_at``).
        project_path: Primary workspace root abs path.
        project_paths: Full ``cfg['projectPaths']`` list (index 0 == primary);
            indices 1.. are extra roots.

    Returns:
        ``(file_list, count, used_ts_fallback)`` where ``file_list`` is a
        list of ``{path, action, root?}`` dicts keyed uniquely by
        ``(root, path)``.
    """
    from lib.project_mod import get_modifications

    conv_id = task.get('convId')
    scan_roots: list[str] = []
    seen_roots: set[str] = set()
    for p in ([project_path] + list((project_paths or [])[1:])):
        if p and p not in seen_roots:
            seen_roots.add(p)
            scan_roots.append(p)

    turn_mods: list[dict] = []
    used_ts_fallback = False
    for root in scan_roots:
        root_mods = get_modifications(root, conv_id=conv_id) or []
        if not root_mods:
            continue
        own = [m for m in root_mods if m.get('taskId') == task.get('id')]
        if not own:
            task_start = task.get('created_at', 0)
            own = [m for m in root_mods if m.get('timestamp', 0) >= task_start]
            if own:
                used_ts_fallback = True
        turn_mods.extend(own)

    if not turn_mods:
        return [], 0, used_ts_fallback

    seen: dict[tuple[str, str], dict] = {}
    for m in turn_mods:
        p = m.get('path', '?')
        t = m.get('type', '')
        root_name = m.get('root', '') or ''
        if t == 'write_file':
            action = 'created' if not m.get('existed', True) else 'written'
        elif t in ('apply_diff', 'apply_diffs'):
            action = 'patched'
        elif t in ('insert_content', 'insert_contents'):
            action = 'inserted'
        elif t == 'run_command':
            # Resolve the exists-check against the mod's OWN root
            # (basePath), not the primary, so extra-root deletes classify
            # correctly.
            base = m.get('basePath') or project_path or ''
            abs_p = p if os.path.isabs(p) else os.path.join(base, p)
            if not m.get('existed', True):
                action = 'created'
            elif 'originalContent' in m and not os.path.exists(abs_p):
                action = 'deleted'
            else:
                action = 'modified'
        else:
            action = t
        seen[(root_name, p)] = {'action': action, 'root': root_name}

    file_list = [
        {'path': p, 'action': info['action'],
         **({'root': info['root']} if info['root'] else {})}
        for (root_name, p), info in seen.items()
    ]
    return file_list, len(turn_mods), used_ts_fallback


def _finalize_and_emit_done(task: dict[str, Any], *, model: str, preset: str, thinking_depth: str | None, cfg: dict[str, Any],
                            last_finish_reason, last_usage, accumulated_usage, api_rounds,
                            tool_call_happened, messages, original_messages,
                            all_search_results_text, max_tokens, thinking_enabled, temperature,
                            _loop_exit_reason, _abort_detected_phase, project_path, project_enabled,
                            round_num, assistant_msg):
    """Post-loop finalization: fallback synthesis, done-event construction, and emit.

    Handles the fallback LLM call when the main loop produced no content,
    determines the final finish reason, generates tool summaries, and emits
    the 'done' event with full diagnostic information.
    """
    tid = task['id'][:8]

    # ── Fallback: synthesize answer from search results if main loop produced nothing ──
    if not task['content'].strip() and tool_call_happened and all_search_results_text and not task['aborted']:
        combined = '\n\n---\n\n'.join(all_search_results_text)
        fb = list(original_messages)
        fb.append({'role':'assistant','content':"I've gathered the information. Let me analyze it."})
        fb.append({'role':'user','content':f'Here are fetched contents:\n\n{combined}\n\nProvide a comprehensive answer. Cite sources.'})
        try:
            snapshot = _strip_base64_for_snapshot(fb)
            append_event(task, build_event(EventType.MESSAGES_SNAPSHOT, round='fallback', label=f'Fallback · {len(fb)}条', messages=snapshot))
        except Exception as e:
            logger.warning('[Task %s] messages_snapshot fallback failed, model=%s: %s', tid, model, e, exc_info=True)
        body = build_body(
            model, fb,
            max_tokens=max_tokens,
            temperature=temperature,
            thinking_enabled=thinking_enabled,
            preset=preset,
            thinking_depth=thinking_depth,
            response_format=cfg.get('responseFormat'),
            stream=True,
        )
        try:
            _, fr, usg = stream_llm_response(task, body, tag='FALLBACK')
            last_finish_reason = fr
            if usg:
                last_usage = usg
                for k, v in usg.items():
                    if isinstance(v, (int, float)):
                        accumulated_usage[k] = accumulated_usage.get(k, 0) + v
                api_rounds.append({'round': 'fallback', 'model': model, 'usage': dict(usg), 'tag': 'FALLBACK'})
                from lib.tasks_pkg.llm_fallback import _emit_round_usage
                _emit_round_usage(task, 'fallback', model, usg, tag='FALLBACK')
        except Exception as e:
            logger.error('[%s] ⚠️ Post-loop fallback failed: %s', tid, e, exc_info=True)
            try:
                from lib.llm_error_format import format_llm_error_for_user
                task['error'] = format_llm_error_for_user(
                    e, model=model, context='post-loop-fallback',
                    source='orchestrator')
            except Exception as _fmt_err:
                logger.warning('[%s] format_llm_error_for_user failed: %s', tid, _fmt_err)
                from lib.error_envelope import make_envelope as _make_env
                task['error'] = _make_env(
                    'internal',
                    detail=f'Post-loop fallback failed: {e}',
                    model=model,
                    context='post-loop-fallback',
                    source='orchestrator',
                    raw=str(e),
                )

    # ── Content-filter: give user a meaningful error instead of blank bubble ──
    if (not task['content'].strip()
            and not task['aborted']
            and (last_finish_reason == 'content_filter'
                 or (_loop_exit_reason and 'content_filter' in str(_loop_exit_reason).lower()))):
        task['content'] = '⚠️ 该回复被模型安全过滤器拦截，请尝试换一种方式提问。\n\n_The response was blocked by the model\'s safety filter. Please try rephrasing your question._'
        logger.info('[%s] Injected content_filter user-facing message (finish_reason=%s, loop_exit=%s)',
                    tid, last_finish_reason, _loop_exit_reason)

    # ── Determine final finish reason ──
    if task['aborted']:
        _pre_abort_finish = last_finish_reason
        last_finish_reason = 'aborted'
        if _abort_detected_phase:
            logger.debug('[%s] Abort was detected INSIDE loop at: %s model=%s '
                         '(original finish_reason was "%s")',
                         tid, _abort_detected_phase, model, _pre_abort_finish)
        else:
            logger.warning('[%s] LATE ABORT: loop exited normally (%s) model=%s '
                           'but task["aborted"] is True. Original finish_reason was "%s". '
                           'The user likely clicked Stop AFTER the model finished but BEFORE the response was fully rendered.',
                           tid, _loop_exit_reason, model, _pre_abort_finish)
    elif last_finish_reason in ('tool_use', 'tool_calls') and not task.get('error'):
        last_finish_reason = 'error'
        from lib.error_envelope import make_envelope as _make_env
        task['error'] = _make_env(
            'internal',
            detail='Model requested tool calls but the loop ended unexpectedly.',
            model=model,
            context='post-loop',
            source='orchestrator',
            raw='finish_reason=%s but loop exited without further tool execution' % last_finish_reason,
        )

    task['finishReason'] = last_finish_reason
    task['usage'] = accumulated_usage if accumulated_usage else last_usage
    task['preset'] = cfg.get('preset') or cfg.get('effort', 'medium')

    # ── Fold in compaction's OWN LLM usage ──
    # L2 smart-summary and the advanced-host summarizers (OpenCode/Hermes/
    # OpenClaw arms) call the LLM but historically discarded that usage, so
    # task['usage'] (→ reported cost) under-counted exactly the summary-based
    # strategies. Drain the per-conv accumulator and add it in, while also
    # exposing it separately so a cost breakdown can show compaction overhead.
    try:
        from lib.tasks_pkg.compaction._compaction_usage import pop_compaction_usage
        _comp_usage = pop_compaction_usage(task.get('convId', ''))
        if _comp_usage:
            task['compactionUsage'] = _comp_usage
            _u = task['usage'] or {}
            for _k, _v in _comp_usage.items():
                if _k == 'n_calls':
                    continue
                if isinstance(_v, (int, float)) and isinstance(_u.get(_k), (int, float)):
                    _u[_k] = _u[_k] + _v
                elif isinstance(_v, (int, float)) and _k not in _u:
                    _u[_k] = _v
            task['usage'] = _u
            logger.info('[Usage] conv=%s folded compaction usage (%d calls) into total: %s',
                        (task.get('convId') or '')[:8], _comp_usage.get('n_calls', 0),
                        {k: v for k, v in _comp_usage.items() if k != 'n_calls'})
    except Exception as _cu_e:
        logger.debug('[Usage] compaction-usage fold failed: %s', _cu_e)

    # ── Generate tool summary for cross-turn context (non-blocking) ──
    if tool_call_happened and not task['aborted']:
        try:
            summary = _generate_tool_summary(messages, model, task)
            if summary:
                task['toolSummary'] = summary
        except Exception as e:
            logger.warning('[Task %s] Tool summary generation failed model=%s (non-fatal): %s', task['id'][:8], model, e, exc_info=True)

    if not task.get('_endpoint_managed'):
        # Latch the autopilot decision window BEFORE flipping status to 'done'.
        # The status flip makes _task_terminal() true for the SSE generator and
        # chat_poll; setting the marker first closes the gap where they'd
        # observe 'done' before the autopilot hook (which can take several
        # seconds for the VU LLM call) has a chance to set it — otherwise a
        # late synthetic done closes the stream without the follow-up baton.
        try:
            from lib.tasks_pkg.autopilot import is_autopilot_enabled
            if is_autopilot_enabled(task):
                task['_autopilot_deciding'] = True
        except Exception as _ap_latch_err:
            logger.debug('[Autopilot] pre-flip decision latch skipped: %s',
                         _ap_latch_err)
        task['status'] = 'done'

    # ── Cleanup reactive compact tracking (prevent memory leak) ──
    from lib.tasks_pkg.llm_fallback import cleanup_reactive_compact_state
    cleanup_reactive_compact_state(task.get('id', ''))

    # ── Release session-stable TTL latch (prevent memory leak) ──
    release_ttl_latch(task.get('id', ''))

    # ── Swarm session teardown (Option A — conversation-scoped) ──
    #
    # A swarm now outlives the single turn that spawned it: its lifetime is
    # bounded by the CONVERSATION, not this task. So on a NORMAL turn end we
    # must NOT abort a swarm whose agents are still running — the user's
    # background work would be discarded (the exact bug this fixes). We only
    # tear down when:
    #   (a) the user explicitly aborted this task (Stop button), OR
    #   (b) the swarm has already terminated on its own.
    # Otherwise we DETACH: leave the live session + its inbox intact so the
    # next turn in this conversation drains pending <swarm-update>s and can
    # await / fetch results. TTL eviction (conv-aware ``_key_is_live``)
    # reaps it only once the conversation goes quiet.
    try:
        from lib.agent_inbox import clear as _clear_inbox
        from lib.swarm.integration import _remove_session as _remove_swarm_session
        from lib.swarm.integration import get_active_session as _get_swarm_session
        from lib.swarm.integration import swarm_key_for as _swarm_key_for
        _swarm_key = _swarm_key_for(task)
        _swarm_sess = _get_swarm_session(_swarm_key)
        _user_aborted = bool(task.get('aborted'))
        if _swarm_sess is not None and (_user_aborted or _swarm_sess.is_terminated):
            try:
                _swarm_sess.abort()
            except Exception as _e:
                logger.debug('[Orchestrator] swarm abort on task end: %s', _e)
            _remove_swarm_session(_swarm_key)
            _clear_inbox(_swarm_key)
            logger.info('[Orchestrator] swarm torn down on task end '
                        '(key=%s reason=%s)', _swarm_key,
                        'user_abort' if _user_aborted else 'terminated')
        elif _swarm_sess is not None:
            logger.info('[Orchestrator] swarm DETACHED on normal turn end — '
                        'still running, will deliver on later turns (key=%s)',
                        _swarm_key)
    except Exception as _e:
        logger.warning('[Orchestrator] swarm/inbox cleanup on task end failed: %s', _e, exc_info=True)

    # ── Log session-level aggregate cache stats ──
    _conv_id = task.get('convId', '')
    if _conv_id:
        _session_stats = get_session_cache_stats(_conv_id)
        if _session_stats and _session_stats['calls'] > 1:
            logger.info(
                '[CacheSession] %s conv=%s END — %d calls, '
                'total_read=%d total_write=%d overall_hit=%d%% '
                'breaks=%d duration=%.1fs model=%s',
                tid, _conv_id[:8],
                _session_stats['calls'],
                _session_stats['total_cache_read'],
                _session_stats['total_cache_write'],
                _session_stats['overall_hit_pct'],
                _session_stats['total_breaks'],
                _session_stats['session_duration_s'],
                _session_stats['model'],
            )

    # ── Periodic stale cache state cleanup (every task completion) ──
    # Lightweight: only scans and removes entries older than 1 hour.
    try:
        cleanup_stale_cache_states(max_age_s=3600)
    except Exception as e:
        logger.debug('[Orchestrator] stale cache cleanup failed: %s', e)

    # ── Tool dedup cache stats (logged at task completion) ──
    _dedup_cache = task.get('_tool_result_cache')
    if _dedup_cache:
        _dedup_size = len(_dedup_cache)
        if _dedup_size > 0:
            logger.info(
                '[DedupCache] %s conv=%s task END — %d cached entries',
                tid, _conv_id[:8] if _conv_id else '???', _dedup_size)

    # ── Diagnostic: log completion stats ──
    _content_len = len(task.get('content') or '')
    _thinking_len = len(task.get('thinking') or '')
    _elapsed = time.time() - task.get('created_at', time.time())
    logger.debug('[Orchestrator] Task %s conv=%s COMPLETED — content=%dchars thinking=%dchars '
                  'error=%s elapsed=%.1fs finishReason=%s toolCalls=%s',
                 task['id'][:8], task.get('convId', ''), _content_len, _thinking_len,
                 task.get('error') or 'none', _elapsed, last_finish_reason,
                 'yes' if tool_call_happened else 'no')
    if _content_len == 0 and _thinking_len == 0 and not task.get('error') and not task.get('aborted'):
        logger.warning('[Orchestrator] Task %s conv=%s ⚠️ COMPLETED WITH EMPTY CONTENT '
                      'and no error! This will appear as a blank message to the user.',
                      task['id'][:8], task.get('convId', ''))

    logger.debug(
        '[Orchestrator] Task %s LIFECYCLE SUMMARY:\n'
        '  loop_exit_reason   = %s\n'
        '  last_finish_reason = %s\n'
        '  rounds_completed   = %d\n'
        '  tool_call_happened = %s\n'
        '  content_length     = %d\n'
        '  thinking_length    = %d\n'
        '  error              = %s\n'
        '  model              = %s\n'
        '  elapsed            = %.1fs\n'
        '  api_rounds         = %d\n'
        '  aborted            = %s\n'
        '  abort_phase        = %s',
        tid, _loop_exit_reason, last_finish_reason, round_num + 1,
        tool_call_happened, _content_len, _thinking_len,
        task.get('error') or 'none', model, _elapsed,
        len(api_rounds), task.get('aborted', False),
        _abort_detected_phase or 'n/a',
    )

    # ── Flag suspicious completions ──
    _suspicion_reasons = _check_suspicious_completion(
        task, last_finish_reason, _loop_exit_reason,
        tool_call_happened, round_num, model,
        assistant_msg=assistant_msg,
    )

    # ── Build done event ──
    done_evt = build_event(EventType.DONE)
    # ★ Always expose the task ID (the whole user→assistant turn, across ALL
    #   tool rounds). The frontend shows it in the cost popover so the user
    #   can quote ONE id back to us for root-cause analysis — and it's the
    #   key every [Task:id] log line is tagged with. Previously taskId was
    #   only set inside the project-modifications block below, so chat-only
    #   turns (no file changes) never received it.
    done_evt['taskId'] = task['id']
    if last_finish_reason: done_evt['finishReason'] = last_finish_reason
    final_usage = accumulated_usage if accumulated_usage else last_usage
    if final_usage: done_evt['usage'] = final_usage
    if task.get('preset'): done_evt['preset'] = task['preset']
    done_evt['model'] = model
    task['model'] = model
    if thinking_depth:
        done_evt['thinkingDepth'] = thinking_depth
        task['thinkingDepth'] = thinking_depth
    if task.get('error'): done_evt['error'] = task['error']
    if task.get('toolSummary'): done_evt['toolSummary'] = task['toolSummary']
    # Tool-schema latch: a mid-conversation tool toggle was held back to keep
    # the prompt cache intact. Tell the frontend so it can offer "Apply now".
    if cfg.get('_toolsetDiverged'):
        done_evt['toolsetDiverged'] = True
        _ts_diff = cfg.get('_toolsetDiff')
        if _ts_diff and (_ts_diff.get('added') or _ts_diff.get('removed')):
            done_evt['toolsetDiff'] = _ts_diff
    if api_rounds:
        done_evt['apiRounds'] = api_rounds
        task['apiRounds'] = api_rounds
    if task.get('_fallback_model'):
        done_evt['fallbackModel'] = task['_fallback_model']
        done_evt['fallbackFrom'] = task.get('_fallback_from', '')
        if task.get('_fallback_reason'):
            done_evt['fallbackReason'] = task['_fallback_reason']
        if task.get('_fallback_kind'):
            done_evt['fallbackKind'] = task['_fallback_kind']
    if project_enabled and task['convId']:
        try:
            # Authoritative source of truth: this round's OWN journalled
            # writes, aggregated across EVERY workspace root the task may
            # have touched (primary + extras).  See
            # ``derive_round_modified_files`` for why scanning the primary
            # alone leaked a concurrent conversation's edit.
            file_list, _n_mods, _used_ts_fallback = derive_round_modified_files(
                task, project_path, cfg.get('projectPaths'))
            if file_list:
                done_evt['modifiedFiles'] = _n_mods
                task['modifiedFiles'] = _n_mods
                # ★ Include taskId so frontend can do per-round undo
                done_evt['taskId'] = task['id']
                done_evt['modifiedFileList'] = file_list
                task['modifiedFileList'] = file_list
                _n_roots = 1 + len([p for p in (cfg.get('projectPaths') or [])[1:]
                                    if p and p != project_path])
                if _n_roots > 1:
                    logger.info('[Task %s] modifiedFileList derived across %d roots: '
                                '%d file(s)%s', task['id'][:8], _n_roots,
                                len(file_list), ' (ts-fallback)' if _used_ts_fallback else '')
        except Exception as e:
            logger.warning('[Task %s] get_modifications failed for conv=%s model=%s: %s',
                      task['id'][:8], task.get('convId', ''), model, e, exc_info=True)
    # ── Continue checkpoint merging: merge pre-checkpoint metadata into
    #   both the done event and the task dict so that:
    #   (a) the frontend done handler sees merged data (even though it also
    #       merges client-side, this makes poll fallback consistent), and
    #   (b) _sync_result_to_conversation writes the full merged set to DB. ──
    _cp_usage = task.get('_checkpointUsage')
    if _cp_usage and done_evt.get('usage'):
        merged_usage = {}
        for k in set(list(_cp_usage.keys()) + list(done_evt['usage'].keys())):
            cv = _cp_usage.get(k)
            nv = done_evt['usage'].get(k)
            merged_usage[k] = (cv + nv) if isinstance(cv, (int, float)) and isinstance(nv, (int, float)) else (nv if nv is not None else cv)
        done_evt['usage'] = merged_usage
        task['usage'] = merged_usage
    elif _cp_usage and not done_evt.get('usage'):
        done_evt['usage'] = _cp_usage
        task['usage'] = _cp_usage

    _cp_api_rounds = task.get('_checkpointApiRounds')
    if _cp_api_rounds:
        merged_api = list(_cp_api_rounds) + (done_evt.get('apiRounds') or [])
        done_evt['apiRounds'] = merged_api
        task['apiRounds'] = merged_api

    _cp_mod_files = task.get('_checkpointModifiedFiles')
    if _cp_mod_files is not None and done_evt.get('modifiedFiles') is not None:
        done_evt['modifiedFiles'] = _cp_mod_files + done_evt['modifiedFiles']
        task['modifiedFiles'] = done_evt['modifiedFiles']

    _cp_mod_list = task.get('_checkpointModifiedFileList')
    if _cp_mod_list:
        # Merge: old + new, dedup by (root, path) so same relative path in
        # different workspace roots stays distinct in multi-root setups.
        merged_map = {}
        def _key(f):
            if isinstance(f, dict):
                return (f.get('root', '') or '', f.get('path', ''))
            return ('', str(f))
        for f in _cp_mod_list:
            merged_map[_key(f)] = f
        for f in (done_evt.get('modifiedFileList') or []):
            merged_map[_key(f)] = f
        merged_list = list(merged_map.values())
        done_evt['modifiedFileList'] = merged_list
        task['modifiedFileList'] = merged_list

    if _suspicion_reasons:
        done_evt['_diagnostics'] = {
            'loop_exit_reason': _loop_exit_reason,
            'rounds_completed': round_num + 1,
            'finish_reason': last_finish_reason,
            'content_len': _content_len,
            'thinking_len': _thinking_len,
            'suspicions': _suspicion_reasons,
        }

    # ── Emit done event (unless endpoint-managed) ──
    #
    # The file-history snapshot for this round runs in a daemon thread
    # AFTER ``persist_task_result`` so queue-dispatch is never blocked
    # by snapshot I/O.  When the snapshot completes we emit a separate
    # ``round_committed`` SSE event carrying ``snapshotId`` (and the
    # legacy ``gitSha`` field, kept for frontend backward-compat) plus
    # any side-channel ``modifiedFileList`` additions discovered by
    # ``diff_name_status``.
    if task.get('_endpoint_managed'):
        _spawn_async_commit_round(task, project_enabled, project_path)
        return
    # ── Producer B: scan the finalized assistant content for inline
    #    renderable artifacts (large fenced ```html / ```markdown blocks,
    #    bare <!doctype html> documents).  Best-effort — failures here
    #    must NOT block the done event or persistence.
    try:
        import lib as _lib_artifacts_gate
        if getattr(_lib_artifacts_gate, 'ARTIFACTS_ENABLED', True):
            from lib.artifacts import scan_message
            scan_message(
                task.get('convId') or '',
                task.get('content') or '',
                msg_id=task.get('_assistantMsgId') or '',
                task_id=task.get('id') or '',
                task=task,
            )
    except Exception as e:
        logger.debug('[Artifacts:scan] orchestrator hook failed (non-fatal): %s',
                     e, exc_info=True)

    # ── Autopilot hook (runs BEFORE the done event so its result can
    #    ride along on the same SSE message).  When autopilot is on and
    #    the VU produces a reply, this also writes the synthetic user
    #    message to the conversation DB and spawns the follow-up task.
    #    The frontend reads ``autopilotNextTaskId`` + ``autopilotVuMessage``
    #    from the done event and connects directly — no polling race.
    # ``task['status']`` was flipped to 'done' before this hook (see the
    # _run_loop tail), but the VU LLM call below can take several seconds.
    # Mark the autopilot decision as in-flight so chat_poll keeps reporting
    # 'running' until the baton exists — otherwise a poll landing in this
    # window would finalize the stream WITHOUT the follow-up handoff and
    # strand the already-spawned successor task.
    #
    # ── Commit the parent's FINAL assistant message to the conversation
    #    DB BEFORE running autopilot.  The autopilot hook appends the
    #    virtual-user turn AND spawns the follow-up task, which registers
    #    as the conversation's latest task and rebuilds its context from
    #    the DB.  The trailing persist_task_result → _sync_result_to_conversation
    #    would then be REJECTED by the freshness guard (superseded by the
    #    autopilot follow-up), freezing the parent reply at its last
    #    streaming checkpoint (truncated content, finishReason=None) and
    #    feeding that truncated copy to the follow-up.  Syncing here first
    #    makes the VU and follow-up layer on top of the complete reply; the
    #    later persist sync becomes a harmless no-op skip.
    from lib.tasks_pkg.autopilot import is_autopilot_enabled
    if is_autopilot_enabled(task) and task.get('convId'):
        try:
            from lib.tasks_pkg.manager import (
                _sync_result_to_conversation,
                build_result_meta,
            )
            _sync_result_to_conversation(task, build_result_meta(task))
        except Exception as _pre_ap_err:
            logger.warning('[Autopilot] pre-hook conv sync failed: %s — '
                           'follow-up may see a truncated parent reply',
                           _pre_ap_err, exc_info=True)
    task['_autopilot_deciding'] = True
    try:
        from lib.tasks_pkg.autopilot import maybe_run_autopilot
        ap_result = maybe_run_autopilot(task)
        if ap_result:
            done_evt['autopilotNextTaskId'] = ap_result['next_task_id']
            done_evt['autopilotVuMessage'] = ap_result['vu_msg']
            # Stash on the task dict too so the baton is transport-agnostic:
            # the poll route surfaces the SAME handoff, so a client that fell
            # back to /api/chat/poll (SSE stripped / timed out) still attaches
            # to the follow-up instead of stranding it (sidebar dot / pause
            # button / translation desync until manual refresh).
            task['_autopilot_followup'] = ap_result
    except Exception as _ap_err:
        logger.warning('[Autopilot] hook raised: %s — continuing without '
                       'follow-up (this turn will still be persisted)',
                       _ap_err, exc_info=True)
    finally:
        task['_autopilot_deciding'] = False

    # ── Stamp cost snapshot on the done event ──
    # Mirrors the persisted-cost write in
    # lib.tasks_pkg.manager._sync_result_to_conversation: cost depends only
    # on usage + model + provider + the active pricing table, all of which
    # are final at this point. Sending it on the done event eliminates the
    # per-render `/api/v1/messages/cost` round-trips on the LIVE path —
    # the persisted-cost write covers reload paths.
    try:
        from lib.cost import compute_cost as _compute_cost
        if done_evt.get('usage'):
            _msg_cost = _compute_cost(
                done_evt['usage'],
                model_id=done_evt.get('model') or task.get('model') or '',
                provider_id=task.get('provider_id') or None,
            )
            if _msg_cost:
                done_evt['cost'] = _msg_cost
        for _rd in done_evt.get('apiRounds') or []:
            if not isinstance(_rd, dict) or _rd.get('cost'):
                continue
            _ru = _rd.get('usage') or {}
            if not _ru:
                continue
            _rc = _compute_cost(
                _ru,
                model_id=_rd.get('model') or done_evt.get('model') or '',
                provider_id=(_rd.get('provider_id')
                              or _rd.get('providerId')
                              or task.get('provider_id') or None),
            )
            if _rc:
                _rd['cost'] = _rc
    except Exception as _ce:
        logger.warning('[Cost] done-event stamp failed (non-fatal): %s', _ce)

    # ★ Comprehensive task-completion summary — keyed on the FULL task id so a
    #   user who quotes the id from the cost popover can grep ONE line that
    #   spans the whole turn (all tool rounds). Includes the per-round cache
    #   miss count so "why did cache break" is answerable straight from the
    #   log without re-deriving it. INFO level → lands in logs/app.log.
    try:
        _rounds = done_evt.get('apiRounds') or []
        _miss_rounds = [r.get('round') for r in _rounds
                        if isinstance(r, dict) and r.get('cacheBreak')]
        _u = done_evt.get('usage') or {}
        _cw = (_u.get('cache_write_tokens')
               or _u.get('cache_creation_input_tokens') or 0)
        _cr = (_u.get('cache_read_tokens')
               or _u.get('cache_read_input_tokens') or 0)
        _cost = (done_evt.get('cost') or {}).get('costCny')
        logger.info(
            '[Task:%s] ■ DONE conv=%s model=%s rounds=%d finish=%s '
            'cache_write=%d cache_read=%d cost=%s elapsed=%.1fs%s',
            task['id'], task.get('convId', '') or '-', model, len(_rounds),
            last_finish_reason or '-', _cw, _cr,
            (f'\u00a5{_cost:.3f}' if isinstance(_cost, (int, float)) else '?'),
            time.time() - task.get('created_at', time.time()),
            (f' \u26a0 CACHE_MISS rounds={_miss_rounds}' if _miss_rounds else ''),
        )
    except Exception as _se:
        logger.debug('[Task:%s] completion summary log failed: %s',
                     task['id'][:8], _se)

    append_event(task, done_evt)
    persist_task_result(task)

    _spawn_async_commit_round(task, project_enabled, project_path)


def _spawn_async_commit_round(task: dict, project_enabled: bool, project_path: str | None) -> None:
    """Run ``file_history.make_snapshot`` in a daemon thread.

    Decoupled from ``_finalize_and_emit_done`` so the snapshot persist
    cannot block ``persist_task_result`` → ``_dispatch_queued_message``.
    On success, emits a ``round_committed`` SSE event carrying
    ``snapshotId`` (and ``gitSha`` for backward-compat) plus any
    file-history-derived ``modifiedFileList`` additions.
    """
    if not (project_enabled and project_path and task.get('id')):
        return
    try:
        threading.Thread(
            target=_run_commit_round_async,
            args=(task, project_path),
            name=f'commit-round-{task["id"][:8]}',
            daemon=True,
        ).start()
    except Exception as e:
        logger.warning('[Task:%s] failed to spawn async commit thread: %s',
                       task['id'][:8], e, exc_info=True)


def _run_commit_round_async(task: dict, project_path: str) -> None:
    """Daemon-thread body for the deferred ``make_snapshot`` call.

    Uses the file-history store (lib.file_history) — the previous
    shadow-git shim was retired in the Tier-3 redesign.  See
    ``lib/file_history/__init__.py`` for the rationale.
    """
    tid = task['id'][:8]
    try:
        from lib import file_history as fh
        from lib.file_history.store import _project_lock as _fh_project_lock
        from lib.file_history.store import load_tracked as _fh_load_tracked
        from lib.project_mod import get_modifications

        if not fh.is_enabled():
            return

        # Pull actual tool names (mod['type']) from this task's modifications.
        _tool_names: list[str] = []
        _rel_paths: list[str] | None = None
        try:
            _turn_mods = [
                m for m in (get_modifications(project_path, conv_id=task.get('convId')) or [])
                if m.get('taskId') == task['id']
            ]
            _tool_names = [m.get('type') or '' for m in _turn_mods]
            _tool_names = [t for t in _tool_names if t]
            _rel_paths = [m.get('path') for m in _turn_mods if m.get('path')]
        except Exception as _e:
            logger.debug('[Task:%s] async tool_names/rel_paths extraction failed: %s',
                         tid, _e)

        # ── Atomic commit region (Fix 3) ───────────────────────────
        # The sequence
        #   prev_snap  = get_last_snapshot_id(...)
        #   _snap_id   = make_snapshot(...)
        #   fh_changes = diff_name_status(prev_snap, _snap_id)
        #   tracked    = load_tracked(...)              # for Fix 2
        # MUST run atomically against the per-project file-history
        # store.  Each individual call already takes the
        # ``_project_lock`` via ``@with_project_lock``, but releasing
        # it between calls lets a concurrent commit thread (from
        # another conversation pointing at the same project root)
        # advance the snapshot log and ``tracked.json`` between our
        # ``prev_snap`` capture and our ``make_snapshot``.  When that
        # happens, our snapshot's file map ends up containing the
        # OTHER task's edits too, and ``diff_name_status`` then
        # attributes those edits to OUR round.  Holding the
        # re-entrant lock across the whole sequence closes the window.
        # The store's per-call ``with_project_lock`` re-acquires the
        # same RLock, which is a no-op while we're holding it.
        fh_changes: list[dict] = []
        tracked_index: dict = {}
        with _fh_project_lock(project_path):
            # Find the snapshot that was active before this round
            # started, so diff_name_status can isolate just the round's
            # changes.
            prev_snap = fh.get_last_snapshot_id(project_path)

            _t0 = time.time()
            _snap_id = fh.make_snapshot(
                project_path,
                task_id=task['id'],
                conv_id=task.get('convId'),
                tool_names=_tool_names or None,
                summary=task.get('toolSummary'),
                rel_paths=_rel_paths or None,
            )
            _elapsed = time.time() - _t0
            if not _snap_id:
                logger.debug('[Task:%s] async make_snapshot returned no id (no-op or disabled) elapsed=%.2fs',
                             tid, _elapsed)
                return

            # Diff + tracked-index snapshot still inside the lock so
            # last_writer_task_id reflects the writers as of this
            # snapshot's instant.
            try:
                fh_changes = fh.diff_name_status(project_path, prev_snap, _snap_id) or []
            except Exception as _e:
                logger.debug('[Task:%s] async diff_name_status fallback: %s',
                             tid, _e)
                fh_changes = []
            try:
                tracked_index = _fh_load_tracked(project_path) or {}
            except Exception as _e:
                logger.debug('[Task:%s] async load_tracked fallback: %s', tid, _e)
                tracked_index = {}

        # Keep ``gitSha`` field for backward-compat with the frontend (which
        # captures it onto _gitSha for prospective undo UI but doesn't
        # currently consume it).  ``snapshotId`` is the new canonical name.
        task['snapshotId'] = _snap_id
        task['gitSha'] = _snap_id
        if _elapsed > 1.0:
            logger.info('[Task:%s] async make_snapshot completed in %.2fs id=%s',
                        tid, _elapsed, _snap_id[:8])

        amend_evt = build_event(EventType.ROUND_COMMITTED,
                                snapshotId=_snap_id,
                                gitSha=_snap_id,
                                taskId=task['id'])

        # File-history-derived additions (run_command / code_exec / MCP side
        # effects that modifications.py doesn't track) come from
        # diff_name_status against the prior snapshot.
        #
        # Fix 2 — per-task attribution: filter the diff to keep ONLY
        # paths whose latest tracked-index entry was last written by
        # THIS task.  Any path whose ``last_writer_task_id`` is some
        # other task belongs to a concurrent conversation operating
        # on the same project root and must not be reported here.
        # ── The fh diff is ENRICHMENT ONLY, never a source of truth. ──
        # The authoritative ``modifiedFileList`` was already built in
        # ``_finalize_and_emit_done`` from this round's OWN writes
        # (modifications journal, aggregated across all roots) — a
        # conversation-isolated signal.  The fh diff is computed against
        # the PRIMARY root's project-global snapshot index, so it
        # legitimately catches only one thing the journal can't: file
        # edits made by OPAQUE writers that don't stamp attribution —
        # ``code_exec`` and arbitrary MCP tools.  (``run_command`` IS
        # journalled by modifications.py, and the file-edit tools
        # write_file / apply_diff(s) / insert_content(s) journal AND
        # stamp ``last_writer_task_id`` on their own tracked entries.)
        #
        # So an fh diff path is only legitimately OURS when:
        #   • its tracked entry's ``last_writer_task_id`` == this task, OR
        #   • the entry is UNATTRIBUTED (empty writer) AND this round ran
        #     an OPAQUE writer that could have produced an unstamped edit.
        # Any other empty-writer path is concurrent-conversation drift on
        # the shared primary root (e.g. another session journalling) and
        # MUST be dropped — that was the cross-conversation leak that let
        # a foreign file appear while this round's real (extra-root) edits
        # were missing.
        #
        # ``_TRACKED_EDIT_TOOLS`` and the read-only set both stamp/leave
        # NO unattributed edits, so a round running only those cannot own
        # an empty-writer path.  Probe by ACTUAL tool name; unknown names
        # (custom MCP tools) count as opaque writers — fail open so a
        # genuine side-channel edit is never suppressed.
        _READ_ONLY_TOOLS = frozenset({
            'list_dir', 'read_files', 'grep_search', 'find_files',
            'web_search', 'fetch_url', 'inspect_image',
        })
        _TRACKED_EDIT_TOOLS = frozenset({
            'write_file', 'apply_diff', 'apply_diffs',
            'insert_content', 'insert_contents', 'run_command',
        })
        _round_has_opaque_writer = False
        try:
            for _r in (task.get('toolRounds') or []):
                if not isinstance(_r, dict):
                    continue
                _tn = _r.get('toolName') or _r.get('tool_name') or ''
                if not _tn:
                    continue
                if _tn in _READ_ONLY_TOOLS or _tn in _TRACKED_EDIT_TOOLS:
                    continue
                # Anything else (code_exec / MCP / unknown) may write
                # without stamping attribution.
                _round_has_opaque_writer = True
                break
        except Exception as _e:
            logger.debug('[Task:%s] fh opaque-writer probe failed: %s', tid, _e)
            _round_has_opaque_writer = True  # fail open — never over-suppress

        try:
            if fh_changes:
                _own_task_id = task.get('id') or ''
                _filtered: list[dict] = []
                _dropped = 0
                _dropped_drift = 0
                for entry in fh_changes:
                    _writer = (tracked_index.get(entry.get('path'), {})
                               .get('last_writer_task_id') or '')
                    if _writer and _writer != _own_task_id:
                        # Attributed to another concurrent task — always drop.
                        _dropped += 1
                    elif not _writer and not _round_has_opaque_writer:
                        # Unattributed path on a round that ran no opaque
                        # writer — it cannot be ours.  Drop (closes the
                        # concurrent-conversation leak).
                        _dropped_drift += 1
                    else:
                        _filtered.append(entry)
                if _dropped:
                    logger.info('[Task:%s] fh side-channel dropped %d path(s) '
                                'attributable to other concurrent task(s)',
                                tid, _dropped)
                if _dropped_drift:
                    logger.info('[Task:%s] fh side-channel dropped %d unattributed '
                                'path(s) on a round with no opaque writer', tid, _dropped_drift)
                fh_changes = _filtered
        except Exception as _e:
            logger.debug('[Task:%s] fh attribution filter failed: %s', tid, _e)

        # Dedup must use the same root-tagging convention that
        # ``modifications.py`` uses when it records a write.  That code
        # reverse-looks-up ``base_path`` in the global ``_roots`` registry
        # and stores the matching root NAME on each mod.  When the merger
        # in ``_emit_done_event`` later builds ``modifiedFileList`` it
        # carries that ``root`` field through.  If we naively dedup the
        # fh side-channel by ``('', path)`` here, every file that
        # modifications.py already recorded with a non-empty ``root``
        # would be re-added by us — producing duplicate rows in the
        # frontend's "files changed" bar (one entry with the root prefix,
        # one without).  Resolve the project root's NAME first and use
        # it as the dedup key so we collapse against the existing entry.
        try:
            if fh_changes:
                fh_root = ''
                try:
                    from lib.project_mod.config import _lock as _proj_lock
                    from lib.project_mod.config import _roots as _proj_roots
                    _abs_proj = os.path.abspath(project_path)
                    with _proj_lock:
                        for _rn, _rs in _proj_roots.items():
                            if os.path.abspath(_rs.get('path') or '') == _abs_proj:
                                fh_root = _rn
                                break
                except Exception as _re:
                    logger.debug('[Task:%s] fh_root lookup failed for %s: %s',
                                 tid, project_path, _re)

                existing = list(task.get('modifiedFileList') or [])
                seen_paths: set[tuple[str, str]] = set()
                for f in existing:
                    if not isinstance(f, dict):
                        continue
                    p = f.get('path', '')
                    r = f.get('root', '') or ''
                    seen_paths.add((r, p))
                    # Also record an unrooted alias so a fh entry that
                    # does not (yet) know the root name still dedups
                    # against an existing rooted entry for the same file.
                    seen_paths.add(('', p))
                added: list[dict] = []
                for entry in fh_changes:
                    p = entry['path']
                    if (fh_root, p) in seen_paths or ('', p) in seen_paths:
                        continue
                    item = {'path': p, 'action': entry['action']}
                    if fh_root:
                        item['root'] = fh_root
                    existing.append(item)
                    added.append(item)
                    seen_paths.add((fh_root, p))
                    seen_paths.add(('', p))
                if added:
                    task['modifiedFileList'] = existing
                    task['modifiedFiles'] = len(existing)
                    amend_evt['modifiedFileList'] = existing
                    amend_evt['modifiedFiles'] = len(existing)
                    amend_evt['addedByGit'] = added
                    logger.info('[Task:%s] async file-history modifiedFileList '
                                'added %d file(s) missed by modifications.py '
                                '(root=%s)', tid, len(added), fh_root or '-')
        except Exception as _e:
            logger.debug('[Task:%s] async diff_name_status fallback: %s',
                         tid, _e)

        # Emit the amend event so any still-connected SSE reader can wire
        # snapshotId onto the assistant message.
        try:
            append_event(task, amend_evt)
        except Exception as _e:
            logger.debug('[Task:%s] append_event for round_committed failed: %s',
                         tid, _e)

        # ── Persist snapshotId to the conversation DB so reloads after
        #    the SSE reader has closed still see it for the redo UI. ──
        try:
            _patch_assistant_message_with_git(task, amend_evt)
        except Exception as _e:
            logger.warning('[Task:%s] failed to patch assistant message with snapshotId: %s',
                           tid, _e, exc_info=True)
    except Exception as e:
        logger.warning('[Task:%s] async make_snapshot failed: %s',
                       tid, e, exc_info=True)


def _patch_assistant_message_with_git(task: dict, amend_evt: dict) -> None:
    """Update the conversation's last assistant message with gitSha + git-derived files.

    Called from the async commit thread after ``persist_task_result`` has
    already run.  Mirrors the subset of ``_sync_result_to_conversation``
    that depends on git output.
    """
    conv_id = task.get('convId') or ''
    task_id = task.get('id') or ''
    git_sha = amend_evt.get('gitSha')
    if not (conv_id and task_id and git_sha):
        return
    from lib.agent_core.store import get_conversation_store
    store = get_conversation_store()
    loaded = store.load_conversation_messages(conv_id)
    if loaded is None:
        return
    messages, _updated_at = loaded
    if not isinstance(messages, list) or not messages:
        return

    # Locate the assistant message for this task.  Prefer matching by
    # _taskId (set by _sync_result_to_conversation); fall back to the
    # last assistant message if not tagged.
    target_idx = -1
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        if not isinstance(m, dict):
            continue
        if m.get('role') != 'assistant':
            continue
        if m.get('_taskId') == task_id:
            target_idx = i
            break
        if target_idx == -1:
            target_idx = i  # remember last assistant as fallback
            # Don't break — keep looking for an exact taskId match.
    if target_idx < 0:
        return
    msg = messages[target_idx]
    msg['_gitSha'] = git_sha
    msg['_snapshotId'] = amend_evt.get('snapshotId') or git_sha
    if amend_evt.get('modifiedFileList'):
        msg['modifiedFileList'] = amend_evt['modifiedFileList']
    if amend_evt.get('modifiedFiles'):
        msg['modifiedFiles'] = amend_evt['modifiedFiles']

    try:
        store.save_conversation_messages(conv_id, messages)
        logger.info('[Task:%s] persisted gitSha=%s to conv=%s msg[%d]',
                    task_id[:8], git_sha[:12], conv_id[:8], target_idx)
    except Exception as _e:
        logger.warning('[Task:%s] gitSha DB write failed: %s',
                       task_id[:8], _e, exc_info=True)


# ══════════════════════════════════════════════════════════
#  run_task — main orchestration loop
# ══════════════════════════════════════════════════════════
def run_task(task: dict[str, Any]) -> None:
    """Main orchestration loop: streams LLM response and dispatches tool calls.

    Parameters
    ----------
    task : dict[str, Any]
        Live task dict created by ``create_task()``.  Mutated in place
        throughout the run (content, usage, status, events, etc.).
    """
    if 'id' not in task:
        raise ValueError("run_task called with a task dict missing 'id' — did you forget to use create_task()?")
    tid = task['id'][:8]
    # Seed the thread-local request-id so audit_log / log_exception / log_context
    # (which auto-stamp req_id) correlate to THIS task. run_task executes on a
    # pooled background thread where req_id() would otherwise be empty, leaving
    # every audit line and swallowed-exception trace un-attributable.
    set_req_id(tid)
    # ★ Autopilot kick-from-idle: a carrier task that runs ONLY the virtual-user
    #   hook (no worker LLM turn).  The conversation already ended and the last
    #   message is the agent's reply, so the simulated user answers it directly.
    #   See lib.tasks_pkg.autopilot._run_autopilot_kick.
    if task.get('_autopilot_kick'):
        from lib.tasks_pkg.autopilot import _run_autopilot_kick
        _run_autopilot_kick(task)
        return
    # ★ Timing: thread picked the task up. Compare against '_t_created'
    #   (set in create_task) to measure how long the user "waited" before the
    #   background worker even started — i.e. thread-pool / queue latency.
    _t_run_start = time.time()
    _t_created = task.get('_t_created')
    if _t_created:
        logger.info('[Timing:%s] queue_wait=%.3fs (create→run_task)',
                    tid, _t_run_start - _t_created)
    # ★ Task START bracket — logged with the FULL task id (not the 8-char
    #   prefix) so a user can copy the id from the cost popover and grep the
    #   whole turn's lifecycle. Pairs with the '[Task:%s] ■ DONE' summary at
    #   completion. Every per-round line in between is tagged [<tid8>] via the
    #   thread-local req_id set just above.
    logger.info('[Task:%s] ▶ START conv=%s msgs=%d',
                task['id'], task.get('convId', '') or '-',
                len(task.get('messages') or []))
    try:
        cfg = task['config']

        # ── Reset swarm auto-continue chain on HUMAN turns ──
        # A human-initiated turn (NOT itself a swarm auto-continuation) means
        # the user is back in the loop, so the consecutive-auto-continue
        # ceiling should start fresh. Auto-continue turns carry
        # ``_swarmAutoContinue`` and must NOT reset the counter (that's what
        # bounds a runaway unattended loop). See lib/swarm/integration.py.
        if not cfg.get('_swarmAutoContinue'):
            try:
                from lib.swarm.integration import (reset_autocontinue_chain,
                                                    swarm_key_for)
                reset_autocontinue_chain(swarm_key_for(task))
            except Exception as _e:
                logger.debug('[Task %s] autocontinue chain reset failed: %s', tid, _e)

        # ── Capability profile: merge named profile defaults UNDER the
        #    explicit cfg (explicit caller values always win).  No-op when
        #    cfg has no 'profile' key or selects the empty 'default'.  Applied
        #    here — before model resolution + tool assembly — so every
        #    downstream consumer sees the merged values.
        from lib.agent_core.profiles import apply_profile, resolve_profile_name
        _profile_name = resolve_profile_name(cfg)
        if _profile_name != 'default':
            cfg = apply_profile(cfg)
            task['config'] = cfg

        # ── Per-client browser routing: set thread-local client ID so all
        #    browser commands (tools, fetch fallback, search fallback) from
        #    this task thread route to the correct device's extension. ──
        _browser_client_id = cfg.get('browserClientId')
        if _browser_client_id:
            from lib.browser import _set_active_client
            _set_active_client(_browser_client_id)
            logger.debug('[Task %s] Browser client routed to %s', tid, _browser_client_id[:12])

        # ── Hard provider pin (multi-tenant isolation) ──
        # When this task was created from an inline `provider` block or a
        # registered @prov_xxx BYO endpoint, bind THIS worker thread to that
        # provider so every LLM dispatch on it (main solve, L2/advanced
        # compaction summaries, endpoint replan turns) can only pick that
        # provider's slot — never silently falling back to an operator key
        # and eating a 429. Cleared in the finally block because worker
        # threads are pooled and reused. See lib/llm_dispatch/provider_pin.py.
        from lib.llm_dispatch.provider_pin import set_pinned_provider
        _pinned_provider_id = task.get('_pinned_provider_id') or ''
        if _pinned_provider_id:
            set_pinned_provider(_pinned_provider_id)
            logger.info('[Task %s] Provider-pinned to %s (hard isolation)',
                        tid, _pinned_provider_id)

        # ── Conversation-sticky routing ──
        # Bind this worker thread to the conversation so every LLM dispatch on
        # it prefers the API key that last served this conv — keeping the
        # Anthropic per-key prompt cache warm across rounds. Soft preference:
        # the picker still falls back to a healthy key if the sticky one is
        # cooled down. Cleared in the finally block (pooled threads).
        # See lib/llm_dispatch/conv_affinity.py.
        from lib.llm_dispatch.conv_affinity import set_conv_affinity
        set_conv_affinity(task.get('convId') or '')

        # ── Section 1: Config & Model Resolution ──
        mcfg = _resolve_model_config(cfg, task['id'])
        model           = mcfg['model']
        thinking_enabled = mcfg['thinking_enabled']
        thinking_depth  = mcfg['thinking_depth']
        preset          = mcfg['preset']
        max_tokens      = mcfg['max_tokens']
        temperature     = mcfg['temperature']
        search_mode     = mcfg['search_mode']
        response_format = mcfg.get('response_format')
        search_enabled  = mcfg['search_enabled']
        fetch_enabled   = mcfg['fetch_enabled']
        project_path    = mcfg['project_path']
        project_enabled = mcfg['project_enabled']
        if project_enabled and project_path:
            # ★ Extract extra root paths from projectPaths (frontend sends all roots).
            #   projectPaths[0] = primary (same as projectPath), rest are extras.
            _all_paths = cfg.get('projectPaths') or []
            _extra_paths = [p for p in _all_paths[1:] if p and p != project_path] if len(_all_paths) > 1 else []
            # ★ Read-only roots: a subset of the configured paths the user
            #   attached for reference only. Writes/edits/create_project and
            #   destructive run_command targeting these are refused; reads are
            #   always allowed. Empty list = today's all-writable behaviour.
            _readonly_paths = [p for p in (cfg.get('readOnlyPaths') or []) if p]
            logger.info('[Task:%s] project_path=%s extra_roots=%d readonly=%d',
                        task['id'], project_path, len(_extra_paths),
                        len(_readonly_paths))
            # ★ Ensure the server's global project state matches this task's
            # project path + extras.  Another conversation may have switched the
            # server to a different project, causing get_context_for_prompt to miss
            # the file tree (path mismatch → no tree in system prompt → LLM
            # doesn't know the project structure → "backend cannot use tools").
            from lib.project_mod import ensure_project_state
            # ★ Pass conv_id for per-conversation root isolation (2026-05-05).
            #   Prevents concurrent tasks from clobbering each other's
            #   workspace-root namespace when they call set_project with
            #   different primary paths. See lib/project_mod/config.py
            #   ::set_conv_roots docstring for background.
            _conv_id_for_roots = task.get('convId') or task.get('id') or ''
            ensure_project_state(project_path, extra_paths=_extra_paths,
                                 conv_id=_conv_id_for_roots,
                                 readonly_paths=_readonly_paths)
            # ── File-history: capture any external (IDE) edits made between rounds.
            #
            #   Runs SILENTLY in a background thread: no phase event, no UI
            #   status — the LLM response starts streaming immediately.  Cost
            #   is bounded by the size of the tracked-files set (files the
            #   assistant has touched this session), not the worktree, so
            #   this is cheap even on slow filesystems.
            #
            #   Correctness guard: if the round has already started mutating
            #   files by the time the probe finishes, we skip the synthetic
            #   external-edit snapshot to avoid misattribution.  The next
            #   round's probe catches the drift cleanly on top of a stable
            #   timeline.
            try:
                from lib import file_history as fh

                if fh.is_enabled() and fh.probe_enabled():
                    def _probe_external_edits():
                        try:
                            if task.get('modifiedFileList') or task.get('modifiedFiles'):
                                logger.debug('[Task:%s] skipping external-edit probe '
                                             '— round already mutated files',
                                             task['id'][:8])
                                return
                            _ext = fh.detect_external_edits(project_path)
                            if (task.get('modifiedFileList')
                                    or task.get('modifiedFiles')):
                                logger.debug('[Task:%s] external-edit probe '
                                             'completed after round started '
                                             'mutating files — not emitting '
                                             'SSE event (attribution ambiguous)',
                                             task['id'][:8])
                                return
                            if _ext.get('committed'):
                                append_event(task, build_event(
                                    EventType.PROJECT_EXTERNAL_EDIT,
                                    files=_ext.get('files', []),
                                    sha=_ext.get('snapshotId'),
                                ))
                                logger.info('[Task:%s] captured %d external edit(s) snap=%s',
                                            task['id'][:8], len(_ext.get('files', [])),
                                            (_ext.get('snapshotId') or '')[:8])
                        except Exception as e:
                            logger.warning('[Task:%s] external-edit detection failed: %s',
                                           task['id'][:8], e)

                    threading.Thread(
                        target=_probe_external_edits,
                        name=f'ext-edit-probe-{task["id"][:8]}',
                        daemon=True,
                    ).start()
            except Exception as e:
                logger.warning('[Task:%s] could not start external-edit probe: %s',
                               task['id'][:8], e)
        code_exec_enabled = mcfg['code_exec_enabled']
        memory_enabled  = mcfg['memory_enabled']
        browser_enabled = mcfg['browser_enabled']
        desktop_enabled = mcfg['desktop_enabled']
        swarm_enabled   = mcfg['swarm_enabled']
        image_gen_enabled = mcfg['image_gen_enabled']
        human_guidance_enabled = mcfg.get('human_guidance_enabled', False)
        scheduler_enabled = mcfg.get('scheduler_enabled', False)
        # ── Memory Prefetch: start loading project and memory contexts in
        #    background threads while tool assembly runs (FUSE I/O can be slow).
        #    Inspired by Claude Code's startRelevantMemoryPrefetch().
        from concurrent.futures import ThreadPoolExecutor as _PrefetchPool
        _prefetch_executor = _PrefetchPool(max_workers=2,
                                           thread_name_prefix='mem-prefetch')
        _prefetch_project_future = None
        _prefetch_memory_future = None

        if project_enabled and project_path:
            _prefetch_conv_id = task.get('convId') or task.get('id') or ''
            def _prefetch_project():
                from lib.project_mod import get_context_for_prompt
                return get_context_for_prompt(project_path,
                                              conv_id=_prefetch_conv_id or None)
            _prefetch_project_future = _prefetch_executor.submit(_prefetch_project)

        # Simple heuristic: if any tool-providing feature is enabled, we'll
        # have real tools → need memory injection + accumulation instructions.
        _has_real_tools_hint = (search_enabled or fetch_enabled or
                                project_enabled or browser_enabled or
                                desktop_enabled or swarm_enabled or
                                code_exec_enabled or image_gen_enabled)
        _pp = project_path if project_enabled else None
        # ★ Extra workspace roots for memory scoping (multi-root session).
        #   Memories are READ (listed / searched / prefetched) across the
        #   primary + every extra root, unioned and de-duplicated; NEW
        #   memories are still written only to the primary project_path.
        #   Mirrors the projectPaths[1:] extraction used for file tools.
        _mem_extra_paths = []
        if project_enabled and _pp:
            _all_mem_paths = cfg.get('projectPaths') or []
            _mem_extra_paths = [p for p in _all_mem_paths[1:]
                                if p and p != _pp] if len(_all_mem_paths) > 1 else []
        # Memory toggle gates EVERYTHING memory-related: the count-hint
        # background load, the per-turn prefetch (BM25 + cheap-LLM rerank),
        # and the accumulation instructions injected into the system prompt.
        # AI still accumulates memories in the background via the
        # search_memories / create_memory tools — only the proactive
        # injection path is muted.
        if memory_enabled:
            def _prefetch_memory():
                from lib.memory import build_memory_context
                return build_memory_context(project_path=_pp,
                                            extra_paths=_mem_extra_paths)
            _prefetch_memory_future = _prefetch_executor.submit(_prefetch_memory)

        # Store prefetch futures on the task for _inject_system_contexts to use
        task['_prefetch_project'] = _prefetch_project_future
        task['_prefetch_memory'] = _prefetch_memory_future

        # ── Section 2: Tool Assembly ──
        tool_list, has_real_tools, max_tool_rounds = _assemble_tool_list(
            cfg, project_path, project_enabled, task['id'],
            search_mode, search_enabled, fetch_enabled,
            code_exec_enabled, browser_enabled, desktop_enabled,
            swarm_enabled,
            image_gen_enabled=image_gen_enabled,
            human_guidance_enabled=human_guidance_enabled,
            scheduler_enabled=scheduler_enabled,
            messages=task['messages'],
            conv_id=task.get('convId', ''),
        )

        # Stash the assembled tool schema on the task so the compaction
        # token-gate can account for its cost. The tool-schema JSON ships
        # in every request and the gateway tokenizes all of it, but the
        # proactive gate (_count_tokens_authoritative) only saw `messages`
        # — under-counting by the full tool-schema size. Stashing here
        # (rather than threading through run_compaction_pipeline →
        # force_compact_if_needed → _should_force_compact) keeps the
        # pipeline signatures untouched.
        task['_tool_schema'] = tool_list

        # (Planner no-tools override removed — all endpoint roles now
        #  get full tool access.  See endpoint_review._run_planner_turn.)

        messages = list(task['messages'])
        original_messages = list(messages)
        tool_round_num = 0
        all_search_results_text = []

        # ── Section 2.5: Server-side tool history restoration ──
        # If keepToolHistory is enabled AND we have stored full messages
        # from a previous turn, replace the frontend's summary-only messages
        # with the full tool_use/tool_result history.
        _keep_tool_history = cfg.get('keepToolHistory', True)
        _conv_id = task.get('convId', '')
        if _keep_tool_history and _conv_id:
            rebuilt, _rebuild_stats = _rebuild_messages_with_history(_conv_id, messages)
            if _rebuild_stats['used_store']:
                # Log the overhead for monitoring
                _oh = _estimate_token_overhead(messages, rebuilt)
                logger.info(
                    '[%s] conv=%s ★ TOOL HISTORY RESTORED: '
                    'frontend=%d msgs → rebuilt=%d msgs '
                    '(tool_msgs=%d, overhead=+%d est_tokens, ratio=%.1fx)',
                    tid, _conv_id[:8],
                    _rebuild_stats['frontend_msg_count'], len(rebuilt),
                    _rebuild_stats['tool_msgs_restored'],
                    _oh['overhead_est_tokens'], _oh['ratio'],
                )
                messages = rebuilt
                original_messages = list(messages)
                # Emit a diagnostic event for the debug panel
                append_event(task, build_event(
                    EventType.PHASE,
                    phase='tool_history_restored',
                    detail=f'Restored {_rebuild_stats["tool_msgs_restored"]} tool messages from server store',
                    stats=_rebuild_stats,
                    overhead=_oh,
                ))
            else:
                logger.debug('[%s] conv=%s keepToolHistory enabled but no stored messages found',
                             tid, _conv_id[:8])

        # ── Section 3: Context Injection ──
        _tool_names = {
            (t.get('function') or {}).get('name')
            for t in (tool_list or [])
            if isinstance(t, dict)
        }
        _tool_names.discard(None)
        _inject_system_contexts(
            messages, project_path, project_enabled,
            memory_enabled, search_enabled, swarm_enabled,
            has_real_tools,
            conv_id=task.get('convId', ''),
            task=task,
            model=model,
            system_prompt_mode=cfg.get('systemPromptMode', 'append'),
            tool_names=_tool_names or None,
            disabled_blocks=_disabled_prompt_blocks(cfg),
        )
        # Cleanup prefetch futures (no longer needed)
        task.pop('_prefetch_project', None)
        task.pop('_prefetch_memory', None)
        _prefetch_executor.shutdown(wait=False)

        # ★ Timing: context assembly complete (config/model resolution, tool
        #   assembly, tool-history restoration, system-context injection — incl.
        #   the FUSE-slow memory/project prefetch). This is the bulk of the
        #   pre-LLM "waiting" window. Stash the anchor on the task so
        #   stream_llm_response can compute time-to-first-token (TTFT).
        _t_prep_done = time.time()
        task['_t_prep_done'] = _t_prep_done
        logger.info('[Timing:%s] prep=%.3fs (run_task→context-ready, '
                    'model=%s) — about to build first LLM request',
                    tid, _t_prep_done - _t_run_start, model)

        # NOTE: Auto-prefetch disabled — the model can fetch URLs on demand
        # via the fetch_url tool call when it deems them relevant, rather than
        # being forced to fetch every URL detected in the user message.
        # if fetch_enabled:
        #     prefetched = _prefetch_user_urls(messages, task)
        #     if prefetched:
        #         tool_round_num = inject_prefetched_urls(messages, prefetched, task)


        logger.debug('[Task %s] conv=%s Start model=%s think=%s search=%s fetch=%s project=%s code_exec=%s',
                    task['id'][:8], task.get('convId', ''), model, thinking_enabled, search_mode, fetch_enabled,
                    'yes' if project_enabled else 'no', 'yes' if code_exec_enabled else 'no')
        tool_call_happened = False
        last_finish_reason = None
        last_usage = None
        assistant_msg = None  # ★ Initialize before loop — prevents UnboundLocalError if loop breaks early
        accumulated_usage = {}  # ★ Accumulate usage across all tool rounds
        api_rounds = []  # ★ Track per-round usage for cost breakdown

        # ★ Inject toolHistory from continue — restore interrupted tool call context
        _injected_tool_calls = inject_tool_history(messages, cfg, task, model)
        if _injected_tool_calls:
            tool_call_happened = True
            tool_round_num = _injected_tool_calls  # offset so new roundNums don't conflict

        # ★ Memory Prefetch (proactive, per-user-turn, round 0 only):
        #   BM25 coarse → cheap-LLM precision → inject <relevant_memories>.
        #   This surfaces past lessons even when the model wouldn't have
        #   thought to call search_memories on its own. Emits SSE
        #   `memory_prefetch` events so the frontend can show an indicator.
        #   Skipped if:
        #     • Memory toggle disabled (memory_enabled=false)
        #     • feature flag disabled
        #     • continue/resume (tool_history was replayed → not a fresh turn)
        #     • no real tools (memory tools unavailable anyway)
        if memory_enabled and has_real_tools and not _injected_tool_calls:
            try:
                from lib.memory.prefetch import run_memory_prefetch
                # Active-tools list lets the cheap-LLM filter drop memories
                # about subsystems the user can't currently use (e.g.
                # browser memories when browser is off).
                _active_tools = []
                for _t in (tool_list or []):
                    try:
                        _active_tools.append(_t['function']['name'])
                    except (KeyError, TypeError) as _e_audit:
                        logger.debug('[orchestrator] run_task caught %s: %s', type(_e_audit).__name__, _e_audit)
                        continue
                run_memory_prefetch(
                    messages,
                    project_path=project_path if project_enabled else None,
                    task=task,
                    emit_event=lambda ev: append_event(task, ev),
                    active_tools=_active_tools,
                    extra_paths=_mem_extra_paths,
                )
            except Exception as _e:
                # Advisory path — never block the task on prefetch failure.
                logger.warning('[Task %s] memory prefetch failed: %s',
                               task['id'][:8], _e, exc_info=True)

        # ★ Apply preserved content prefix from Continue — ensures backend checkpoints
        #   include text the LLM generated alongside completed tool rounds in the prior
        #   task, so page-refresh mid-stream doesn't lose that content.
        #
        #   ⚠ IMPORTANT: contentPrefix is NEVER re-injected into `messages` as a
        #   trailing assistant turn.  That would only work against OpenAI-compat
        #   endpoints — Anthropic Messages API rejects a trailing assistant turn
        #   ("This model does not support assistant message prefill. The
        #   conversation must end with a user message.").  Rather than branching
        #   by provider we keep the universal behaviour: use contentPrefix only
        #   as a bookkeeping seed for `task['content']` so the resumed response
        #   displays [preserved text] + [freshly generated continuation].  The
        #   freshly generated part begins from the tool-result checkpoint, which
        #   is replayed via `inject_tool_history` above — that shape every
        #   provider accepts.
        _content_prefix = cfg.get('contentPrefix') or ''
        if _content_prefix:
            with task['content_lock']:
                task['content'] = _content_prefix
            logger.debug('[%s] conv=%s Applied contentPrefix (%d chars) from continue checkpoint',
                         tid, task.get('convId', ''), len(_content_prefix))

        # ★ Stash checkpoint metadata for merging into done event and DB persistence.
        #   NOTE: we do NOT pre-populate task['toolRounds'] with checkpoint rounds
        #   because the frontend's state/delta handlers would double-count them
        #   (frontend does _continueToolRounds.concat(ev.toolRounds)).  Instead,
        #   checkpoint rounds are merged only when writing to DB and in the done event.
        _checkpoint_tr = cfg.get('checkpointToolRounds') or []
        if _checkpoint_tr:
            task['_checkpointToolRounds'] = list(_checkpoint_tr)
            logger.debug('[%s] conv=%s Stashed %d checkpoint toolRounds for DB merge',
                         tid, task.get('convId', ''), len(_checkpoint_tr))
        if cfg.get('checkpointUsage'):
            task['_checkpointUsage'] = cfg['checkpointUsage']
        if cfg.get('checkpointApiRounds'):
            task['_checkpointApiRounds'] = cfg['checkpointApiRounds']
        if cfg.get('checkpointModifiedFiles'):
            task['_checkpointModifiedFiles'] = cfg['checkpointModifiedFiles']
        if cfg.get('checkpointModifiedFileList'):
            task['_checkpointModifiedFileList'] = cfg['checkpointModifiedFileList']

        # ★ 禁止添加 anti-loop / 预算警告 / _force_stop 等机制。
        #   不允许在运行时向 messages 注入任何 [SYSTEM NOTE] 或 [SYSTEM:] 消息来
        #   干扰模型的正常生成。详见 max_tool_rounds 注释。

        _loop_exit_reason = 'max_rounds_exhausted'  # ★ DIAGNOSTIC: track why the loop ended
        _abort_detected_phase = None  # ★ Track exactly WHEN abort was detected
        _premature_retry_count = 0    # ★ Track retries for PREMATURE STREAM CLOSE
        _PREMATURE_RETRY_MAX = 2      # ★ Max premature-close retries (must match stream_handler)
        _consecutive_tool_timeouts = 0  # ★ Track consecutive tool-execution timeouts to prevent runaway loops
        _MAX_CONSECUTIVE_TOOL_TIMEOUTS = 3  # ★ Force-stop after this many consecutive tool timeouts
        _last_checkpoint = 0.0  # ★ Throttle crash-recovery checkpoints (epoch seconds)
        round_num = -1
        # ★ WHILE-loop instead of FOR — the ceiling expands when premature-close
        #   retries are used, so even max_tool_rounds=0 (no tools) gets retry
        #   iterations.  Without this, `continue` in a single-iteration for-loop
        #   exits immediately and the retry never actually fires.
        #   Ceiling: max_tool_rounds + 1 (base) + _premature_retry_count (bonus).
        #   Original for-loop was: range(max_tool_rounds + 1) = [0..max_tool_rounds].
        while round_num + 1 <= max_tool_rounds + _premature_retry_count:
            round_num += 1
            if task['aborted']:
                _abort_detected_phase = f'loop_start_round_{round_num}'
                _loop_exit_reason = f'aborted_at_round_{round_num}'
                _abort_ts = task.get('_abort_timestamp', 0)
                _now = time.time()
                _delay = f'{_now - _abort_ts:.1f}s ago' if _abort_ts else 'unknown'
                logger.debug('[%s] Task aborted at START of round %d model=%s '
                             '(abort signal arrived %s, content so far: %dchars)',
                             tid, round_num, model, _delay, len(task.get('content') or ''))
                break

            # ★ Emit phase event so the frontend knows what's happening
            _emit_tool_round_phase(task, assistant_msg if round_num > 0 else {}, round_num)

            # ★ Context compaction: two-layer pipeline
            #   L1: micro-compact cold tool results (every round, zero LLM cost)
            #   L2: smart summary as synthetic tool result (on context overflow)
            run_compaction_pipeline(messages, round_num, task=task)

            # ★ Per-turn attachments: dynamic context injection
            #   Inspired by Claude Code's getAttachments() — injects session
            #   memory, file reminders, tool discovery deltas each turn.
            #   Wrapped defensively: attachment building is advisory and must
            #   never crash an otherwise-healthy task. Any bug here (e.g. a
            #   malformed tool_call arg from the model) degrades to "no
            #   attachments this round" rather than aborting the task.
            if round_num > 0:  # skip round 0 (system contexts just injected)
                try:
                    _attachments = compute_turn_attachments(
                        messages, task, round_num,
                        conv_id=task.get('convId', ''),
                        project_path=project_path,
                        project_enabled=project_enabled,
                    )
                    if _attachments:
                        inject_attachments(messages, _attachments,
                                            conv_id=task.get('convId') or None)
                except Exception as e:
                    logger.error('[Task:%s] compute_turn_attachments failed '
                                 'round=%d: %s — continuing without attachments',
                                 tid, round_num, e, exc_info=True)

            # ★ Legacy cleanup: strip old "Current date and time:" from user
            #   messages.  Date is now injected in the system prompt (step 4.5)
            #   as date-only format.  This just ensures conversations with
            #   old-format timestamps get cleaned up for proper cache prefix.
            inject_search_addendum_to_user(messages, search_enabled,
                                           round_num=round_num)

            # ★ Drain swarm inbox — async sub-agent completions (and any other
            #   model-facing notifications) get injected as `user`-role
            #   `_isMeta` messages right before the LLM call. Drained AFTER
            #   attachments / search-addendum so it sits at the end of the
            #   message list (just before the model takes its next turn).
            #   Safe injection rule: if the previous turn ended with an
            #   assistant tool_call awaiting tool_result, postpone — the
            #   pair must close before another role can speak.
            try:
                _last_msg = messages[-1] if messages else None
                _has_unmatched_tool_call = (
                    bool(_last_msg)
                    and _last_msg.get('role') == 'assistant'
                    and _last_msg.get('tool_calls')
                )
                if not _has_unmatched_tool_call:
                    from lib.agent_inbox import drain as _drain_inbox
                    from lib.swarm.integration import swarm_key_for as _swarm_key_for
                    # NOTE: drain with the conversation-scoped SWARM KEY — the
                    # inbox is keyed by ``swarm_key_for(task)`` (conv id when
                    # present, else task id) so <swarm-update>s enqueued by a
                    # PRIOR turn's background agents are still drained on a
                    # later "continue" turn of the same conversation. (Before
                    # Option A this used ``task['id']`` and cross-turn updates
                    # were stranded.) ``tid`` is just the 8-char log prefix.
                    _inbox_items = _drain_inbox(_swarm_key_for(task))
                    if _inbox_items:
                        # Coalesce ALL drained items into a single user
                        # message — one message with N <swarm-update>
                        # blocks instead of N adjacent user messages.
                        # Reasons:
                        #   1. Cuts message count → cleaner cache prefix.
                        #   2. <swarm-update> is treated as factual data
                        #      (not a system reminder), so this is a real
                        #      user-role message — no _isMeta flag, no
                        #      <system-reminder> wrapper.  Mirrors Claude
                        #      Code's <task-notification> approach.
                        _payloads = [it.get('value', '') for it in _inbox_items
                                     if it.get('value')]
                        if _payloads:
                            messages.append({
                                'role':    'user',
                                'content': '\n\n'.join(_payloads),
                            })
                            # Persist the delivered flag so a restart mid-turn
                            # doesn't re-inject these <swarm-update>s on resume.
                            try:
                                from lib.swarm import persistence as _swarm_persist
                                _swarm_persist.mark_delivered(
                                    _swarm_key_for(task),
                                    [it.get('agent_id', '') for it in _inbox_items
                                     if it.get('agent_id')])
                            except Exception as _mde:
                                logger.debug('[Task %s] swarm mark_delivered failed: %s',
                                             tid, _mde)
                            logger.info(
                                '[Task %s] injected %d swarm-update item(s) '
                                'as 1 user message at round %d',
                                tid, len(_payloads), round_num + 1)
                            append_event(task, build_event(
                                EventType.SWARM_INBOX_INJECT,
                                round=round_num + 1,
                                count=len(_payloads),
                                agentIds=[it.get('agent_id', '')
                                          for it in _inbox_items
                                          if it.get('value')],
                                # ★ Carry the actual <swarm-update> payloads
                                #   (truncated) so the frontend can render an
                                #   in-timeline ptool-panel row showing exactly
                                #   what the model received — not just a count.
                                previews=[{
                                    'agentId': it.get('agent_id', ''),
                                    'text': (it.get('value') or '')[:1200],
                                } for it in _inbox_items if it.get('value')],
                            ))
            except Exception as _e:
                logger.error(
                    '[Task %s] swarm inbox drain/inject failed at round %d: %s '
                    '— continuing without notifications',
                    tid, round_num + 1, _e, exc_info=True)

            _tools_this_round = tool_list if (tool_list and round_num < max_tool_rounds) else None

            # ★ Emit messages snapshot for debug panel (before LLM call)
            try:
                snapshot = _strip_base64_for_snapshot(messages)
                snap_evt = build_event(
                    EventType.MESSAGES_SNAPSHOT,
                    round=round_num + 1,
                    label=f'Round {round_num + 1} 请求前 · {len(messages)}条',
                    messages=snapshot,
                )
                if _tools_this_round:
                    snap_evt['tools'] = _tools_this_round
                append_event(task, snap_evt)
            except Exception:
                logger.warning('[Task %s] messages_snapshot failed at round %d model=%s', tid, round_num + 1, model, exc_info=True)

            # ★ Cache-aware tool result ordering: sort consecutive tool results
            #   by tool_call_id so the prefix is deterministic across rounds
            #   (important for automatic prefix caching on OpenAI/Qwen).
            sort_tool_results(messages)

            body = build_body(
                model, messages,
                max_tokens=max_tokens,
                temperature=temperature,
                thinking_enabled=thinking_enabled,
                preset=preset,
                thinking_depth=thinking_depth,
                tools=_tools_this_round,
                response_format=response_format,
                stream=True,
            )
            # ★ Attach task_id for session-stable TTL latch in
            #   add_cache_breakpoints (prevents mid-session cache key shift).
            body['_task_id'] = task['id']

            # ★ Streaming tool execution: pre-execute read-only tools while
            #   the model is still generating subsequent tool calls.
            #   Also emits tool_start events immediately during streaming so
            #   the frontend shows "Searching…" / "Running…" without delay.
            from lib.tasks_pkg.streaming_tool_executor import StreamingToolAccumulator
            _stream_acc = StreamingToolAccumulator(
                task, project_path=cfg.get('projectPath'),
                tool_round_num=tool_round_num,
                round_num=round_num,
                project_enabled=project_enabled,
            )

            # ★ LLM call with automatic fallback to Opus on failure
            try:
                llm_result = _llm_call_with_fallback(
                    task, body, model, round_num, max_tokens,
                    tool_call_happened, tool_list, max_tool_rounds,
                    messages, preset, thinking_enabled,
                    accumulated_usage, api_rounds,
                    on_tool_call_ready=_stream_acc.on_tool_call_ready,
                )
                assistant_msg = llm_result['assistant_msg']
                last_finish_reason = llm_result['finish_reason']
                last_usage = llm_result['usage'] or last_usage
                model = llm_result['model']
                preset = llm_result['preset']
                thinking_enabled = llm_result['thinking_enabled']

                if llm_result['_loop_action'] == 'break':
                    _loop_exit_reason = llm_result['_loop_exit_reason']
                    break
            except Exception as e:
                if isinstance(e, AbortedError):
                    logger.info('[%s] ✋ User abort caught at round %d', tid, round_num)
                    _loop_exit_reason = 'user_abort'
                    break
                raise

            # ★ Prompt cache break detection: track what changed between turns
            #   to diagnose unexpected cost spikes.
            #   Inspired by Claude Code's promptCacheBreakDetection.ts.
            if task.get('convId') and last_usage:
                _cache_break = detect_cache_break(
                    task['convId'], messages,
                    tools=_tools_this_round, model=model,
                    usage=last_usage,
                )
                # Stamp the break reason onto the round we just recorded so
                # the frontend cost popover can explain WHY cache_read dropped
                # (system-prompt change, tools change, TTL expiry, …). Guard on
                # the round number so we don't mis-attribute when this round
                # produced no usage and api_rounds[-1] is an earlier round.
                if _cache_break and api_rounds and api_rounds[-1].get('round') == round_num + 1:
                    api_rounds[-1]['cacheBreak'] = _cache_break
                # ★ Stamp WHAT the model did this round (the tool calls it
                #   emitted). This is the causal driver of the NEXT round's
                #   cache `write`: round N's assistant output (text + these
                #   tool_calls) PLUS the tool results fed back get appended to
                #   the prefix and cached on round N+1. Recording the tool
                #   names lets the cost popover explain why a round that
                #   "generated" only a few hundred output tokens leads to a
                #   multi-thousand-token write next round.
                if api_rounds and api_rounds[-1].get('round') == round_num + 1:
                    try:
                        _tcs = (assistant_msg or {}).get('tool_calls') or []
                        _names = [
                            (tc.get('function') or {}).get('name') or '?'
                            for tc in _tcs if isinstance(tc, dict)
                        ]
                        if _names:
                            api_rounds[-1]['toolCalls'] = _names
                    except Exception as _te:
                        logger.debug('[%s] tool-call stamp failed: %s', tid, _te)
                    # ★ Stamp the EXACT decomposition of this round's `write`
                    #   into {toolResults, prevOutput, envelope} computed from
                    #   real recorded usage (see _compute_write_breakdown). The
                    #   frontend renders these three sub-items — which sum to
                    #   exactly `write` — instead of doing the arithmetic (and
                    #   only proxying it) client-side.
                    try:
                        _wb = _compute_write_breakdown(task, api_rounds, round_num)
                        if _wb:
                            api_rounds[-1]['writeBreakdown'] = _wb
                    except Exception as _we:
                        logger.debug('[%s] write-breakdown stamp failed: %s', tid, _we)
                # ★ Per-round cache stats at INFO level for production visibility
                log_round_cache_stats(
                    task['convId'], round_num, last_usage,
                    model=model, tid=task['id'],
                )

            # ★ Read back updated tool_round_num from streaming accumulator
            #   (tool_start events emitted during streaming already consumed
            #   round numbers, so parse_tool_calls must start from here).
            if _stream_acc.announced_tc_map:
                tool_round_num = _stream_acc.tool_round_num

            # ★ Inject pre-computed streaming tool results into dedup cache.
            #   execute_tool_pipeline will find these and skip re-execution.
            if _stream_acc.submitted_count > 0:
                _prefetch_hits = _stream_acc.inject_into_cache(task)
                if _prefetch_hits:
                    logger.info('[%s] Streaming tool exec: %d results pre-computed '
                                'and injected into cache', tid, _prefetch_hits)

            # ★ Post-stream analysis: premature close, abort, normal exit
            stream_decision = analyse_stream_result(
                assistant_msg, last_finish_reason, task, tid, model,
                round_num, _premature_retry_count, messages,
                usage=last_usage,
            )
            _premature_retry_count = stream_decision['premature_retry_count']
            last_finish_reason = stream_decision['last_finish_reason']
            if stream_decision['abort_detected_phase']:
                _abort_detected_phase = stream_decision['abort_detected_phase']
            if stream_decision['action'] == 'break':
                _loop_exit_reason = stream_decision['loop_exit_reason']
                break
            if stream_decision['action'] == 'continue':
                continue

            # ── Per-round diagnostic: log finish_reason for every tool round ──
            _round_content = len((assistant_msg or {}).get('content', '') or '')
            _round_tcs = len((assistant_msg or {}).get('tool_calls', []))
            logger.info('[%s] conv=%s Round %d result: finish_reason=%s model=%s '
                        'content=%dchars tool_calls=%d → proceeding to tool execution',
                        tid, task.get('convId', ''), round_num + 1, last_finish_reason, model,
                        _round_content, _round_tcs)

            # ── max_budget_usd gate (Claude Agent SDK parity) ──
            # Hard $ ceiling on accumulated cost.  0 / unset disables.
            _max_budget = float(cfg.get('maxBudgetUsd') or 0.0)
            if _max_budget > 0:
                from lib.cost_estimator import check_budget
                _exceeded, _cost, _reason = check_budget(
                    task, accumulated_usage, model, _max_budget,
                    round_num=round_num,
                )
                if _exceeded:
                    last_finish_reason = 'budget_exceeded'
                    from lib.error_envelope import make_envelope as _make_env
                    task['error'] = _make_env(
                        'budget_exceeded',
                        detail=_reason,
                        model=model,
                        context='budget-gate',
                        source='orchestrator',
                        raw=f'cost_usd={_cost:.6f} max={_max_budget:.6f}',
                    )
                    _loop_exit_reason = f'budget_exceeded_round_{round_num}_${_cost:.4f}'
                    break

            # ── Tool round budget check ──
            if round_num >= max_tool_rounds:
                # Safety ceiling: tool round budget exhausted
                last_finish_reason = 'tool_rounds_exhausted'
                from lib.error_envelope import make_envelope as _make_env
                task['error'] = _make_env(
                    'tool_rounds_exhausted',
                    detail=f'Tool call limit reached ({max_tool_rounds} rounds).',
                    model=model,
                    context='tool-budget',
                    source='orchestrator',
                    raw=f'max_tool_rounds={max_tool_rounds}',
                )
                logger.warning('[Task %s] conv=%s ⚠️ Tool rounds exhausted at round %d/%d', task['id'][:8], task.get('convId', ''), round_num+1, max_tool_rounds)
                _loop_exit_reason = f'tool_rounds_exhausted_{round_num}'
                break

            tool_call_happened = True
            clean_msg = {'role': 'assistant'}
            clean_msg['tool_calls'] = assistant_msg['tool_calls']
            if assistant_msg.get('content'): clean_msg['content'] = assistant_msg['content']
            if assistant_msg.get('reasoning_content'): clean_msg['reasoning_content'] = assistant_msg['reasoning_content']
            # ★ Carry the Claude thinking-block signature so the NEXT tool-loop
            #   turn replays a signed thinking block (build_body rebuilds
            #   reasoning_details from it). Without this, every in-loop turn
            #   after the first is a lossy continuation against Claude.
            if assistant_msg.get('thinking_signature'): clean_msg['thinking_signature'] = assistant_msg['thinking_signature']
            messages.append(clean_msg)

            # ★ Incremental auto-translate: this round's prose segment is now
            #   self-contained (the model finished its commentary and is about
            #   to call tools). Translate it in the background so it's ready by
            #   task end instead of one big translation stall. Gated + isolated
            #   inside the helper; a no-op when autoTranslate is off.
            try:
                from lib.translate import submit_round_segment
                submit_round_segment(task, round_num, assistant_msg.get('content') or '')
            except Exception as _ite:
                logger.debug('[%s] incremental translate submit failed (non-fatal): %s', tid, _ite)

            # ★ Expose live messages to context_compact tool handler
            task['_compact_messages'] = messages

            # ══════════════════════════════════════════
            #  Tool Execution Pipeline (delegated to tool_dispatch)
            # ══════════════════════════════════════════

            # ── Abort check before tool execution ──
            if task['aborted']:
                _abort_detected_phase = f'before_tool_exec_round_{round_num}'
                _loop_exit_reason = f'aborted_before_tools_round_{round_num}'
                # ★ Remove the assistant message with tool_calls that we just
                #   appended (line ~879) — since we're skipping tool execution,
                #   leaving it creates orphaned tool_use blocks without matching
                #   tool_result.  This causes HTTP 400 on the next turn when
                #   server_message_store replays the full message history.
                if messages and messages[-1].get('tool_calls'):
                    _popped = messages.pop()
                    logger.info('[%s] Removed trailing tool_calls message (abort) — '
                                'prevents orphaned tool_use in stored history', tid)
                    # If it had content alongside tool_calls, keep just the content
                    if _popped.get('content'):
                        messages.append({'role': 'assistant', 'content': _popped['content']})
                        logger.debug('[%s] Re-added assistant content without tool_calls', tid)
                logger.info('[%s] Task aborted before tool execution at round %d — skipping all tools', tid, round_num)
                break

            # ── Phase 1: Parse all tool_calls ──
            #   Pass early_announced so parse_tool_calls skips re-emitting
            #   tool_start events that were already sent during streaming.
            parsed_tcs, tool_round_num = parse_tool_calls(
                assistant_msg, task, round_num, tool_round_num, project_enabled,
                early_announced=_stream_acc.announced_tc_map,
            )

            # ── Phase 1b: Sanitize tool_calls in messages so the next API
            #   round doesn't carry malformed JSON args back to the gateway.
            #
            #   Background: when a model emits ``tool_calls=[{arguments: '...'}]``
            #   where ``arguments`` is invalid JSON (common with weaker models
            #   that mis-escape backslashes in regex args, e.g. ``\d`` instead
            #   of ``\\d``), parse_tool_calls() catches the JSONDecodeError and
            #   builds an error tool_result.  But the assistant message we
            #   already appended at line ~1361 still contains the RAW bad args.
            #
            #   On the next round, server_message_store / orchestrator replays
            #   ``assistant(tool_calls=[..bad args..]) + tool(error_msg)`` to
            #   the upstream gateway, which validates the JSON-string itself
            #   and rejects with HTTP 400 ``invalid function arguments json
            #   string``.  The whole conversation gets stuck — model never
            #   sees the error tool_result, can't recover, task ends in
            #   ``finishReason=error``.
            #
            #   Fix: walk parsed_tcs and any tc with non-None ``_args_parse_error``
            #   gets its ``arguments`` overwritten to ``'{}'`` in messages[-1].
            #   The error tool_result still teaches the model what went wrong;
            #   the gateway sees valid JSON and lets the next round through.
            #   See May 2026 incident memory.
            for tc, fn_name, tc_id, fn_args, rn, round_entry, args_parse_err in parsed_tcs:
                if not args_parse_err:
                    continue
                # Find the matching tool_call in messages[-1] by tc_id and
                # rewrite its arguments to a syntactically valid empty JSON.
                last_msg = messages[-1] if messages else {}
                for live_tc in last_msg.get('tool_calls', []) or []:
                    if live_tc.get('id') != tc_id:
                        continue
                    fn = live_tc.get('function') or {}
                    bad_args = fn.get('arguments', '')
                    fn['arguments'] = '{}'
                    logger.info(
                        '[%s] conv=%s Sanitized malformed tool_call args for '
                        'tool=%s tc_id=%s (was %d chars) — error fed back to '
                        'model in matching tool_result; gateway sees valid JSON',
                        tid, task.get('convId', ''), fn_name, tc_id[:12],
                        len(bad_args) if isinstance(bad_args, str) else 0)
                    break

            # ── Phase 2: Emit execution phase event ──
            emit_tool_exec_phase(task, parsed_tcs)

            # ── Phase 3: Execute tools (approval + parallel + result append) ──
            _tool_timed_out = execute_tool_pipeline(
                task, parsed_tcs, cfg, project_path, project_enabled,
                tool_list, messages, all_search_results_text, round_num, model,
            )

            # Clean up live messages ref after tool execution
            task.pop('_compact_messages', None)

            # ── Phase 4b: Consecutive tool-timeout circuit breaker ──
            if _tool_timed_out:
                _consecutive_tool_timeouts += 1
                logger.warning(
                    '[%s] conv=%s Tool timeout at round %d (%d/%d consecutive) model=%s',
                    tid, task.get('convId', ''), round_num + 1, _consecutive_tool_timeouts,
                    _MAX_CONSECUTIVE_TOOL_TIMEOUTS, model)
                if _consecutive_tool_timeouts >= _MAX_CONSECUTIVE_TOOL_TIMEOUTS:
                    logger.error(
                        '[%s] conv=%s ⚠️ FORCE STOP: %d consecutive tool timeouts — breaking loop to prevent runaway task. model=%s',
                        tid, task.get('convId', ''), _consecutive_tool_timeouts, model)
                    from lib.error_envelope import make_envelope as _make_env
                    task['error'] = _make_env(
                        'tool_timeout',
                        detail=f'{_consecutive_tool_timeouts} consecutive tool execution timeouts.',
                        model=model,
                        context='tool-loop',
                        source='orchestrator',
                        raw=f'consecutive_tool_timeouts={_consecutive_tool_timeouts}',
                    )
                    _loop_exit_reason = f'consecutive_tool_timeouts_{_consecutive_tool_timeouts}'
                    break
            else:
                _consecutive_tool_timeouts = 0  # Reset on successful tool execution

            # ══════════════════════════════════════════
            #  ★ Crash-recovery checkpoint: persist partial state to DB
            # ══════════════════════════════════════════
            # After each tool execution round, save current content/thinking
            # to task_results + conversation so data survives a server crash.
            # Throttled to at most once every 10 seconds to avoid DB pressure.
            _now = time.time()
            if _now - _last_checkpoint >= 5:
                try:
                    checkpoint_task_partial(task)
                    _last_checkpoint = _now
                except Exception as e:
                    logger.warning('[%s] Checkpoint after round %d failed (non-fatal): %s', tid, round_num + 1, e, exc_info=True)



        # ── Append final assistant reply to messages if it wasn't already ──
        # When the LLM returns text content WITHOUT tool_calls, the loop
        # breaks before appending the assistant message (tool_calls path at
        # line ~698 is the only place messages.append(clean_msg) happens).
        # Without this, _run_single_turn returns messages missing the
        # assistant's reply, and endpoint mode's critic never sees the
        # worker's output.
        if assistant_msg and not assistant_msg.get('tool_calls'):
            _final_content = assistant_msg.get('content') or ''
            _final_reasoning = assistant_msg.get('reasoning_content') or ''
            if _final_content or _final_reasoning:
                _final_assistant = {'role': 'assistant', 'content': _final_content}
                if _final_reasoning:
                    _final_assistant['reasoning_content'] = _final_reasoning
                messages.append(_final_assistant)
                logger.debug('[%s] Appended final assistant reply to messages '
                             '(%d content chars, %d reasoning chars)',
                             tid, len(_final_content), len(_final_reasoning))
                # ★ Incremental auto-translate: the closing prose segment (the
                #   model's final answer after the last tool round, or the
                #   whole reply when no tools were called). round_num here is
                #   the final round index — unique vs the in-loop submissions.
                try:
                    from lib.translate import submit_round_segment
                    submit_round_segment(task, round_num, _final_content)
                except Exception as _ite:
                    logger.debug('[%s] incremental translate submit (final) failed: %s', tid, _ite)
                # Emit a final snapshot so the debug panel shows the complete
                # message list. The only in-loop snapshots are "请求前" (before
                # the assistant reply exists) and the post-tool one (skipped on
                # a no-tool-call completion), so without this the panel is stuck
                # on [system?, user].
                try:
                    snap = _strip_base64_for_snapshot(messages)
                    snap_evt = build_event(
                        EventType.MESSAGES_SNAPSHOT,
                        round='final',
                        label=f'最终回复后 · {len(messages)}条',
                        messages=snap)
                    # Carry the tool schema so the panel's tools section
                    # survives — showMessagesInDebug rebuilds _debugCache and
                    # drops the cached tools unless this snapshot re-supplies them.
                    if tool_list:
                        snap_evt['tools'] = tool_list
                    append_event(task, snap_evt)
                except Exception:
                    logger.warning('[Task %s] final messages_snapshot failed model=%s',
                                   tid, model, exc_info=True)

        # ── Write back updated messages to task so callers (e.g.
        #    _run_single_turn → endpoint.py) can access the complete
        #    conversation including assistant replies and tool results.
        #    Without this, task['messages'] still holds the PRE-run_task
        #    snapshot, and endpoint mode's critic never sees the worker's output.
        task['messages'] = messages

        # ── Save full messages to server store for next turn ──
        if _keep_tool_history and _conv_id:
            try:
                _save_messages_to_store(_conv_id, messages)
            except Exception as e:
                logger.warning('[%s] conv=%s Failed to save messages to store: %s',
                               tid, _conv_id[:8], e, exc_info=True)

        # ── Post-loop finalization: fallback, done event, persist ──
        _finalize_and_emit_done(
            task,
            model=model, preset=preset, thinking_depth=thinking_depth, cfg=cfg,
            last_finish_reason=last_finish_reason, last_usage=last_usage,
            accumulated_usage=accumulated_usage, api_rounds=api_rounds,
            tool_call_happened=tool_call_happened, messages=messages,
            original_messages=original_messages,
            all_search_results_text=all_search_results_text,
            max_tokens=max_tokens, thinking_enabled=thinking_enabled,
            temperature=temperature,
            _loop_exit_reason=_loop_exit_reason,
            _abort_detected_phase=_abort_detected_phase,
            project_path=project_path, project_enabled=project_enabled,
            round_num=round_num,
            assistant_msg=assistant_msg,
        )

        # ── Autopilot now runs INSIDE _finalize_and_emit_done (before
        #    the done SSE event is emitted), so its result can ride on
        #    the same event.  No standalone hook here.
    except Exception as e:
        logger.error('[Orchestrator] run_task FATAL error task=%s', task.get('id', '?')[:8], exc_info=True)
        # Prefer the user-friendly message attached by _llm_call_with_fallback;
        # otherwise format the raw exception here so the frontend error-block
        # always tells the user how to recover.
        _user_err = getattr(e, '_user_message', None)
        if not _user_err:
            try:
                from lib.llm_error_format import format_llm_error_for_user
                _user_err = format_llm_error_for_user(
                    e, model=task.get('config', {}).get('model', ''),
                    context='task-fatal', source='orchestrator')
            except Exception as _fmt_err:
                logger.warning('[Orchestrator] format_llm_error_for_user failed: %s', _fmt_err)
                from lib.error_envelope import make_envelope as _make_env
                _user_err = _make_env(
                    'internal',
                    detail=f'Task fatal: {e}',
                    model=task.get('config', {}).get('model', ''),
                    context='task-fatal',
                    source='orchestrator',
                    raw=str(e),
                )
        task['error'] = _user_err; task['status'] = 'error'; task['finishReason'] = 'error'
        if task.get('_endpoint_managed'):
            return   # let endpoint.py handle the error
        append_event(task, build_event(EventType.DONE, error=_user_err, finishReason='error'))
        persist_task_result(task)
    finally:
        # ── Clear the per-task request-id correlation tag (pooled threads are
        #    reused; a stale tid would mis-attribute the NEXT task's logs). ──
        set_req_id('')
        # ── Clear the hard provider pin so it can't bleed into the NEXT
        #    task that lands on this pooled worker thread. ──
        try:
            from lib.llm_dispatch.provider_pin import clear_pinned_provider
            clear_pinned_provider()
        except Exception as _pp_err:
            logger.debug('[Task:%s] clear_pinned_provider failed: %s', tid, _pp_err)
        # ── Clear the conversation binding (pooled threads are reused). ──
        try:
            from lib.llm_dispatch.conv_affinity import clear_conv_affinity
            clear_conv_affinity()
        except Exception as _ca_err:
            logger.debug('[Task:%s] clear_conv_affinity failed: %s', tid, _ca_err)
        # ── Release this worker thread's thread-local DB connection back to
        #    the shared pool.  run_task runs on long-lived threads (the
        #    asyncio.to_thread default pool, or daemon task threads); without
        #    this each one would pin a PG connection for its entire lifetime,
        #    exhausting the connection semaphore under high concurrency
        #    (see the "pool exhausted / tracked_threads ≫ active" symptom). ──
        try:
            from lib.agent_core.store import get_conversation_store
            get_conversation_store().release_connection()
        except Exception as _ctd_err:
            logger.debug('[Task:%s] release_connection on task end failed: %s',
                         tid, _ctd_err)


# ══════════════════════════════════════════════════════════
#  _run_single_turn — reusable building block for endpoint mode
# ══════════════════════════════════════════════════════════

def _run_single_turn(
    task: dict[str, Any],
    messages_override: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Execute ONE full work turn (LLM + tool loop) and return the results.

    This wrapper:
    1. Resets per-turn accumulation fields (content, thinking, usage, etc.)
    2. Optionally replaces the messages list
    3. Delegates to the full ``run_task`` machinery
    4. Returns dict with keys: content, thinking, usage, finishReason, messages, error

    **Note:** This mutates ``task`` in place (content, thinking, status, etc.).
    It does NOT emit 'done' events — the caller (endpoint.py) decides when the
    overall session is done.

    Parameters
    ----------
    task : dict
        The live task dict (from ``create_task``).  Must already be in ``tasks``.
    messages_override : list | None
        If provided, replaces ``task['messages']`` before calling.

    Returns
    -------
    dict  with keys: content, thinking, usage, finishReason, messages, error
    """
    if 'id' not in task:
        raise ValueError("_run_single_turn called with a task dict missing 'id' — did you forget to use create_task()?")
    tid = task['id'][:8]
    logger.debug('[Endpoint] _run_single_turn %s ENTRY — messages_override=%s',
                 tid, 'yes' if messages_override is not None else 'no')

    # Override messages if supplied
    if messages_override is not None:
        task['messages'] = list(messages_override)

    # Reset per-turn accumulation fields so run_task starts clean
    with task['content_lock']:
        task['content']  = ''
        task['thinking'] = ''
    task['usage']        = {}
    task['status']       = 'running'
    task['error']        = None
    task['finishReason'] = None
    task['toolRounds'] = []    # fresh tool rounds per turn

    # Flag to tell run_task NOT to emit final 'done' event
    task['_endpoint_managed'] = True

    try:
        run_task(task)
    finally:
        task.pop('_endpoint_managed', None)

    result = {
        'content':      task.get('content', ''),
        'thinking':     task.get('thinking', ''),
        'usage':        task.get('usage', {}),
        'finishReason': task.get('finishReason', 'stop'),
        'messages':     list(task.get('messages', [])),
        'error':        task.get('error'),
    }
    # ★ Propagate fallback info so endpoint mode can surface it to the frontend
    if task.get('_fallback_model'):
        result['fallbackModel'] = task['_fallback_model']
        result['fallbackFrom']  = task.get('_fallback_from', '')
        if task.get('_fallback_reason'):
            result['fallbackReason'] = task['_fallback_reason']
        if task.get('_fallback_kind'):
            result['fallbackKind'] = task['_fallback_kind']

    logger.debug('[Endpoint] _run_single_turn %s → %d chars, finish=%s',
                 tid, len(result['content']), result['finishReason'])
    return result
