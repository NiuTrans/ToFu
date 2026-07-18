# HOT_PATH
"""Hermes faithful compaction methods.

  * ``prune_tool_outputs_hermes`` — informative-stub pre-pass (LLM-free)
  * ``summarize_hermes``          — iterative running summary (trigger ctx*0.50)

Verified against ``NousResearch/hermes-agent``
``agent/context_compressor.py`` (see ``_faithful_methods`` docstring).
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
#  Hermes — _prune_old_tool_results() / _summarize_tool_result() (informative stubs)
# ═══════════════════════════════════════════════════════════════════════════════

_HERMES_PRUNE_MIN_CHARS = 200


def _hermes_tool_stub(msg: dict) -> str:
    """Build a Hermes-style informative 1-line stub for a tool output, e.g.
    ``[read_files] output (1,200 chars)`` / ``[grep_search] -> N lines``.
    Falls back to the generic placeholder Hermes uses for unknowns."""
    name = msg.get('name', 'tool')
    text = _content_text(msg)
    lines = text.count('\n') + 1 if text else 0
    if not text:
        return '[Old tool output cleared to save context space]'
    return f'[{name}] output cleared ({len(text):,} chars, {lines} lines)'


@register_step('prune_tool_outputs_hermes')
def prune_tool_outputs_hermes(ctx: CompactionContext) -> int:
    """Hermes pre-pass: replace OLD tool outputs (>200 chars, outside the
    protected recent tail) with informative 1-line stubs. Keeps turns."""
    messages = ctx.messages
    tool_idx = [i for i, m in enumerate(messages) if m.get('role') == 'tool']
    if len(tool_idx) <= 2:
        return 0
    # Hermes protects a recent tail of tool results; emulate with the same
    # 40k tail OpenCode uses unless overridden. (Hermes ties this to its
    # summary tail budget; for the pre-pass a fixed recent guard suffices.)
    protect = int(getattr(ctx.constants, 'HERMES_PRUNE_PROTECT', 20_000))
    protect_tail: set[int] = set()
    acc = 0
    for i in reversed(tool_idx):
        if acc >= protect:
            break
        protect_tail.add(i)
        acc += _msg_tokens(messages[i])

    saved = 0
    n = 0
    for i in tool_idx:
        if i in protect_tail or ctx.is_in_cache_prefix(i):
            continue
        text = _content_text(messages[i])
        if not text or len(text) <= _HERMES_PRUNE_MIN_CHARS:
            continue
        if text.startswith('[') and 'cleared' in text[:60]:
            continue
        stub = _hermes_tool_stub(messages[i])
        saved += (len(text) - len(stub)) // 4
        messages[i]['content'] = stub
        ctx.stamp(messages[i], len(text), len(stub))
        n += 1
    if n:
        logger.info('[Hermes-prune] conv=%s stubbed %d old tool outputs',
                    _log_id(ctx.conv_id), n)
    return saved


# ═══════════════════════════════════════════════════════════════════════════════
#  Hermes summarize — iterative running summary, trigger ctx*0.50, head=3 tail=0.20
# ═══════════════════════════════════════════════════════════════════════════════

_HERMES_SECTIONS = (
    '## Active Task\n## In Progress\n## Pending User Asks\n## Remaining Work'
)
_HERMES_THRESHOLD_PCT = 0.50
_HERMES_PROTECT_FIRST_N = 3
_HERMES_TAIL_RATIO = 0.20
_HERMES_PROTECT_LAST_N = 20


@register_step('summarize_hermes', kind=STEP_KIND_STRUCTURAL, needs=('llm',))
def summarize_hermes(ctx: CompactionContext) -> int:
    """Hermes _generate_summary: trigger prompt_tokens ≥ context_length×0.50;
    protect head (protect_first_n=3) + tail (ratio 0.20, floor protect_last_n=20);
    ITERATIVE running summary (previous fed back); 4-section SUMMARY_PREFIX."""
    _fm = _facade()
    ctx_len = _fm._raw_context_limit(ctx)
    threshold = int(ctx_len * _HERMES_THRESHOLD_PCT)
    total = _fm._tok(ctx.messages, ctx.task)
    if total < threshold:
        logger.debug('[Hermes] conv=%s under threshold (%d<%d) — skip',
                     _log_id(ctx.conv_id), total, threshold)
        return 0
    if not _fm._cooldown_ok(ctx.conv_id):
        return 0

    tail_budget = int(threshold * _HERMES_TAIL_RATIO)
    middle, text = _fm._select_middle_turns(ctx, tail_budget,
                                            protect_first_n=_HERMES_PROTECT_FIRST_N,
                                            protect_last_n=_HERMES_PROTECT_LAST_N)
    if not middle or len(text) < 400:
        return 0

    with _summary_state_lock:
        prev = _running_summaries.get(ctx.conv_id, '')
    if prev:
        instruction = (
            'You maintain a RUNNING summary of an AI coding agent session. '
            'Below is the CURRENT running summary then NEW transcript. UPDATE '
            'the running summary: migrate completed items out of "In Progress", '
            'fold finished work into "Active Task", add new pending items. '
            'PRESERVE file paths, identifiers, error messages verbatim. Keep '
            f'EXACTLY these sections:\n{_HERMES_SECTIONS}\nOutput ONLY the updated summary.'
        )
        blob = f'=== CURRENT RUNNING SUMMARY ===\n{prev}\n\n=== NEW TRANSCRIPT ===\n{text}'
    else:
        instruction = (
            'Summarize this AI coding agent session into a running summary. '
            'PRESERVE file paths, identifiers, error messages verbatim. Use '
            f'EXACTLY these sections:\n{_HERMES_SECTIONS}\nOutput ONLY the summary.'
        )
        blob = text
    summary = ctx.summarize(blob, instruction=instruction,
                            max_tokens=int(getattr(ctx.constants,
                                                   'FAITHFUL_SUMMARY_MAX_TOKENS', 1800)))
    if not summary:
        return 0
    with _summary_state_lock:
        _running_summaries[ctx.conv_id] = summary
    saved = _fm._apply_summary(ctx, middle, summary,
                               '[Running summary of session so far]', total)
    logger.info('[Hermes] conv=%s threshold %d≥%d → iterative summary (%s, %d turns, ~%d tok)',
                _log_id(ctx.conv_id), total, threshold,
                'updated' if prev else 'initial', len(middle), saved)
    return saved
