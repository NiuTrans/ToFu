"""Friendly-label registry for spilled-to-disk tool results.

When an oversized tool result is persisted to disk
(:mod:`lib.tasks_pkg.compaction._persist`), the on-disk filename embeds an
opaque ``tool_use_id`` — e.g. ``search_toolu_019Yqeh…_9_AI_Model_Deprecation.txt``
— which is meaningless to a human reading the ``read_files`` tool-call line in
the UI.  ``_persist_to_disk`` registers a human-readable ``(tool_name,
description)`` for each file it writes; the tool-display layer
(:mod:`lib.tasks_pkg.tool_display`) looks it up by path so the line renders
e.g. ``web search result — "AI Model Deprecation Tracker 2026…"`` instead.

Leaf module: imports only stdlib + :mod:`lib.log`, so both the persist writer
and the display reader can import it without circular-import risk.  The cache
is process-local and lost on restart (same limitation as
:mod:`lib.mcp.project_names`); callers fall back to :func:`describe_filename`
— a stateless best-effort filename parser — on a cache miss.
"""

from __future__ import annotations

import os
import re
import threading

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = [
    'register',
    'lookup',
    'describe_filename',
    'friendly_label',
    'clear',
]

# filepath (and basename) → (tool_name, description)
_labels: dict[str, tuple[str, str]] = {}
_lock = threading.Lock()

# Cap defensively — a long agentic session may spill many results.
_MAX_ENTRIES = 4000

# Human verb per tool, used by friendly_label().
_TOOL_VERB = {
    'web_search': 'web search result',
    'grep_search': 'grep matches',
    'fetch_url': 'fetched page',
    'find_files': 'find results',
}

# Split-persist filename prefix → tool name (see _persist.py).
_PREFIX_TOOL = {
    'search': 'web_search',
    'grep': 'grep_search',
    'fetch': 'fetch_url',
    'find': 'find_files',
}

# Max description chars kept for display.
_DESC_MAX = 60


def register(filepath: str, tool_name: str, description: str = '') -> None:
    """Record a friendly ``(tool_name, description)`` for a persisted file.

    Keyed by both the full path and its basename so a later lookup matches
    regardless of how the model spells the path back.
    """
    if not filepath or not tool_name:
        return
    desc = (description or '').strip()
    if len(desc) > _DESC_MAX:
        desc = desc[:_DESC_MAX - 1] + '…'
    entry = (tool_name, desc)
    base = os.path.basename(filepath)
    with _lock:
        if len(_labels) >= _MAX_ENTRIES and filepath not in _labels:
            # FIFO-ish eviction — good enough for a cap we rarely hit.
            _labels.pop(next(iter(_labels)), None)
        _labels[filepath] = entry
        if base:
            _labels[base] = entry


def lookup(path: str) -> tuple[str, str] | None:
    """Return the registered ``(tool_name, description)`` for ``path``, or None.

    Tries the exact path first, then the basename (the model sometimes
    relays a relative or differently-rooted spelling of the same file).
    """
    if not path:
        return None
    with _lock:
        hit = _labels.get(path)
        if hit is not None:
            return hit
        return _labels.get(os.path.basename(path))


def clear() -> None:
    """Empty the registry — intended for tests."""
    with _lock:
        _labels.clear()


_SPLIT_RE = re.compile(r'^(search|fetch|find)_(.+?)_(\d+)_(.+)$')
_GREP_RE = re.compile(r'^grep_(.+)$')


def describe_filename(name: str) -> tuple[str, str] | None:
    """Best-effort parse of a persisted-result filename → (tool_name, description).

    Stateless fallback used when the in-process registry misses (e.g. after a
    server restart).  Recognises the split-persist prefixes written by
    ``_persist.py`` (``search_`` / ``fetch_`` / ``find_`` / ``grep_``).  The
    description is reconstructed from the human fragment in the filename
    (underscores → spaces); returns '' for the description when it can't be
    isolated.  Returns None when the name isn't a recognised persisted result.
    """
    if not name:
        return None
    base = os.path.basename(name)
    if base.endswith('.txt'):
        base = base[:-4]

    m = _SPLIT_RE.match(base)
    if m:
        prefix, _id, _idx, frag = m.group(1), m.group(2), m.group(3), m.group(4)
        tool = _PREFIX_TOOL.get(prefix, prefix)
        return tool, _humanize(frag)

    m = _GREP_RE.match(base)
    if m:
        # grep_{id}_{safe_fname} — the id and filename both contain
        # underscores so they can't be split reliably; surface the tool
        # only (still far better than the raw filename).
        return 'grep_search', ''

    return None


def friendly_label(tool_name: str, description: str = '') -> str:
    """Compose the display string for a persisted result, e.g.
    ``web search result — "AI Model Deprecation Tracker 2026…"``.
    """
    verb = _TOOL_VERB.get(tool_name, f'{tool_name} output')
    desc = (description or '').strip()
    if len(desc) > _DESC_MAX:
        desc = desc[:_DESC_MAX - 1] + '…'
    if not desc:
        return verb
    # Quote free-text descriptions (titles, patterns); leave path/URL bare.
    if tool_name in ('grep_search', 'fetch_url'):
        return f'{verb} — {desc}'
    return f'{verb} — "{desc}"'


def _humanize(fragment: str) -> str:
    """Turn a sanitized filename fragment back into readable text."""
    return re.sub(r'_+', ' ', fragment).strip()
