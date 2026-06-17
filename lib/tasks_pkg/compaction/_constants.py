"""Tunable constants + module state for the compaction package.

This is a leaf module: it imports nothing from other compaction
sub-modules.  All of its public names are re-exported by the package
``__init__.py`` so the hot-reload contract (``import lib as _lib;
_lib.tasks_pkg.compaction.MICRO_HOT_TAIL``) keeps working.

§10.1 NOTE: every value in this file is a hyperparameter — changes
require explicit user sign-off + an ``audit_log('config_change', …)``
entry.  See memory ``compaction-defaults-2026-04-19-relaxed`` for the
last sanctioned tuning pass.
"""

import os
import threading


# ═══════════════════════════════════════════════════════════════════════════════
#  Layer 1 — Micro-compaction
# ═══════════════════════════════════════════════════════════════════════════════

MICRO_HOT_TAIL = 40
"""Number of most-recent tool results to keep uncompressed.
Everything older is archived to DB and replaced with a placeholder.

2026-04-19: Raised 30 → 60 to reduce compaction aggressiveness. Most
SWE-bench-style tasks complete in 20-40 tool calls; doubling the hot
tail keeps them fully uncompressed. Normal chats rarely exceed 60 either.

2026-06-07: Lowered 60 → 40. The dspro 8-arm compaction A/B
(maps_workdir_pro8b) showed OpenCode's more active cold-output pruning
cut input tokens ~10% at the lowest cost/solve with no resolve-rate
regression vs the 60-keep `tofu` arm. Keeping the count-based hot tail
(not token-based) but tightening it to 40 captures most of that saving
while still covering the 20-40-tool-call body of typical SWE-bench runs."""

MICRO_COMPACT_THRESHOLD = 2000
"""Minimum character count before a tool result is worth compacting.
Results shorter than this are left in place even outside the hot tail.

2026-04-19: Raised 500 → 2000 to reduce compaction aggressiveness.
Leaves small/medium reads (grep output, file reads <2KB) untouched even
when cold — only bulky outputs (big files, web_search blobs) get compacted."""

# How many recent assistant messages keep their thinking blocks intact.
#
# 2026-04-19: Raised 4 → 20 to reduce compaction aggressiveness. Thinking
# blocks are the model's scratchpad — stripping them mid-task forces the
# model to re-derive reasoning each turn. 20 covers ~95% of conversations
# (incl. SWE-bench iterations) while still protecting pathological 100+
# turn sessions from unbounded growth. Non-reasoning models don't emit
# thinking blocks, so this is a no-op for them.
_THINKING_HOT_TAIL = 20


# ═══════════════════════════════════════════════════════════════════════════════
#  Layer 2 / Force compact
# ═══════════════════════════════════════════════════════════════════════════════

_SUMMARY_TRIGGER_RATIO = 0.90
"""Trigger force-compact when tokens exceed this fraction of usable context.

2026-04-19: Raised 0.80 → 0.90 to reduce compaction aggressiveness.
Layer 2 summary is lossy; delay firing until we're truly close to the wall.
With 1M context that's still 100k tokens of headroom."""

_SUMMARY_MAX_TOKENS = 3000
"""Maximum output tokens for the summary LLM call."""

_SUMMARY_COOLDOWN = 30.0
"""Seconds between consecutive summary attempts for the same conv_id.
Prevents rapid re-triggering when the model generates a long response
right after a summary."""

_DEFAULT_CONTEXT_LIMIT = 1_000_000
"""Fallback context limit when the model name is not recognized.
Raised to 1M since primary models (Claude 4.6) support 1M context (GA 2026-03-13)."""

_OUTPUT_RESERVE = 128_000
"""Tokens reserved for model output generation.

2026-06-02: Raised 32K → 128K to match the real max-output cap of the
primary Claude Opus/Sonnet 4.6–4.8 family (128K output tokens, which
count against the same 1M window). The old 32K under-reserved by ~96K,
so a near-full prompt plus a long answer could overrun 1M before
force-compact fired. With 1M context this still leaves ~864K usable
before the 0.90 trigger (~778K)."""

_COMPACTION_RESERVE = 8_000
"""Tokens reserved for the compaction LLM call itself."""

_COMPACT_TOOL_NAME = 'context_compact'
"""Tool name for the synthetic compact tool pair."""


# ═══════════════════════════════════════════════════════════════════════════════
#  Wire-size safety (gateway HTTP 413)
# ═══════════════════════════════════════════════════════════════════════════════

_WIRE_BYTE_SOFT_LIMIT = 4 * 1024 * 1024
"""Reactive-compact target for the serialized request body, in bytes.

The upstream LLM gateway (openresty at your-llm-gateway.example.com) rejects request
bodies exceeding its `client_max_body_size` with HTTP 413 BEFORE upstream
ever tokenizes them.  Our token-count estimate is truthful for upstream
billing (Claude charges ~(W·H)/750 tokens per image), but blind to the
wire-byte volume of base64-encoded image_url blocks.  A single 1024×510
screenshot costs ~706 upstream tokens but ~576 KB on the wire (a ~800×
ratio).  See debug/probe_image_tokens.py for the empirical numbers.

Reactive compaction treats wire-byte overflow as a separate dimension
from token overflow.  The target is conservative (4 MB) to leave ample
headroom below any sane gateway limit."""

_WIRE_IMAGE_KEEP_TAIL = 2
"""When reactive-stripping images, keep only the N most-recent image_url
blocks across the whole message list.  Matches `_IMAGE_HOT_TAIL` in
micro_compact — but here we apply it UNCONDITIONALLY (ignore hot-tail
protection) because a 413 proves the current payload is already too big
on the wire regardless of token budget."""


# ═══════════════════════════════════════════════════════════════════════════════
#  Turn-based preservation (2026-04-26)
# ═══════════════════════════════════════════════════════════════════════════════
#
# Replaces the old "count user-assistant pairs" abstraction which failed
# silently on agentic workloads (see conv=modearkif6k9tr post-mortem).
#
# A *turn* is defined as:
#     [ user_msg, ...all subsequent non-user messages until next user ]
# i.e. one human request plus every assistant/tool message produced in
# response.  A single agentic turn can have 100+ tool messages; the old
# pair-count abstraction couldn't express this.
#
# Preservation policy:
#   INVARIANT      — always preserve the current (in-flight) turn in full.
#   BEST-EFFORT    — add prior turns newest→oldest while under budget.
#   HARD CAP       — never preserve more than _MAX_PRESERVE_TURNS turns.
#   REFUSE         — if no user message exists at all, skip compaction.

_PRESERVE_BUDGET_RATIO = 0.30
"""Fraction of usable context that ``_find_turn_boundary`` is allowed to
keep verbatim.  The rest becomes summary input.  Scales with the model's
context window (so 1M-context Opus 4.7 keeps proportionally more than
128K gpt-4)."""

_MAX_PRESERVE_TURNS = 16
"""Hard upper bound on how many turns may be preserved verbatim, even
if budget allows more.  Defends against pathological short-turn streams
(e.g. 100+ tiny user messages) from defeating the budget mechanism."""


# ═══════════════════════════════════════════════════════════════════════════════
#  Vision-API image token estimates
# ═══════════════════════════════════════════════════════════════════════════════
#
# Images are NOT tokenised as base64 text — the API charges a fixed
# amount based on resolution, not on the byte size of the data URL.

_IMAGE_TOKENS_LOW = 85       # detail=low  (fixed)
_IMAGE_TOKENS_HIGH = 800     # detail=high average (85 base + tiles)
_IMAGE_TOKENS_DEFAULT = _IMAGE_TOKENS_HIGH  # conservative default


# ═══════════════════════════════════════════════════════════════════════════════
#  Layer 0 — Tool Result Budgeting
# ═══════════════════════════════════════════════════════════════════════════════
#
# Per-tool maximum result chars.  Results exceeding this are persisted
# to disk + replaced with a preview so a single grep_search or read_files
# can't eat the entire context window.

# Tools whose results should NEVER be truncated by budget_tool_result.
# Like Claude Code's Read tool (maxResultSizeChars = Infinity), truncating
# read results is counterproductive — the model will just re-call the tool,
# wasting tokens and time.  These tools already have their own internal
# limits (MAX_READ_CHARS=100K per file, BATCH_CHAR_BUDGET=200K).
# micro_compact (Layer 1) will compress them later when they become cold.
_BUDGET_EXEMPT_TOOLS = frozenset({
    'read_files',
})

TOOL_RESULT_MAX_CHARS: dict[str, int] = {
    'read_files':    0,          # exempt — see _BUDGET_EXEMPT_TOOLS
    'grep_search':   30_000,
    'find_files':    20_000,
    'list_dir':      15_000,
    'run_command':   40_000,
    'fetch_url':     50_000,
    'web_search':    30_000,
    'browser_read_tab': 40_000,
    'browser_get_interactive_elements': 30_000,
    'browser_execute_js': 30_000,
    'browser_get_app_state': 30_000,

}
_DEFAULT_TOOL_RESULT_MAX = 60_000
"""Default budget for tools not listed above."""

# ── Disk persistence for oversized results ──
# Instead of irreversibly truncating large tool results (head+tail),
# write the full content to a temp file and return a preview + file path.
# The model can later use read_files to access the full content.
# Inspired by Claude Code's toolResultStorage.ts persistence mechanism.

_PERSIST_DIR_BASE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))),
    'data', 'tool-results',
)
_PERSIST_PREVIEW_CHARS = 2000
"""Preview size for persisted results (truncated at newline boundary)."""

# ── Per-round aggregate budget ──
# Prevents context explosion from parallel tool calls.  If total tool
# result chars in one round exceed this, the largest non-exempt results
# are persisted to disk.
MAX_ROUND_TOOL_RESULTS_CHARS = 300_000

# ── Absolute single-result hard ceiling (tool-agnostic backstop) ──
# Layer-2 catch-all in clamp_tool_result_text: NO single tool result text
# may exceed this, regardless of tool or per-tool budget — including the
# _BUDGET_EXEMPT_TOOLS (read_files).  This is the structural tripwire that
# ends the "opaque blob enters the text stream" bug CLASS: every historical
# variant (relative-path PNG decoded as text, str()'d __screenshot__ dict,
# base64 leak) tokenises far above this and would have been clamped to a
# degraded-but-alive result instead of a fatal HTTP 400.  Set well above any
# legitimate single text file (a 512KB source file ≈ 512K chars) but far
# below the ~1M-token wall.  __screenshot__ dicts are exempt — they never
# enter the text stream (native image_url protocol).
_SINGLE_RESULT_HARD_CEILING_CHARS = 800_000


# ═══════════════════════════════════════════════════════════════════════════════
#  Internal state (cooldown lock, summary cooldowns, lazy-init latch)
# ═══════════════════════════════════════════════════════════════════════════════
#
# These live here (not in _archive.py / _layer2.py) because two modules
# need them: _layer2's ``_should_force_compact`` reads
# ``_summary_cooldowns`` under ``_cooldown_lock``, and _reactive's
# ``reactive_compact`` clears the entry under the same lock to bypass
# the cooldown.

_summary_cooldowns: dict[str, float] = {}
"""Mapping of conv_id → timestamp of last summary attempt."""

_cooldown_lock = threading.Lock()
"""Protects concurrent access to _summary_cooldowns."""

# Lazy DB-table init latch shared by _archive.py.  Lives here so the
# double-checked lock state survives any module reload pattern.
_tables_initialized = False
_tables_lock = threading.Lock()
