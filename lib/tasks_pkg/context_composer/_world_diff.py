"""World-state differential annotation (Codex world_state.rs port, adapted).

codex-rs builds a per-step ``WorldState`` where every section carries the
PREVIOUS turn's value and emits only when it changed — the model is told the
delta instead of re-reading a full restatement each step.

Tofu already eliminates the redundancy pattern Codex's diffing targets
(persistence strips a turn's tail blocks, so each turn re-renders them fresh
and exactly ONE copy rides the wire — there is nothing to skip). What does
NOT exist here is the change signal: when a sibling conversation claims an
epic between two of this conv's turns, the ``project_board`` block just
arrives with different bytes and the model must diff it mentally.

This module closes that gap for the volatile, turn-stability tail blocks:
each annotated block gets a one-line world-state trailer —

  * first sight   — no trailer (no baseline; the store learns the render);
  * unchanged     — ``[World-state: unchanged since your last turn]`` so the
                    model can skim instead of re-parse;
  * changed       — ``[World-state Δ since your last turn: +A −B line(s)]``
                    plus the added/removed lines (difflib, capped) so the
                    model sees exactly what moved.

The baseline store is in-memory only, keyed by conv id. A process restart
simply means "no baseline" → full render with no trailer — never a wrong
delta. Only whitelisted block ids are annotated (structured, line-oriented,
multi-conv-churned renders); free-form blocks (relevant_memories, plan_mode)
are left alone — a line diff there would be noise.

Hooked from ``compose_context`` (single composition boundary) so endpoint
re-entries and ``append_context_blocks`` round-scoped renders never double-
annotate.
"""

from __future__ import annotations

import dataclasses
import difflib
import threading
import time

from lib.log import get_logger
from lib.tasks_pkg.context_composer._models import ContextBlock

logger = get_logger(__name__)

# Block ids eligible for differential annotation — line-oriented, churned by
# SIBLING conversations between this conv's turns.
_DIFFABLE_BLOCK_IDS = frozenset({
    'project_board', 'project_goals', 'related_conversations',
})

_MAX_CONVS = 256
_MAX_BLOCKS_PER_CONV = 16
# Cap the delta excerpt: a board upheaval must not flood the tail.
_MAX_DELTA_LINES = 8

_UNCHANGED_TRAILER = '[World-state: unchanged since your last turn]'
_DELTA_HEADER = '[World-state Δ since your last turn: +%d −%d line(s)]'

_store: dict[str, dict[str, dict]] = {}
_lock = threading.Lock()


def _delta_lines(prev_text: str, new_text: str) -> tuple[int, int, list[str]]:
    """Line-level added/removed counts + a capped excerpt of the delta."""
    prev = prev_text.splitlines()
    new = new_text.splitlines()
    added: list[str] = []
    removed: list[str] = []
    for line in difflib.unified_diff(prev, new, lineterm='', n=0):
        if line.startswith('+++') or line.startswith('---'):
            continue
        if line.startswith('+'):
            added.append(line)
        elif line.startswith('-'):
            removed.append(line)
    excerpt = (removed + added)[:_MAX_DELTA_LINES]
    overflow = (len(added) + len(removed)) - len(excerpt)
    if overflow > 0:
        excerpt.append(f'… (+{overflow} more changed line(s))')
    return len(added), len(removed), excerpt


def annotate_turn_blocks(conv_id: str,
                         blocks: list[ContextBlock]) -> None:
    """Append world-state change trailers to whitelisted blocks, in place.

    Learns the new baseline AFTER annotating, so the NEXT turn diffs against
    this render. No-op for an empty conv_id (headless callers get no store
    pollution) and for blocks without content. Never raises — an annotation
    failure must not break prompt assembly.
    """
    if not conv_id:
        return
    try:
        now = time.time()
        with _lock:
            conv = _store.get(conv_id)
            if conv is None:
                if len(_store) >= _MAX_CONVS:
                    oldest = min(
                        _store,
                        key=lambda k: max(
                            (v.get('ts', 0.0) for v in _store[k].values()),
                            default=0.0))
                    _store.pop(oldest, None)
                conv = {}
                _store[conv_id] = conv
            for idx, block in enumerate(blocks):
                if block.id not in _DIFFABLE_BLOCK_IDS:
                    continue
                text = (block.content or '').strip()
                if not text or block.suppressed_reason:
                    continue
                # ContextBlock is a frozen dataclass — swap the list entry
                # for a copy carrying the trailer + provenance stamp.
                prev = conv.get(block.id)
                new_content = text
                if prev is None:
                    state = 'baseline'
                elif prev.get('text') == text:
                    state = 'unchanged'
                    new_content = text + '\n\n' + _UNCHANGED_TRAILER
                else:
                    n_add, n_del, excerpt = _delta_lines(
                        prev.get('text') or '', text)
                    state = f'changed:+{n_add}-{n_del}'
                    trailer = _DELTA_HEADER % (n_add, n_del)
                    if excerpt:
                        trailer += '\n' + '\n'.join(excerpt)
                    new_content = text + '\n\n' + trailer
                if len(conv) >= _MAX_BLOCKS_PER_CONV and block.id not in conv:
                    oldest_b = min(
                        conv, key=lambda k: conv[k].get('ts', 0.0))
                    conv.pop(oldest_b, None)
                conv[block.id] = {'text': text, 'ts': now}
                prov = dict(block.provenance)
                prov['worldState'] = state
                blocks[idx] = dataclasses.replace(
                    block, content=new_content, provenance=prov)
                if state.startswith('changed'):
                    logger.debug('[WorldDiff] conv=%s block=%s %s',
                                 conv_id[:8], block.id, state)
    except Exception as e:
        logger.warning('[WorldDiff] annotation failed conv=%s: %s',
                       (conv_id or '')[:8], e)


def _reset_for_tests() -> None:
    """Test hook: clear every stored baseline."""
    with _lock:
        _store.clear()


__all__ = ['annotate_turn_blocks', '_reset_for_tests']
