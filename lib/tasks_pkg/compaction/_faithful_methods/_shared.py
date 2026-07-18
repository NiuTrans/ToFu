# HOT_PATH
"""Shared LLM-free helpers used across the faithful compaction methods:
token counting, context-limit resolution, text extraction, boundary-aware
middle-turn selection, and summary splicing.
"""

from __future__ import annotations

from lib.log import get_logger
from lib.tasks_pkg.compaction._steps import CompactionContext

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
#  Shared helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _tok(messages: list, task) -> int:
    from lib.tasks_pkg.compaction._tokens import _count_tokens_authoritative
    n, _ = _count_tokens_authoritative(messages, task)
    return n


def _msg_tokens(msg: dict) -> int:
    from lib.tasks_pkg.compaction._tokens import _estimate_msg_tokens
    return _estimate_msg_tokens(msg)


def _raw_context_limit(ctx: CompactionContext) -> int:
    """The model's RAW context window (e.g. 128_000), before any reserve.
    Uses the static limit so each system can apply its OWN reserve, rather
    than the project's _usable_context (which bakes in its own reserve).

    Experiment knob: a per-arm ``OVERFLOW_CONTEXT_LIMIT`` (via
    ``compaction.constant_overrides``) pins the budget the summarizers'
    overflow triggers are computed against, WITHOUT touching the model's
    real context window (global ``model_context_limits`` is left alone).
    This lets a true-1M model (e.g. deepseek-v4-pro) be benchmarked in the
    same compaction regime as a 128k model — the triggers fire at the
    pinned budget while the model retains its full capacity. Absent ⇒ the
    real model limit (default behavior unchanged). MUST be disclosed in
    any writeup as an experiment-local trigger override, not a model cap."""
    override = getattr(ctx.constants, 'OVERFLOW_CONTEXT_LIMIT', None)
    if override:
        try:
            return int(override)
        except (TypeError, ValueError) as e:
            logger.debug('[faithful] bad OVERFLOW_CONTEXT_LIMIT %r: %s', override, e)
    from lib.tasks_pkg.compaction._tokens import _get_context_limit
    return _get_context_limit(ctx.task)


def _max_output_tokens(ctx: CompactionContext) -> int:
    """Best-effort model max-output for OpenCode's reserve = min(20k, maxOut).

    Uses ``_clamp_max_tokens(model, large)`` which returns the model's
    per-family max-output cap (clamping a large request down to it)."""
    model = (ctx.task or {}).get('config', {}).get('model', '')
    try:
        from lib.model_info import _clamp_max_tokens
        capped = _clamp_max_tokens(model, 1_000_000)
        if capped and capped > 0:
            return int(capped)
    except Exception as e:
        logger.debug('[faithful] max_output lookup failed: %s', e)
    return 8192


def _content_text(msg: dict) -> str:
    c = msg.get('content', '')
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return '\n'.join(b.get('text', '') for b in c
                         if isinstance(b, dict) and b.get('type') == 'text')
    return ''


def _select_middle_turns(ctx, keep_recent_tokens: int, protect_first_n: int,
                         protect_last_n: int = 0):
    """Return (middle_turns, rendered_text): evictable turns between a
    protected head (first ``protect_first_n`` turns) and a recent tail
    (most-recent turns until ``keep_recent_tokens`` covered, with a floor
    of ``protect_last_n`` turns). Boundary-aware via MessageEditor."""
    editor = ctx.edit
    if editor is None:
        return [], ''
    turns = editor.turns()
    if len(turns) <= protect_first_n + 1:
        return [], ''

    tail_protected = set()
    acc = 0
    tail_count = 0
    for t in reversed(turns):
        if acc >= keep_recent_tokens and tail_count >= protect_last_n:
            break
        tail_protected.add(t.start)
        acc += sum(_msg_tokens(ctx.messages[i]) for i in t.indices)
        tail_count += 1

    evictable = {t.start for t in editor.evictable_turns()}
    middle = []
    for k, t in enumerate(turns):
        if k < protect_first_n:
            continue
        if t.start in tail_protected:
            continue
        if t.start not in evictable:
            continue
        middle.append(t)

    chunks = []
    for t in middle:
        for i in t.indices:
            txt = _content_text(ctx.messages[i])
            if txt:
                chunks.append(f"[{ctx.messages[i].get('role','?')}] {txt}")
    return middle, '\n\n'.join(chunks).strip()


def _apply_summary(ctx, middle, summary_text: str, banner: str, before_total: int) -> int:
    """Drop the middle turns and splice the summary in at their position."""
    if not middle or not summary_text:
        return 0
    insert_at = min(middle[0].start, len(ctx.messages))
    ctx.edit.drop_turns(middle)
    ctx.messages.insert(min(insert_at, len(ctx.messages)),
                        {'role': 'assistant', 'content': f'{banner}\n{summary_text}'})
    after = _tok(ctx.messages, ctx.task)
    return max(0, before_total - after)
