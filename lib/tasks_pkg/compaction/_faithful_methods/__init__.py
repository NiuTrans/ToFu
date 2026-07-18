# HOT_PATH
"""Faithful, primary-source-verified reimplementations of the
OpenCode / Hermes / OpenClaw context-compaction strategies, for use as
research-paper baselines.

Verified 2026-06-04 against current primary source (see memory
``verified-compaction-algorithms-opencode-hermes-openclaw``):
  * OpenCode — `sst/opencode` `session/overflow.ts` + `session/compaction.ts` (dev)
  * Hermes   — `NousResearch/hermes-agent` `agent/context_compressor.py`
  * OpenClaw — `openclaw/openclaw` + docs.openclaw.ai (derives from pi-mono)

CRITICAL FIDELITY PRINCIPLE: each system has its OWN trigger threshold and
its OWN protected-region sizing. They are NOT shared. A common trigger
would make the three arms collapse into one and invalidate the comparison.
Each step below computes its trigger from the model's real context limit
using that system's published formula:

  OpenCode trigger : count >= limit_input − min(20_000, max_output)
  Hermes   trigger : prompt_tokens >= context_length × 0.50
  OpenClaw trigger : context_tokens >  context_window − reserve(floor 20_000)

All summarization protects a head + a recent tail, summarizes the middle
band, drops those turns boundary-aware (never splitting a tool pair, via
MessageEditor), and splices the summary in. Pruning (OpenCode/Hermes) is a
distinct LLM-free pre-pass that stubs tool OUTPUTS (keeps the call).

Steps registered:
  prune_tool_outputs_opencode  (transform)         — OpenCode prune()
  prune_tool_outputs_hermes    (transform)         — Hermes informative stubs
  summarize_opencode           (structural+llm)    — OpenCode one-shot middle-band
  summarize_hermes             (structural+llm)    — Hermes iterative running summary
  summarize_openclaw           (structural+llm)    — OpenClaw summarize-only + memflush + ID-strict

Out of scope (infra the harness lacks; documented so we don't overclaim):
OpenClaw successor-transcripts + on-disk memory file (we emulate the
memory-flush as an in-context durable note), Hermes LCM lossless plugin.

────────────────────────────────────────────────────────────────────────
This is a FACADE package: the implementations live in cohesive submodules
(``_state``, ``_shared``, ``_opencode``, ``_hermes``, ``_openclaw``) and
are re-exported here so ``from lib.tasks_pkg.compaction._faithful_methods
import X`` works byte-identically. Importing the method submodules below
re-triggers the ``@register_step`` registration side-effect that
``compaction/__init__.py`` relies on (import-for-registration).
"""

from __future__ import annotations

from lib.log import get_logger

logger = get_logger(__name__)

# ── Shared state (single owner: exactly one _running_summaries per process) ──
from lib.tasks_pkg.compaction._faithful_methods._state import (  # noqa: E402,F401
    _FAITHFUL_SUMMARY_COOLDOWN,
    _cooldown_ok,
    _last_summary_at,
    _log_id,
    _running_summaries,
    _summary_state_lock,
    reset_running_summary,
)

# ── Shared LLM-free helpers ──────────────────────────────────────────────────
from lib.tasks_pkg.compaction._faithful_methods._shared import (  # noqa: E402,F401
    _apply_summary,
    _content_text,
    _max_output_tokens,
    _msg_tokens,
    _raw_context_limit,
    _select_middle_turns,
    _tok,
)

# ── Method submodules — imported for their @register_step side-effect ────────
from lib.tasks_pkg.compaction._faithful_methods._opencode import (  # noqa: E402,F401
    _OC_PRUNE_MINIMUM,
    _OC_PRUNE_PROTECT,
    _OC_PRUNE_PROTECTED_TOOLS,
    _OC_PRUNE_SKIP_RECENT_TURNS,
    _OC_SUMMARY_PROMPT,
    _OC_TOOL_OUTPUT_MAX_CHARS,
    _oc_usable,
    prune_tool_outputs_opencode,
    summarize_opencode,
)
from lib.tasks_pkg.compaction._faithful_methods._hermes import (  # noqa: E402,F401
    _HERMES_PROTECT_FIRST_N,
    _HERMES_PROTECT_LAST_N,
    _HERMES_PRUNE_MIN_CHARS,
    _HERMES_SECTIONS,
    _HERMES_TAIL_RATIO,
    _HERMES_THRESHOLD_PCT,
    _hermes_tool_stub,
    prune_tool_outputs_hermes,
    summarize_hermes,
)
from lib.tasks_pkg.compaction._faithful_methods._openclaw import (  # noqa: E402,F401
    _OPENCLAW_KEEP_RECENT,
    _OPENCLAW_RESERVE_FLOOR,
    _OPENCLAW_SUMMARY_PROMPT,
    summarize_openclaw,
)

__all__ = [
    # public
    'reset_running_summary',
    'prune_tool_outputs_opencode',
    'prune_tool_outputs_hermes',
    'summarize_opencode',
    'summarize_hermes',
    'summarize_openclaw',
    # shared state (re-exported by reference)
    '_running_summaries',
    '_summary_state_lock',
    '_last_summary_at',
    '_FAITHFUL_SUMMARY_COOLDOWN',
    '_cooldown_ok',
    '_log_id',
    # shared helpers
    '_tok',
    '_msg_tokens',
    '_raw_context_limit',
    '_max_output_tokens',
    '_content_text',
    '_select_middle_turns',
    '_apply_summary',
]
