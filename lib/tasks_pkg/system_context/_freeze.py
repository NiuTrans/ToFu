# HOT_PATH
"""Per-conv volatile-context freeze — the turn_boundary_rebill root fix.

WHY (measured 2026-08-01: 44 turn-boundary re-bills, ~25 with true gap < 300s)
-----------------------------------------------------------------------------
A new user turn's round-1 read back ~0 even seconds after the previous turn
ended warm. The volatile context blocks are re-rendered FRESH every task:

  * the CLAUDE.md/journal ``_isMeta`` carrier (index 1 — INSIDE the cached
    prefix after the first tool round): the journal digest changes as the
    project works, so the carrier bytes change every turn;
  * the memory-count hint in the system floor (changes on every memory CRUD);
  * the previous turn's tail blocks (digest / charter / goals / board / date /
    pref_detail / relevant_memories) on the previous user message: persistence
    strips them, so the next task rebuilds that message BARE — a prefix
    mutation at its position for any tool-heavy prior turn.

Any byte drift in the HEAD (system floor / index-1 carrier) re-keys the whole
cached prefix; a lost tail block on a deep-prefix user message does the same.

THE FIX (epic pt_62ed8cce25324eb2, owner-approved direction 跨轮冻结)
--------------------------------------------------------------------
While the previous turn's cache entry is still alive (the warm window — the
head marker's TTL: 5 min default, 1h with extended-cache-ttl), inject
BYTE-FROZEN renders of the volatile head blocks and restore the previous
turn's tail blocks onto historical user messages verbatim. Freshness is
traded for cache stability ONLY within the window the entry would survive
anyway; once it expires, fresh renders are free (the prefix re-bills
regardless). Outside the window every render is fresh — no stale context
beyond the TTL ever reaches the model.

Everything here is in-memory: the sidecar only needs to outlive the warm
window (≤ 1h), never a process restart (a restart leaves Anthropic's cache
warm for minutes but the first post-restart turn simply renders fresh —
a one-time miss, never a wrong one).
"""

from __future__ import annotations

import hashlib
import threading
import time

from lib.log import get_logger

logger = get_logger(__name__)

# Head-marker TTL: mirrors the extended-cache-ttl latch (lib.CACHE_EXTENDED_TTL).
_HEAD_TTL_DEFAULT_S = 300.0
_HEAD_TTL_EXTENDED_S = 3600.0

# Tail-sidecar retention: entries older than this are pruned on access
# (2× the extended TTL — generous margin, still bounded).
_TAIL_MAX_AGE_S = 7200.0
# Sidecar bounds (defensive — real convs hold far fewer).
_TAIL_MAX_MSGS_PER_CONV = 96
_MAX_CONVS = 256


def head_ttl_s() -> float:
    """Effective head-marker TTL in seconds (5m default / 1h extended)."""
    import lib as _lib
    return _HEAD_TTL_EXTENDED_S if getattr(_lib, 'CACHE_EXTENDED_TTL', False) \
        else _HEAD_TTL_DEFAULT_S


class _ConvFreeze:
    __slots__ = ('project_path', 'carrier_body', 'mem_hint', 'frozen_at',
                 'last_used', 'tail_blocks')

    def __init__(self):
        self.project_path = ''
        self.carrier_body = None      # frozen CLAUDE.md/journal carrier body
        self.mem_hint = None          # frozen memory-count hint
        self.frozen_at = 0.0
        self.last_used = 0.0
        # {bare_content_hash: {'blocks': [...], 'ts': float}}
        self.tail_blocks = {}


_freeze: dict[str, _ConvFreeze] = {}
_lock = threading.Lock()


def _md5(text: str) -> str:
    return hashlib.md5(text.encode('utf-8', errors='replace')).hexdigest()[:12]


def _canon_user_text(content) -> str:
    """Canonical text of a user message's content (str | block list).

    Used ONLY for identity hashing (never sent anywhere) — two rebuilds of the
    same stored row must produce the same key. Text blocks only; non-text
    blocks (images) are position-stable across rebuilds and add no text.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, str):
                parts.append(b)
            elif isinstance(b, dict) and b.get('type') == 'text':
                parts.append(b.get('text') or '')
        return '\x01'.join(parts)
    return ''


def _last_cache_activity(conv_id: str) -> float:
    """Newest cache-state timestamp any thread holds for this conv (0 = none)."""
    try:
        from lib.tasks_pkg.cache_tracking._state import _cache_lock, _cache_states
        latest = 0.0
        with _cache_lock:
            for (cid, _tid), st in _cache_states.items():
                if cid == conv_id and st.last_update_time > latest:
                    latest = st.last_update_time
        return latest
    except Exception as e:
        logger.debug('[CtxFreeze] cache-activity probe failed conv=%s: %s',
                     (conv_id or '')[:8], e)
        return 0.0


def is_warm(conv_id: str, now: float | None = None) -> bool:
    """True when this conv's previous cache entry is very likely still alive.

    Primary signal: the conv's last LLM round (any thread) landed within the
    head-marker TTL. Fallback: the freeze record's own last use (covers the
    first tasks before any cache state exists for the conv).
    """
    if not conv_id:
        return False
    now = now if now is not None else time.time()
    last = _last_cache_activity(conv_id)
    if not last:
        with _lock:
            rec = _freeze.get(conv_id)
            last = rec.last_used if rec else 0.0
    return bool(last) and (now - last) < head_ttl_s()


def get_frozen_head(conv_id: str, project_path: str) -> dict | None:
    """Return {'carrier_body', 'mem_hint'} for warm reuse, else None.

    The carrier body / hint are reused verbatim — the previous turn's cache
    entry is still alive, so byte-identical head blocks keep the hit. The
    project_path guard invalidates on a genuine context switch (a different
    project must never inherit stale renders).
    """
    if not conv_id or not is_warm(conv_id):
        return None
    with _lock:
        rec = _freeze.get(conv_id)
        if rec is None or rec.project_path != (project_path or ''):
            return None
        out = {}
        if rec.carrier_body is not None:
            out['carrier_body'] = rec.carrier_body
        if rec.mem_hint is not None:
            out['mem_hint'] = rec.mem_hint
        rec.last_used = time.time()
        return out or None


def store_head(conv_id: str, project_path: str, *,
               carrier_body=None, mem_hint=None) -> None:
    """Persist freshly-rendered head blocks for warm-window reuse."""
    if not conv_id:
        return
    with _lock:
        rec = _freeze.get(conv_id)
        if rec is None:
            if len(_freeze) >= _MAX_CONVS:
                oldest = min(_freeze, key=lambda k: _freeze[k].last_used)
                _freeze.pop(oldest, None)
            rec = _ConvFreeze()
            _freeze[conv_id] = rec
        rec.project_path = project_path or ''
        rec.frozen_at = time.time()
        rec.last_used = rec.frozen_at
        if carrier_body is not None:
            rec.carrier_body = carrier_body
        if mem_hint is not None:
            rec.mem_hint = mem_hint


# ═══════════════════════════════════════════════════════════════════════════════
#  Tail-block sidecar — restore last turn's volatile blocks onto historical
#  user messages so the rebuilt wire is byte-identical to last turn's.
# ═══════════════════════════════════════════════════════════════════════════════

def _relevant_memories_tag() -> str:
    try:
        from lib.memory.prefetch._inject import _RELEVANT_MEMORIES_TAG
        return _RELEVANT_MEMORIES_TAG
    except Exception as e:
        logger.debug('[CtxFreeze] relevant-memories tag import failed: %s', e)
        return '<relevant_memories>'


def _strip_tagged_text_blocks(content, tag: str):
    """Drop text blocks carrying ``tag`` from a block list (str passes through)."""
    if not isinstance(content, list):
        return content
    return [b for b in content
            if not (isinstance(b, dict) and b.get('type') == 'text'
                    and tag in (b.get('text') or ''))]


def bare_tail_hash(content) -> str:
    """Identity hash of a user message's PRE-INJECTION form.

    The next task rebuilds historical messages bare (persistence strips
    injected blocks), so the restore lookup must key on exactly that rebuilt
    form: the entry content minus the prefetch's <relevant_memories> block
    (the prefetch runs before _inject_system_contexts and only touches the
    LAST user message — historical messages never carry it at restore time).
    """
    bare = _strip_tagged_text_blocks(content, _relevant_memories_tag())
    return _md5(_canon_user_text(bare))


def _normalize_blocks(content) -> list:
    """Content → a fresh list of block dicts (wire shape)."""
    if isinstance(content, str):
        return [{'type': 'text', 'text': content}] if content.strip() else []
    if isinstance(content, list):
        out = []
        for b in content:
            if isinstance(b, dict):
                out.append(dict(b))
            else:
                out.append({'type': 'text', 'text': str(b)})
        return out
    return []


def restore_tail_blocks(messages: list, conv_id: str) -> int:
    """Re-attach recorded tail blocks onto historical (non-last) user messages.

    Idempotent by construction: the lookup key is the message's BARE content
    hash, so a message that already carries restored/injected blocks no longer
    matches and is never re-touched (endpoint-mode re-entry safety). The LAST
    user message is always skipped — it gets this turn's fresh blocks (its
    position is the already-volatile tail, so fresh bytes there are free).
    """
    if not conv_id:
        return 0
    with _lock:
        rec = _freeze.get(conv_id)
        tb = dict(rec.tail_blocks) if rec else {}
        if rec:
            _prune_tail_locked(rec, time.time())
    if not tb:
        return 0
    last_user_idx = -1
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get('role') == 'user':
            last_user_idx = i
            break
    restored = 0
    for i, m in enumerate(messages):
        if i == last_user_idx or m.get('role') != 'user':
            continue
        entry = tb.get(bare_tail_hash(m.get('content')))
        if entry is None:
            continue
        m['content'] = [dict(b) for b in entry['blocks']]
        restored += 1
    if restored:
        logger.debug('[CtxFreeze] conv=%s restored tail blocks onto %d '
                     'historical user message(s)', conv_id[:8], restored)
    return restored


def record_tail_block(conv_id: str, entry_content, messages: list) -> None:
    """Snapshot the last user message's final content for next-turn restore.

    ``entry_content`` is the message's content as captured at the START of
    ``_inject_system_contexts`` (post-prefetch, pre-system-context) — its
    relevant-memories-stripped hash is the key the next task's bare rebuild
    will match. The stored value is the message's FINAL content block list
    (wholesale-restored next turn, so ordering/wrappers can never drift).
    """
    if not conv_id:
        return
    last_user_idx = -1
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get('role') == 'user':
            last_user_idx = i
            break
    if last_user_idx < 0:
        return
    final_blocks = _normalize_blocks(messages[last_user_idx].get('content'))
    if not final_blocks:
        return
    key = bare_tail_hash(entry_content)
    with _lock:
        rec = _freeze.get(conv_id)
        if rec is None:
            if len(_freeze) >= _MAX_CONVS:
                oldest = min(_freeze, key=lambda k: _freeze[k].last_used)
                _freeze.pop(oldest, None)
            rec = _ConvFreeze()
            _freeze[conv_id] = rec
        # LRU-cap the per-conv tail map (real convs: one entry per user turn).
        if len(rec.tail_blocks) >= _TAIL_MAX_MSGS_PER_CONV \
                and key not in rec.tail_blocks:
            oldest_k = min(rec.tail_blocks,
                           key=lambda k: rec.tail_blocks[k]['ts'])
            rec.tail_blocks.pop(oldest_k, None)
        rec.tail_blocks[key] = {'blocks': final_blocks, 'ts': time.time()}
        rec.last_used = time.time()


def _prune_tail_locked(rec: _ConvFreeze, now: float) -> None:
    """Drop tail entries older than the retention window (caller holds _lock)."""
    stale = [k for k, v in rec.tail_blocks.items()
             if now - v.get('ts', 0.0) > _TAIL_MAX_AGE_S]
    for k in stale:
        rec.tail_blocks.pop(k, None)


def _reset_for_tests() -> None:
    """Test hook: clear all freeze state."""
    with _lock:
        _freeze.clear()


__all__ = [
    'head_ttl_s', 'is_warm', 'get_frozen_head', 'store_head',
    'bare_tail_hash', 'restore_tail_blocks', 'record_tail_block',
    '_reset_for_tests',
]
