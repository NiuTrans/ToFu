"""Disk persistence for oversized tool results — format-aware splitters.

When a tool result exceeds its per-tool budget (Layer 0), instead of
irreversibly truncating to head+tail, we write the full content to disk
and return a preview + file path the model can ``read_files`` if needed.

Five tools get format-aware splitters that fan out into one file per
result instead of one giant blob:

  * ``web_search``  → one file per search hit
  * ``grep_search`` → one file per matched source file
  * ``find_files``  → one file per batched search section
  * ``fetch_url``   → one file per URL
  * (default)       → single-file persistence

This sub-package preserves the ``lib.tasks_pkg.compaction._persist`` import
path byte-identically: every symbol previously defined in the ``_persist.py``
module is re-exported here. Implementations live in:

  * ``_helpers``   — ``_short_id`` / ``_human_size`` / ``_first_meaningful_line``
                     / ``_sanitize_filename`` / ``_truncate_head_tail`` +
                     the ``_VERT_BLOCK_RE`` / ``_DECORATIVE_LINE_RE`` regexes.
  * ``_splitters`` — the four per-tool splitters + ``_generate_web_search_preview``.
  * this ``__init__`` — the ``_persist_to_disk`` dispatcher that routes to them.

Imports nothing from sibling sub-modules except ``_constants``.
"""

import os

from lib.log import get_logger
from lib.tasks_pkg.persist_registry import register as _register_label
from lib.tasks_pkg.compaction._constants import (
    _DEFAULT_TOOL_RESULT_MAX,
    _PERSIST_DIR_BASE,
    _PERSIST_PREVIEW_CHARS,
    TOOL_RESULT_MAX_CHARS,
)
from lib.tasks_pkg.compaction._persist._helpers import (
    _DECORATIVE_LINE_RE,
    _VERT_BLOCK_RE,
    _first_meaningful_line,
    _human_size,
    _sanitize_filename,
    _short_id,
    _truncate_head_tail,
)
from lib.tasks_pkg.compaction._persist._splitters import (
    _generate_web_search_preview,
    _persist_fetch_url_split,
    _persist_find_files_split,
    _persist_grep_search_split,
    _persist_web_search_split,
)

logger = get_logger(__name__)


def _persist_to_disk(content: str, tool_name: str, tool_use_id: str = '',
                     conv_id: str = '') -> str:
    """Persist oversized tool result to disk and return a summary with file paths.

    For tools with structured, multi-item results (web_search, grep_search),
    each item is saved to a **separate** file so the model can selectively
    read only the items it needs via read_files.

    For single-blob tools (fetch_url, run_command, etc.), the full content
    is saved to a single file as before.

    Args:
        content:     Full tool result string.
        tool_name:   Name of the tool that produced the result.
        tool_use_id: Tool call ID (used for filename uniqueness).
        conv_id:     Conversation ID (used for directory grouping).

    Returns:
        A formatted string with file path(s) + preview/index.
    """
    dir_name = conv_id[:12] if conv_id else 'default'
    persist_dir = os.path.join(_PERSIST_DIR_BASE, dir_name)
    os.makedirs(persist_dir, exist_ok=True)

    safe_id = _short_id(tool_use_id)

    # ── Try split-persist for multi-item tools ──
    if tool_name == 'web_search':
        result = _persist_web_search_split(content, persist_dir, safe_id)
        if result is not None:
            return result

    if tool_name == 'grep_search':
        result = _persist_grep_search_split(content, persist_dir, safe_id)
        if result is not None:
            return result

    if tool_name == 'fetch_url':
        result = _persist_fetch_url_split(content, persist_dir, safe_id)
        if result is not None:
            return result

    if tool_name == 'find_files':
        result = _persist_find_files_split(content, persist_dir, safe_id)
        if result is not None:
            return result

    # ── Default: single file persistence ──
    filename = f'{tool_name}_{safe_id}.txt'
    filepath = os.path.join(persist_dir, filename)

    try:
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(content)
    except Exception as e:
        logger.warning('[Persist] Failed to write %s: %s', filepath, e,
                       exc_info=True)
        return _truncate_head_tail(content, tool_name,
                                   TOOL_RESULT_MAX_CHARS.get(tool_name, _DEFAULT_TOOL_RESULT_MAX))

    logger.info('[Persist] %s result persisted to disk: %s (%s)',
                tool_name, filepath, _human_size(len(content)))

    _register_label(filepath, tool_name, _first_meaningful_line(content))

    # Default preview: first N chars truncated at newline boundary
    preview = content[:_PERSIST_PREVIEW_CHARS]
    last_nl = preview.rfind('\n')
    if last_nl > _PERSIST_PREVIEW_CHARS // 2:
        preview = preview[:last_nl]

    return (
        f'[Persisted to: {filepath}]\n'
        f'Output too large ({_human_size(len(content))}). '
        f'Full output saved to: {filepath}\n'
        f'Use read_files to access the full content if needed.\n\n'
        f'Preview:\n'
        f'{preview}\n'
    )


# ── Facade re-exports ────────────────────────────────────────────────────
# Every symbol the pre-split ``_persist.py`` module exposed stays importable
# from ``lib.tasks_pkg.compaction._persist`` byte-identically.
__all__ = [
    '_persist_to_disk',
    # splitters + preview
    '_persist_web_search_split',
    '_persist_grep_search_split',
    '_persist_find_files_split',
    '_persist_fetch_url_split',
    '_generate_web_search_preview',
    # helpers
    '_short_id',
    '_human_size',
    '_first_meaningful_line',
    '_sanitize_filename',
    '_truncate_head_tail',
    # module-level regexes (referenced by NC tests + relocation logic)
    '_VERT_BLOCK_RE',
    '_DECORATIVE_LINE_RE',
]
