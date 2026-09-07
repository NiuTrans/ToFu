# HOT_PATH
"""OpenClaw faithful compaction method.

  * ``summarize_openclaw`` — summarize-only (no prune pre-pass); trigger
    context_tokens > context_window − reserve(floor 20k); identifier-strict;
    emulates the pre-compaction memory-flush as a durable in-context note.

Verified against ``openclaw/openclaw`` + docs.openclaw.ai (see
``_faithful_methods`` docstring).
"""

from __future__ import annotations

from lib.log import get_logger
from lib.tasks_pkg.compaction._steps import (
    STEP_KIND_STRUCTURAL,
    CompactionContext,
    register_step,
)

from ._shared import (
    _apply_summary,
    _raw_context_limit,
    _select_middle_turns,
    _tok,
)
from ._state import _cooldown_ok, _log_id

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
#  OpenClaw — summarize-only, trigger ctx−reserve(20k floor), memory-flush + ID-strict
# ═══════════════════════════════════════════════════════════════════════════════

_OPENCLAW_RESERVE_FLOOR = 20_000
_OPENCLAW_KEEP_RECENT = 20_000
_OPENCLAW_SUMMARY_PROMPT = (
    'Summarize the earlier portion of this AI coding agent session into a '
    'compact entry so the agent can continue. IDENTIFIER PRESERVATION IS '
    'STRICT: reproduce every file path, opaque ID, symbol name, and error '
    'string EXACTLY — never paraphrase or invent identifiers. Cover the '
    'goal, what was done, key decisions, and what remains. Output only the summary.'
)


@register_step('summarize_openclaw', kind=STEP_KIND_STRUCTURAL, needs=('llm',))
def summarize_openclaw(ctx: CompactionContext) -> int:
    """OpenClaw compaction: trigger context_tokens > context_window − reserve
    (reserve floor 20k); summarize-only (no tool-output prune pre-pass);
    keep a recent tail; identifier-preservation STRICT; emulate the
    pre-compaction memory-flush as a durable in-context note."""
    ctx_window = _raw_context_limit(ctx)
    reserve = int(getattr(ctx.constants, 'OPENCLAW_RESERVE', _OPENCLAW_RESERVE_FLOOR))
    threshold = max(0, ctx_window - reserve)
    total = _tok(ctx.messages, ctx.task)
    if total <= threshold:
        logger.debug('[OpenClaw] conv=%s under threshold (%d≤%d) — skip',
                     _log_id(ctx.conv_id), total, threshold)
        return 0
    if not _cooldown_ok(ctx.conv_id):
        return 0

    keep_recent = int(getattr(ctx.constants, 'OPENCLAW_KEEP_RECENT', _OPENCLAW_KEEP_RECENT))
    middle, text = _select_middle_turns(
        ctx, keep_recent, protect_first_n=1, protect_last_n=1)
    if not middle or len(text) < 400:
        return 0

    # Memory-flush emulation: ask for durable notes folded INTO the summary
    # (we have no on-disk memory file in the harness, so the durable note
    # rides in-context — documented as an emulation, not the real disk flush).
    summary = ctx.summarize(text, instruction=_OPENCLAW_SUMMARY_PROMPT,
                            max_tokens=int(getattr(ctx.constants,
                                                   'FAITHFUL_SUMMARY_MAX_TOKENS', 1800)))
    if not summary:
        return 0
    saved = _apply_summary(
        ctx,
        middle,
        summary,
        '[Compaction entry — earlier turns summarized (identifiers preserved)]',
        total,
    )
    logger.info('[OpenClaw] conv=%s overflow %d>%d → summarize-only (%d turns, ~%d tok)',
                _log_id(ctx.conv_id), total, threshold, len(middle), saved)
    return saved
