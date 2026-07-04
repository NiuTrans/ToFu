"""lib/tools/registry.py — Declarative tool-assembly registry.

This module is the **single seam** through which both built-in and
third-party tools declare *what schema the LLM sees* and *when that tool
is active*.  It exists to collapse the hand-maintained ``if feature: …``
ladder that used to live in ``lib.tasks_pkg.model_config._assemble_tool_list``
into a list of self-describing :class:`ToolSpec` objects.

Why this matters
----------------
Before this module, adding a native tool meant editing a core
orchestration file (``model_config.py``) — a hardcoded if-branch per
feature.  Now a tool author registers a :class:`ToolSpec` (schema + gate)
once, in their own file, and the orchestrator picks it up with **zero**
core edits.  Third-party packages can contribute tools via the
``tofu.tools`` entry-point group (see :func:`discover_plugin_specs`).

Design contract (DO NOT BREAK)
------------------------------
1. **Ordering is prompt-cache-critical.**  Specs are emitted in
   registration order within their phase.  The built-in registration
   order reproduces the A/B-validated layout exactly:
   search → fetch → read_files → project|code_exec → browser → desktop →
   image_gen → conv_ref → human_guidance → ⟨base/capability boundary⟩ →
   memory → scheduler → swarm → mcp.
2. **Two phases.**  ``phase='base'`` specs are emitted first and counted
   toward ``has_base_tools`` (the value the orchestrator calls
   ``has_real_tools``).  ``phase='capability'`` specs are emitted after,
   and may read :attr:`ToolContext.has_base_tools` to self-gate.
3. **Lazy imports.**  A spec's ``build(ctx)`` is called at request time, so
   heavy schema imports (browser, swarm, mcp, …) stay out of startup.
4. **Side-effect gates are allowed.**  ``build()`` may log (e.g.
   "browser requested but extension not connected") and may return an
   empty list — exactly mirroring the legacy behaviour.

Plugin isolation (multi-tenant)
-------------------------------
``discover_plugin_specs()`` loads third-party ``tofu.tools`` entry points into
the SAME process-global ``_TOOL_SPECS`` list as the built-ins.  On a shared,
multi-tenant server (e.g. the headless ``/api/v1/agent/run`` API) that means a
plugin installed for one caller would otherwise be visible to EVERY caller —
its tool schema (and any imperative "always call me first" text in that schema)
silently pollutes unrelated requests.

To prevent this, every spec carries a :attr:`ToolSpec.source`
(``'builtin'`` | ``'plugin'``) tag and plugins additionally carry a
:attr:`ToolSpec.plugin_name`.  :func:`assemble_tool_list` consults the
per-request :attr:`ToolContext.enabled_plugins` allow-list:

* built-in specs are ALWAYS evaluated;
* a plugin spec is evaluated only when its ``plugin_name`` is allow-listed.

The allow-list is resolved per request by :func:`resolve_enabled_plugins` from
``cfg['plugins']`` (request-scoped) falling back to the
``TOFU_DEFAULT_TOOL_PLUGINS`` env var (deployment-wide default).  The default
when neither is set is **fail-closed**: NO third-party plugins are visible.  A
dedicated single-tenant deployment that wants the old "everything I installed
is on" behaviour sets ``TOFU_DEFAULT_TOOL_PLUGINS=*`` (or passes
``cfg['plugins']='*'``), which maps to ``enabled_plugins=None`` (gate fully
open).  This isolation is a VISIBILITY boundary (the LLM never sees the schema),
not a security sandbox — a plugin's handler code still lives in-process.
"""

from __future__ import annotations

import os
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

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
        except AttributeError:
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
    except (TypeError, ValueError):
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
        except AttributeError:
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

# A dispatch handler — same signature as lib.protocols.ToolHandler.  Typed
# loosely here to avoid importing the protocol into this low-level module.
ToolHandlerFn = Callable[..., tuple[str, str, bool]]


# ══════════════════════════════════════════════════════════
#  ToolContext — the inputs every gate/build decision needs
# ══════════════════════════════════════════════════════════

@dataclass
class ToolContext:
    """Everything a :class:`ToolSpec` needs to decide whether to contribute.

    Built once per task by :func:`assemble_tool_list` from the resolved
    model config.  Two fields are mutated by the assembler *between*
    phases so capability-phase specs can self-gate:

    - :attr:`current_count` — number of tools accumulated so far (lets a
      base-phase spec like conv-ref require that *some* tool already exists).
    - :attr:`has_base_tools` — set ``True`` once the base phase produced ≥1
      tool; read by capability-phase specs (memory, scheduler).
    """

    cfg: dict[str, Any]
    task_id: str
    project_path: str
    project_enabled: bool
    search_mode: str
    search_enabled: bool
    fetch_enabled: bool
    code_exec_enabled: bool
    browser_enabled: bool
    desktop_enabled: bool
    swarm_enabled: bool
    image_gen_enabled: bool = False
    human_guidance_enabled: bool = False
    scheduler_enabled: bool = False
    messages: list[dict[str, Any]] | None = None

    # Conversation id — used to make schema-shaping decisions sticky for a
    # conversation's lifetime (e.g. the multi-root path hint). Empty for
    # one-off / stateless assembly (tests, compat adapters).
    conv_id: str = ''

    # ── Multi-tenant plugin visibility allow-list ──
    # Which third-party (``source='plugin'``) tool specs this task may see.
    #   * ``None``  → ALL plugins visible (single-tenant / legacy behaviour —
    #                 e.g. a dedicated app/ deployment that owns its process).
    #   * ``set()`` → NO plugins visible (the safe headless multi-tenant
    #                 default: a shared server never leaks one tenant's
    #                 plugins to another).
    #   * ``{names}`` → only plugins whose ``ToolSpec.plugin_name`` is in the
    #                 set are visible.
    # Built-in specs are NEVER affected by this field. Populated from
    # ``cfg['plugins']`` (per-request) falling back to the
    # ``TOFU_DEFAULT_TOOL_PLUGINS`` env var — see :meth:`resolve_enabled_plugins`.
    enabled_plugins: set[str] | None = None

    # ── Mutated by the assembler between phases ──
    current_count: int = 0
    has_base_tools: bool = False

    @property
    def tid(self) -> str:
        """Short task-id prefix for log lines."""
        return (self.task_id or '')[:8]

    def plugin_allowed(self, plugin_name: str) -> bool:
        """Whether a ``source='plugin'`` spec named *plugin_name* is visible.

        ``enabled_plugins is None`` → all plugins allowed (legacy / dedicated
        single-tenant process). Otherwise the plugin must be explicitly listed.
        A plugin spec with an empty ``plugin_name`` (a misconfigured plugin
        that didn't get tagged) is treated as NOT allow-listed unless the gate
        is fully open (``None``) — fail-closed, never leak by accident.
        """
        if self.enabled_plugins is None:
            return True
        return bool(plugin_name) and plugin_name in self.enabled_plugins

    @property
    def multiroot_active(self) -> bool:
        """True when more than one workspace root is configured for this task.

        Read from ``cfg['projectPaths']`` (the full root list the frontend
        sends; element 0 is the primary, the rest are extras). Used to decide
        whether path-taking tool schemas should carry the ``rootname:`` prefix
        hint — single-root sessions keep the cache-stable default schema.

        **Sticky per conversation.** Once a conversation has gone multi-root,
        the hint stays on for the rest of that conversation even if a later
        task transiently reports a single ``projectPaths`` (e.g. an extra root
        was auto-registered mid-conversation by an absolute-path write, then a
        subsequent task's snapshot lags). Flapping this value rewrites every
        path-taking tool's schema and breaks the prompt-cache prefix — see
        ``mark_multiroot_sticky``. A conversation never silently downgrades to
        single-root mid-stream; the latch is cleared only on cleanup.
        """
        live = self._multiroot_live()
        if not self.conv_id:
            # Stateless assembly (tests / compat adapters): no latch.
            return live
        if live:
            # Going multi-root is a LEGITIMATE one-time schema change: the
            # model in THIS conversation needs the ``rootname:`` path hint
            # immediately. On the OFF→ON transition, re-establish the
            # tool-schema latch so the next assembly (this same round, since
            # multiroot_active is read before latch_tool_list) re-freezes the
            # snapshot WITH the hint — one deliberate cache rebuild, then
            # byte-stable again, and no permanent phantom empty-name-diff
            # divergence. Mirrors the clear_all_tool_list_latches MCP-mutation
            # precedent. Idempotent: only the first mark fires the clear.
            if mark_multiroot_sticky(self.conv_id):
                clear_tool_list_latch(self.conv_id)
                logger.info('[ToolLatch] conv=%s went multi-root — cleared '
                            'tool-schema latch so the rootname hint re-freezes '
                            '(one-time cache rebuild)', self.conv_id[:8])
            return True
        return is_multiroot_sticky(self.conv_id)

    def _multiroot_live(self) -> bool:
        """The raw, un-latched multi-root signal from this task's cfg."""
        paths = self.cfg.get('projectPaths') or []
        if not isinstance(paths, (list, tuple)):
            return False
        distinct = {p for p in paths if p}
        return len(distinct) > 1

    @property
    def has_conv_ref(self) -> bool:
        """True when a USER turn actually attached a referenced conversation.

        Enables the conversation-reference tools (``list_conversations`` /
        ``get_conversation``) only when the user genuinely attached a
        conversation via the ``@`` affordance — never because the literal
        token happens to appear in free-form prose.

        Detection, in priority order, scanning **user-role messages only**:
          1. The structured ``convRefs`` / ``convRefTexts`` field — the
             authoritative signal set by the send path when a reference is
             attached (present on raw conversation rows).
          2. The server-injected wrapper signature
             ``[REFERENCED_CONVERSATION`` ... ``title="`` — what
             ``conv_message_builder`` prepends to the user message after
             resolving a ref (present on API-built messages, which no longer
             carry ``convRefs``). The ``title="`` guard distinguishes the
             real injected block from someone quoting the bare token.

        Assistant content is NEVER scanned: a conversation *about* this
        feature (where the model quotes the marker, as in this very chat)
        must not self-enable the tools and break the prompt-cache latch.
        """
        if not self.messages:
            return False
        for m in self.messages:
            if m.get('role') != 'user':
                continue
            if m.get('convRefs') or m.get('convRefTexts'):
                return True
            c = m.get('content', '')
            if isinstance(c, str) and '[REFERENCED_CONVERSATION' in c \
                    and 'title="' in c:
                return True
        return False


# ══════════════════════════════════════════════════════════
#  ToolSpec — one self-describing tool (or tool family)
# ══════════════════════════════════════════════════════════

@dataclass(frozen=True)
class ToolSpec:
    """A declarative tool contribution.

    Parameters
    ----------
    key:
        Unique feature key (e.g. ``'search'``, ``'project'``, ``'memory'``).
        Used for de-dup, introspection, and log lines — NOT shown to the LLM.
    build:
        ``Callable[[ToolContext], list[dict]]`` returning the OpenAI-style
        function schemas to add (possibly empty).  Called at request time,
        so do lazy imports here.  May log and may inspect
        :attr:`ToolContext.current_count` / :attr:`~ToolContext.has_base_tools`.
    phase:
        ``'base'`` (counted toward ``has_real_tools``, emitted first) or
        ``'capability'`` (emitted after the base/capability boundary).
    provides:
        Tool names this spec can contribute.  Used for introspection and to
        derive write/idempotent partitioning downstream.  Optional — purely
        informational; the schemas returned by ``build`` are authoritative.
    write_tools / idempotent_tools:
        Subsets of :attr:`provides` that mutate state / are safe to cache.
        Consumed by ``tool_dispatch`` to keep its concurrency + dedup
        partitions in sync without a second hand-maintained list.
    category / description:
        Human-readable metadata for tooling and docs.
    handler:
        Optional :data:`ToolHandlerFn` that executes this tool's calls.  When
        set, it is auto-synced into the dispatch ``tool_registry`` for every
        name in :attr:`provides` (or :attr:`handler_names` if given).  This is
        what lets an EXTERNAL plugin ship schema + gate + executor from a
        single ``tofu.tools`` entry point — no separate handler-registration
        step in core.  Built-in tools leave this ``None`` and keep registering
        handlers via ``@tool_registry`` decorators in
        ``lib/tasks_pkg/handlers/`` (unchanged).
    handler_names:
        Override the set of tool names :attr:`handler` is bound to.  Defaults
        to :attr:`provides`.  Use this when the handler serves names not listed
        in ``provides`` (rare).
    handler_special:
        If set (e.g. ``'__code_exec__'``), register :attr:`handler` as a
        *special* dispatch key instead of by exact name.
    source:
        Provenance of this spec — ``'builtin'`` (registered by core at import
        time) or ``'plugin'`` (contributed by a third-party ``tofu.tools``
        entry point).  Set automatically by :func:`discover_plugin_specs`;
        built-ins keep the default.  Drives the per-request visibility gate in
        :func:`assemble_tool_list`: ``'builtin'`` specs are ALWAYS evaluated,
        ``'plugin'`` specs only when allow-listed via
        :attr:`ToolContext.enabled_plugins`.  This is the multi-tenant
        isolation seam — see the module-level "Plugin isolation" note.
    plugin_name:
        For ``source='plugin'`` specs, the entry-point name the spec was loaded
        from (e.g. ``'liantong_kb'``).  This — NOT :attr:`key` — is what a
        caller lists in ``config.plugins`` / ``TOFU_DEFAULT_TOOL_PLUGINS`` to
        make the plugin visible.  One entry point may register several specs;
        they all share its ``plugin_name``.  Empty for built-ins.
    """

    key: str
    build: Callable[[ToolContext], list[dict]]
    phase: str = 'base'
    provides: frozenset[str] = field(default_factory=frozenset)
    write_tools: frozenset[str] = field(default_factory=frozenset)
    idempotent_tools: frozenset[str] = field(default_factory=frozenset)
    category: str = ''
    description: str = ''
    handler: ToolHandlerFn | None = None
    handler_names: frozenset[str] = field(default_factory=frozenset)
    handler_special: str = ''
    source: str = 'builtin'
    plugin_name: str = ''


# ── Module-level registry ─────────────────────────────────
_TOOL_SPECS: list[ToolSpec] = []
_REGISTERED_KEYS: set[str] = set()


def register_tool_spec(spec: ToolSpec, *, replace: bool = False) -> None:
    """Register a :class:`ToolSpec`.

    Built-ins register at import time (preserving the canonical order);
    plugins register via the ``tofu.tools`` entry point.

    Args:
        spec: The spec to add.
        replace: If ``True`` and a spec with the same ``key`` exists, replace
            it in place (preserving position).  Otherwise a duplicate key is
            rejected with a warning so a misbehaving plugin can't silently
            shadow a built-in.
    """
    if spec.key in _REGISTERED_KEYS:
        if replace:
            for i, existing in enumerate(_TOOL_SPECS):
                if existing.key == spec.key:
                    _TOOL_SPECS[i] = spec
                    logger.info('[ToolRegistry] replaced spec key=%s', spec.key)
                    _sync_one(spec, _dispatch_registry)
                    return
        logger.warning('[ToolRegistry] duplicate spec key=%s ignored '
                       '(pass replace=True to override)', spec.key)
        return
    if spec.phase not in ('base', 'capability'):
        logger.warning('[ToolRegistry] spec key=%s has unknown phase=%r; '
                       'treating as capability', spec.key, spec.phase)
    _TOOL_SPECS.append(spec)
    _REGISTERED_KEYS.add(spec.key)
    # If the dispatch registry already exists (late registration, e.g. a plugin
    # loaded after startup), sync this spec's handler immediately.  At import
    # time _dispatch_registry is None and the executor's startup
    # sync_spec_handlers() picks everything up.
    _sync_one(spec, _dispatch_registry)


def all_specs() -> list[ToolSpec]:
    """Return the registered specs in registration order (a shallow copy)."""
    return list(_TOOL_SPECS)


# ── Handler sync: push spec-attached handlers into the dispatch registry ──
# The dispatch registry (``lib.tasks_pkg.executor.tool_registry``) is created
# AFTER this module is imported, so we can't bind at module-load time.  The
# executor calls :func:`sync_spec_handlers` once at startup; thereafter
# :func:`register_tool_spec` syncs each late-registered spec on its own.
_dispatch_registry: Any = None


def _sync_one(spec: ToolSpec, registry: Any) -> None:
    """Register *spec*'s handler (if any) into the dispatch *registry*."""
    if spec.handler is None or registry is None:
        return
    try:
        if spec.handler_special:
            registry.register_special(
                spec.handler_special, spec.handler,
                category=spec.category, description=spec.description)
            logger.info('[ToolRegistry] synced handler for special key=%s '
                        '(spec=%s)', spec.handler_special, spec.key)
            return
        names = spec.handler_names or spec.provides
        if not names:
            logger.warning('[ToolRegistry] spec key=%s has a handler but no '
                           'provides/handler_names to bind it to — skipped',
                           spec.key)
            return
        registry.register(
            set(names), spec.handler,
            category=spec.category, description=spec.description)
        logger.info('[ToolRegistry] synced handler for %s (spec=%s)',
                    sorted(names), spec.key)
    except Exception as e:
        logger.error('[ToolRegistry] failed to sync handler for spec=%s: %s',
                     spec.key, e, exc_info=True)


def sync_spec_handlers(registry: Any) -> int:
    """Bind every spec-attached handler into *registry*; remember it.

    Called once by the executor at startup (after ``tool_registry`` exists and
    the built-in ``@tool_registry`` decorators have run).  Idempotent —
    re-registering the same name is a harmless overwrite.

    Returns:
        Count of specs whose handler was synced.
    """
    global _dispatch_registry
    _dispatch_registry = registry
    count = 0
    for spec in _TOOL_SPECS:
        if spec.handler is not None:
            _sync_one(spec, registry)
            count += 1
    return count


def assemble_tool_list(ctx: ToolContext) -> tuple[list[dict], bool]:
    """Build the active tool list from registered specs.

    Emits ``phase='base'`` specs first (counted toward ``has_base_tools``),
    then ``phase='capability'`` specs.  The running count and the
    ``has_base_tools`` flag are exposed on *ctx* between phases so specs can
    self-gate.

    Returns:
        ``(tool_list, has_base_tools)``.  ``tool_list`` may be empty.
    """
    tool_list: list[dict] = []

    def _visible(spec: ToolSpec) -> bool:
        # Built-ins always evaluated; plugins gated by the per-request
        # allow-list so one tenant's installed plugin can't leak into another's
        # tool surface on a shared server.
        if spec.source != 'plugin':
            return True
        if ctx.plugin_allowed(spec.plugin_name):
            return True
        logger.debug('[Task %s] plugin spec key=%s (plugin=%s) hidden — not in '
                     'enabled_plugins', ctx.tid, spec.key, spec.plugin_name)
        return False

    # ── Base phase ──
    for spec in _TOOL_SPECS:
        if spec.phase != 'base' or not _visible(spec):
            continue
        ctx.current_count = len(tool_list)
        try:
            contributed = spec.build(ctx) or []
        except Exception as e:
            logger.error('[ToolRegistry] spec %s build failed: %s',
                         spec.key, e, exc_info=True)
            contributed = []
        tool_list.extend(contributed)

    ctx.has_base_tools = len(tool_list) > 0

    # ── Capability phase ──
    for spec in _TOOL_SPECS:
        if spec.phase != 'capability' or not _visible(spec):
            continue
        ctx.current_count = len(tool_list)
        try:
            contributed = spec.build(ctx) or []
        except Exception as e:
            logger.error('[ToolRegistry] spec %s build failed: %s',
                         spec.key, e, exc_info=True)
            contributed = []
        tool_list.extend(contributed)

    return tool_list, ctx.has_base_tools


# ══════════════════════════════════════════════════════════
#  Built-in spec builders — each reproduces one legacy branch
#  exactly (including its logging).  Heavy imports stay lazy.
# ══════════════════════════════════════════════════════════

def _build_search(ctx: ToolContext) -> list[dict]:
    # 'single' is a retired mode kept as a legacy alias for old conversations
    # — it now behaves like 'multi' (the one-shot SEARCH_TOOL_SINGLE schema
    # was removed). Only 'off' yields no search tool.
    from lib.tools import SEARCH_TOOL_MULTI
    if ctx.search_mode in ('single', 'multi'):
        return [SEARCH_TOOL_MULTI]
    return []


def _build_fetch(ctx: ToolContext) -> list[dict]:
    from lib.tools import FETCH_URL_TOOL
    if ctx.fetch_enabled or ctx.search_enabled:
        return [FETCH_URL_TOOL]
    return []


def _build_read_files(ctx: ToolContext) -> list[dict]:
    # read_files is ALWAYS on — handles project-relative AND absolute local
    # paths (images, PDFs, Office docs, text), so the model can read local
    # content even with no project attached.
    from lib.tools import READ_FILES_TOOL
    if ctx.project_enabled and ctx.multiroot_active:
        from lib.tools.project import with_multiroot_hint
        return with_multiroot_hint([READ_FILES_TOOL])
    return [READ_FILES_TOOL]


def _build_inspect_image(ctx: ToolContext) -> list[dict]:
    # inspect_image is ALWAYS on (like read_files) — it re-renders a region
    # of any local image at full resolution so the model can read detail the
    # initial downscale discarded. No project / vision toggle gates it; the
    # dispatch path drops the resulting image for text-only models anyway.
    from lib.tools import INSPECT_IMAGE_TOOL
    if ctx.project_enabled and ctx.multiroot_active:
        from lib.tools.project import with_multiroot_hint
        return with_multiroot_hint([INSPECT_IMAGE_TOOL])
    return [INSPECT_IMAGE_TOOL]


def _build_project_or_code_exec(ctx: ToolContext) -> list[dict]:
    from lib.tools import CODE_EXEC_TOOL, PROJECT_TOOLS
    if ctx.project_enabled:
        if ctx.multiroot_active:
            from lib.tools.project import with_multiroot_hint
            return with_multiroot_hint(PROJECT_TOOLS)
        return list(PROJECT_TOOLS)
    if ctx.code_exec_enabled:
        return [CODE_EXEC_TOOL]
    return []


def _build_browser(ctx: ToolContext) -> list[dict]:
    if not ctx.browser_enabled:
        return []
    from lib.browser import is_extension_connected
    if is_extension_connected():
        from lib.browser.advanced import ADVANCED_BROWSER_TOOLS
        from lib.tools import BROWSER_TOOLS
        tools = list(BROWSER_TOOLS) + list(ADVANCED_BROWSER_TOOLS)
        logger.debug('[Task %s] Browser extension connected — browser tools '
                     'enabled (%d tools)', ctx.tid, len(tools))
        return tools
    logger.warning('[Task %s] Browser requested but extension not connected',
                   ctx.tid)
    return []


def _build_desktop(ctx: ToolContext) -> list[dict]:
    if not ctx.desktop_enabled:
        return []
    from lib.desktop import is_desktop_agent_connected
    if is_desktop_agent_connected():
        from lib.desktop_tools import DESKTOP_TOOLS
        logger.debug('[Task %s] 🖥️ Desktop agent connected — %d desktop tools '
                     'enabled', ctx.tid, len(DESKTOP_TOOLS))
        return list(DESKTOP_TOOLS)
    logger.warning('[Task %s] Desktop requested but agent not connected',
                   ctx.tid)
    return []


def _build_image_gen(ctx: ToolContext) -> list[dict]:
    if not ctx.image_gen_enabled:
        return []
    from lib.tools.image_gen import GENERATE_IMAGE_TOOL
    logger.debug('[Task %s] 🎨 Image generation tool enabled', ctx.tid)
    return [GENERATE_IMAGE_TOOL]


def _build_conv_ref(ctx: ToolContext) -> list[dict]:
    # CONV_REF_TOOLS = [list_conversations, get_conversation] — BOTH are
    # read-only (discover siblings + open one). Register them in two cases:
    #   (a) the user @-mentioned a conversation (the classic explicit path), OR
    #   (b) we're in project mode — the always-on cross-conv digest
    #       (system_context.py ★4.4) names sibling conversations for ambient
    #       awareness, so the model must be ABLE to open a surfaced sibling
    #       rather than being told about phantom tools. Gating only on
    #       has_conv_ref meant the digest header advertised tools absent from
    #       the schema on a plain project turn (the conv_tools_available
    #       branch). Registering them in project mode closes that gap.
    # Both branches require at least one base tool (current_count > 0): with no
    # tools at all there's no schema to extend.
    if ctx.current_count <= 0:
        return []
    if ctx.has_conv_ref or (ctx.project_enabled and ctx.project_path):
        from lib.tools import CONV_REF_TOOLS
        logger.debug('[Task %s] 💬 conv_ref tools enabled (has_conv_ref=%s '
                     'project=%s)', ctx.tid, ctx.has_conv_ref,
                     bool(ctx.project_enabled and ctx.project_path))
        tools = list(CONV_REF_TOOLS)
        # Project Charter tools (Pillar #2): the shared north star. Only in
        # project mode (a charter is per-project) — read + propose. Commit is
        # human-gated and is NEVER exposed as an agent tool.
        if ctx.project_enabled and ctx.project_path:
            from lib.tools import BOARD_TOOLS, CHARTER_TOOLS, PEER_TOOLS
            tools += list(CHARTER_TOOLS)
            # Project Board tools (Pillar #3): the coordination board — the
            # mechanism that makes conversations auto-coordinate (claim/avoid
            # duplicating). Project-scoped, same gate.
            tools += list(BOARD_TOOLS)
            # Project Peer tools (Pillar #6): cross-conversation communication
            # — live peer status + advisory messaging + advisory/gated
            # intervention. Same project gate; registered on every project turn
            # so the model can coordinate without the phantom-tool trap.
            tools += list(PEER_TOOLS)
        return tools
    return []


def _build_human_guidance(ctx: ToolContext) -> list[dict]:
    if ctx.human_guidance_enabled and ctx.current_count > 0:
        from lib.tools.human_guidance import ASK_HUMAN_TOOL
        logger.info('[Task %s] 🙋 Human guidance (ask_human) tool enabled',
                    ctx.tid)
        return [ASK_HUMAN_TOOL]
    if ctx.human_guidance_enabled:
        logger.debug('[Task %s] 🙋 Human guidance requested but no base tools '
                     '— skipped', ctx.tid)
    return []


def _build_memory(ctx: ToolContext) -> list[dict]:
    # Memory tools attach whenever ANY real tool exists.  Note: this is gated
    # on has_base_tools, NOT on memoryEnabled — the memoryEnabled flag only
    # controls the system-prompt memory instructions (see system_context.py).
    if not ctx.has_base_tools:
        return []
    from lib.memory import ALL_MEMORY_TOOLS
    return list(ALL_MEMORY_TOOLS)


def _build_scheduler(ctx: ToolContext) -> list[dict]:
    if ctx.scheduler_enabled and ctx.has_base_tools:
        from lib.scheduler.tool_defs import SCHEDULER_TOOLS
        logger.debug('[Task %s] ⏰ Scheduler tools enabled (%d tools)',
                     ctx.tid, len(SCHEDULER_TOOLS))
        return list(SCHEDULER_TOOLS)
    return []


def _build_swarm(ctx: ToolContext) -> list[dict]:
    # NOT gated on has_base_tools — a bare-conversation research swarm is a
    # valid use case (mirrors the read_files decoupling).
    if not ctx.swarm_enabled:
        return []
    from lib.swarm.tools import (
        AWAIT_AGENTS_TOOL,
        GET_AGENT_RESULT_TOOL,
        SPAWN_AGENTS_TOOL,
    )
    logger.debug('[Task %s] 🐝 Async swarm enabled — spawn_agents / '
                 'await_agents / get_agent_result (project_enabled=%s)',
                 ctx.tid, ctx.project_enabled)
    return [SPAWN_AGENTS_TOOL, AWAIT_AGENTS_TOOL, GET_AGENT_RESULT_TOOL]


def _build_mcp(ctx: ToolContext) -> list[dict]:
    # Bridge to external MCP servers — schemas fetched dynamically at request
    # time.  Default: enabled.  Benchmarks may pass mcpEnabled=False.
    if not ctx.cfg.get('mcpEnabled', True):
        logger.debug('[Task %s] MCP disabled via mcpEnabled=false', ctx.tid)
        return []
    try:
        from lib.mcp import get_bridge
        bridge = get_bridge()
        if bridge.connected:
            mcp_tools = bridge.get_openai_tool_defs()
            if mcp_tools:
                logger.info('[Task %s] 🔌 MCP tools loaded: %d from %d servers',
                            ctx.tid, len(mcp_tools), bridge.server_count)
                return list(mcp_tools)
    except Exception as e:
        logger.debug('[Task %s] MCP bridge not available: %s', ctx.tid, e)
    return []


def _build_custom(ctx: ToolContext) -> list[dict]:
    # Per-request custom tools brought by a headless /api/v1/agent/run caller.
    # The route validates + mints a ToolEnvironment, stashes its clean schemas
    # on cfg['_customToolSchemas'], and attaches the env as task['_tool_env']
    # (whose handlers the executor resolves before the global registry).
    # Registered LAST so the cache-stable built-in ordering is untouched.
    schemas = ctx.cfg.get('_customToolSchemas')
    if not schemas or not isinstance(schemas, list):
        return []
    logger.info('[Task %s] 🧩 Custom tools injected: %d', ctx.tid, len(schemas))
    return list(schemas)


def _register_builtins() -> None:
    """Register the built-in tool specs in canonical (cache-stable) order."""
    builtins = [
        # ── base phase (counted toward has_real_tools) ──
        ToolSpec('search', _build_search, phase='base',
                 provides=frozenset({'web_search'}),
                 idempotent_tools=frozenset({'web_search'}),
                 category='search', description='Web search'),
        ToolSpec('fetch', _build_fetch, phase='base',
                 provides=frozenset({'fetch_url'}),
                 idempotent_tools=frozenset({'fetch_url'}),
                 category='search', description='Fetch a URL'),
        ToolSpec('read_files', _build_read_files, phase='base',
                 provides=frozenset({'read_files'}),
                 idempotent_tools=frozenset({'read_files'}),
                 category='project', description='Read local files'),
        ToolSpec('inspect_image', _build_inspect_image, phase='base',
                 provides=frozenset({'inspect_image'}),
                 idempotent_tools=frozenset({'inspect_image'}),
                 category='project', description='Zoom/rotate/crop image viewer'),
        ToolSpec('project', _build_project_or_code_exec, phase='base',
                 provides=frozenset({
                     'list_dir', 'grep_search', 'find_files',
                     'write_file', 'apply_diff', 'apply_diffs',
                     'insert_content', 'insert_contents',
                     'create_project', 'run_command',
                 }),
                 write_tools=frozenset({
                     'write_file', 'apply_diff', 'apply_diffs',
                     'insert_content', 'insert_contents',
                     'create_project', 'run_command',
                 }),
                 idempotent_tools=frozenset({
                     'list_dir', 'grep_search', 'find_files',
                 }),
                 category='project', description='Project file tools / code exec'),
        ToolSpec('browser', _build_browser, phase='base',
                 category='browser', description='Browser automation tools'),
        ToolSpec('desktop', _build_desktop, phase='base',
                 category='desktop', description='Desktop agent tools'),
        ToolSpec('image_gen', _build_image_gen, phase='base',
                 provides=frozenset({'generate_image'}),
                 category='image', description='Image generation'),
        ToolSpec('conv_ref', _build_conv_ref, phase='base',
                 provides=frozenset({'list_conversations', 'get_conversation',
                                     'project_charter_read', 'project_charter_propose',
                                     'project_board_read', 'project_board_post',
                                     'project_board_claim', 'project_board_complete',
                                     'project_board_block',
                                     'project_peer_status', 'project_message',
                                     'project_intervene'}),
                 idempotent_tools=frozenset({'list_conversations', 'get_conversation',
                                             'project_charter_read', 'project_board_read',
                                             'project_peer_status'}),
                 category='conversation', description='Conversation reference tools'),
        ToolSpec('human_guidance', _build_human_guidance, phase='base',
                 provides=frozenset({'ask_human'}),
                 category='human', description='Ask the human for guidance'),
        # ── capability phase ──
        ToolSpec('memory', _build_memory, phase='capability',
                 write_tools=frozenset({
                     'create_memory', 'update_memory',
                     'delete_memory', 'merge_memories',
                 }),
                 category='memory', description='Memory CRUD tools'),
        ToolSpec('scheduler', _build_scheduler, phase='capability',
                 category='scheduler', description='Scheduler / proactive agent tools'),
        ToolSpec('swarm', _build_swarm, phase='capability',
                 provides=frozenset({
                     'spawn_agents', 'await_agents', 'get_agent_result',
                 }),
                 category='swarm', description='Async multi-agent swarm'),
        ToolSpec('mcp', _build_mcp, phase='capability',
                 category='mcp', description='External MCP-server tools'),
        # ── per-request custom tools (always last; handlers are task-local) ──
        ToolSpec('custom', _build_custom, phase='capability',
                 category='custom',
                 description='Per-request custom tools (handlers via task[_tool_env])'),
    ]
    for spec in builtins:
        register_tool_spec(spec)


# ══════════════════════════════════════════════════════════
#  Plugin discovery — external packages contribute via entry points
# ══════════════════════════════════════════════════════════

def discover_plugin_specs() -> int:
    """Load third-party tool specs from the ``tofu.tools`` entry-point group.

    A plugin package declares in its ``pyproject.toml``::

        [project.entry-points."tofu.tools"]
        weather = "my_pkg.weather:register"

    where ``register`` is a callable that receives :func:`register_tool_spec`
    and uses it to add one or more :class:`ToolSpec` objects::

        def register(register_tool_spec):
            register_tool_spec(ToolSpec('weather', _build_weather, ...))

    Failures in any single plugin are logged and skipped — a broken plugin
    never takes down tool assembly.

    Returns:
        The number of entry points successfully loaded.
    """
    loaded = 0
    try:
        from importlib.metadata import entry_points
    except Exception as e:  # pragma: no cover — importlib.metadata always present on 3.8+
        logger.debug('[ToolRegistry] importlib.metadata unavailable: %s', e)
        return 0
    try:
        eps = entry_points(group='tofu.tools')
    except TypeError:
        # Python <3.10 returns a dict-like; filter by group key.
        eps = entry_points().get('tofu.tools', [])  # type: ignore[attr-defined]
    except Exception as e:
        logger.debug('[ToolRegistry] entry_points lookup failed: %s', e)
        return 0
    for ep in eps:
        ep_name = getattr(ep, 'name', '?')
        try:
            register_fn = ep.load()
            # Hand the plugin a wrapper that STAMPS provenance onto every spec
            # it registers, so we don't depend on the plugin author remembering
            # to set source/plugin_name. This is what makes the per-request
            # visibility gate (ToolContext.enabled_plugins) able to tell a
            # plugin's specs apart from built-ins — see the module "Plugin
            # isolation" note. ``replace`` is safe on the frozen dataclass.
            def _stamping_register(spec: ToolSpec, *, replace_existing: bool = False,
                                   _pname: str = ep_name, **_kw) -> None:
                # Accept the plugin author's ``replace=`` kwarg under either
                # name (back-compat) without colliding with dataclasses.replace.
                do_replace = replace_existing or bool(_kw.get('replace'))
                stamped = replace(spec, source='plugin', plugin_name=_pname)
                register_tool_spec(stamped, replace=do_replace)
            register_fn(_stamping_register)
            loaded += 1
            logger.info('[ToolRegistry] loaded plugin tool spec(s) from %s',
                        ep_name)
        except Exception as e:
            logger.warning('[ToolRegistry] plugin %s failed to load: %s',
                           ep_name, e, exc_info=True)
    return loaded


def available_plugins() -> dict[str, list[str]]:
    """Map each loaded plugin name → the spec keys it registered.

    Introspection helper for ops / docs / a future ``/api/v1/capabilities``
    surface: lets an operator see WHICH third-party plugins are installed in
    this process and therefore what a caller may name in ``config.plugins``.
    Built-in specs are excluded.
    """
    out: dict[str, list[str]] = {}
    for spec in _TOOL_SPECS:
        if spec.source == 'plugin' and spec.plugin_name:
            out.setdefault(spec.plugin_name, []).append(spec.key)
    return out


# ══════════════════════════════════════════════════════════
#  Per-request plugin allow-list resolution
# ══════════════════════════════════════════════════════════

_DEFAULT_PLUGINS_ENV = 'TOFU_DEFAULT_TOOL_PLUGINS'


def _parse_plugin_spec(value: Any) -> set[str] | None:
    """Normalise a raw plugins value into an allow-list set (or ``None``).

    Accepts:
      * ``'*'`` / ``['*']`` / ``'all'`` → ``None`` (gate fully open, ALL
        plugins visible).
      * a comma/space-separated string  → set of names.
      * a list/tuple/set of names       → set of names.
      * ``None`` / ``''`` / ``[]``      → empty set (NO plugins visible).

    The ``'*'`` sentinel maps to ``None`` because that is exactly the
    ``ToolContext.enabled_plugins`` value meaning "allow everything".
    """
    if value is None:
        return set()
    if isinstance(value, str):
        v = value.strip()
        if v in ('*', 'all'):
            return None
        if not v:
            return set()
        return {tok for tok in re.split(r'[,\s]+', v) if tok}
    if isinstance(value, (list, tuple, set)):
        items = {str(x).strip() for x in value if str(x).strip()}
        if '*' in items or 'all' in items:
            return None
        return items
    logger.debug('[ToolRegistry] ignoring unrecognised plugins value: %r', value)
    return set()


def resolve_enabled_plugins(cfg: dict[str, Any]) -> set[str] | None:
    """Resolve the per-request plugin allow-list for :class:`ToolContext`.

    Resolution order (first non-absent wins):

    1. ``cfg['plugins']`` — request-scoped. A headless caller sets this via
       ``config.plugins`` on ``/api/v1/agent/run`` (or any orchestrator cfg).
    2. ``TOFU_DEFAULT_TOOL_PLUGINS`` env var — deployment-wide default. A
       dedicated single-tenant install (e.g. liantong's ``app/`` copy) sets
       this once so it never has to pass ``plugins`` per request.
    3. Neither set → **fail-closed**: empty set → NO third-party plugins.

    Each level accepts the :func:`_parse_plugin_spec` vocabulary, including the
    ``'*'`` wildcard (→ ``None`` = all plugins visible).

    Returns:
        ``None`` (all plugins), ``set()`` (none), or a set of plugin names.
    """
    if 'plugins' in cfg:
        return _parse_plugin_spec(cfg.get('plugins'))
    env = os.environ.get(_DEFAULT_PLUGINS_ENV)
    if env is not None:
        return _parse_plugin_spec(env)
    return set()


# Register built-ins + discover plugins at import time.
_register_builtins()
discover_plugin_specs()
