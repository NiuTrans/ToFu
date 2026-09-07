# HOT_PATH
"""Tool partitions + result reuse and provider-response call identity.

Houses the stateless tool-partition tables (write vs result reuse), per-task
partition union, the distinct retry-idempotency contract, deterministic cache
keys, project-cache invalidation, cache entry helpers, and the canonical
signature/scope builders shared by direct and gateway occurrences from one
provider response.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any

from lib.log import get_logger
from lib.token_counter import count_text
from lib.tools.resource_policy import (
    TOOL_RESULT_CACHE_HARD_CAPACITY,
    tool_result_cache_capacity,
)
logger = get_logger(__name__)


_TOOL_RESULT_CACHE_MIN_CAPACITY = 16


def _call_id_signature(fn_name: str, fn_args: Any) -> str:
    """Stable name+argument identity; never trusts display metadata."""
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


def _execute_gateway_delegation_scope(
    task: dict[str, Any], round_num: int,
) -> str:
    """Identity of one provider response for direct↔gateway delegation."""
    attempt_id = str(
        task.get('_attemptId') or task.get('attemptId') or task.get('id') or '')
    world_version = str(
        task.get('_worldVersion') or task.get('worldVersion') or '')
    return f'{attempt_id}\0{round_num}\0{world_version}'



def _publish_execute_gateway_direct_siblings(
    task: dict[str, Any],
    round_num: int,
    direct_calls: list[tuple[str, str, dict[str, Any]]],
) -> None:
    """Publish the runnable direct-call multiset for one provider response.

    Each signature retains provider order and occurrence cardinality. Gateway
    handlers consume IDs FIFO so one direct occurrence delegates at most one
    identical ``execute_tools.calls[]`` child; same-channel duplicates remain
    independent model actions.
    """
    by_signature: dict[str, list[str]] = {}
    for name, call_id, arguments in direct_calls:
        if not name or not call_id:
            continue
        signature = _call_id_signature(name, arguments)
        by_signature.setdefault(signature, []).append(str(call_id))
    task['_execute_gateway_direct_siblings'] = {
        'scope': _execute_gateway_delegation_scope(task, round_num),
        'by_signature': by_signature,
    }


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

# ── Result dedup — cache explicitly reusable results within a task ──
# These tools produce the same result for the same arguments within one task
# execution.  When the model repeats a call, we return the cached result
# instantly instead of re-executing (e.g. re-fetching a URL).
#
# Every production writer must use ``_store_tool_result_cache_entry``.  The
# per-task FIFO bound is launch-probed and the terminal lifecycle owner drops
# the whole cache after settlement.  Cache pressure may therefore cause a
# safe live re-execution, never retention that grows with an arbitrarily long
# task or with the one-hour terminal TaskRuntime TTL.
#
# The literal base below covers built-in tools (incl. browser-internal names
# that the ToolSpec registry doesn't enumerate).  We then union the
# ``cacheable_tools`` policy declared by every registered ToolSpec. Legacy
# plugins that omit it retain the historical idempotent=>cacheable default.
_CACHEABLE_TOOLS_BASE = frozenset({
    'web_search', 'fetch_url',
    'read_files', 'list_dir', 'grep_search', 'find_files',
})


def _resolve_tool_result_cache_capacity(
    environment: dict[str, str] | None = None,
) -> int:
    """Resolve the per-task receipt count from the shared resource policy."""
    try:
        return tool_result_cache_capacity(environment)
    except Exception as exc:
        # Import/probe failure must not break tool execution.  This is the
        # same lean floor accepted for an explicit operator override; normal
        # probe failure resolves to the resource policy's 64-entry default.
        logger.warning(
            '[DedupCache] capacity resolution failed; using %d: %s',
            _TOOL_RESULT_CACHE_MIN_CAPACITY, exc)
        return _TOOL_RESULT_CACHE_MIN_CAPACITY


def _record_tool_result_cache_evictions(
    task: dict[str, Any], evicted: int, capacity: int,
) -> None:
    """Record content-free pressure evidence without logging every eviction."""
    if evicted <= 0:
        return
    try:
        previous = max(0, int(task.get('_tool_result_cache_evictions') or 0))
    except (TypeError, ValueError, OverflowError):
        previous = 0
    total = min(1_000_000_000, previous + evicted)
    task['_tool_result_cache_evictions'] = total
    if total == 1 or total & (total - 1) == 0:
        logger.info(
            '[DedupCache] task=%s evicted=%d total_evictions=%d capacity=%d',
            str(task.get('id') or '')[:12] or '?', evicted, total, capacity)


def _trim_tool_result_cache(
    task: dict[str, Any], cache: dict, capacity: int,
) -> int:
    """Drop oldest receipts until ``cache`` satisfies its finite capacity."""
    evicted = 0
    while len(cache) > capacity:
        oldest_key = next(iter(cache))
        cache.pop(oldest_key, None)
        evicted += 1
    _record_tool_result_cache_evictions(task, evicted, capacity)
    return evicted


def _ensure_tool_result_cache(task: dict[str, Any]) -> dict:
    """Return the task cache, repairing legacy shape and enforcing its cap."""
    cache = task.get('_tool_result_cache')
    if not isinstance(cache, dict):
        if cache is not None:
            logger.warning(
                '[DedupCache] task=%s replaced invalid cache type=%s',
                str(task.get('id') or '')[:12] or '?',
                type(cache).__name__)
        cache = {}
        task['_tool_result_cache'] = cache

    capacity = task.get('_tool_result_cache_capacity')
    try:
        capacity = int(capacity)
    except (TypeError, ValueError, OverflowError):
        capacity = 0
    if not (_TOOL_RESULT_CACHE_MIN_CAPACITY
            <= capacity <= TOOL_RESULT_CACHE_HARD_CAPACITY):
        capacity = _resolve_tool_result_cache_capacity()
        task['_tool_result_cache_capacity'] = capacity
    _trim_tool_result_cache(task, cache, capacity)
    return cache


def _store_tool_result_cache_entry(
    task: dict[str, Any], cache_key: str, cache_entry: Any,
) -> dict:
    """Insert one receipt as newest and enforce the task-local FIFO bound."""
    cache = _ensure_tool_result_cache(task)
    # Replacing an existing receipt refreshes its FIFO age.  This matters for
    # the prefetch-to-budgeted rewrite: the final compact form is the live
    # receipt, not an old entry that should be first under pressure.
    if cache_key in cache:
        cache.pop(cache_key, None)
    cache[cache_key] = cache_entry
    capacity = int(task['_tool_result_cache_capacity'])
    _trim_tool_result_cache(task, cache, capacity)
    return cache


def _record_tool_call_id_receipt_evictions(
    task: dict[str, Any], evicted: int, capacity: int,
) -> None:
    """Record content-free call-id ledger pressure at logarithmic cadence."""
    if evicted <= 0:
        return
    try:
        previous = max(
            0, int(task.get('_tool_call_id_receipt_evictions') or 0))
    except (TypeError, ValueError, OverflowError):
        previous = 0
    total = min(1_000_000_000, previous + evicted)
    task['_tool_call_id_receipt_evictions'] = total
    if total == 1 or total & (total - 1) == 0:
        logger.info(
            '[CallIdReceipts] task=%s evicted=%d total_evictions=%d '
            'capacity=%d',
            str(task.get('id') or '')[:12] or '?', evicted, total, capacity)


def _ensure_tool_call_id_receipts(task: dict[str, Any]) -> dict[str, dict]:
    """Return a bounded, content-free completed-call identity ledger.

    The ledger is diagnostic/recycle metadata, never replay authority. Call-id
    collision correctness comes from the active history index in the pipeline,
    so pressure may safely discard the oldest signature receipt.
    """
    receipts = task.get('_tool_call_id_receipts')
    if not isinstance(receipts, dict):
        if receipts is not None:
            logger.warning(
                '[CallIdReceipts] task=%s replaced invalid ledger type=%s',
                str(task.get('id') or '')[:12] or '?',
                type(receipts).__name__)
        receipts = {}
        task['_tool_call_id_receipts'] = receipts

    # Repair live/rehydrated legacy rows that pinned a complete tool body.
    invalid_keys = []
    for call_id, receipt in receipts.items():
        if not isinstance(receipt, dict):
            invalid_keys.append(call_id)
            continue
        receipts[call_id] = {
            'signature': str(receipt.get('signature') or ''),
            'name': str(receipt.get('name') or ''),
            'status': str(receipt.get('status') or 'done'),
        }
    for call_id in invalid_keys:
        receipts.pop(call_id, None)

    _ensure_tool_result_cache(task)
    capacity = int(task['_tool_result_cache_capacity'])
    evicted = len(invalid_keys)
    while len(receipts) > capacity:
        receipts.pop(next(iter(receipts)), None)
        evicted += 1
    _record_tool_call_id_receipt_evictions(task, evicted, capacity)
    return receipts


def _store_tool_call_id_receipt(
    task: dict[str, Any], call_id: str, receipt: dict[str, Any],
) -> dict[str, dict]:
    """Store one newest call identity without retaining model/tool content."""
    receipts = _ensure_tool_call_id_receipts(task)
    normalized = {
        'signature': str(receipt.get('signature') or ''),
        'name': str(receipt.get('name') or ''),
        'status': str(receipt.get('status') or 'done'),
    }
    call_id = str(call_id or '')
    if not call_id:
        return receipts
    receipts.pop(call_id, None)
    receipts[call_id] = normalized
    capacity = int(task['_tool_result_cache_capacity'])
    evicted = 0
    while len(receipts) > capacity:
        receipts.pop(next(iter(receipts)), None)
        evicted += 1
    _record_tool_call_id_receipt_evictions(task, evicted, capacity)
    return receipts

# ── Concurrency safety partitioning ──
# Inspired by Claude Code's isConcurrencySafe flag per tool.
# Write tools run SERIALLY (even when auto_apply=True) to prevent
# filesystem race conditions.  Read-only tools run in parallel.
# This is separate from _CACHEABLE_TOOLS (dedup) — a tool can be
# concurrent-safe (run in parallel) but not idempotent (don't cache).
_WRITE_TOOLS_BASE = frozenset({
    'write_file', 'edit_file', 'apply_diff', 'apply_diffs',
    'insert_content', 'insert_contents',
    'run_command',
    'create_memory', 'update_memory', 'delete_memory', 'merge_memories',
})


def _registry_tool_flags() -> tuple[frozenset, frozenset]:
    """Union the literal base sets with ToolSpec-declared flags.

    Keeps the concurrency/dedup partitions in sync with the declarative tool
    registry (incl. third-party plugins) without a second hand-maintained
    list.  Falls back to the base sets if the registry import fails.
    """
    write = set(_WRITE_TOOLS_BASE)
    cacheable = set(_CACHEABLE_TOOLS_BASE)
    try:
        from lib.tools.registry import all_specs
        for spec in all_specs():
            write |= set(spec.write_tools)
            declared = spec.cacheable_tools
            cacheable |= set(
                spec.idempotent_tools if declared is None else declared)
    except Exception as e:
        logger.debug('[tool_dispatch] registry flag union skipped: %s', e)
    return frozenset(write), frozenset(cacheable)


def _registry_confirmation_tools() -> frozenset[str]:
    """Resolve ToolSpec always-confirm writes live for each task."""
    names: set[str] = set()
    try:
        from lib.tools.registry import all_specs
        for spec in all_specs():
            names |= set(spec.confirmation_tools)
    except Exception as exc:
        logger.debug('[tool_dispatch] confirmation flag union skipped: %s', exc)
    return frozenset(names)


def _registry_idempotent_tools() -> frozenset[str]:
    """Return retry-safe names without conflating them with result reuse."""
    # Literal legacy/internal reads (not all have a visible ToolSpec) retain
    # their established retry contract. Registered families then add the
    # authoritative idempotent declaration even when cacheable_tools is empty.
    names = set(_CACHEABLE_TOOLS_BASE)
    try:
        from lib.tools.registry import all_specs

        for spec in all_specs():
            names.update(spec.idempotent_tools)
    except Exception as exc:
        logger.debug('[tool_dispatch] registry idempotency union skipped: %s',
                     exc)
    return frozenset(names)


def _task_confirmation_tools(task: dict[str, Any]) -> frozenset[str]:
    """Task-local confirmation partition, including reviewed custom tools."""
    names = set(_registry_confirmation_tools())
    env = task.get('_tool_env')
    if env is not None:
        try:
            names |= set(getattr(env, 'confirmation_names', ()))
        except Exception as exc:
            logger.debug('[tool_dispatch] task confirmation union skipped: %s',
                         exc)
    return frozenset(names)


_WRITE_TOOLS, _CACHEABLE_TOOLS = _registry_tool_flags()
# Compatibility export for callers/tests written before cache policy was
# separated from the idempotency contract. Its value is the cache partition.
_IDEMPOTENT_TOOLS = _CACHEABLE_TOOLS
_INITIAL_WRITE_TOOLS = _WRITE_TOOLS
_INITIAL_IDEMPOTENT_TOOLS = _IDEMPOTENT_TOOLS


def _task_partitions(task: dict[str, Any]) -> tuple[frozenset, frozenset]:
    """Per-task write/result-reuse partitions plus the task's custom env.

    Registry flags are resolved live so a late plugin registration or hot
    replacement cannot leave a stale concurrency/cache partition. Per-request
    custom tools (``task['_tool_env']``) add their own flags afterward.
    """
    live_write, live_idem = _registry_tool_flags()
    # An explicit owner-module override is useful for isolated policy tests;
    # otherwise the live registry prevents removed plugin flags surviving in
    # an import-time snapshot.
    write = set(
        live_write if _WRITE_TOOLS is _INITIAL_WRITE_TOOLS else _WRITE_TOOLS)
    idem = set(
        live_idem if _IDEMPOTENT_TOOLS is _INITIAL_IDEMPOTENT_TOOLS
        else _IDEMPOTENT_TOOLS)
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


def _task_idempotent_tools(task: dict[str, Any]) -> frozenset[str]:
    """Return the task's retry-safe read contract for semantic consumers.

    Unlike :func:`_task_partitions`, this deliberately includes mutable
    observers that must execute fresh. Loop/progress guards need to recognize
    those calls as reads without accidentally making their results reusable.
    """
    names = set(_registry_idempotent_tools())
    env = task.get('_tool_env')
    if env is not None:
        try:
            names.update(env.idempotent_names)
        except Exception as exc:
            logger.debug(
                '[tool_dispatch] task idempotency union skipped: %s', exc)
    return frozenset(names)


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


def _unpack_cache_entry(cached) -> tuple:
    """Unpack a dedup cache entry into (content, is_search, source, display,
    engine_breakdown, vertical, search_diag).

    Handles all legacy tuple lengths (2–9) and bare values gracefully.
    ``search_diag`` (slot 7) carries the orchestrator's zero-result
    diagnostic (``reason`` / ``engine_errors`` / …) so a cache/prefetch hit
    of a FAILED search renders the honest network-error/no-matches row
    instead of a fabricated single "result".
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
        return (cached, False, 'dedup', None, None, None, None)
    # Pad to length 7 with defaults. Additive sidecars in slots 8–9 are handled
    # separately so callers that unpack exactly seven values remain compatible.
    defaults = (None, False, 'dedup', None, None, None, None)
    padded = tuple(cached) + defaults[len(cached):]
    if len(cached) < 2 or len(cached) > 9:
        logger.warning('[Dedup] cache entry has unexpected length %d', len(cached))
    return padded[:7]


def _cache_entry_projection_items(cached):
    """Return optional bounded model-projection items from cache slot 8.

    The slot is additive: old cache entries remain valid, and the ordinary
    seven-field cache unpacking API does not change shape.
    """
    if isinstance(cached, (tuple, list)) and len(cached) >= 8:
        items = cached[7]
        return items if isinstance(items, list) else None
    return None


def _cache_entry_producer_metadata(cached):
    """Return optional producer omission evidence from cache slot 9."""
    if isinstance(cached, (tuple, list)) and len(cached) >= 9:
        metadata = cached[8]
        return metadata if isinstance(metadata, dict) else None
    return None


def _build_cache_hit_meta(
    fn_name: str,
    fn_args: dict[str, Any],
    cached_content,
    is_prefetch: bool,
    cached_display=None,
) -> dict[str, Any]:
    """Build tool-specific display metadata for a cache/prefetch hit.

    The generic ``_build_simple_meta`` lacks fields the frontend needs for
    rich rendering (e.g. ``url`` for fetch_url, proper title/snippet for
    web_search).  This helper builds metadata that matches what the normal
    tool handler would produce, so the UI shows the same preview regardless
    of whether the result was freshly executed or served from cache.

    ``cached_display`` carries tool-specific rich display state memoized at
    store time. For read_files/inspect_image it is the merged inline-render
    descriptor list (``imageDataUris`` — images AND SVG source data URIs), so
    a dedup replay renders identically to the fresh read.
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

    # ── read_files SVG text hit: reattach the inline-render URIs ──
    # SVG source caches as a plain str (its markup rides the model stream),
    # so the fresh path's out-of-band ``imageDataUris`` are memoized in
    # ``cached_display``. Reattach them so the dedup replay renders the
    # vector image, not a bare text row.
    if (fn_name in ('read_files', 'inspect_image')
            and isinstance(cached_display, list) and cached_display):
        svg_uris = [d for d in cached_display if isinstance(d, dict) and d.get('uri')]
        if svg_uris:
            chars = len(cached_content) if isinstance(cached_content, str) else 0
            source_label = 'Prefetch' if is_prefetch else 'Cache'
            badge_suffix = '' if is_prefetch else ' (cached)'
            n = len(svg_uris)
            filename = os.path.basename(fn_args.get('path', '') or '')
            return {
                'toolName': fn_name,
                'title': (f'{n} images' if n > 1 else (filename or 'image')),
                'snippet': f'{chars:,} chars',
                'source': source_label, 'fetched': True,
                'fetchedChars': chars,
                'badge': f'svg{badge_suffix}',
                'imageDataUris': svg_uris,
            }

    # Imported lazily: executor/__init__ imports the handler registry, which
    # imports this module — a top-level import here closes the cycle whenever
    # _flags is the first of the two to be imported.
    from lib.tasks_pkg.executor import _build_simple_meta

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

    # ── web_search: NO synthetic meta here ──
    # A web_search cache hit is finalized upstream (_pipeline.py) from the
    # stored display_results / search_diag — including the 0-result case,
    # which renders the honest diagnostic row. The synthetic single meta
    # this branch used to build ('Search: {query}' + fetched:True + N chars)
    # fabricated a "1 result ✓ fetched" row out of what was actually a
    # FAILED search's model-facing diagnostic text.

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
