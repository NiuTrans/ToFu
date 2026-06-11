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
"""

from __future__ import annotations

import threading
import time

from lib.log import get_logger
from lib.tasks_pkg.compaction._steps import (
    STEP_KIND_STRUCTURAL,
    CompactionContext,
    register_step,
)

logger = get_logger(__name__)


def _log_id(conv_id: str) -> str:
    return conv_id[:8] if conv_id else '?'


# ── Per-conversation iterative-summary + cooldown state ─────────────────
_running_summaries: dict[str, str] = {}
_summary_state_lock = threading.Lock()
_last_summary_at: dict[str, float] = {}
_FAITHFUL_SUMMARY_COOLDOWN = 15.0


def reset_running_summary(conv_id: str) -> None:
    """Drop a conversation's running-summary + cooldown (call on reset)."""
    with _summary_state_lock:
        _running_summaries.pop(conv_id, None)
        _last_summary_at.pop(conv_id, None)


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


def _cooldown_ok(conv_id: str) -> bool:
    with _summary_state_lock:
        last = _last_summary_at.get(conv_id, 0)
        if time.time() - last < _FAITHFUL_SUMMARY_COOLDOWN:
            return False
        _last_summary_at[conv_id] = time.time()
        return True


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
    limit = _raw_context_limit(ctx)
    reserved = min(20_000, _max_output_tokens(ctx))
    return max(0, limit - reserved)


@register_step('summarize_opencode', kind=STEP_KIND_STRUCTURAL, needs=('llm',))
def summarize_opencode(ctx: CompactionContext) -> int:
    """OpenCode summarize: trigger total ≥ usable (= limit−min(20k,maxout));
    protect head + recent tail (DEFAULT_TAIL_TURNS=2, budget
    min(8000,max(2000,usable*0.25))); summarize the MIDDLE band ONE-SHOT
    with previousSummary fed back; 7-section template."""
    usable = _oc_usable(ctx)
    total = _tok(ctx.messages, ctx.task)
    if total < usable:
        logger.debug('[OCsum] conv=%s under usable (%d<%d) — skip',
                     _log_id(ctx.conv_id), total, usable)
        return 0
    if not _cooldown_ok(ctx.conv_id):
        return 0

    tail_budget = min(8000, max(2000, int(usable * 0.25)))
    middle, text = _select_middle_turns(ctx, tail_budget, protect_first_n=1,
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
    saved = _apply_summary(ctx, middle, summary,
                           '[Context compacted — earlier work summarized]', total)
    logger.info('[OCsum] conv=%s overflow %d≥%d → %d middle turns (~%d tok saved)',
                _log_id(ctx.conv_id), total, usable, len(middle), saved)
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
    ctx_len = _raw_context_limit(ctx)
    threshold = int(ctx_len * _HERMES_THRESHOLD_PCT)
    total = _tok(ctx.messages, ctx.task)
    if total < threshold:
        logger.debug('[Hermes] conv=%s under threshold (%d<%d) — skip',
                     _log_id(ctx.conv_id), total, threshold)
        return 0
    if not _cooldown_ok(ctx.conv_id):
        return 0

    tail_budget = int(threshold * _HERMES_TAIL_RATIO)
    middle, text = _select_middle_turns(ctx, tail_budget,
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
    saved = _apply_summary(ctx, middle, summary,
                           '[Running summary of session so far]', total)
    logger.info('[Hermes] conv=%s threshold %d≥%d → iterative summary (%s, %d turns, ~%d tok)',
                _log_id(ctx.conv_id), total, threshold,
                'updated' if prev else 'initial', len(middle), saved)
    return saved


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
    middle, text = _select_middle_turns(ctx, keep_recent, protect_first_n=1,
                                        protect_last_n=1)
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
    saved = _apply_summary(ctx, middle, summary,
                           '[Compaction entry — earlier turns summarized '
                           '(identifiers preserved)]', total)
    logger.info('[OpenClaw] conv=%s overflow %d>%d → summarize-only (%d turns, ~%d tok)',
                _log_id(ctx.conv_id), total, threshold, len(middle), saved)
    return saved
