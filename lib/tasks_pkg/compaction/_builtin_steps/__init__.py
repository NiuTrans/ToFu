# HOT_PATH
"""Built-in Layer-1 compaction steps (the former Phase A–D of micro_compact).

Each function here is a faithful extraction of one phase of the historical
``micro_compact`` body, re-expressed against :class:`CompactionContext`.
They are registered by name so they can be ordered / ablated / replaced
purely by configuration (see ``_steps.py``).

Invariants preserved from the monolith:
  * Cache-aware: every step skips indices inside the prompt-cache prefix
    via ``ctx.is_in_cache_prefix(idx)``.
  * Durable placeholders: tool-result compaction calls ``ctx.stamp(...)``
    which wraps the ``toolContent`` write-back + ``tool_compacted`` SSE
    emit owned by the ``micro_compact`` shell.
  * Zero LLM cost — none of these call the model (enforced by
    ``tests/test_compaction_invariants.py::test_layer1_does_not_invoke_dispatch_chat``).

Step order in the default L1 pass (must match the old phase order for
byte-identical output):
    strip_thinking → compact_tool_results → [fold_paired_interstitial]
    → strip_cold_images → [compact_cold_assistant]
The two bracketed steps are gated (paired / assistant compaction) and are
only included in the step list when their respective flags are set.

This package is a FACADE: the step implementations live in cohesive
submodules (``_thinking``, ``_toolresults``, ``_interstitial``, ``_images``,
``_assistant``, with the shared ``_log_id`` helper in ``_shared``).
Importing this package executes every step submodule so their
``@register_step`` decorators fire at package-import time — preserving the
registration side-effect the compaction package relies on
(``import lib.tasks_pkg.compaction._builtin_steps`` for its effect, from
``compaction/__init__.py`` and ``_layer1.py``).
"""

from __future__ import annotations

from lib.log import get_logger

logger = get_logger(__name__)


# ── Shared helper (re-exported for backward-compat / test imports) ───────
from lib.tasks_pkg.compaction._builtin_steps._shared import (  # noqa: E402,F401
    _log_id,
)

# ── Phase A — strip_thinking ─────────────────────────────────────────────
from lib.tasks_pkg.compaction._builtin_steps._thinking import (  # noqa: E402,F401
    strip_thinking,
)

# ── Phase B — compact_tool_results ───────────────────────────────────────
from lib.tasks_pkg.compaction._builtin_steps._toolresults import (  # noqa: E402,F401
    compact_tool_results,
    _find_paired_assistant,
)

# ── Phase B2 — fold_paired_interstitial (gated) ──────────────────────────
from lib.tasks_pkg.compaction._builtin_steps._interstitial import (  # noqa: E402,F401
    fold_paired_interstitial,
)

# ── Phase C — strip_cold_images ──────────────────────────────────────────
from lib.tasks_pkg.compaction._builtin_steps._images import (  # noqa: E402,F401
    strip_cold_images,
)

# ── Phase D — compact_cold_assistant (gated) ─────────────────────────────
from lib.tasks_pkg.compaction._builtin_steps._assistant import (  # noqa: E402,F401
    compact_cold_assistant,
)


# Default ungated L1 step order (matches the historical phase order).
DEFAULT_L1_STEPS = ['strip_thinking', 'compact_tool_results', 'strip_cold_images']


__all__ = [
    'DEFAULT_L1_STEPS',
    '_log_id',
    'strip_thinking',
    'compact_tool_results',
    '_find_paired_assistant',
    'fold_paired_interstitial',
    'strip_cold_images',
    'compact_cold_assistant',
]
