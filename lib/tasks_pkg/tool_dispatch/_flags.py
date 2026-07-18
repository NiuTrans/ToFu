# HOT_PATH
"""Tool partitions + dedup cache — write/idempotent classification and caching.

Houses the stateless tool-partition tables (write vs idempotent),
per-task partition union, the deterministic cache-key builder, project-cache
invalidation, cache-entry unpacking, and the rich cache-hit display metadata.
"""

from __future__ import annotations

import json
import os
from typing import Any

from lib.log import get_logger
from lib.token_counter import count_text
from lib.tasks_pkg.executor import _build_simple_meta

logger = get_logger(__name__)


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
    'project_charter_read', 'project_board_read',
    'project_peer_status',
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
    # Resolve the module-level partitions through the FACADE so a test that
    # patches ``tool_dispatch._WRITE_TOOLS`` / ``._IDEMPOTENT_TOOLS`` on the
    # package is honoured at call time (byte-identical to the pre-split
    # single-module behaviour). Falls back to this module's own globals.
    try:
        import lib.tasks_pkg.tool_dispatch as _facade
        _wt = getattr(_facade, '_WRITE_TOOLS', _WRITE_TOOLS)
        _it = getattr(_facade, '_IDEMPOTENT_TOOLS', _IDEMPOTENT_TOOLS)
    except Exception as e:
        logger.debug('[tool_dispatch] facade partition resolve failed, using local: %s', e)
        _wt, _it = _WRITE_TOOLS, _IDEMPOTENT_TOOLS
    write = set(_wt)
    idem = set(_it)
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
