# HOT_PATH
"""Tool dispatch — parsing, labelling, approval-gating, and parallel execution.

Extracted from the inner loop of ``orchestrator.run_task`` to isolate the
tool-execution pipeline.  The two public entry-points are:

- :func:`parse_tool_calls` — parse raw ``tool_calls`` from the assistant
  message into a structured list with JSON repair.
- :func:`execute_tool_pipeline` — run the full approval → parallel-dispatch
  → result-append pipeline.
"""

from __future__ import annotations

import json
import os
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from lib.agent_core.events import EventType, build_event
from lib.log import get_logger
from lib.protocols import TaskEventSink

logger = get_logger(__name__)

from lib.tasks_pkg.approval import request_write_approval
from lib.tasks_pkg.compaction import budget_tool_result, clamp_tool_result_text, enforce_round_aggregate_budget, mark_empty_result
from lib.tasks_pkg.executor import SWARM_TOOL_NAMES, _build_simple_meta, _execute_tool_one, _finalize_tool_round, tool_registry
from lib.tasks_pkg.manager import _strip_base64_for_snapshot, append_event
from lib.tasks_pkg.tool_display import _build_tool_round_entry
from lib.tasks_pkg.tool_hooks import run_post_hooks, run_pre_hooks
from lib.token_counter import count_text
from lib.tool_input_repair import resolve_tool_name, schema_hint, validate_then_repair
from lib.tools import build_project_tool_meta
from lib.model_info import model_supports_vision


# ── Harness self-repair UI surfacing ─────────────────────────────────
# When the dispatcher recovers a malformed tool call (truncated/invalid JSON
# or schema-shape coercions from lib/tool_input_repair.py), we attach a small
# ``_repaired`` descriptor to the tool round so the frontend can show a
# "fixed" badge + tooltip explaining what was corrected.  Human-readable
# labels for the schema-repair pattern names emitted by validate_then_repair.
_REPAIR_PATTERN_LABELS = {
    'null_omission': 'dropped null optional',
    'stringified_json': 'parsed stringified JSON',
    'stringified_primitive': 'coerced string to number/bool',
    'bare_string_to_array': 'wrapped string in array',
    'empty_placeholder_unwrap': 'unwrapped object to array',
    'leaked_tool_call_syntax': 'stripped leaked tool-call markup',
}


def _build_repair_summary(json_repaired: bool, repair_log, tool_name_aliased: str | None = None,
                          resolved_tool_name: str | None = None) -> dict | None:
    """Build a UI-facing repair descriptor, or None when nothing was fixed.

    Returns ``{'label': str, 'detail': str, 'patterns': [...]}`` describing
    the auto-corrections applied to a tool call's arguments.

    ``tool_name_aliased`` / ``resolved_tool_name``: when the model called a
    tool by a wrong-harness name (e.g. ``read_file``) that we rewrote to the
    canonical Tofu tool (``read_files``), record it so the UI shows the fix.
    """
    parts = []
    patterns = []
    if tool_name_aliased and resolved_tool_name:
        parts.append(f'renamed tool {tool_name_aliased} → {resolved_tool_name}')
        patterns.append('tool_name_alias')
    if json_repaired:
        parts.append('recovered malformed JSON')
    for entry in (repair_log or []):
        try:
            path, pattern = entry
        except (ValueError, TypeError) as e:
            logger.debug('[tool_dispatch] skipping malformed repair_log entry %r: %s', entry, e)
            continue
        patterns.append(pattern)
        label = _REPAIR_PATTERN_LABELS.get(pattern, pattern)
        parts.append(f'{path}: {label}')
    if not parts:
        return None
    return {
        'label': 'auto-fixed',
        'detail': '; '.join(parts),
        'patterns': patterns,
    }


def _apply_repair_to_round(round_entry: dict, fn_name: str, fn_args: dict,
                           repair_summary: dict, project_enabled: bool,
                           conv_id) -> None:
    """Patch a stale (early-announced) round entry after a late repair.

    The streaming early-announce path renders the round display BEFORE the
    schema-repair pass runs, so a malformed arg (e.g. ``reads`` as a JSON
    string) produces a garbled display line.  Once repaired, rebuild the
    display from the corrected args and attach the ``_repaired`` descriptor.
    """
    round_entry['_repaired'] = repair_summary
    try:
        tc_args_str = json.dumps(fn_args, ensure_ascii=False) if fn_args else '{}'
        _, fresh_entry, _ = _build_tool_round_entry(
            fn_name, fn_args, round_entry.get('toolCallId', ''), tc_args_str,
            round_entry.get('roundNum', 1) - 1, project_enabled, conv_id=conv_id,
        )
        # Only refresh the human-facing display string; keep roundNum, status,
        # llmRound, toolCallId, etc. intact on the live entry.
        if fresh_entry.get('query'):
            round_entry['query'] = fresh_entry['query']
        round_entry['toolArgs'] = tc_args_str
    except Exception as e:
        logger.debug('[ToolDispatch] repair display refresh failed for %s: %s',
                     fn_name, e)


def _safe_count_tokens(text: str, model: str = '') -> int:
    """Count tokens for a tool result, swallowing backend failures.

    The token counter is best-effort metadata: a backend hiccup must never
    abort tool execution. Returns 0 on any failure so the frontend can
    fall back to chars.
    """
    if not text:
        return 0
    try:
        return count_text(text, model=model)
    except Exception as e:
        logger.debug('[ToolDispatch] count_text failed: %s', e)
        return 0

# ── Idempotent tool dedup — cache read-only tool results within a task ──
# These tools produce the same result for the same arguments within one task
# execution.  When the model repeats a call, we return the cached result
# instantly instead of re-executing (e.g. re-fetching a URL).
#
# The literal base below covers built-in tools (incl. browser-internal names
# that the ToolSpec registry doesn't enumerate).  We then union the
# ``idempotent_tools`` flags declared by every registered ToolSpec — so a
# third-party plugin that marks its tool idempotent is honoured automatically
# with no edit here.  See lib/tools/registry.py.
_IDEMPOTENT_TOOLS_BASE = frozenset({
    'web_search', 'fetch_url',
    'read_files', 'list_dir', 'grep_search', 'find_files',
    'browser_read_tab', 'browser_list_tabs',
    'browser_get_history', 'browser_get_cookies',
    'browser_summarize_page', 'browser_get_app_state',
    'browser_get_interactive_elements',
    'list_conversations', 'get_conversation',
})

# ── Concurrency safety partitioning ──
# Inspired by Claude Code's isConcurrencySafe flag per tool.
# Write tools run SERIALLY (even when auto_apply=True) to prevent
# filesystem race conditions.  Read-only tools run in parallel.
# This is separate from _IDEMPOTENT_TOOLS (dedup) — a tool can be
# concurrent-safe (run in parallel) but not idempotent (don't cache).
_WRITE_TOOLS_BASE = frozenset({
    'write_file', 'apply_diff', 'apply_diffs',
    'insert_content', 'insert_contents',
    'create_project', 'run_command',
    'create_memory', 'update_memory', 'delete_memory', 'merge_memories',
})


def _registry_tool_flags() -> tuple[frozenset, frozenset]:
    """Union the literal base sets with ToolSpec-declared flags.

    Keeps the concurrency/dedup partitions in sync with the declarative tool
    registry (incl. third-party plugins) without a second hand-maintained
    list.  Falls back to the base sets if the registry import fails.
    """
    write = set(_WRITE_TOOLS_BASE)
    idem = set(_IDEMPOTENT_TOOLS_BASE)
    try:
        from lib.tools import all_specs
        for spec in all_specs():
            write |= set(spec.write_tools)
            idem |= set(spec.idempotent_tools)
    except Exception as e:
        logger.debug('[tool_dispatch] registry flag union skipped: %s', e)
    return frozenset(write), frozenset(idem)


_WRITE_TOOLS, _IDEMPOTENT_TOOLS = _registry_tool_flags()


def _task_partitions(task: dict[str, Any]) -> tuple[frozenset, frozenset]:
    """Per-task write/idempotent partitions: base UNION the task's custom env.

    The module-level ``_WRITE_TOOLS`` / ``_IDEMPOTENT_TOOLS`` are frozen at
    import and cover built-ins + ToolSpec plugins. Per-request custom tools
    (``task['_tool_env']``) declare their own ``write`` / ``idempotent`` flags,
    which would otherwise be invisible here — a custom write tool would run in
    the parallel pool (race) and a custom read tool would never dedup. Union
    them in at dispatch time so the partitions are correct for THIS task.
    """
    write = set(_WRITE_TOOLS)
    idem = set(_IDEMPOTENT_TOOLS)
    # ── MCP tools: conservative write classification ──
    # External MCP tools carry no built-in safety partition, so by default we
    # treat every discovered MCP tool as a WRITE tool (serial dispatch +
    # approval-eligible in Manual mode). A tool whose MCP ``readOnlyHint``
    # annotation is explicitly True is exempted (stays in the parallel pool).
    # This closes the hole where an arbitrary remote-mutating MCP tool ran in
    # parallel with no approval. Computed per-task because the MCP bridge may
    # connect after this module is imported.
    try:
        from lib.mcp import get_bridge
        bridge = get_bridge()
        if bridge.connected:
            for ns_name, read_only in bridge.get_tool_safety().items():
                if not read_only:
                    write.add(ns_name)
    except Exception as e:
        logger.debug('[tool_dispatch] MCP partition classification skipped: %s', e)
    # ── Per-request custom tools (task-local env) ──
    env = task.get('_tool_env')
    if env is not None:
        try:
            write |= env.write_names
            idem |= env.idempotent_names
        except Exception as e:
            logger.debug('[tool_dispatch] task partition union skipped: %s', e)
    return frozenset(write), frozenset(idem)



def _execute_tool_one_pooled(*args, **kwargs):
    """Run ``_execute_tool_one`` then release the worker's thread-local DB conn.

    Parallel tools run on a per-round ``ThreadPoolExecutor`` whose threads are
    reused across submissions and only torn down at pool shutdown. A tool that
    touches the DB (memory, conversation, mcp handlers) would otherwise pin a
    connection on its worker thread for the whole round; under high concurrency
    that compounds across tasks and exhausts the connection semaphore. Release
    at the worker boundary so the connection returns to the shared pool.
    """
    try:
        return _execute_tool_one(*args, **kwargs)
    finally:
        try:
            from lib.agent_core.store import get_conversation_store
            get_conversation_store().release_connection()
        except Exception as _ctd_err:
            logger.debug('[tool_dispatch] pooled release_connection failed: %s', _ctd_err)


def _make_cache_key(fn_name: str, fn_args: dict[str, Any]) -> str:
    """Build a deterministic cache key from tool name + arguments.

    Sorts dict keys recursively so argument ordering doesn't matter.
    """
    try:
        canonical = json.dumps(fn_args, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError) as _e_audit:
        logger.debug('[tool_dispatch] _make_cache_key caught %s: %s', type(_e_audit).__name__, _e_audit)
        canonical = str(fn_args)
    return f'{fn_name}::{canonical}'


# Project tools whose cache entries become stale after a write operation
_PROJECT_CACHEABLE_TOOLS = frozenset({
    'read_files', 'list_dir', 'grep_search', 'find_files',
})


def _invalidate_project_cache(cache: dict, trigger: str = 'write_op') -> None:
    """Remove all project-tool cache entries after a write operation.

    Called after write_file / apply_diff / code_exec so that subsequent
    read_files / grep_search calls re-read the (now-modified) filesystem.

    Args:
        cache: The per-task dedup cache dict.
        trigger: Name of the operation that triggered invalidation
                 (for logging).
    """
    stale_keys = [k for k in cache if k.split('::', 1)[0] in _PROJECT_CACHEABLE_TOOLS]
    for k in stale_keys:
        del cache[k]
    if stale_keys:
        # Group by tool name for readable logging
        tool_counts: dict[str, int] = {}
        for k in stale_keys:
            tool_name = k.split('::', 1)[0]
            tool_counts[tool_name] = tool_counts.get(tool_name, 0) + 1
        breakdown = ', '.join(f'{n}={c}' for n, c in sorted(tool_counts.items()))
        logger.info('[DedupInvalidate] %d entries invalidated by %s: %s',
                    len(stale_keys), trigger, breakdown)


def _build_cache_hit_meta(
    fn_name: str,
    fn_args: dict[str, Any],
    cached_content,
    is_prefetch: bool,
) -> dict[str, Any]:
    """Build tool-specific display metadata for a cache/prefetch hit.

    The generic ``_build_simple_meta`` lacks fields the frontend needs for
    rich rendering (e.g. ``url`` for fetch_url, proper title/snippet for
    web_search).  This helper builds metadata that matches what the normal
    tool handler would produce, so the UI shows the same preview regardless
    of whether the result was freshly executed or served from cache.
    """
    # ── read_files image: preserve inline-render data URI ──
    # Prefetched/cached read_files image results are __screenshot__ dicts
    # (batches are collapsed to the first image upstream). str() on the dict
    # would dump base64 into the snippet, so handle it explicitly.
    if isinstance(cached_content, dict) and cached_content.get('__screenshot__'):
        fmt = cached_content.get('format', 'png')
        comp_size = cached_content.get('compressedSize', 0)
        filename = os.path.basename(fn_args.get('path', '') or '')
        source_label = 'Prefetch' if is_prefetch else 'Cache'
        badge_suffix = '' if is_prefetch else ' (cached)'
        # Multi-image batch carries every image in ``images``; fall back to
        # the dict itself for a single image.
        img_dicts = cached_content.get('images') or [cached_content]
        descriptors = []
        for img in img_dicts:
            uri = img.get('dataUrl', '') or ''
            if uri:
                descriptors.append({
                    'uri': uri,
                    'format': img.get('format', fmt),
                    'filename': img.get('filename', '') or filename,
                })
        n = len(img_dicts)
        title = f'🖼️ {filename}' if filename else '🖼️ image'
        snippet = f'{filename or "image"} ({fmt}, {comp_size:,} bytes)'
        if n > 1:
            title = f'🖼️ {n} images'
            snippet = f'{n} images loaded'
        meta = {
            'toolName': fn_name,
            'title': title,
            'snippet': snippet,
            'source': source_label, 'fetched': True,
            'fetchedChars': comp_size, 'url': '',
            'badge': f'🖼️ {fmt}{badge_suffix}',
        }
        if descriptors:
            meta['imageDataUris'] = descriptors
        return meta

    content_str = cached_content if isinstance(cached_content, str) else str(cached_content)
    chars = len(content_str)
    source_label = 'Prefetch' if is_prefetch else 'Cache'
    badge_suffix = '' if is_prefetch else ' (cached)'

    # ── fetch_url: include URL so frontend can render clickable link ──
    if fn_name == 'fetch_url':
        # Batch mode fallback — no display_results available, best-effort summary
        urls = fn_args.get('urls')
        if urls and isinstance(urls, list):
            n = len(urls)
            return {
                'toolName': fn_name,
                'title': f'{n} URLs{badge_suffix}',
                'snippet': f'{chars:,} chars total',
                'source': source_label,
                'fetched': True,
                'fetchedChars': chars,
            }
        target_url = fn_args.get('url', '')
        from lib.tasks_pkg.tool_display import _short_url
        short = _short_url(target_url) if target_url else ''
        is_pdf = target_url.lower().rstrip('/').endswith('.pdf')
        fetched_ok = bool(content_str) and not content_str.startswith('Failed to fetch')
        chars_label = (
            f'{chars:,} chars' if fetched_ok else 'Failed'
        )
        return {
            'title': f'{"PDF" if is_pdf else "Page"}: {short}{badge_suffix}',
            'snippet': chars_label,
            'url': target_url,
            'source': source_label,
            'fetched': fetched_ok,
            'fetchedChars': chars if fetched_ok else 0,
        }

    # ── web_search: keep results from cached content (already display-formatted) ──
    # For web_search, the cached_content is the text formatted for the LLM,
    # not display_results.  Build a minimal meta with char count.
    if fn_name == 'web_search':
        queries = fn_args.get('queries')
        if queries and isinstance(queries, list):
            n = len(queries)
            return {
                'toolName': fn_name,
                'title': f'{n} searches{badge_suffix}',
                'snippet': f'{chars:,} chars total',
                'source': source_label,
                'fetched': True,
                'fetchedChars': chars,
            }
        query = fn_args.get('query', '')
        return {
            'toolName': fn_name,
            'title': f'Search: {query[:60]}{badge_suffix}',
            'snippet': f'{chars:,} chars',
            'source': source_label,
            'fetched': True,
            'fetchedChars': chars,
        }

    # ── Fallback for all other tools ──
    if is_prefetch:
        return _build_simple_meta(
            fn_name, cached_content, source=source_label,
            title=fn_name,
            snippet='Pre-executed during streaming',
        )
    else:
        return _build_simple_meta(
            fn_name, cached_content, source=source_label,
            title=f'{fn_name} (cached)',
            snippet='Duplicate call — returning cached result',
            badge='cached',
        )


# ── Human-readable labels for tool-execution phase events ──────────────
_TOOL_EXEC_LABELS = {
    'web_search':   '🔍 Searching the web',
    'fetch_url':    '🌐 Fetching pages',
    'read_files':   '📖 Reading files',
    'list_dir':     '📂 Listing directory',
    'grep_search':  '🔎 Searching code',
    'find_files':   '🔎 Finding files',
    'write_file':   '✏️ Writing files',
    'apply_diff':   '✏️ Applying changes',
    'apply_diffs':  '✏️ Applying changes',
    'insert_content':'📥 Inserting content',
    'insert_contents':'📥 Inserting content',
    'code_exec':    '▶️ Running code',
    'bash_exec':    '▶️ Running command',
    'create_memory': '💡 Saving memory',
    'ask_human': '🙋 Asking for your input',
}


# _repair_json now lives in lib.utils — no lazy import wrapper needed.
from lib.utils import repair_json as _repair_json


def tool_label(tn: str) -> str:
    """Get a human-readable label for a tool name, with MCP fallback.

    Used by both ``emit_tool_exec_phase`` and ``orchestrator._emit_tool_round_phase``
    to produce consistent labels.
    """
    label = _TOOL_EXEC_LABELS.get(tn)
    if label:
        return label
    from lib.mcp.types import MCP_TOOL_PREFIX, parse_namespaced_name
    if tn.startswith(MCP_TOOL_PREFIX):
        parsed = parse_namespaced_name(tn)
        if parsed:
            return f'🔌 {parsed[0]}/{parsed[1]}'
    return tn


def parse_tool_calls(
    assistant_msg: dict[str, Any],
    task: dict[str, Any],
    round_num: int,
    tool_round_num: int,
    project_enabled: bool,
    early_announced: dict[str, tuple] | None = None,
) -> tuple[list[tuple], int]:
    """Parse raw tool_calls from the assistant message into structured tuples.

    For each tool call, parses (or repairs) the JSON arguments, builds the
    display round-entry via ``_build_tool_round_entry``, appends search
    rounds to the task, and emits the corresponding SSE event.

    When ``early_announced`` is provided (from ``StreamingToolAccumulator``),
    tool calls that were already announced during streaming are NOT re-emitted.
    Their existing round entries (already in ``task['toolRounds']``) are
    reused, avoiding duplicate ``tool_start`` events on the frontend.

    Parameters
    ----------
    assistant_msg : dict
        The assistant message with a ``tool_calls`` list.
    task : dict
        Live task dict — mutated (``toolRounds`` appended, events emitted).
    round_num : int
        Zero-based loop iteration index (for logging).
    tool_round_num : int
        Current tool round counter (updated as tool rounds are created).
    project_enabled : bool
        Whether project-mode is active.
    early_announced : dict, optional
        Map of ``tc_id → (roundNum, round_entry)`` for tools already announced
        via ``StreamingToolAccumulator.on_tool_call_ready``.  These will reuse
        the existing round entry and skip SSE emission.

    Returns
    -------
    tuple[list, int]
        ``(parsed_tcs, tool_round_num)`` where ``parsed_tcs`` is a list of
        7-tuples: ``(tc, fn_name, tc_id, fn_args, rn, round_entry,
        _args_parse_error)``.
    """
    tid = task['id'][:8]
    parsed_tcs = []
    _early = early_announced or {}
    # ★ Capture per-round assistant content (text LLM emitted alongside tool calls)
    _assistant_content = (assistant_msg.get('content') or '').strip()
    _ac_tagged = False  # only tag the first entry per round
    # ★ Capture per-round reasoning/thinking text so Continue can replay it
    #   against APIs that accept thinking continuity (Claude extended-thinking).
    #   Currently sourced from OpenAI-compat `reasoning_content`; if an upstream
    #   proxy surfaces the block-level signature separately we can extend the
    #   key set below (`thinkingSignature`).
    _assistant_thinking = (assistant_msg.get('reasoning_content') or '').strip()
    _assistant_thinking_signature = assistant_msg.get('thinking_signature') or ''

    _total_tcs = len(assistant_msg['tool_calls'])
    # Build set of function names that have non-empty arguments,
    # so we can identify phantom duplicates (same name, empty args).
    _names_with_real_args = set()
    for _tc in assistant_msg['tool_calls']:
        _fn = (_tc.get('function') or {})
        if (_fn.get('arguments', '') or '').strip():
            _names_with_real_args.add(_fn.get('name', ''))
    for tc in assistant_msg['tool_calls']:
        fn_obj = tc.get('function') or {}
        fn_name = fn_obj.get('name', '')
        if not fn_name:
            logger.warning('[Task %s] Skipping tool call with missing function name: %s', tid, tc)
            continue
        # Guard against spurious internal tool names that leaked through streaming
        # (e.g. 'antml:thinking' from Anthropic proxy artifacts)
        if ':' in fn_name or fn_name.startswith('__'):
            logger.warning('[Task %s] Skipping spurious/internal tool call name: %s', tid, fn_name)
            continue
        # Guard against corrupted tool names with XML/HTML artifacts
        # (e.g. MiniMax emitting 'list_dir">.</parameter>\n</invoke>\n<invoke name="grep_search'
        #  when it hallucinates raw XML in its tool call output)
        if not fn_name.replace('_', '').replace('-', '').isalnum():
            logger.warning('[Task %s] Skipping malformed tool name (non-alphanumeric): %.80s', tid, fn_name)
            continue
        # ── Tool-NAME repair (alias resolution) ──
        # The model sometimes calls a tool by a name from another harness
        # (read_file→read_files, bash→run_command, Grep→grep_search, …).
        # Rewrite well-known synonyms to the canonical Tofu tool so the call
        # executes instead of dying on a hard "Unknown tool" wall. Only
        # confident 1:1 mappings are applied; anything unrecognized passes
        # through and still surfaces the honest unknown-tool error.
        if fn_name not in tool_registry:
            _resolved, _alias_kind = resolve_tool_name(fn_name)
            if _alias_kind and _resolved != fn_name:
                logger.info('[Task %s] Aliased tool name %r → %r (%s)',
                            tid, fn_name, _resolved, _alias_kind)
                _tool_name_aliased = fn_name
                fn_name = _resolved
                # Rewrite the name in-place so the persisted assistant message
                # carries the canonical tool name — otherwise replay/continue
                # would re-trigger the same alias every turn and the stored
                # tool_call name would mismatch the executed tool.
                fn_obj['name'] = _resolved
            else:
                _tool_name_aliased = None
        else:
            _tool_name_aliased = None
        # Guard against phantom tool calls: valid name but empty arguments,
        # AND another tool call with the SAME name has real arguments.
        # This avoids dropping legitimate no-arg tools
        # that appear alongside other tool calls.
        _raw_check = (fn_obj.get('arguments', '') or '').strip()
        if not _raw_check and fn_name in _names_with_real_args:
            logger.warning('[Task %s] Skipping phantom tool call %s (tc_id=%s) '
                           'with empty arguments — duplicate of another %s call '
                           'with real args',
                           tid, fn_name, tc.get('id', '?')[:12], fn_name)
            continue
        tc_id = tc.get('id') or f'call_{uuid.uuid4().hex[:12]}'
        _args_parse_error = None
        # Harness self-repair tracking — surfaced to the UI so the user knows
        # the displayed/executed args were auto-corrected from a malformed
        # model output.  ``_json_repaired`` = recovered truncated/invalid JSON;
        # ``_repair_log`` = schema-shape coercions (stringified_json, …).
        _json_repaired = False
        _repair_log = None

        # ── Parse arguments (with repair fallback) ──
        try:
            raw_args = fn_obj.get('arguments', '') or ''
            fn_args = json.loads(raw_args) if raw_args.strip() else {}
        except (json.JSONDecodeError, TypeError, KeyError) as e:
            try:
                raw = fn_obj.get('arguments', '{}')
                fn_args = _repair_json(raw if isinstance(raw, str) else '{}')
                _json_repaired = True
                # Successful repair — the LLM streamed slightly-malformed JSON
                # (common with long code blobs in write_file). _repair_json
                # recovered it and the tool call proceeds normally. Debug so
                # error.log stays clean; only unrecovered failures below
                # warrant a warning.
                logger.debug(
                    '[Task %s] conv=%s Repaired malformed JSON for tool=%s tc_id=%s at round %d: original_err=%s',
                    tid, task.get('convId', ''), fn_name, tc_id, round_num, e)

            except Exception as e:
                # Capture the raw offending argument string so the failure is
                # diagnosable after the fact. Without this, error.log records
                # only the char offset ("Expecting ':' delimiter ... char 132")
                # and the malformed text is discarded — making every
                # structural-JSON failure impossible to reconstruct.
                _raw_for_log = raw_args if isinstance(raw_args, str) else str(raw_args)
                _err_pos = getattr(e, 'pos', None)
                if isinstance(_err_pos, int):
                    _lo = max(0, _err_pos - 40)
                    _around = _raw_for_log[_lo:_err_pos + 40]
                    _window = ' near=...%r...' % _around
                else:
                    _window = ''
                logger.warning(
                    '[Task %s] conv=%s Failed to parse tool args for tool=%s tc_id=%s at round %d: %s | '
                    'raw_args(%d chars)=%.800r%s',
                    tid, task.get('convId', ''), fn_name, tc_id, round_num, e,
                    len(_raw_for_log), _raw_for_log, _window, exc_info=True)

                fn_args = {}
                _hint = schema_hint(fn_name)
                _args_parse_error = (
                    f'ERROR: Your tool call for `{fn_name}` had malformed JSON '
                    f'arguments — {e}. Please retry with valid JSON.'
                    + (f' {_hint}' if _hint else '')
                )

        # ── Schema-driven shape repair (Awais open-model-harness patterns) ──
        # Runs only when JSON parse succeeded. Repairs:
        #   null_omission, stringified_json, stringified_primitive,
        #   bare_string_to_array, empty_placeholder_unwrap.
        # Valid inputs are never touched. Tools without an indexed schema
        # pass through unchanged. See lib/tool_input_repair.py.
        if not _args_parse_error and isinstance(fn_args, dict):
            try:
                fn_args, _repair_log = validate_then_repair(
                    fn_name, fn_args,
                    model=task.get('model', '') or '',
                )
                if _repair_log:
                    logger.debug(
                        '[Task %s] tool=%s tc_id=%s: repaired %d arg(s) %s',
                        tid, fn_name, tc_id[:12], len(_repair_log), _repair_log,
                    )
            except Exception as _re:
                logger.warning(
                    '[Task %s] tool=%s tc_id=%s: input-repair pass failed (passing args through): %s',
                    tid, fn_name, tc_id[:12], _re, exc_info=True,
                )

        # ── Build a UI-facing repair summary (None when nothing was fixed) ──
        _repair_summary = _build_repair_summary(
            _json_repaired, _repair_log,
            tool_name_aliased=_tool_name_aliased, resolved_tool_name=fn_name,
        )

        # ── Check if this tool was already announced during streaming ──
        if tc_id in _early:
            rn, round_entry = _early[tc_id]
            # ★ Harness fixed this call's args AFTER the streaming early-
            #   announce already rendered the (garbled) display — patch the
            #   stale round entry so the UI shows the corrected line + badge.
            if _repair_summary:
                _apply_repair_to_round(round_entry, fn_name, fn_args, _repair_summary,
                                       project_enabled,
                                       task.get('convId') or task.get('id'))
            # ★ Attach assistantContent to the first early-announced entry
            if _assistant_content and not _ac_tagged:
                round_entry['assistantContent'] = _assistant_content
                if _assistant_thinking:
                    round_entry['thinking'] = _assistant_thinking
                if _assistant_thinking_signature:
                    round_entry['thinkingSignature'] = _assistant_thinking_signature
                _ac_tagged = True
            # ★ Preserve Gemini thought_signature (and any other vendor-specific
            #   extra_content) so the frontend can round-trip it on Continue.
            #   Gemini 3.x REQUIRES echoing the signature back on subsequent
            #   requests that replay this tool_call, else HTTP 400.  See
            #   memory gemini-thought-signature-openai-compat.
            if tc.get('extra_content'):
                round_entry['extraContent'] = tc['extra_content']
            logger.debug('[Task %s] Reusing early-announced tool_start for '
                         '%s tc_id=%s rn=%d', tid, fn_name, tc_id[:8], rn)
            # Swarm tools need extra bookkeeping
            if fn_name in SWARM_TOOL_NAMES:
                task['_swarmRoundNum'] = rn
            parsed_tcs.append((tc, fn_name, tc_id, fn_args, rn, round_entry, _args_parse_error))
            continue

        # ── Serialize args for continue context ──
        tc_args_str = json.dumps(fn_args, ensure_ascii=False) if fn_args else '{}'

        # ── Build round entry + event via dispatch-dict helper ──
        tool_round_num, round_entry, event_payload = _build_tool_round_entry(
            fn_name, fn_args, tc_id, tc_args_str,
            tool_round_num, project_enabled,
            conv_id=task.get('convId') or task.get('id'),
        )
        rn = round_entry['roundNum']
        # ★ Tag with LLM round so frontend can batch tool calls from the
        #   same assistant turn — needed for accurate Continue grouping.
        round_entry['llmRound'] = round_num
        event_payload['llmRound'] = round_num
        # ★ Harness self-repair badge — tells the user this call's arguments
        #   were auto-corrected from a malformed model output.
        if _repair_summary:
            round_entry['_repaired'] = _repair_summary
            event_payload['_repaired'] = _repair_summary
        # ★ Tag first entry with assistant content so Continue can replay it
        if _assistant_content and not _ac_tagged:
            round_entry['assistantContent'] = _assistant_content
            event_payload['assistantContent'] = _assistant_content
            if _assistant_thinking:
                round_entry['thinking'] = _assistant_thinking
                event_payload['thinking'] = _assistant_thinking
            if _assistant_thinking_signature:
                round_entry['thinkingSignature'] = _assistant_thinking_signature
                event_payload['thinkingSignature'] = _assistant_thinking_signature
            _ac_tagged = True
        # ★ Preserve Gemini thought_signature on the persisted tool round.
        #   Captured off the assistant tool_call entry by lib.llm's
        #   streaming parser (see: "Gemini thought_signature: preserve
        #   extra_content" branch in lib/llm/stream.py).  Without this,
        #   a Continue request against Gemini drops the signature and the
        #   next API call 400s.
        if tc.get('extra_content'):
            round_entry['extraContent'] = tc['extra_content']
            event_payload['extraContent'] = tc['extra_content']
        task['toolRounds'].append(round_entry)
        append_event(task, event_payload)

        # Swarm tools need extra bookkeeping for sub-agent event routing
        if fn_name in SWARM_TOOL_NAMES:
            task['_swarmRoundNum'] = rn

        parsed_tcs.append((tc, fn_name, tc_id, fn_args, rn, round_entry, _args_parse_error))

    return parsed_tcs, tool_round_num


def emit_tool_exec_phase(
    task: dict,
    parsed_tcs: list,
    *,
    event_sink: TaskEventSink | None = None,
) -> None:
    """Emit a ``phase`` event indicating which tools are about to execute.

    Builds a human-readable summary using :data:`_TOOL_EXEC_LABELS` and
    sends it as a ``tool_exec`` phase event.

    Parameters
    ----------
    task : dict
        Live task dict — event is appended.
    parsed_tcs : list[tuple]
        The parsed tool-call tuples from :func:`parse_tool_calls`.
    event_sink : TaskEventSink, optional
        Optional :class:`~lib.protocols.TaskEventSink` for dependency injection.
        When provided, ``event_sink.append_event()`` is used instead of the
        concrete ``lib.tasks_pkg.manager.append_event`` import.  Pass a mock
        for testing.  ``None`` (default) falls back to the concrete import.
    """
    tool_names_list = [item[1] for item in parsed_tcs]
    unique_tool_names = list(dict.fromkeys(tool_names_list))
    n = len(parsed_tcs)

    if n == 1:
        detail = tool_label(unique_tool_names[0])
    else:
        labeled = [tool_label(tn) for tn in unique_tool_names]
        detail = f'Executing {n} tools: {", ".join(labeled)}'

    _append = event_sink.append_event if event_sink is not None else append_event
    _append(task, build_event(
        EventType.PHASE,
        phase='tool_exec',
        detail=detail,
        tools=tool_names_list,
    ))


# ── Serial-dispatch config for long-blocking tools ──────────────────────
# These tools must run serially (outside the thread pool) because they block
# for user input or extended periods, exceeding TOOL_PARALLEL_TIMEOUT.
# Each entry: tool_name → {match: callable(fn_args) → bool, inject: dict of extra args}
_SERIAL_BLOCKING_TOOLS: dict[str, dict] = {
    'ask_human': {
        'match': lambda _args: True,
        'reason': 'blocks for user input',
    },
    'await_task': {
        'match': lambda args: args.get('action') == 'wait',
        'reason': 'long-blocking, bypasses pool timeout',
        'inject': lambda task, rn: {'_parent_task': task},
    },
    'timer_create': {
        'match': lambda _args: True,
        'reason': 'blocking poll, bypasses pool timeout',
        'inject': lambda task, rn: {'_parent_task': task, '_tool_round_num': rn},
    },
}


def _unpack_cache_entry(cached) -> tuple:
    """Unpack a dedup cache entry into (content, is_search, source, display, engine_breakdown, vertical).

    Handles all legacy tuple lengths (2–6) and bare values gracefully.
    """
    if not isinstance(cached, (tuple, list)):
        # A non-(tuple/list) entry means a buggy writer poisoned the dedup
        # cache; we still wrap it, but a str/dict becomes the model-visible
        # result verbatim while anything else is str()'d into garbage — both
        # are real defects worth surfacing, not a routine fallback.
        if isinstance(cached, (str, dict)):
            logger.debug('[Dedup] cache value is %s not tuple — wrapping', type(cached).__name__)
        else:
            logger.warning('[Dedup] cache value is unexpected type %s (not tuple/str/dict) '
                           '— wrapping; model will see str() of it', type(cached).__name__)
        return (cached, False, 'dedup', None, None, None)
    # Pad to length 6 with defaults
    defaults = (None, False, 'dedup', None, None, None)
    padded = tuple(cached) + defaults[len(cached):]
    if len(cached) < 2 or len(cached) > 6:
        logger.warning('[Dedup] cache entry has unexpected length %d', len(cached))
    return padded[:6]


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

    parallel_items = []
    for item in parsed_tcs:
        tc, fn_name, tc_id, fn_args, rn, round_entry, _parse_err = item

        # JSON parse failure → return error to LLM, skip execution
        if _parse_err:
            if round_entry:
                _finalize_tool_round(
                    task, rn, round_entry,
                    [{'type': 'error', 'content': _parse_err}],
                    query_override=round_entry.get('query', fn_name),
                )
            tool_results[tc_id] = (_parse_err, False)
            continue

        # ── Dedup check for idempotent tools ──
        if fn_name in _idempotent_tools:
            cache_key = _make_cache_key(fn_name, fn_args)
            cached = _cache.get(cache_key)
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
                        )
                        _finalize_tool_round(
                            task, rn, round_entry, [_meta],
                            query_override=round_entry.get('query', fn_name),
                        )
                tool_results[tc_id] = (dedup_content, cached_is_search)
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

        if needs_approval:
            approved, reject_content = _handle_approval(
                task, fn_name, fn_args, rn, round_entry, project_path, round_num, model)
            if not approved:
                tool_results[tc_id] = (reject_content, False)
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
            tc_id_ret, tool_content, is_search = _execute_tool_one(
                task, tc, fn_name, tc_id, fn_args, rn, round_entry,
                cfg, project_path, project_enabled,
                all_tools=tool_list,
            )
            tool_results[tc_id_ret] = (tool_content, is_search)
            logger.info('[Task %s] %s serial dispatch completed at round %d '
                        '(result_len=%d)', tid, fn_name, round_num, len(str(tool_content)))
            continue

        # ── Pre-tool hooks: validate/block/modify before execution ──
        # Inspired by Claude Code's PreToolUse hooks.
        _hook_result = run_pre_hooks(fn_name, fn_args, task)
        if _hook_result and _hook_result.action == 'block':
            logger.info('[Task %s] Pre-hook BLOCKED tool %s: %s',
                        tid, fn_name, _hook_result.message)
            tool_results[tc_id] = (
                f'Tool blocked by pre-execution hook: {_hook_result.message}',
                False,
            )
            continue

        parallel_items.append(item)

    # ══════════════════════════════════════════
    #  Write-tool serial phase (concurrency safety)
    #  Inspired by Claude Code's isConcurrencySafe partitioning:
    #  write tools run serially to prevent filesystem race conditions.
    # ══════════════════════════════════════════
    _serial_write_items = [
        item for item in parallel_items if item[1] in _write_tools
    ]
    parallel_items = [
        item for item in parallel_items if item[1] not in _write_tools
    ]
    for item in _serial_write_items:
        tc, fn_name, tc_id, fn_args, rn, round_entry, _pe = item
        if task.get('aborted'):
            logger.info('[Task %s] Skipping serial write tool %s — task aborted', tid, fn_name)
            tool_results[tc_id] = ('Task aborted by user.', False)
            continue
        logger.debug('[Task %s] Serial write dispatch: %s at round %d', tid, fn_name, round_num)
        tc_id_ret, tool_content, is_search = _execute_tool_one(
            task, tc, fn_name, tc_id, fn_args, rn, round_entry,
            cfg, project_path, project_enabled,
            all_tools=tool_list,
        )
        tool_results[tc_id_ret] = (tool_content, is_search)
        _invalidate_project_cache(_cache, trigger=fn_name)

    # ══════════════════════════════════════════
    #  Main phase: Parallel execution (read-only tools)
    # ══════════════════════════════════════════
    if parallel_items:
        # ── Abort check before spawning parallel pool ──
        if task.get('aborted'):
            logger.info('[Task %s] Skipping %d parallel tools — task aborted', tid, len(parallel_items))
            for tc, fn_name, tc_id, fn_args, rn, round_entry, _pe in parallel_items:
                tool_results[tc_id] = ('Task aborted by user.', False)
            parallel_items = []  # skip the pool entirely

    if parallel_items:
        max_parallel = int(os.environ.get('TOOL_MAX_PARALLEL_WORKERS', '16'))
        max_workers = min(max_parallel, len(parallel_items))
        pool = ThreadPoolExecutor(max_workers=max_workers)
        _timed_out = False
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
            tool_timeout = int(os.environ.get('TOOL_PARALLEL_TIMEOUT', '300'))
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
                                    _cache[_pi_cache_key] = (tool_content, is_search, 'dedup', _pi_display, _pi_eng_bkdn, _pi_vert)
                                    break
                        # ── Invalidate project cache after write/exec ops ──
                        elif fut_fn_name in ('write_file', 'apply_diff', 'apply_diffs',
                                             'insert_content', 'insert_contents',
                                             'create_project',
                                             'code_exec', 'bash_exec', 'run_command'):
                            _invalidate_project_cache(_cache, trigger=fut_fn_name)
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
            except TimeoutError:
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
                    else:
                        fut.cancel()
                        tool_results[fut_tc_id] = (f'Tool execution timed out: {fut_fn_name}', False)
        finally:
            # On timeout use wait=False + cancel_futures=True to avoid
            # blocking indefinitely on still-running tool threads.
            # On normal completion wait=True is fine (all futures done).
            pool.shutdown(wait=not _timed_out, cancel_futures=_timed_out)

    # ══════════════════════════════════════════
    #  Post-phase: Add tool messages in original order
    # ══════════════════════════════════════════
    _round_results_for_budget: list[tuple[str, str, str]] = []  # (tc_id, content, tool_name)
    for tc, fn_name, tc_id, fn_args, rn, round_entry, _pe in parsed_tcs:
        tool_content, is_search = tool_results.get(tc_id, (f'Unknown tool: {fn_name}', False))
        if is_search:
            all_search_results_text.append(tool_content)

        # Convert screenshot dict → image_url content block for vision models
        if isinstance(tool_content, dict) and tool_content.get('__screenshot__'):
            _active_model = task.get('model', '') if task else ''
            if _active_model and not model_supports_vision(_active_model):
                # Text-only model: never build an image_url block (build_body
                # would strip it later and leave a misleading "analyze it
                # visually" text). Instead, return a truthful tool result AT
                # the tool-call site so the model knows the image is unreadable
                # and stops re-rendering / re-reading images.
                tc_content_str = (
                    '[Image not shown — the current model (%s) has no vision '
                    'support, so this image cannot be analyzed. Do not retry '
                    'reading images; rely on text, code, and test output '
                    'instead.]' % _active_model)
                messages.append({'role': 'tool', 'tool_call_id': tc_id,
                                 'content': tc_content_str})
                logger.info(
                    '[Task %s] conv=%s text-only model %s — image tool result '
                    'for tc=%s replaced with no-vision placeholder',
                    tid, task.get('convId', '') if task else '', _active_model, tc_id)
                if round_entry:
                    round_entry['toolContent'] = tc_content_str
                try:
                    append_event(task, build_event(
                        EventType.TOOL_COMPLETE,
                        roundNum=rn,
                        toolCallId=tc_id,
                        toolName=fn_name,
                        toolContent=tc_content_str,
                    ))
                except Exception as e:
                    logger.warning(
                        '[Task %s] tool_complete event error for tool=%s at round %d (non-fatal): %s',
                        tid, fn_name, round_num, e, exc_info=True)
                continue
            _append_screenshot_message(messages, tc_id, tool_content)
            # Emit tool_complete for screenshot with text fallback
            try:
                tc_content_str = tool_content.get('_text_fallback', '') or 'Image captured.'
                if round_entry:
                    round_entry['toolContent'] = tc_content_str
                append_event(task, build_event(
                    EventType.TOOL_COMPLETE,
                    roundNum=rn,
                    toolCallId=tc_id,
                    toolName=fn_name,
                    toolContent=tc_content_str,
                ))
            except Exception as e:
                logger.warning(
                    '[Task %s] tool_complete event error for tool=%s at round %d (non-fatal): %s',
                    tid, fn_name, round_num, e, exc_info=True)
        else:
            # ★ Post-tool hooks: modify/enrich result after execution.
            # Inspired by Claude Code's PostToolUse hooks.
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

            # Collect for aggregate budget check
            _round_results_for_budget.append((tc_id, tool_content, fn_name))

            messages.append({'role': 'tool', 'tool_call_id': tc_id, 'content': tool_content})

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

                _evt = build_event(
                    EventType.TOOL_COMPLETE,
                    roundNum=rn,
                    toolCallId=tc_id,
                    toolName=fn_name,
                    toolContent=tc_content_str,
                )
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

    # Emit snapshot AFTER tool results appended
    try:
        snapshot = _strip_base64_for_snapshot(messages)
        snap_evt = build_event(
            EventType.MESSAGES_SNAPSHOT,
            round=round_num + 1,
            label=f'Round {round_num + 1} 工具结果后 · {len(messages)}条',
            messages=snapshot,
        )
        if tool_list:
            snap_evt['tools'] = tool_list
        append_event(task, snap_evt)
    except Exception:
        logger.warning(
            '[Task %s] messages_snapshot post-tool failed at round %d model=%s',
            tid, round_num + 1, model, exc_info=True)

    return _pipeline_timed_out


# ── Private helpers ────────────────────────────────────────────────────


# ── Approval metadata enrichers ────────────────────────────────────────
# Registry pattern: each tool type that needs approval has a dedicated
# function that enriches the base ``approval_meta`` dict.

def _approval_meta_run_command(approval_meta, fn_args):
    """Enrich approval metadata for ``run_command``."""
    approval_meta['command'] = fn_args.get('command', '')
    approval_meta['description'] = fn_args.get('description', '')
    approval_meta['path'] = fn_args.get('working_dir', '') or ''


def _approval_meta_write_file(approval_meta, fn_args):
    """Enrich approval metadata for ``write_file``."""
    content = fn_args.get('content', '')
    approval_meta['contentPreview'] = content[:500] + ('…' if len(content) > 500 else '')
    approval_meta['contentLines'] = content.count('\n') + 1
    approval_meta['contentChars'] = len(content)


def _approval_meta_apply_diff(approval_meta, fn_args):
    """Enrich approval metadata for ``apply_diff`` (single edit)."""
    search_text = fn_args.get('search', '')
    replace_text = fn_args.get('replace', '')
    approval_meta['search'] = search_text[:2000] + ('…' if len(search_text) > 2000 else '')
    approval_meta['replace'] = replace_text[:2000] + ('…' if len(replace_text) > 2000 else '')
    approval_meta['searchLines'] = search_text.count('\n') + 1
    approval_meta['searchChars'] = len(search_text)
    approval_meta['replaceLines'] = replace_text.count('\n') + 1
    approval_meta['replaceChars'] = len(replace_text)
    if fn_args.get('replace_all'):
        approval_meta['replaceAll'] = True


def _approval_meta_apply_diffs(approval_meta, fn_args):
    """Enrich approval metadata for ``apply_diffs`` (batch)."""
    edits = fn_args.get('edits') or []
    paths = list(dict.fromkeys(
        e.get('path', '?') for e in edits if isinstance(e, dict)
    ))
    approval_meta['path'] = (
        ', '.join(paths[:5])
        + (f' +{len(paths)-5} more' if len(paths) > 5 else '')
    )
    approval_meta['editCount'] = len(edits)
    approval_meta['batchMode'] = True
    approval_meta['description'] = f'Batch: {len(edits)} edits across {len(paths)} file(s)'
    edit_summaries = []
    for e in edits[:20]:
        if not isinstance(e, dict):
            continue
        s_text = e.get('search', '')
        r_text = e.get('replace', '')
        edit_summaries.append({
            'path': e.get('path', '?'),
            'description': e.get('description', ''),
            'search': s_text[:500] + ('…' if len(s_text) > 500 else ''),
            'replace': r_text[:500] + ('…' if len(r_text) > 500 else ''),
            'searchLines': s_text.count('\n') + 1,
            'replaceLines': r_text.count('\n') + 1,
        })
    approval_meta['editSummaries'] = edit_summaries


def _approval_meta_insert_content(approval_meta, fn_args):
    """Enrich approval metadata for ``insert_content`` (single insertion)."""
    anchor_text = fn_args.get('anchor', '')
    content_text = fn_args.get('content', '')
    pos = fn_args.get('position', 'after')
    approval_meta['search'] = anchor_text[:2000] + ('…' if len(anchor_text) > 2000 else '')
    approval_meta['replace'] = content_text[:2000] + ('…' if len(content_text) > 2000 else '')
    approval_meta['searchLines'] = anchor_text.count('\n') + 1
    approval_meta['searchChars'] = len(anchor_text)
    approval_meta['replaceLines'] = content_text.count('\n') + 1
    approval_meta['replaceChars'] = len(content_text)
    approval_meta['description'] = approval_meta.get('description', '') or f'Insert {pos} anchor'


def _approval_meta_insert_contents(approval_meta, fn_args):
    """Enrich approval metadata for ``insert_contents`` (batch)."""
    edits = fn_args.get('edits') or []
    paths = list(dict.fromkeys(
        e.get('path', '?') for e in edits if isinstance(e, dict)
    ))
    approval_meta['path'] = (
        ', '.join(paths[:5])
        + (f' +{len(paths)-5} more' if len(paths) > 5 else '')
    )
    approval_meta['editCount'] = len(edits)
    approval_meta['batchMode'] = True
    approval_meta['description'] = f'Batch: {len(edits)} insertions across {len(paths)} file(s)'
    edit_summaries = []
    for e in edits[:20]:
        if not isinstance(e, dict):
            continue
        anchor_text = e.get('anchor', '')
        content_text = e.get('content', '')
        pos = e.get('position', 'after')
        edit_summaries.append({
            'path': e.get('path', '?'),
            'description': e.get('description', f'Insert {pos} anchor'),
            'search': anchor_text[:500] + ('…' if len(anchor_text) > 500 else ''),
            'replace': content_text[:500] + ('…' if len(content_text) > 500 else ''),
            'searchLines': anchor_text.count('\n') + 1,
            'replaceLines': content_text.count('\n') + 1,
        })
    approval_meta['editSummaries'] = edit_summaries


def _approval_meta_create_project(approval_meta, fn_args):
    """Enrich approval metadata for ``create_project``."""
    target_path = fn_args.get('path', '')
    root_name = fn_args.get('name', '')
    overwrite = bool(fn_args.get('overwrite', False))
    approval_meta['path'] = target_path
    approval_meta['rootName'] = root_name
    approval_meta['overwrite'] = overwrite
    if overwrite:
        approval_meta['description'] = (
            f'Create / register workspace root at {target_path} '
            f'(overwrite=true — existing non-empty dir will be registered as-is)'
        )
    else:
        approval_meta['description'] = f'Create new workspace root at {target_path}'


# Module-level dispatch table — maps tool name → approval meta enricher.
# Only tools that need special approval metadata are listed; tools not in
# this dict get the base metadata only (path + description).
_APPROVAL_META_ENRICHERS = {
    'run_command':      _approval_meta_run_command,
    'write_file':       _approval_meta_write_file,
    'apply_diff':       _approval_meta_apply_diff,
    'apply_diffs':      _approval_meta_apply_diffs,
    'insert_content':   _approval_meta_insert_content,
    'insert_contents':  _approval_meta_insert_contents,
    'create_project':   _approval_meta_create_project,
}


def _handle_approval(
    task: dict[str, Any],
    fn_name: str,
    fn_args: dict[str, Any],
    rn: int,
    round_entry: dict[str, Any],
    project_path: str | None,
    round_num: int,
    model: str,
) -> tuple[bool, str | None]:
    """Gate a write operation on manual user approval (no execution).

    Emits a ``write_approval_request`` event and blocks waiting for the user
    response. This function ONLY decides — it never executes the tool. On
    approval the caller lets the tool fall through to the normal serial
    write-tool dispatch, so a single execution path serves project writes,
    run_command, memory, MCP, and custom write tools.

    Uses the :data:`_APPROVAL_META_ENRICHERS` dispatch table to build
    tool-specific approval metadata.

    Returns
    -------
    tuple[bool, str | None]
        ``(approved, reject_content)``. When approved, ``(True, None)`` and
        ``round_entry`` is left ``pending_approval`` for the executor to
        finalize. When rejected, ``(False, message)`` and the round has
        already been finalized with a ``rejected`` badge.
    """
    tid = task['id'][:8]
    approval_id = f'{task["id"]}_{uuid.uuid4().hex[:8]}'
    approval_meta = {
        'approvalId': approval_id,
        'toolName': fn_name,
        'path': fn_args.get('path', ''),
        'description': fn_args.get('description', ''),
    }

    # Dispatch to tool-specific enricher (if one exists)
    enricher = _APPROVAL_META_ENRICHERS.get(fn_name)
    if enricher is not None:
        enricher(approval_meta, fn_args)

    round_entry['status'] = 'pending_approval'
    round_entry['approvalId'] = approval_id
    round_entry['approvalMeta'] = approval_meta
    append_event(task, build_event(
        EventType.WRITE_APPROVAL_REQUEST,
        roundNum=rn,
        toolCallId=round_entry.get('toolCallId', ''),
        approvalId=approval_id,
        meta=approval_meta,
    ))
    logger.debug(
        '[Task %s] Waiting for write approval: tool=%s path=%s round=%d model=%s',
        tid, fn_name, fn_args.get('path', ''), round_num, model,
    )

    approved = request_write_approval(approval_id, timeout=120)

    if not approved:
        tool_content = f'⚠️ User rejected this {fn_name} operation on {fn_args.get("path", "")}.'
        meta = build_project_tool_meta(fn_name, fn_args, tool_content)
        meta['badge'] = 'rejected'
        meta['writeOk'] = False
        _finalize_tool_round(task, rn, round_entry, [meta])
        return False, tool_content

    # Approved — do NOT execute here. The caller lets the item fall through to
    # the normal serial write-tool dispatch (_execute_tool_one), so a single
    # execution path serves project writes, run_command, memory, MCP, and custom
    # write tools uniformly. round_entry stays 'pending_approval' until that
    # execution finalizes it.
    logger.info('[Task %s] Write approved: tool=%s — dispatching via normal path',
                tid, fn_name)
    return True, None


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
