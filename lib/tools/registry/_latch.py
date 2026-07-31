"""lib/tools/registry/_latch.py — Prompt-cache stability latches.

Two independent, process-global latches that keep a conversation's tool-schema
bytes stable across rounds so the ~65k-token prompt-cache prefix is not
invalidated:

  * **Sticky multi-root** — once a conversation goes multi-root, the
    ``_MULTIROOT_PATH_HINT`` stays on every path-taking tool for the rest of
    the conversation (:func:`mark_multiroot_sticky` / :func:`is_multiroot_sticky`
    / :func:`clear_multiroot_sticky`).
  * **Per-conversation tool-schema latch** — freezes the EXACT tool list a
    conversation first used and serves it byte-identical every later round
    (:func:`latch_tool_list` / :func:`tool_list_diverged` / :func:`tool_list_diff`
    / :func:`clear_tool_list_latch` / :func:`clear_all_tool_list_latches`).

This module has NO dependency on the spec/registry layer — the dependency
direction is ``_spec → _latch`` (``ToolContext.multiroot_active`` calls
:func:`mark_multiroot_sticky` / :func:`clear_tool_list_latch`), never the
reverse.  Keeping the latch state single-homed here means every consumer
(routes/api_v1/mcp.py, the assembler, tests) mutates ONE set of module-level
dicts + locks.
"""

from __future__ import annotations

import os
import threading

from lib.log import get_logger

logger = get_logger(__name__)


# ══════════════════════════════════════════════════════════
#  Sticky multi-root latch (prompt-cache stability)
# ══════════════════════════════════════════════════════════
# When a conversation transitions to multi-root, every path-taking tool gains
# the ``_MULTIROOT_PATH_HINT`` text on its ``path`` field (see
# lib/tools/project.py::with_multiroot_hint). If that decision flapped between
# rounds, the tool-schema bytes would change and invalidate the whole prompt
# cache prefix (~65k tokens). We therefore latch "this conversation is
# multi-root" once and never downgrade mid-conversation. Cleared by
# ``clear_multiroot_sticky`` when the conversation's cache state is evicted.
_multiroot_sticky: set[str] = set()
_multiroot_sticky_lock = threading.Lock()


def mark_multiroot_sticky(conv_id: str) -> bool:
    """Latch *conv_id* as multi-root for the rest of the conversation.

    Returns ``True`` only on the OFF→ON transition (the first time this
    conversation is marked multi-root), ``False`` if it was already sticky.
    Callers use the transition signal to re-establish the tool-schema latch
    exactly once — see :meth:`ToolContext.multiroot_active`.
    """
    if not conv_id:
        return False
    with _multiroot_sticky_lock:
        if conv_id in _multiroot_sticky:
            return False
        _multiroot_sticky.add(conv_id)
        return True


def is_multiroot_sticky(conv_id: str) -> bool:
    """Whether *conv_id* has been latched multi-root."""
    if not conv_id:
        return False
    with _multiroot_sticky_lock:
        return conv_id in _multiroot_sticky


def clear_multiroot_sticky(conv_id: str) -> None:
    """Release the multi-root latch for *conv_id* (on cache-state cleanup)."""
    if not conv_id:
        return
    with _multiroot_sticky_lock:
        _multiroot_sticky.discard(conv_id)


# ══════════════════════════════════════════════════════════
#  Sticky project-ready latch (prompt-cache stability)
# ══════════════════════════════════════════════════════════
# The whole project tool family (list_dir / grep_search / find_files /
# write_file / apply_diff / insert_content / create_project / run_command) is
# gated on ``project_enabled`` (= a non-empty project_path). A conversation
# whose FIRST turn had no project attached (empty roots) assembles a tool list
# WITHOUT that family; the tool-schema latch then freezes that "no-project"
# snapshot for the whole conversation, so attaching a project on a LATER turn
# never restores the tools (read_files / inspect_image survive only because
# they are always-on). Attaching a project mid-conversation is a LEGITIMATE
# one-time schema change (exactly like going multi-root), so we latch the
# OFF→ON transition and use it to re-establish the tool-schema latch once —
# mirroring ``mark_multiroot_sticky``.
_project_ready_sticky: set[str] = set()
_project_ready_sticky_lock = threading.Lock()


def mark_project_ready_sticky(conv_id: str) -> bool:
    """Latch *conv_id* as project-enabled for the rest of the conversation.

    Returns ``True`` only on the OFF→ON transition (the first time this
    conversation is seen with a project attached), ``False`` if it was already
    sticky. Callers use the transition signal to re-establish the tool-schema
    latch exactly once — see :meth:`ToolContext.project_ready`.
    """
    if not conv_id:
        return False
    with _project_ready_sticky_lock:
        if conv_id in _project_ready_sticky:
            return False
        _project_ready_sticky.add(conv_id)
        return True


def is_project_ready_sticky(conv_id: str) -> bool:
    """Whether *conv_id* has been latched project-enabled."""
    if not conv_id:
        return False
    with _project_ready_sticky_lock:
        return conv_id in _project_ready_sticky


def clear_project_ready_sticky(conv_id: str) -> None:
    """Release the project-ready latch for *conv_id* (on cache-state cleanup)."""
    if not conv_id:
        return
    with _project_ready_sticky_lock:
        _project_ready_sticky.discard(conv_id)


# ══════════════════════════════════════════════════════════
#  Sticky project-REMOTE latch (RWA 拍板 3A, same precedent)
# ══════════════════════════════════════════════════════════
# Binding a conversation to a remote worktree changes tool DESCRIPTIONS
# (with_remote_hint — the local-execution note) while keeping names +
# parameter schemas byte-identical. That description change is a
# LEGITIMATE one-time schema change, so the OFF→ON transition clears the
# tool-schema latch exactly once — mirroring mark_project_ready_sticky.
_project_remote_sticky: set[str] = set()
_project_remote_sticky_lock = threading.Lock()


def mark_project_remote_sticky(conv_id: str) -> bool:
    """Latch *conv_id* as remote-bound. True only on the OFF→ON transition."""
    if not conv_id:
        return False
    with _project_remote_sticky_lock:
        if conv_id in _project_remote_sticky:
            return False
        _project_remote_sticky.add(conv_id)
        return True


def is_project_remote_sticky(conv_id: str) -> bool:
    """Whether *conv_id* has been latched remote-bound."""
    if not conv_id:
        return False
    with _project_remote_sticky_lock:
        return conv_id in _project_remote_sticky


def clear_project_remote_sticky(conv_id: str) -> None:
    """Release the project-remote latch for *conv_id*."""
    if not conv_id:
        return
    with _project_remote_sticky_lock:
        _project_remote_sticky.discard(conv_id)


# ══════════════════════════════════════════════════════════
#  Per-conversation tool-SCHEMA latch (the (B) root fix)
# ══════════════════════════════════════════════════════════
# The whole tools array sits in the cached prompt prefix (BP1-3). ANY byte
# change between rounds — a user toggling Swarm/Scheduler/Browser on the
# frontend, an MCP server re-emitting a tool, etc. — invalidates the entire
# prefix (~65k tokens). The (A) fixes removed the *incidental* code-side flaps
# (multiroot hint, MCP ordering). This latch removes the LAST class: it freezes
# the EXACT tool list a conversation first used, and serves that byte-identical
# snapshot for every later round, so a mid-conversation change cannot break the
# cache. The change is not lost — it is DEFERRED: it applies to the next NEW
# conversation, or immediately if the user clicks "Apply now" (which clears the
# latch). We still assemble fresh every round (cheap) purely to DETECT a
# divergence and signal the frontend.
#
# Kill switch: env TOFU_TOOLSET_LATCH=0 disables the freeze (assembly returns
# the live list every round, legacy behaviour).
_tool_latch: dict[str, tuple[str, list[dict]]] = {}  # conv_id → (hash, snapshot)
_tool_latch_diverged: dict[str, bool] = {}           # conv_id → last divergence flag
# conv_id → {'added': [name, ...], 'removed': [name, ...]} for the most recent
# divergence — lets the frontend show WHICH tools the held-back toggle changed.
_tool_latch_diff: dict[str, dict[str, list[str]]] = {}
_tool_latch_lock = threading.Lock()


def _tool_names(tool_list: list[dict] | None) -> list[str]:
    """Extract the ordered tool names from an OpenAI-style tool list."""
    names: list[str] = []
    for t in (tool_list or []):
        try:
            name = (t.get('function') or {}).get('name')
        except AttributeError as e:
            logger.debug('[ToolReg] _tool_names: non-dict tool entry (%s) — '
                         'skipping', e)
            name = None
        if name:
            names.append(name)
    return names


def _toolset_latch_enabled() -> bool:
    """Whether the tool-schema latch is active (default ON)."""
    val = os.environ.get('TOFU_TOOLSET_LATCH', '1').strip().lower()
    return val not in ('0', 'false', 'no', 'off')


def _hash_tool_list(tool_list: list[dict]) -> str:
    """Stable hash of a tool list's bytes (order-sensitive, content-sensitive)."""
    import hashlib
    import json
    try:
        blob = json.dumps(tool_list, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError) as e:
        logger.debug('[ToolReg] _hash_tool_list: tool list not JSON-serialisable '
                     '(%s) — hashing str() form', e)
        blob = str(tool_list)
    return hashlib.md5(blob.encode('utf-8', errors='replace')).hexdigest()


def _diagnose_byte_drift(snapshot: list[dict], fresh: list[dict]) -> str:
    """Describe the FIRST tool whose bytes differ between two same-name lists.

    Called only when the latch flags a divergence but the set of tool NAMES is
    unchanged — i.e. a tool's *content* or the list *order* drifted. Returns a
    short human string naming the offending tool + which field changed
    (``position`` / ``description`` / ``parameters``), with values truncated
    per CLAUDE.md §2.6 so the log line stays grep-able and bounded. Returns
    ``''`` when no positional difference is found (e.g. a pure re-order that
    the name-set view already collapsed).
    """
    import json

    def _fn(t: dict) -> dict:
        try:
            return t.get('function') or {}
        except AttributeError as e:
            logger.debug('[ToolLatch] _diagnose_byte_drift: non-dict tool entry '
                         '(%s) — treating as empty', e)
            return {}

    def _trunc(s: str, n: int = 240) -> str:
        s = str(s)
        return s if len(s) <= n else s[:n] + f'…(+{len(s) - n} chars)'

    for i, (a, b) in enumerate(zip(snapshot, fresh)):
        fa, fb = _fn(a), _fn(b)
        na, nb = fa.get('name'), fb.get('name')
        if na != nb:
            return f'position {i}: frozen={na!r} fresh={nb!r} (tool order changed)'
        da, db = fa.get('description', ''), fb.get('description', '')
        if da != db:
            return (f'tool={na!r} field=description '
                    f'frozen={_trunc(da)!r} fresh={_trunc(db)!r}')
        pa = json.dumps(fa.get('parameters'), sort_keys=True, ensure_ascii=False)
        pb = json.dumps(fb.get('parameters'), sort_keys=True, ensure_ascii=False)
        if pa != pb:
            return (f'tool={na!r} field=parameters '
                    f'frozen={_trunc(pa, 320)} fresh={_trunc(pb, 320)}')
    if len(snapshot) != len(fresh):
        return (f'length changed: frozen={len(snapshot)} fresh={len(fresh)} '
                '(same names, different count — duplicate?)')
    return ''


def latch_tool_list(conv_id: str,
                    fresh: list[dict] | None) -> tuple[list[dict] | None, bool]:
    """Freeze the tool list for a conversation; return ``(effective, diverged)``.

    First round for *conv_id* establishes the snapshot and returns *fresh*
    unchanged (``diverged=False``). Later rounds return the FROZEN snapshot
    byte-for-byte; ``diverged`` is ``True`` whenever the freshly-assembled list
    differs from the frozen one (i.e. the user changed a tool toggle). The
    divergence is computed fresh each round, so toggling a tool then toggling
    it back reports ``diverged=False``.

    No-ops (returns ``(fresh, False)``) when *conv_id* is empty (stateless
    assembly) or the latch is disabled via ``TOFU_TOOLSET_LATCH=0``.
    """
    if not conv_id or not _toolset_latch_enabled():
        return fresh, False
    fresh_list = fresh or []
    fresh_hash = _hash_tool_list(fresh_list)
    import copy
    with _tool_latch_lock:
        entry = _tool_latch.get(conv_id)
        if entry is None:
            _tool_latch[conv_id] = (fresh_hash, copy.deepcopy(fresh_list))
            _tool_latch_diverged[conv_id] = False
            return fresh, False
        latched_hash, snapshot = entry
        diverged = fresh_hash != latched_hash
        _tool_latch_diverged[conv_id] = diverged
        if diverged:
            frozen_names = set(_tool_names(snapshot))
            fresh_names = set(_tool_names(fresh_list))
            added = sorted(fresh_names - frozen_names)
            removed = sorted(frozen_names - fresh_names)
            _tool_latch_diff[conv_id] = {'added': added, 'removed': removed}
            # Empty name-diff but bytes changed → a tool's CONTENT or the list
            # ORDER drifted (e.g. an MCP server emitting a non-deterministic
            # schema). Pinpoint the first offending tool+field so we can tell a
            # spurious flap from a legitimate change instead of just showing a
            # generic banner. Guarded to this case so the common toggle path
            # pays nothing.
            if not added and not removed:
                try:
                    detail = _diagnose_byte_drift(snapshot, fresh_list)
                except Exception as e:
                    logger.debug('[ToolLatch] drift diagnosis failed conv=%s: %s',
                                 conv_id[:8], e)
                    detail = f'(drift diagnosis failed: {e})'
                logger.warning('[ToolLatch] conv=%s diverged with EMPTY '
                               'name-diff (byte-level schema drift) — %s',
                               conv_id[:8], detail or '(no positional diff found)')
        else:
            _tool_latch_diff.pop(conv_id, None)
        # Return the frozen snapshot (copy so callers can't mutate the latch).
        return copy.deepcopy(snapshot), diverged


def tool_list_diverged(conv_id: str) -> bool:
    """Whether the latched tool list currently differs from a fresh assembly.

    Cheap read of the flag computed by the most recent :func:`latch_tool_list`
    call for *conv_id*. Used to decide whether to surface the "apply on next
    conversation" affordance. Returns ``False`` when unknown.
    """
    return _tool_latch_diverged.get(conv_id, False)


def tool_list_diff(conv_id: str) -> dict[str, list[str]]:
    """Names added/removed by the held-back toggle for *conv_id*.

    Returns ``{'added': [...], 'removed': [...]}`` (sorted, possibly empty)
    describing how the freshly-assembled tool list differs from the frozen
    snapshot, as computed by the most recent :func:`latch_tool_list` call.
    Lets the frontend show WHICH tools a pending change touches. Returns
    empty lists when there is no current divergence.
    """
    diff = _tool_latch_diff.get(conv_id)
    if not diff:
        return {'added': [], 'removed': []}
    return {'added': list(diff.get('added', [])),
            'removed': list(diff.get('removed', []))}


def clear_tool_list_latch(conv_id: str) -> None:
    """Drop the frozen tool list for *conv_id* (on "Apply now" or cleanup).

    The next :func:`latch_tool_list` call re-establishes the snapshot from the
    then-current toggles — i.e. the deferred tool change takes effect and the
    prompt cache rebuilds once.
    """
    if not conv_id:
        return
    with _tool_latch_lock:
        _tool_latch.pop(conv_id, None)
    _tool_latch_diverged.pop(conv_id, None)
    _tool_latch_diff.pop(conv_id, None)


def clear_all_tool_list_latches() -> int:
    """Drop EVERY conversation's frozen tool list. Returns the count cleared.

    Called when the *global* tool surface changes for a deliberate,
    infrequent reason — chiefly an MCP server install / uninstall / connect /
    disconnect (see ``routes/api_v1/mcp.py``). Unlike a composer-toggle flap
    (which the latch intentionally defers to the next conversation to protect
    the ~65k-token prompt-cache prefix), an MCP mutation is an explicit "I want
    this tool set now" action that should take effect on the next round of
    EVERY conversation, not just the active one.

    Cost is self-limiting: the prompt cache keys on the tool array's *bytes*,
    not the latch identity. A conversation whose effective tool set is
    unchanged by the mutation re-establishes a byte-identical snapshot on its
    next round → no cache rebuild. Only conversations whose tool set genuinely
    changed pay the one-time rebuild — which is exactly what we want.
    """
    with _tool_latch_lock:
        n = len(_tool_latch)
        _tool_latch.clear()
    _tool_latch_diverged.clear()
    _tool_latch_diff.clear()
    if n:
        logger.info('[ToolRegistry] cleared %d tool-schema latch(es) — global '
                    'tool surface changed (MCP mutation); next round of each '
                    'conversation re-assembles from current tools', n)
    return n
