# HOT_PATH
"""OpenCode faithful compaction methods.

  * ``prune_tool_outputs_opencode`` — OpenCode ``prune()`` (LLM-free pre-pass)
  * ``summarize_opencode``          — one-shot middle-band summary w/ previousSummary

Verified against ``sst/opencode`` ``session/overflow.ts`` +
``session/compaction.ts`` (see module ``_faithful_methods`` docstring).
"""

from __future__ import annotations

from lib.log import get_logger
from lib.tasks_pkg.compaction._steps import (
    STEP_KIND_STRUCTURAL,
    CompactionContext,
    register_step,
)

from ._shared import _content_text, _msg_tokens
from ._state import _log_id, _running_summaries, _summary_state_lock

logger = get_logger(__name__)


# The tunable helpers (context-limit resolution, token counting, cooldown
# gate, middle-turn selection, summary splicing) are resolved through the
# package FACADE at CALL time — never bound at import — so that callers /
# tests that patch ``lib.tasks_pkg.compaction._faithful_methods.<helper>``
# take effect, exactly as when everything lived in one module.
def _facade():
    import lib.tasks_pkg.compaction._faithful_methods as _fm
    return _fm


# ═══════════════════════════════════════════════════════════════════════════════
#  OpenCode — prune() (LLM-free pre-pass)
#  compaction.ts: PRUNE_PROTECT=40k, PRUNE_MINIMUM=20k, TOOL_OUTPUT_MAX_CHARS=2000,
#  PRUNE_PROTECTED_TOOLS=["skill"], walk backwards, skip 2 most-recent turns.
# ═══════════════════════════════════════════════════════════════════════════════

_OC_PRUNE_PROTECT = 40_000
_OC_PRUNE_MINIMUM = 20_000
_OC_TOOL_OUTPUT_MAX_CHARS = 2_000
_OC_PRUNE_PROTECTED_TOOLS = frozenset({'skill'})
_OC_PRUNE_SKIP_RECENT_TURNS = 2


@register_step('prune_tool_outputs_opencode')
def prune_tool_outputs_opencode(ctx: CompactionContext) -> int:
    """OpenCode prune(): walk backwards over messages; skip the 2
    most-recent turns; protect the most-recent 40k tokens of tool output;
    mark older completed tool outputs (>2000 chars, tool not in {skill})
    for pruning; only commit if reclaimed > 20k. Strips OUTPUT, keeps call.
    """
    messages = ctx.messages
    # Turn index by counting user messages from the end (backward).
    user_seen = 0
    protected_recent: set[int] = set()
    cur_turn_msgs: list[int] = []
    for i in range(len(messages) - 1, -1, -1):
        cur_turn_msgs.append(i)
        if messages[i].get('role') == 'user':
            user_seen += 1
            if user_seen <= _OC_PRUNE_SKIP_RECENT_TURNS:
                protected_recent.update(cur_turn_msgs)
            cur_turn_msgs = []

    tool_idx = [i for i, m in enumerate(messages) if m.get('role') == 'tool']
    if not tool_idx:
        return 0

    # Protect most-recent 40k tokens of tool output (backward).
    protect_tail: set[int] = set()
    acc = 0
    for i in reversed(tool_idx):
        if acc >= _OC_PRUNE_PROTECT:
            break
        protect_tail.add(i)
        acc += _msg_tokens(messages[i])

    candidates = []
    reclaim = 0
    for i in tool_idx:
        if i in protect_tail or i in protected_recent or ctx.is_in_cache_prefix(i):
            continue
        if messages[i].get('name', '') in _OC_PRUNE_PROTECTED_TOOLS:
            continue
        text = _content_text(messages[i])
        if not text or (text.startswith('[') and 'pruned' in text[:40]):
            continue
        if len(text) <= _OC_TOOL_OUTPUT_MAX_CHARS:
            continue
        reclaim += _msg_tokens(messages[i])
        candidates.append((i, len(text)))

    if reclaim < _OC_PRUNE_MINIMUM:
        logger.debug('[OCprune] conv=%s reclaim=%d < %d — skip',
                     _log_id(ctx.conv_id), reclaim, _OC_PRUNE_MINIMUM)
        return 0

    saved = 0
    for i, old_len in candidates:
        msg = messages[i]
        stub = '[Tool output pruned to save context]'
        saved += (old_len - len(stub)) // 4
        msg['content'] = stub
        ctx.stamp(msg, old_len, len(stub))

    logger.info('[OCprune] conv=%s pruned %d tool outputs (reclaim≈%d ≥ %d)',
                _log_id(ctx.conv_id), len(candidates), reclaim, _OC_PRUNE_MINIMUM)
    return saved


# ═══════════════════════════════════════════════════════════════════════════════
#  OpenCode summarize — one-shot, middle band, 7-section template, previousSummary
# ═══════════════════════════════════════════════════════════════════════════════

_OC_SUMMARY_PROMPT = (
    'You are compacting an AI coding agent session. Produce a structured '
    'summary of the work below so the agent can continue seamlessly. '
    'PRESERVE verbatim: file paths, exact identifiers/symbols, error '
    'messages. Use EXACTLY these sections:\n'
    '## Goal\n## Constraints\n## Progress\n### Done\n### In Progress\n'
    '### Blocked\n## Key Decisions\n## Next Steps\n## Critical Context\n'
    '## Relevant Files\nOutput only the summary.'
)


def _oc_usable(ctx) -> int:
    _fm = _facade()
    limit = _fm._raw_context_limit(ctx)
    reserved = min(20_000, _fm._max_output_tokens(ctx))
    return max(0, limit - reserved)


@register_step('summarize_opencode', kind=STEP_KIND_STRUCTURAL, needs=('llm',))
def summarize_opencode(ctx: CompactionContext) -> int:
    """OpenCode summarize: trigger total ≥ usable (= limit−min(20k,maxout));
    protect head + recent tail (DEFAULT_TAIL_TURNS=2, budget
    min(8000,max(2000,usable*0.25))); summarize the MIDDLE band ONE-SHOT
    with previousSummary fed back; 7-section template."""
    _fm = _facade()
    usable = _fm._oc_usable(ctx)
    total = _fm._tok(ctx.messages, ctx.task)
    if total < usable:
        logger.debug('[OCsum] conv=%s under usable (%d<%d) — skip',
                     _log_id(ctx.conv_id), total, usable)
        return 0
    if not _fm._cooldown_ok(ctx.conv_id):
        return 0

    tail_budget = min(8000, max(2000, int(usable * 0.25)))
    middle, text = _fm._select_middle_turns(ctx, tail_budget, protect_first_n=1,
                                            protect_last_n=2)
    if not middle or len(text) < 400:
        return 0

    with _summary_state_lock:
        prev = _running_summaries.get(ctx.conv_id, '')
    blob = (f'=== PREVIOUS SUMMARY ===\n{prev}\n\n=== NEW WORK ===\n{text}'
            if prev else text)
    summary = ctx.summarize(blob, instruction=_OC_SUMMARY_PROMPT,
                            max_tokens=int(getattr(ctx.constants,
                                                   'FAITHFUL_SUMMARY_MAX_TOKENS', 1800)))
    if not summary:
        return 0
    with _summary_state_lock:
        _running_summaries[ctx.conv_id] = summary
    saved = _fm._apply_summary(ctx, middle, summary,
                               '[Context compacted — earlier work summarized]', total)
    logger.info('[OCsum] conv=%s overflow %d≥%d → %d middle turns (~%d tok saved)',
                _log_id(ctx.conv_id), total, usable, len(middle), saved)
    return saved
