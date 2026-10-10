"""lib/agent_verdict/_rounds.py — Usage-dict accumulation helper.

The state-changing tool-round counter (``count_state_changing_rounds``) and its
tool sets live in ``_handoff`` (they are consumed there alongside the sentinels
and are re-exported from the package ``__init__``).  This module carries the
remaining round-bookkeeping primitive: merging per-turn usage dicts.

Pure logic — imports only ``lib.log``.
"""

from __future__ import annotations

from lib.log import get_logger

logger = get_logger(__name__)


# ══════════════════════════════════════════════════════════
#  Usage accumulation
# ══════════════════════════════════════════════════════════

def accumulate_usage(total, delta):
    """Merge ``delta`` usage dict into ``total`` (in-place)."""
    for k, v in (delta or {}).items():
        if isinstance(v, (int, float)):
            total[k] = total.get(k, 0) + v
