# HOT_PATH
"""Experimental Layer-1 compaction methods (Stage 4 of the joint
cache+compaction optimization).

Unlike the built-in steps in ``_builtin_steps.py`` — which replace a cold
tool result with a "re-call tool if needed" placeholder and thereby
*forget* the information — the methods here aim to be
**information-preserving** so they can shrink context WITHOUT lowering
task-success rate (the thing aggressive compaction risks).

Methods
-------
* ``latest_state_dedup`` (M1)
    A coding agent re-reads the same file many times.  Only the *most
    recent* read of a given path is the current truth; every *earlier*
    read of that same path is stale.  M1 collapses the stale earlier
    reads to a one-line "superseded by a later read" marker, keeping the
    latest read verbatim.  Zero information loss, and it directly kills
    the read→compact→re-read churn loop the placeholder approach causes.

* ``fold_observations`` (M2)
    Replace a cold ``grep_search`` / ``find_files`` / ``list_dir`` result
    with a one-line *structured fact* extracted cheaply (no LLM): e.g.
    ``grep_search → 3 match line(s)``.  Preserves the *answer* the tool
    produced rather than just "it was compacted", so the model rarely
    needs to re-call it.

Both are registered steps: select them via
``task['config']['compaction']['steps']``.  Both respect
``ctx.is_in_cache_prefix`` and call ``ctx.stamp`` for durable placeholders,
exactly like the built-ins, and neither calls the LLM.
"""

from __future__ import annotations

import re

from lib.log import get_logger
from lib.tasks_pkg.compaction._steps import (
    STEP_KIND_STRUCTURAL,
    CompactionContext,
    register_step,
)

logger = get_logger(__name__)


def _log_id(conv_id: str) -> str:
    return conv_id[:8] if conv_id else '?'


def _content_str(msg: dict) -> str | None:
    """Return the message's text content as a string, joining multimodal
    text blocks.  Returns None when there is no text to operate on."""
    c = msg.get('content', '')
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        parts = [b.get('text', '') for b in c
                 if isinstance(b, dict) and b.get('type') == 'text']
        return '\n'.join(parts) if parts else None
    return None


def _already_compacted(text: str) -> bool:
    head = text[:80]
    return (text.startswith('[') and
            ('compacted' in head or 'superseded' in head
             or text.startswith('[Persisted to:')))


# ═══════════════════════════════════════════════════════════════════════════════
#  M1 — latest-state file dedup
# ═══════════════════════════════════════════════════════════════════════════════

# Matches the read_tools header, e.g.:
#   "File: lib/server.py (320 lines, 12.1KB)"
#   "File: src/main.py (lines 1-200 of 980)"
_FILE_HEADER_RE = re.compile(r'^File:\s+(?P<path>[^\s(]+)', re.MULTILINE)

# Tools whose results represent the *state of a file path* (so an older
# read of the same path is superseded by a newer one).
_FILE_READ_TOOLS = frozenset({'read_files', 'read_file'})


def _paths_in_read_result(text: str) -> list[str]:
    """Extract the file path(s) a read result covers, via its ``File:``
    headers.  A batched read_files result has several headers."""
    return _FILE_HEADER_RE.findall(text)


@register_step('latest_state_dedup')
def latest_state_dedup(ctx: CompactionContext) -> int:
    """Supersede stale earlier reads of a file path (M1).

    For each ``read_files`` tool result, determine the set of file paths
    it covers.  Walking newest→oldest, the first time a path is seen it is
    "live"; any older result whose paths are ALL already covered by newer
    reads is collapsed to a one-line superseded marker.

    Conservative rules (to never drop live information):
      * Only acts on results from ``_FILE_READ_TOOLS``.
      * A result is superseded only if EVERY path it covers has been seen
        in a strictly newer result (a batched read covering an extra path
        is kept).
      * The single most-recent result is always kept verbatim.
      * Cache-prefix and hot-tail rules still apply (we never touch the
        ``MICRO_HOT_TAIL`` newest tool results, nor the cache prefix).
    """
    _c = ctx.constants
    messages = ctx.messages

    tool_indices = [i for i, m in enumerate(messages) if m.get('role') == 'tool']
    if len(tool_indices) <= _c.MICRO_HOT_TAIL:
        return 0
    cold_set = set(tool_indices[:-_c.MICRO_HOT_TAIL])

    seen_paths: set[str] = set()
    superseded = 0
    tokens_saved = 0

    # Walk newest→oldest so the most recent read of each path wins.
    for idx in reversed(tool_indices):
        msg = messages[idx]
        if msg.get('name') not in _FILE_READ_TOOLS:
            continue
        text = _content_str(msg)
        if text is None or _already_compacted(text):
            # Still register its paths as "seen" so older dupes supersede.
            if text is not None:
                for p in _paths_in_read_result(text):
                    seen_paths.add(p)
            continue

        paths = _paths_in_read_result(text)
        if not paths:
            continue

        is_cold = idx in cold_set and not ctx.is_in_cache_prefix(idx)
        all_superseded = all(p in seen_paths for p in paths)

        if is_cold and all_superseded:
            old_len = len(text)
            path_list = ', '.join(paths[:5]) + ('…' if len(paths) > 5 else '')
            placeholder = (
                f'[read_files superseded — a later read of '
                f'{path_list} reflects the current file state]'
            )
            msg['content'] = placeholder
            tokens_saved += (old_len - len(placeholder)) // 4
            superseded += 1
            ctx.stamp(msg, old_len, len(placeholder))
        else:
            # This read is the freshest for at least one path → it's live.
            for p in paths:
                seen_paths.add(p)

    if superseded > 0:
        logger.info('[M1-dedup] conv=%s  superseded %d stale file reads '
                    '(~%d tokens saved; latest read kept verbatim)',
                    _log_id(ctx.conv_id), superseded, tokens_saved)
    return tokens_saved


# ═══════════════════════════════════════════════════════════════════════════════
#  M2 — observation folding (structured one-line facts)
# ═══════════════════════════════════════════════════════════════════════════════

# Tools whose output can be losslessly summarised to a one-line fact.
_FOLDABLE_TOOLS = frozenset({'grep_search', 'find_files', 'list_dir'})


def _fold_fact(tool_name: str, text: str) -> str | None:
    """Produce a one-line structured fact for a foldable tool result, or
    None if we can't summarise it confidently (then leave it to the
    generic compactor)."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if tool_name == 'grep_search':
        # Result lines look like "path:line: match" — count them.
        match_lines = [ln for ln in lines
                       if re.match(r'.+:\d+:', ln) or re.search(r':\d+:', ln)]
        n = len(match_lines) if match_lines else max(0, len(lines) - 1)
        sample = match_lines[0][:120] if match_lines else ''
        fact = f'[grep_search folded — {n} match line(s)]'
        if sample:
            fact += f' first: {sample}'
        return fact
    if tool_name == 'find_files':
        n = len(lines)
        return f'[find_files folded — {n} path(s) matched]'
    if tool_name == 'list_dir':
        return f'[list_dir folded — {len(lines)} entr(y/ies) listed]'
    return None


@register_step('fold_observations')
def fold_observations(ctx: CompactionContext) -> int:
    """Fold cold search/list tool results to a one-line structured fact (M2).

    Preserves the *answer* (how many hits / paths / entries, plus the
    first hit) instead of a generic "re-call tool" placeholder, so the
    model keeps the useful signal at a fraction of the tokens.
    """
    _c = ctx.constants
    messages = ctx.messages

    tool_indices = [i for i, m in enumerate(messages) if m.get('role') == 'tool']
    if len(tool_indices) <= _c.MICRO_HOT_TAIL:
        return 0
    cold_indices = tool_indices[:-_c.MICRO_HOT_TAIL]

    folded = 0
    tokens_saved = 0

    for idx in cold_indices:
        if ctx.is_in_cache_prefix(idx):
            continue
        msg = messages[idx]
        tool_name = msg.get('name', '')
        if tool_name not in _FOLDABLE_TOOLS:
            continue
        text = _content_str(msg)
        if text is None or _already_compacted(text):
            continue
        if len(text) <= _c.MICRO_COMPACT_THRESHOLD:
            continue
        fact = _fold_fact(tool_name, text)
        if not fact:
            continue
        old_len = len(text)
        msg['content'] = fact
        tokens_saved += (old_len - len(fact)) // 4
        folded += 1
        ctx.stamp(msg, old_len, len(fact))

    if folded > 0:
        logger.info('[M2-fold] conv=%s  folded %d cold observation results '
                    '(~%d tokens saved; structured fact preserved)',
                    _log_id(ctx.conv_id), folded, tokens_saved)
    return tokens_saved


# ═══════════════════════════════════════════════════════════════════════════════
#  Advanced-host examples — different method FORMS (structural / LLM)
#
#  These demonstrate that the generalized step contract supports method
#  shapes that do NOT fit the cheap every-round L1 host.  They run only
#  under the advanced host (Stage B), selected via
#  ``task['config']['compaction']['advanced_steps']``.
# ═══════════════════════════════════════════════════════════════════════════════

# Tools whose tool_call args carry a file path (used to decide whether an
# old turn's work has been superseded by later edits to the same files).
_PATH_TOOLS = frozenset({
    'read_files', 'read_file', 'write_file', 'apply_diff', 'apply_diffs',
    'insert_content', 'insert_contents',
})


@register_step('drop_superseded_turns', kind=STEP_KIND_STRUCTURAL)
def drop_superseded_turns(ctx: CompactionContext) -> int:
    """STRUCTURAL example: delete whole evictable turns whose assistant
    messages are pure mechanical tool activity (no natural-language
    synthesis).

    A *turn* = ``[user request, ...assistant/tool work]``.  Whole-turn
    eviction necessarily drops the turn's user request too — that is the
    nature of structural turn-drop (it is why Layer 2 *summarizes* before
    dropping).  This standalone variant is therefore lossy by design and
    is opt-in: it targets cold turns that were pure exploration
    (assistant emitted only tool calls, no prose decision/explanation),
    on the theory that such turns carry no reasoning the model still
    needs.  Methods that must preserve the request should pair this with
    a summary (see :func:`summarize_oldest_turn`).

    The :class:`MessageEditor` guarantees we never drop the in-flight
    turn or a turn overlapping the cache prefix, and whole-turn deletion
    can't orphan a tool_call↔tool pair.  Real structural methods
    (relevance-based eviction, etc.) plug in the same way.
    """
    editor = ctx.edit
    if editor is None:  # not granted (wrong host) — defensive no-op
        return 0

    def _turn_is_pure_tool_activity(turn) -> bool:
        msgs = ctx.messages
        saw_tool = False
        for i in turn.indices:
            m = msgs[i]
            role = m.get('role')
            if role == 'tool':
                saw_tool = True
            elif role == 'assistant':
                # Any natural-language assistant content → keep the turn
                # (it holds a decision/explanation worth preserving). The
                # user request is part of the turn and is dropped with it.
                c = m.get('content')
                if isinstance(c, str) and c.strip():
                    return False
                if isinstance(c, list) and any(
                        isinstance(b, dict) and b.get('type') == 'text'
                        and b.get('text', '').strip() for b in c):
                    return False
        return saw_tool

    victims = [t for t in editor.evictable_turns()
               if _turn_is_pure_tool_activity(t)]
    if not victims:
        return 0
    return editor.drop_turns(victims)


@register_step('summarize_oldest_turn', kind=STEP_KIND_STRUCTURAL,
               needs=('llm',))
def summarize_oldest_turn(ctx: CompactionContext) -> int:
    """LLM example: replace the single oldest evictable turn with a terse
    cheap-model summary injected as one assistant message.

    Demonstrates the ``needs=('llm',)`` capability (``ctx.summarize``)
    combined with structural surgery (``ctx.edit``): the turn's prose is
    summarized, the turn is dropped, and a one-line summary message is
    spliced in at the turn's former position.  A real recursive/rolling
    summarizer would maintain a running note across rounds; this keeps
    the example minimal but exercises both granted capabilities.
    """
    editor = ctx.edit
    if editor is None:
        return 0
    evictable = editor.evictable_turns()
    if not evictable:
        return 0

    oldest = evictable[0]
    msgs = ctx.messages

    # Gather the turn's natural-language text for summarization.
    chunks = []
    for i in oldest.indices:
        t = _content_str(msgs[i])
        if t and not _already_compacted(t):
            role = msgs[i].get('role', '?')
            chunks.append(f'[{role}] {t}')
    blob = '\n\n'.join(chunks).strip()
    if len(blob) < 400:  # not worth an LLM round-trip
        return 0

    summary = ctx.summarize(
        blob, instruction='Summarize this earlier turn of a coding session '
        'in 2-3 sentences, preserving file paths, decisions, and outcomes.',
        max_tokens=256)
    if not summary:
        return 0

    saved = editor.drop_turns([oldest])
    if saved <= 0:
        return 0

    # Splice the summary in at the (now-shifted) position of the dropped
    # turn's start.  After drop_turns, the message that was at
    # oldest.end is now at index oldest.start.
    summary_msg = {
        'role': 'assistant',
        'content': f'[Earlier turn summarized] {summary}',
    }
    insert_at = min(oldest.start, len(msgs))
    msgs.insert(insert_at, summary_msg)
    saved -= len(summary_msg['content']) // 4
    logger.info('[Adv-summary] conv=%s  summarized + dropped oldest turn '
                '(~%d net tokens saved)',
                _log_id(ctx.conv_id), max(0, saved))
    return max(0, saved)



# ═══════════════════════════════════════════════════════════════════════════════
#  OpenCode-inspired transform steps (LLM-free)
#
#  Two ideas borrowed from sst/opencode's compaction.ts, expressed as
#  registered transform steps:
#    * prune_with_hysteresis — only prune cold tool output when there is a
#      WORTHWHILE amount to reclaim (PRUNE_MINIMUM), and always protect a
#      token-budget tail (PRUNE_PROTECT). The hysteresis avoids the
#      compact→re-read→recompact churn loop a per-result threshold causes.
#    * adaptive_hot_tail — replace the fixed MICRO_HOT_TAIL count with a
#      token-budget boundary that walks back over tool-pairs, so the hot
#      window self-tunes to the model's context size.
# ═══════════════════════════════════════════════════════════════════════════════

# Defaults mirror opencode's PRUNE_PROTECT (40k) / PRUNE_MINIMUM (20k) in
# spirit but scaled to our smaller per-step budgets; overridable via
# constant_overrides for experiment arms.
_PRUNE_PROTECT_TOKENS_DEFAULT = 12_000
_PRUNE_MINIMUM_TOKENS_DEFAULT = 4_000


def _tool_text_len(msg: dict) -> int:
    t = _content_str(msg)
    return len(t) if t else 0


@register_step('prune_with_hysteresis')
def prune_with_hysteresis(ctx: CompactionContext) -> int:
    """Prune cold tool output, but only when worthwhile (OpenCode-style).

    Two-stage gate (the hysteresis):
      1. Protect a token-budget tail: walk backward over tool results
         accumulating their estimated tokens until ``PRUNE_PROTECT``
         tokens are covered — those are never pruned (cheaper + simpler
         than a fixed count, and adapts to result sizes).
      2. Only prune the remaining (older) tool results if the total
         reclaimable estimate exceeds ``PRUNE_MINIMUM``. If there's little
         to gain, do nothing — this is what prevents the
         compact→re-read→recompact churn loop that a per-result threshold
         causes (re-reading a file re-creates a big result that would be
         re-pruned next round for tiny gains).

    Tunables (via ``constant_overrides``):
      * ``PRUNE_PROTECT_TOKENS`` (default 12000)
      * ``PRUNE_MINIMUM_TOKENS``  (default 4000)
    """
    _c = ctx.constants
    messages = ctx.messages
    protect = int(getattr(_c, 'PRUNE_PROTECT_TOKENS', _PRUNE_PROTECT_TOKENS_DEFAULT))
    minimum = int(getattr(_c, 'PRUNE_MINIMUM_TOKENS', _PRUNE_MINIMUM_TOKENS_DEFAULT))

    tool_indices = [i for i, m in enumerate(messages) if m.get('role') == 'tool']
    if not tool_indices:
        return 0

    # Stage 1: protect a token-budget tail (walk newest→oldest).
    protected: set[int] = set()
    acc = 0
    for idx in reversed(tool_indices):
        if acc >= protect:
            break
        protected.add(idx)
        acc += _tool_text_len(messages[idx]) // 4

    prunable = [i for i in tool_indices if i not in protected
                and not ctx.is_in_cache_prefix(i)]

    # Estimate reclaimable tokens across prunable cold results.
    reclaimable = 0
    candidates = []
    for idx in prunable:
        msg = messages[idx]
        text = _content_str(msg)
        if text is None or _already_compacted(text):
            continue
        if len(text) <= _c.MICRO_COMPACT_THRESHOLD:
            continue
        reclaimable += len(text) // 4
        candidates.append((idx, len(text)))

    # Stage 2: hysteresis — only act if the gain clears the minimum.
    if reclaimable < minimum:
        logger.debug('[OC-prune] conv=%s  reclaimable=%d < minimum=%d — '
                     'skipping (avoids churn)',
                     _log_id(ctx.conv_id), reclaimable, minimum)
        return 0

    pruned = 0
    tokens_saved = 0
    for idx, old_len in candidates:
        msg = messages[idx]
        tool_name = msg.get('name', 'tool')
        placeholder = (f'[{tool_name} output pruned — was {old_len:,} chars '
                       f'— re-call tool if needed]')
        msg['content'] = placeholder
        tokens_saved += (old_len - len(placeholder)) // 4
        pruned += 1
        ctx.stamp(msg, old_len, len(placeholder))

    if pruned > 0:
        logger.info('[OC-prune] conv=%s  pruned %d cold tool results past the '
                    '%d-token protected tail (~%d tokens saved; minimum=%d met)',
                    _log_id(ctx.conv_id), pruned, protect, tokens_saved, minimum)
    return tokens_saved


_ADAPTIVE_TAIL_BUDGET_DEFAULT = 24_000


@register_step('adaptive_hot_tail')
def adaptive_hot_tail(ctx: CompactionContext) -> int:
    """Token-budget hot tail instead of a fixed MICRO_HOT_TAIL count.

    Walks backward over ALL messages accumulating estimated tokens until
    ``ADAPTIVE_TAIL_BUDGET`` is covered; every cold tool result before
    that boundary (and outside the cache prefix) is compacted with the
    same placeholder style as the generic compactor.

    This is a drop-in alternative to ``compact_tool_results`` for arms
    that want the hot window to scale with content size rather than a
    fixed message count. Use ONE of them in a given arm, not both.

    Tunable: ``ADAPTIVE_TAIL_BUDGET`` (default 24000) via ``constant_overrides``.
    """
    _c = ctx.constants
    messages = ctx.messages
    budget = int(getattr(_c, 'ADAPTIVE_TAIL_BUDGET', _ADAPTIVE_TAIL_BUDGET_DEFAULT))

    # Find the boundary index: everything at boundary..end is "hot".
    acc = 0
    boundary = len(messages)
    for idx in range(len(messages) - 1, -1, -1):
        if acc >= budget:
            boundary = idx + 1
            break
        t = _content_str(messages[idx])
        acc += (len(t) // 4) if t else 0
        boundary = idx

    compacted = 0
    tokens_saved = 0
    for idx in range(boundary):
        if ctx.is_in_cache_prefix(idx):
            continue
        msg = messages[idx]
        if msg.get('role') != 'tool':
            continue
        text = _content_str(msg)
        if text is None or _already_compacted(text):
            continue
        if len(text) <= _c.MICRO_COMPACT_THRESHOLD:
            continue
        tool_name = msg.get('name', 'tool')
        old_len = len(text)
        placeholder = (f'[{tool_name} result compacted — was {old_len:,} chars '
                       f'— re-call tool if full content needed]')
        msg['content'] = placeholder
        tokens_saved += (old_len - len(placeholder)) // 4
        compacted += 1
        ctx.stamp(msg, old_len, len(placeholder))

    if compacted > 0:
        logger.info('[OC-adaptive] conv=%s  compacted %d cold tool results '
                    'outside %d-token hot tail (~%d tokens saved)',
                    _log_id(ctx.conv_id), compacted, budget, tokens_saved)
    return tokens_saved
