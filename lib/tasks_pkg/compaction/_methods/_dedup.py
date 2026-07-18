# HOT_PATH
"""M1 — latest-state file dedup.

A coding agent re-reads the same file many times.  Only the *most recent*
read of a given path is the current truth; every *earlier* read of that
same path is stale.  ``latest_state_dedup`` collapses the stale earlier
reads to a one-line "superseded by a later read" marker, keeping the
latest read verbatim.  Zero information loss.
"""

from __future__ import annotations

import re

from lib.log import get_logger
from lib.tasks_pkg.compaction._steps import CompactionContext, register_step
from lib.tasks_pkg.compaction._methods._shared import (
    _already_compacted,
    _content_str,
    _log_id,
)

logger = get_logger(__name__)


# Matches the read_tools header, e.g.:
#   "File: lib/server.py (320 lines, 12.1KB)"
#   "File: src/main.py (lines 1-200 of 980)"
_FILE_HEADER_RE = re.compile(r'^File:\s+(?P<path>[^\s(]+)', re.MULTILINE)

# Tools whose results represent the *state of a file path* (so an older
# read of the same path is superseded by a newer one).
_FILE_READ_TOOLS = frozenset({'read_files', 'read_file'})


def _paths_in_read_result(text: str) -> list[str]:
    """Extract the file path(s) a read result covers, via its ``File:``
    headers.  A batched read_files result has several headers."""
    return _FILE_HEADER_RE.findall(text)


@register_step('latest_state_dedup')
def latest_state_dedup(ctx: CompactionContext) -> int:
    """Supersede stale earlier reads of a file path (M1).

    For each ``read_files`` tool result, determine the set of file paths
    it covers.  Walking newest→oldest, the first time a path is seen it is
    "live"; any older result whose paths are ALL already covered by newer
    reads is collapsed to a one-line superseded marker.

    Conservative rules (to never drop live information):
      * Only acts on results from ``_FILE_READ_TOOLS``.
      * A result is superseded only if EVERY path it covers has been seen
        in a strictly newer result (a batched read covering an extra path
        is kept).
      * The single most-recent result is always kept verbatim.
      * Cache-prefix and hot-tail rules still apply (we never touch the
        ``MICRO_HOT_TAIL`` newest tool results, nor the cache prefix).
    """
    _c = ctx.constants
    messages = ctx.messages

    tool_indices = [i for i, m in enumerate(messages) if m.get('role') == 'tool']
    if len(tool_indices) <= _c.MICRO_HOT_TAIL:
        return 0
    cold_set = set(tool_indices[:-_c.MICRO_HOT_TAIL])

    seen_paths: set[str] = set()
    superseded = 0
    tokens_saved = 0

    # Walk newest→oldest so the most recent read of each path wins.
    for idx in reversed(tool_indices):
        msg = messages[idx]
        if msg.get('name') not in _FILE_READ_TOOLS:
            continue
        text = _content_str(msg)
        if text is None or _already_compacted(text):
            # Still register its paths as "seen" so older dupes supersede.
            if text is not None:
                for p in _paths_in_read_result(text):
                    seen_paths.add(p)
            continue

        paths = _paths_in_read_result(text)
        if not paths:
            continue

        is_cold = idx in cold_set and not ctx.is_in_cache_prefix(idx)
        all_superseded = all(p in seen_paths for p in paths)

        if is_cold and all_superseded:
            old_len = len(text)
            path_list = ', '.join(paths[:5]) + ('…' if len(paths) > 5 else '')
            placeholder = (
                f'[read_files superseded — a later read of '
                f'{path_list} reflects the current file state]'
            )
            msg['content'] = placeholder
            tokens_saved += (old_len - len(placeholder)) // 4
            superseded += 1
            ctx.stamp(msg, old_len, len(placeholder))
        else:
            # This read is the freshest for at least one path → it's live.
            for p in paths:
                seen_paths.add(p)

    if superseded > 0:
        logger.info('[M1-dedup] conv=%s  superseded %d stale file reads '
                    '(~%d tokens saved; latest read kept verbatim)',
                    _log_id(ctx.conv_id), superseded, tokens_saved)
    return tokens_saved
