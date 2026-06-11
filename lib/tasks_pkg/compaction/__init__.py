# HOT_PATH
"""
Context compaction — four-layer progressive compression pipeline (L0–L3).

The layers are numbered by *when* they fire, not by where they live.  Only
L1 + L2 run inside ``run_compaction_pipeline`` (called once per round by the
orchestrator); L0 fires earlier at tool-result entry, and L3 fires later as
an error recovery path.  The numbering below is the single source of truth —
every sub-module docstring uses the same L0/L1/L2/L3 labels.

Layer 0 — Tool-result budgeting (``_budget`` / ``_persist``, zero LLM cost):

    Fires at tool-result *entry* time from ``tool_dispatch.py`` — NOT from
    ``run_compaction_pipeline``.  Oversized results are persisted to disk
    and replaced with a preview + file path the model can re-read, so a
    single grep/fetch can't swallow the context window.  A per-round
    aggregate guard caps parallel tool-call explosions.

Layer 1 — Micro-compaction (``_layer1``, runs before every LLM call, zero LLM cost):

    Keeps a "hot tail" of the N most recent tool results untouched.
    Tool results that fall outside the hot tail are replaced in the
    messages list with a short placeholder that tells the model the
    result was compacted and it can re-call the tool if needed.

    This layer runs every round, is idempotent (skips already-compacted
    results), and requires no LLM calls.  The transforms are pluggable
    steps (see ``_steps`` / ``_builtin_steps``); the orchestration shell
    owns durable-placeholder persistence.

Layer 2 — Context compact (``_layer2``, force-triggered by orchestrator only):

    NOT in the model's tool list — the model never calls this voluntarily.
    Force-injected by the orchestrator when estimated token count exceeds
    ``_SUMMARY_TRIGGER_RATIO`` of usable context window.

    Pure LLM summary with selective turn compression:
      - A cheap model evaluates each historical user↔assistant turn
        for relevance to the current query
      - Critical turns (score 3) preserved verbatim
      - Useful turns (score 2) compressed to key sentences
      - Tangential turns (score 1) reduced to one-line mentions
      - Irrelevant turns (score 0) dropped entirely

    The summary is injected as a synthetic tool_call + tool_result pair.
    Old messages before the boundary are replaced.

Layer 3 — Reactive compaction (``_reactive``, error recovery, off-pipeline):

    Emergency path invoked from ``llm_fallback.py`` when the upstream API
    rejects a request as too long (HTTP 400 prompt-too-long) or too large
    (HTTP 413 wire bytes).  Archives, aggressively strips images, runs an
    aggressive micro_compact, force-compacts with a tight budget, then
    head-truncates as a last resort.  Capped at ``_REACTIVE_COMPACT_MAX_RETRIES``.

Concurrency safety:
    All persistent state is keyed by conv_id.  Multiple conversations
    can compact concurrently without interference.  No filesystem
    artifacts — everything goes through the database.
"""

# No code lives in this file — it is a pure re-export facade.
# All implementations live in the sub-modules listed below.


# ═══════════════════════════════════════════════════════════════════════════════
#  Constants  (re-exported from ._constants for backwards-compat hot-reload)
# ═══════════════════════════════════════════════════════════════════════════════

from lib.tasks_pkg.compaction._constants import (  # noqa: E402,F401
    MICRO_HOT_TAIL,
    MICRO_COMPACT_THRESHOLD,
    _THINKING_HOT_TAIL,
    _SUMMARY_TRIGGER_RATIO,
    _SUMMARY_MAX_TOKENS,
    _SUMMARY_COOLDOWN,
    _DEFAULT_CONTEXT_LIMIT,
    _OUTPUT_RESERVE,
    _COMPACTION_RESERVE,
    _COMPACT_TOOL_NAME,
    _WIRE_BYTE_SOFT_LIMIT,
    _WIRE_IMAGE_KEEP_TAIL,
    _PRESERVE_BUDGET_RATIO,
    _MAX_PRESERVE_TURNS,
    _IMAGE_TOKENS_LOW,
    _IMAGE_TOKENS_HIGH,
    _IMAGE_TOKENS_DEFAULT,
    _BUDGET_EXEMPT_TOOLS,
    TOOL_RESULT_MAX_CHARS,
    _DEFAULT_TOOL_RESULT_MAX,
    _PERSIST_DIR_BASE,
    _PERSIST_PREVIEW_CHARS,
    MAX_ROUND_TOOL_RESULTS_CHARS,
    _summary_cooldowns,
    _cooldown_lock,
    _tables_initialized,
    _tables_lock,
)




# ═══════════════════════════════════════════════════════════════════════════════
#  DB helpers (re-exported from ._archive)
# ═══════════════════════════════════════════════════════════════════════════════

from lib.tasks_pkg.compaction._archive import (  # noqa: E402,F401
    _archive_transcript,
    _ensure_compaction_tables,
    _init_tables,
    cleanup_compaction_data,
)


# ═══════════════════════════════════════════════════════════════════════════════
#  Layer 0 — Tool Result Budgeting (inline, per-result cap)
#  Inspired by Claude Code's toolResultStorage — large tool results are
#  truncated IMMEDIATELY when they enter context, not deferred to later.
# ═══════════════════════════════════════════════════════════════════════════════

# All Layer 0 constants now live in ._constants and are re-exported via
# the import at the top of this file.  The values _BUDGET_EXEMPT_TOOLS,
# TOOL_RESULT_MAX_CHARS, _DEFAULT_TOOL_RESULT_MAX, _PERSIST_DIR_BASE,
# _PERSIST_PREVIEW_CHARS, and MAX_ROUND_TOOL_RESULTS_CHARS are reachable
# through ``lib.tasks_pkg.compaction.<name>`` as before.

from lib.tasks_pkg.compaction._persist import (  # noqa: E402,F401
    _generate_web_search_preview,
    _persist_fetch_url_split,
    _persist_find_files_split,
    _persist_grep_search_split,
    _persist_to_disk,
    _persist_web_search_split,
    _sanitize_filename,
    _truncate_head_tail,
)




from lib.tasks_pkg.compaction._budget import (  # noqa: E402,F401
    budget_tool_result,
    enforce_round_aggregate_budget,
    mark_empty_result,
)


from lib.tasks_pkg.compaction._layer1 import micro_compact  # noqa: E402,F401

# Step framework + built-in steps.  Importing _builtin_steps here ensures
# the five default L1 steps are registered at package-import time (so
# list_steps() is introspectable and config arms can be validated before
# the first micro_compact call).
from lib.tasks_pkg.compaction._steps import (  # noqa: E402,F401
    CompactionContext,
    CompactionStep,
    MessageEditor,
    STEP_KIND_STRUCTURAL,
    STEP_KIND_TRANSFORM,
    register_step,
    get_step,
    get_step_spec,
    list_steps,
    run_steps,
)
import lib.tasks_pkg.compaction._builtin_steps  # noqa: E402,F401
import lib.tasks_pkg.compaction._methods  # noqa: E402,F401 (experimental steps)
import lib.tasks_pkg.compaction._faithful_methods  # noqa: E402,F401 (verified OpenCode/Hermes/OpenClaw)

from lib.tasks_pkg.compaction._advanced import advanced_compact  # noqa: E402,F401


# ═══════════════════════════════════════════════════════════════════════════════
#  Token estimation helpers
# ═══════════════════════════════════════════════════════════════════════════════

from lib.tasks_pkg.compaction._tokens import (  # noqa: E402,F401
    _count_tokens_authoritative,
    _estimate_msg_tokens,
    _estimate_total_tokens,
    _get_context_limit,
    _get_static_context_limit,
    build_context_policy,
    resolve_model_context_limit,
    _human_size,
    _parse_reported_token_count,
    _parse_context_overflow,
    _PROMPT_TOO_LONG_RE,
    _should_force_compact,
)



from lib.tasks_pkg.compaction._layer2 import (  # noqa: E402,F401
    _extract_current_query,
    _extract_recently_accessed_files,
    _find_turn_boundary,
    _format_messages_for_summary,
    _generate_query_aware_summary,
    _SUMMARY_SYSTEM_PROMPT,
    execute_compact_tool,
    force_compact_if_needed,
    smart_summary_compact,
)



from lib.tasks_pkg.compaction._reactive import (  # noqa: E402,F401
    _estimate_wire_bytes,
    _head_truncate,
    _strip_images_aggressive,
    reactive_compact,
)


from lib.tasks_pkg.compaction._pipeline import (  # noqa: E402,F401
    _reinject_system_contexts_after_compact,
    run_compaction_pipeline,
)
