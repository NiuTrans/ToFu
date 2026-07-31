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

from lib.log import get_logger

logger = get_logger(__name__)


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
while still covering the 20-40-tool-call body of typical SWE-bench runs.

2026-07 (single-giant-turn review): DELIBERATELY KEPT count-based (40), NOT
switched to the token-budget ``adaptive_hot_tail``. The accumulation vector
that motivated a token cap — a burst of L0-exempt ``read_files`` (each ~50–65k
tokens) sitting inside the 40-count hot tail growing past the window before a
turn ends — is now closed STRUCTURALLY at L2/L3, not by retuning L1:
  • L2 ``execute_compact_tool`` folds cold tool-call ROUNDS out of the
    preserved (in-flight) turn (``_fold_recent_intra_turn``), and its trigger
    (``_should_force_compact``) is TOKEN-based — so a hot tail that is large in
    BYTES still trips L2 and gets folded regardless of the L1 count.
  • L3 ``_head_truncate`` is now tool-pairing-safe, the bounded last resort.
So the count-based L1 tail is a cheap, cache-stable, well-understood default
whose worst case is now bounded by the token-aware layers above it. Switching
L1's default to ``adaptive_hot_tail`` is a hyperparameter change (§10.1)
gated on a fresh A/B — it remains available as an opt-in arm
(``compaction.steps=['adaptive_hot_tail']``) but is NOT the default, because
the overflow risk it targeted no longer depends on the L1 count."""

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

_SUMMARY_INPUT_CHAR_CAP = 64_000
"""Hard ceiling on the summary LLM's INPUT size, in characters (§10.1).

The manual /compact's entire wall clock is the single cheap-model summary call
(benchmarked ~96% of a 3 MB conversation's compaction time; all projection +
reconcile CPU is <150 ms). The old ceiling was 200_000 chars — ~3× larger than
a faithful 9-section working-state summary needs — so a giant fold fed the
cheap model a ~200k-char prompt and that single call is what felt slow.

Lowered 200k → 64k (owner sign-off 2026-07-18, §10.1). ``_summary_input_char_budget``
takes ``min(this, usable - reserves)`` so small-window models still bind on
``usable`` first (unchanged); this cap only bites on large (>=~200k) windows —
exactly the multi-MB conversations where /compact was slow. Trimming beyond the
cap is MESSAGE-AWARE (``_format_messages_for_summary``): every ``[user]`` message
is preserved verbatim (summary prompt §6 is MANDATORY), only middle assistant
content is elided. Env-overridable, FAIL-OPEN via
``TOFU_SUMMARY_INPUT_CHAR_CAP`` (unset/garbage→64k; clamped to a sane
[20k, 400k] range so a typo can neither disable the cap nor starve the summary)."""


def summary_input_char_cap() -> int:
    """Resolve the summary-input char ceiling (§10.1 hyperparameter).

    FAIL-OPEN: unset/non-int → :data:`_SUMMARY_INPUT_CHAR_CAP`; any value is
    clamped to [20_000, 400_000] so a bad override can neither remove the cap
    (defeating the speed win) nor starve the summary below a usable floor. Read
    at call time so an operator can retune without a restart."""
    raw = (os.environ.get('TOFU_SUMMARY_INPUT_CHAR_CAP') or '').strip()
    if not raw:
        return _SUMMARY_INPUT_CHAR_CAP
    try:
        val = int(raw)
    except (ValueError, TypeError) as e:
        logger.debug('[Compact] TOFU_SUMMARY_INPUT_CHAR_CAP=%r not an int (%s) '
                     '— using default %d', raw, e, _SUMMARY_INPUT_CHAR_CAP)
        return _SUMMARY_INPUT_CHAR_CAP
    return max(20_000, min(400_000, val))

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

_ARCHIVE_RETENTION_DEFAULT = 50
"""Max ``transcript_archive`` rows retained PER CONVERSATION (ring buffer).

Every force + reactive compaction inserts a full ``messages_json`` row, so on
a year-scale conversation with thousands of compactions the table grows
unbounded (rows were previously only deleted on whole-conversation cleanup).
A GC-on-insert in ``_archive_transcript`` keeps the newest N; older raw
transcripts age out.  Recoverability for the compaction VIEWER is bounded to
the last N compactions — ample for inspecting recent context, and the LIVE
message list is never touched by pruning.  Env-overridable, FAIL-OPEN:
``TOFU_COMPACTION_ARCHIVE_RETENTION`` unset→50, ``0``/<=0→unlimited (never
prune), garbage→default."""


def archive_retention() -> int:
    """Resolve the per-conversation transcript_archive retention (ring buffer).

    FAIL-OPEN: unset→:data:`_ARCHIVE_RETENTION_DEFAULT`, ``0``/<=0→UNLIMITED
    (never prune), non-int→default.  Read at call time (not import) so an
    operator can retune without a restart.
    """
    raw = (os.environ.get('TOFU_COMPACTION_ARCHIVE_RETENTION') or '').strip()
    if not raw:
        return _ARCHIVE_RETENTION_DEFAULT
    try:
        val = int(raw)
    except (ValueError, TypeError) as e:
        logger.debug('[Compact] TOFU_COMPACTION_ARCHIVE_RETENTION=%r not an int '
                     '(%s) — using default %d', raw, e, _ARCHIVE_RETENTION_DEFAULT)
        return _ARCHIVE_RETENTION_DEFAULT
    return val if val > 0 else 0


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
#  Manual /compact — intra-turn compaction (档B, 2026-07-16)
# ═══════════════════════════════════════════════════════════════════════════════
#
# The turn-based boundary always preserves the CURRENT turn whole. A single
# agentic turn (one user request answered with dozens of tool rounds) can
# fill the whole window on its own, so the manual /compact used to refuse it
# ("nothing_to_compact") — the 1M single-giant-turn bug. 档B folds the COLD
# tool rounds INSIDE that one preserved assistant's ``toolRounds`` array (never
# slicing across messages, so no tool round is ever split) when the preserved
# turn itself exceeds ``usable * _PRESERVE_BUDGET_RATIO``.
#
# §10.1: these are hyperparameters — sign-off recorded in JOURNAL 2026-07-16 +
# audit_log('config_change', approved_by='user') at the read site.

_INTRA_TURN_HOT_ROUNDS = 8
"""Most-recent tool rounds kept VERBATIM inside a giant preserved turn when
intra-turn folding (档B) fires — in BOTH the manual /compact path AND the
automatic L2 force-compact path. Everything older than this hot tail is
summarized + dropped. 8 covers the immediate working state (the rounds the
model is actively reasoning about) while collapsing the long cold body that
fills the window. Owner-signed 2026-07-16 (§10.1).

Shared by ``_manual._collect_reserve_folds`` (RAW ``toolRounds``) and
``_layer2._compact.execute_compact_tool`` (expanded api-form round spans) via
the single fold-boundary policy ``_split_cold_rounds`` — one sanctioned value,
two index spaces, so the keep-vs-fold decision can never drift between the two
compaction paths."""

_MANUAL_INTRA_TURN_HOT_ROUNDS = _INTRA_TURN_HOT_ROUNDS
"""Back-compat alias — the manual path's original name for
``_INTRA_TURN_HOT_ROUNDS`` (same sanctioned value, 8). Kept so existing imports
and tests (``from ._constants import _MANUAL_INTRA_TURN_HOT_ROUNDS``) resolve
unchanged now that the automatic path shares the constant."""

_MANUAL_COMPACT_MIN_TOKENS = 4000
"""Manual /compact floor. The ONLY case that declines with
``nothing_to_compact`` is a conversation whose projected total is below this —
a genuinely tiny conversation where compaction would save nothing. Anything
at/above the floor is compactable (turn-based OR intra-turn 档B), so a full
1M single-giant-turn conversation is never wrongly refused. Owner-signed
2026-07-16 (§10.1)."""

_MANUAL_RECONCILE_BUDGET_SEC = 3.0
"""Wall-clock budget for the manual /compact reload→reconcile→CAS loop.

The summary LLM call takes seconds; a sibling agent turn can write to the
conversation TAIL meanwhile. Those writes append/modify the PRESERVED region,
NOT the folded (summarized) region — so the compaction is still valid and must
NOT lose the concurrent work. The engine reloads after summarizing, verifies
the folded prefix is byte-unchanged, rebuilds over the CURRENT tail, and CAS-es
on the CURRENT ``updated_at``.

Why a TIME budget, not a fixed retry COUNT: the reconcile window is now
LLM-free, but on a multi-MB conversation one reload→rebuild→CAS pass still
costs tens-to-hundreds of ms (DB read + json.loads + sorted-key fingerprint +
build_search_text + json_dumps_pg + UPDATE). Under a real write burst
(~1–2 PATCHes/sec, observed 15 in 25 s) a fixed count (e.g. 3 passes) can be
punched through — every pass loses to a fresh tail write → the user sees a 409
on the "must never fail" path. A time budget instead keeps retrying against the
ever-fresher tail until it lands, so a bounded write burst can no longer surface
as a failure. Only a change WITHIN the folded region (a genuine conflict) or
exhausting this budget yields ``stale``. Env-overridable via
``TOFU_MANUAL_RECONCILE_BUDGET_SEC`` (FAIL-OPEN: unset/garbage→3.0, <=0→a small
floor so we always make at least one CAS attempt)."""

_MANUAL_RECONCILE_HARD_ITER_CAP = 1000
"""Absolute iteration ceiling for the reconcile loop — a pure infinite-loop
guard, NOT the operative bound (the time budget above is). Reached only if the
clock never advances (e.g. a frozen/mocked time source); a real burst is bounded
by the seconds budget long before this."""


def manual_reconcile_budget_sec() -> float:
    """Resolve the manual /compact reconcile time budget (seconds).

    FAIL-OPEN: unset/non-float → :data:`_MANUAL_RECONCILE_BUDGET_SEC`; a
    non-positive value is floored to 0.001 s so the loop still makes at least
    one reload→CAS attempt (never disables the reconcile entirely). Read at call
    time so an operator can retune without a restart."""
    raw = (os.environ.get('TOFU_MANUAL_RECONCILE_BUDGET_SEC') or '').strip()
    if not raw:
        return _MANUAL_RECONCILE_BUDGET_SEC
    try:
        val = float(raw)
    except (ValueError, TypeError) as e:
        logger.debug('[Compact] TOFU_MANUAL_RECONCILE_BUDGET_SEC=%r not a float '
                     '(%s) — using default %.1f', raw, e, _MANUAL_RECONCILE_BUDGET_SEC)
        return _MANUAL_RECONCILE_BUDGET_SEC
    return val if val > 0 else 0.001


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
# wasting tokens and time.
#
# ⚠️ THE EXEMPTION MEANS WHAT IT SAYS: ``read_files`` has NO result-size
# ceiling on this layer.  An earlier version of this comment justified the
# exemption with "these tools already have their own internal limits
# (MAX_READ_CHARS=100K per file, BATCH_CHAR_BUDGET=200K)" — BOTH numbers were
# false, and the justification they propped up was therefore fiction:
#   * ``MAX_READ_CHARS`` is 1_000_000 (10× the claimed 100K), which is ABOVE
#     ``MAX_FILE_SIZE`` (512 KB) — so that per-file knife can never fire.
#   * ``read_files``' ``BATCH_CHAR_BUDGET`` is 50 MB (262× the claimed 200K).
# The bounds that ACTUALLY constrain this tool are:
#   1. the 512 KB per-file INPUT gate (``MAX_FILE_SIZE``), and
#   2. ``_SINGLE_RESULT_HARD_CEILING_CHARS`` (800_000) — the tool-agnostic
#      backstop in ``clamp_tool_result_text``, not a per-tool budget.
# Do not re-justify this exemption with a number written in a comment; the
# claim is pinned by an executable assertion in
# ``tests/test_read_files_exemption_contract.py`` instead.
#
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
    # conv_ref bounds its OWN output at lib/conv_ref/_detail.MAX_CHARS (80k) by
    # selecting whole messages. Its budget here must sit ABOVE that so L0 never
    # fires on a normal read: previously the default 60k clipped conv_ref's
    # already-80k-truncated text and persisted THAT to disk while telling the
    # model "Full output saved to: …" — measured 0.54% of the real record on a
    # 15.7 MB row. One layer owns the cap; L0 stays a backstop for the
    # pathological single-huge-message case, where what it writes IS what the
    # tool produced.
    'get_conversation': 100_000,

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
