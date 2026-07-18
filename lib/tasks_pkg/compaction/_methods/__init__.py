# HOT_PATH
"""Experimental Layer-1 compaction methods (Stage 4 of the joint
cache+compaction optimization).

Unlike the built-in steps in ``_builtin_steps.py`` — which replace a cold
tool result with a "re-call tool if needed" placeholder and thereby
*forget* the information — the methods here aim to be
**information-preserving** so they can shrink context WITHOUT lowering
task-success rate (the thing aggressive compaction risks).

Methods
-------
* ``latest_state_dedup`` (M1)
    A coding agent re-reads the same file many times.  Only the *most
    recent* read of a given path is the current truth; every *earlier*
    read of that same path is stale.  M1 collapses the stale earlier
    reads to a one-line "superseded by a later read" marker, keeping the
    latest read verbatim.  Zero information loss, and it directly kills
    the read→compact→re-read churn loop the placeholder approach causes.

* ``fold_observations`` (M2)
    Replace a cold ``grep_search`` / ``find_files`` / ``list_dir`` result
    with a one-line *structured fact* extracted cheaply (no LLM): e.g.
    ``grep_search → 3 match line(s)``.  Preserves the *answer* the tool
    produced rather than just "it was compacted", so the model rarely
    needs to re-call it.

Both are registered steps: select them via
``task['config']['compaction']['steps']``.  Both respect
``ctx.is_in_cache_prefix`` and call ``ctx.stamp`` for durable placeholders,
exactly like the built-ins, and neither calls the LLM.

This package is a FACADE: the implementations live in cohesive submodules
(``_dedup``, ``_fold``, ``_drop``, ``_summarize``, ``_prune``, ``_tail``,
with shared helpers in ``_shared``).  Importing this package executes every
submodule so their ``@register_step`` decorators fire at package-import
time — preserving the registration side-effect the compaction package
relies on (``import lib.tasks_pkg.compaction._methods`` for its effect).
"""

from __future__ import annotations

from lib.log import get_logger

logger = get_logger(__name__)


# ── Shared helpers (re-exported for backward-compat / test imports) ──────
from lib.tasks_pkg.compaction._methods._shared import (  # noqa: E402,F401
    _log_id,
    _content_str,
    _already_compacted,
)

# ── M1 — latest-state file dedup ─────────────────────────────────────────
from lib.tasks_pkg.compaction._methods._dedup import (  # noqa: E402,F401
    latest_state_dedup,
    _paths_in_read_result,
    _FILE_HEADER_RE,
    _FILE_READ_TOOLS,
)

# ── M2 — observation folding ─────────────────────────────────────────────
from lib.tasks_pkg.compaction._methods._fold import (  # noqa: E402,F401
    fold_observations,
    _fold_fact,
    _FOLDABLE_TOOLS,
)

# ── Advanced-host structural: drop_superseded_turns ──────────────────────
from lib.tasks_pkg.compaction._methods._drop import (  # noqa: E402,F401
    drop_superseded_turns,
    _PATH_TOOLS,
)

# ── Advanced-host LLM: summarize_oldest_turn ─────────────────────────────
from lib.tasks_pkg.compaction._methods._summarize import (  # noqa: E402,F401
    summarize_oldest_turn,
)

# ── OpenCode-inspired: prune_with_hysteresis ─────────────────────────────
from lib.tasks_pkg.compaction._methods._prune import (  # noqa: E402,F401
    prune_with_hysteresis,
    _tool_text_len,
    _PRUNE_PROTECT_TOKENS_DEFAULT,
    _PRUNE_MINIMUM_TOKENS_DEFAULT,
)

# ── OpenCode-inspired: adaptive_hot_tail ─────────────────────────────────
from lib.tasks_pkg.compaction._methods._tail import (  # noqa: E402,F401
    adaptive_hot_tail,
    _ADAPTIVE_TAIL_BUDGET_DEFAULT,
)


__all__ = [
    # shared helpers
    '_log_id', '_content_str', '_already_compacted',
    # M1
    'latest_state_dedup', '_paths_in_read_result',
    '_FILE_HEADER_RE', '_FILE_READ_TOOLS',
    # M2
    'fold_observations', '_fold_fact', '_FOLDABLE_TOOLS',
    # structural
    'drop_superseded_turns', '_PATH_TOOLS',
    # LLM
    'summarize_oldest_turn',
    # OpenCode prune
    'prune_with_hysteresis', '_tool_text_len',
    '_PRUNE_PROTECT_TOKENS_DEFAULT', '_PRUNE_MINIMUM_TOKENS_DEFAULT',
    # OpenCode tail
    'adaptive_hot_tail', '_ADAPTIVE_TAIL_BUDGET_DEFAULT',
]
