"""System context injection orchestrator — the per-turn assembly entrypoint.

Extracted from ``lib.tasks_pkg.system_context`` (facade-preserving split).

Houses :func:`_inject_system_contexts` (the big per-task assembler with its
inline ``<parallel_execution>`` swarm-prompt literal), the disabled-block
config parser, the shared last-user-text extractor, and the static-block
idempotency marker.
"""

from lib.log import get_logger

logger = get_logger(__name__)

from lib.tasks_pkg import system_prompt_cc

from lib.tasks_pkg.system_context._reminders import (
    _system_text,
    _wrap_system_reminder,
    _append_to_system_message,
    _refresh_tail_block,
)
from lib.tasks_pkg.system_context._profile import (
    _PROFILE_MARKER,
    _PROFILE_DETAIL_MARKER,
    _insert_user_context_message,
    _append_user_profile_block,
    _refresh_detail_block,
)
from lib.tasks_pkg.system_context import _freeze as _ctx_freeze


# Marker embedded in the Claude-Code static block. Used as the idempotency
# probe that says "the CC static prompt is already in the system message".
_CC_STATIC_MARKER = "IMPORTANT: You must NEVER generate or guess URLs"


def _disabled_prompt_blocks(cfg: dict) -> set[str] | None:
    """Extract the user's disabled static-prompt block IDs from task config.

    Config shape (set by the per-block system-prompt editor)::

        cfg['systemPromptBlocks'] = {'disabled': ['tone_and_style', ...]}

    Returns a set of block IDs, or ``None`` when nothing is disabled (so the
    default — keep every block — is preserved for old configs).
    """
    try:
        spb = cfg.get('systemPromptBlocks') or {}
        disabled = spb.get('disabled') or []
        ids = {str(x) for x in disabled if x}
        return ids or None
    except Exception as e:
        logger.debug('[Inject] disabled-blocks parse failed: %s', e)
        return None


def _inject_system_contexts(messages, project_path, project_enabled,
                             memory_enabled, search_enabled, swarm_enabled,
                             has_real_tools, conv_id: str = '',
                             task: dict = None, model: str = '',
                             system_prompt_mode: str = 'append',
                             tool_names: set[str] | None = None,
                             disabled_blocks: set[str] | None = None):
    """Inject the Claude-Code-style system + user contexts into *messages*.

    Modifies the messages list directly. Final shape:

      System message (one cache-stable block per entry):
        1. Static Claude-Code-style prompt
           (intro / # System / # Doing tasks / # Executing actions with care
            / # Using your tools / # Tone and style / # Output efficiency
            / # Function Result Clearing / SUMMARIZE / system-reminder note
            / # Environment / Notes: / Current date)
        2. (optional) Memory accumulation instructions + count hint
        3. (optional) Swarm / parallel-execution prompt

      User message at index 1 (prepended before real user turn):
        <system-reminder> CLAUDE.md / project-intelligence </system-reminder>
        with ``_isMeta: True`` — matches Claude Code's ``prependUserContext``.

    Memory prefetch support: if ``task`` is provided and contains
    ``_prefetch_project`` / ``_prefetch_memory`` futures, their already-
    completed results are consumed instead of re-computing (saving FUSE I/O
    latency).

    **Idempotency.**  Each section checks for its own marker via
    ``_system_text(messages)`` and skips if already present — required for
    endpoint-mode (Planner / Worker / Critic share the same messages) and
    for post-compaction re-injection.

    Args:
        model: Model ID for the ``# Environment`` section.
        system_prompt_mode: ``'append'`` (default) injects the built-in
            Claude-Code static block on top of any user system prompt;
            ``'replace'`` suppresses the static block so the user's
            system prompt is the sole base. CLAUDE.md / memory / swarm /
            date are still injected in both modes (they track feature
            toggles, not the base prose). A ``'replace'`` request with no
            user system prompt falls back to ``'append'`` so the model is
            never left with an empty base.
        tool_names: Set of tool names registered for this turn, forwarded
            to ``build_static_prompt`` so the ``# Using your tools`` section
            only names dedicated tools that actually exist. ``None`` (the
            default) ships all bullets — back-compat for callers without a
            tool list.
        disabled_blocks: Static-prompt block IDs the user switched OFF in the
            per-block editor (see ``system_prompt_cc.BLOCK_META``). Forwarded
            to ``build_static_prompt`` so those blocks are dropped. ``None``
            keeps every block.
    """
    _cid = conv_id or ''

    # ── Turn-boundary freeze (epic pt_62ed8cce25324eb2) ──
    # 1) RESTORE last turn's volatile tail blocks onto historical user
    #    messages so the rebuilt wire is byte-identical to what those
    #    messages carried on the wire last turn (persistence strips the
    #    blocks; without restore a tool-heavy prior turn's user message is a
    #    prefix mutation at a deep position → whole-body re-bill).
    # 2) CAPTURE the last user message's entry content (post-prefetch,
    #    pre-system-context) — the key under which its final content is
    #    recorded at the end of this assembly for the NEXT turn's restore.
    # Skipped for snapshot/debug callers (conv_id empty → side-effect-free).
    if _cid:
        try:
            _ctx_freeze.restore_tail_blocks(messages, _cid)
        except Exception as _fre:
            logger.debug('[CtxFreeze] restore failed conv=%s: %s',
                         (_cid or '?')[:8], _fre)
    _tail_entry_content = None
    for _i in range(len(messages) - 1, -1, -1):
        if messages[_i].get('role') == 'user':
            _c = messages[_i].get('content')
            _tail_entry_content = (list(_c) if isinstance(_c, list) else _c)
            break

    # ── Context-assembly trace (STRICTLY LOCAL — no module-level state). ──
    #   One unified [Context] observability layer for prompt assembly: every
    #   block that gets spliced records (name, chars-actually-spliced) here and
    #   emits a DEBUG drill-down line; suppressed seams emit a DEBUG "skip" line
    #   with the reason. At the END of assembly _emit_context_summary() writes
    #   ONE INFO line naming every injected block + total — the per-assembly
    #   trace (this function runs ONCE per task at round 0, so the summary is
    #   per-assembly, NOT per-round). All three helpers swallow their own
    #   exceptions: instrumentation must NEVER break the turn it observes.
    #   `chars` is the length of the string ACTUALLY handed to the splice
    #   helper (wrapper included for <system-reminder>-wrapped blocks), so the
    #   summary total equals the real delta in assembled prompt bytes.
    _trace: list[tuple[str, int]] = []

    def _trace_fallback(detail: str) -> None:
        # Last-resort diagnostic for a trace-helper fault. Deliberately
        # NON-'[Context]'-prefixed and itself fully swallowed so a failing
        # logging backend can never propagate out of the instrumentation and
        # break the turn it is only observing.
        try:
            logger.debug('[CtxTrace] %s', detail)
        except Exception:
            pass

    def _ctx_injected(name: str, chars: int) -> None:
        try:
            _trace.append((name, int(chars)))
            logger.debug('[Context] conv=%s inject block=%s chars=%d',
                         (_cid or '?')[:8], name, chars)
        except Exception as _e:
            _trace_fallback('inject log failed for %s: %s' % (name, _e))

    def _ctx_suppressed(name: str, reason: str) -> None:
        try:
            logger.debug('[Context] conv=%s skip block=%s reason=%s',
                         (_cid or '?')[:8], name, reason)
        except Exception as _e:
            _trace_fallback('suppress log failed for %s: %s' % (name, _e))

    def _emit_context_summary() -> None:
        """Emit the single per-assembly INFO trace line from _trace."""
        try:
            _blocks = ','.join('%s:%d' % (n, c) for n, c in _trace)
            _total = sum(c for _, c in _trace)
            _round = len((task or {}).get('toolRounds') or []) if task else 0
            logger.info('[Context] conv=%s round=%d blocks=[%s] total=%d',
                        (_cid or '?')[:8], _round, _blocks, _total)
        except Exception as _e:
            _trace_fallback('summary emit failed: %s' % _e)

    # ── Idempotency probe: detect an already-injected system message ──
    _existing = _system_text(messages)

    # ── Replace mode: the user's system prompt fully substitutes the
    #    built-in static block. The user prompt arrives as messages[0]
    #    (role=system) from the message builder. Only honour replace when
    #    that prompt is actually non-empty — otherwise an empty 'replace'
    #    would leave the model with no base prompt at all. Endpoint-mode
    #    re-entry already has the static block, so the marker guard below
    #    still wins there.
    _replace_static = (
        system_prompt_mode == 'replace'
        and _CC_STATIC_MARKER not in _existing
        and bool(_existing.strip())
    )

    # ── Helper: try to get prefetched result, else compute synchronously ──
    def _get_prefetched(key, fallback_fn):
        """Get result from prefetch future if available, else call fallback."""
        if task and task.get(key):
            future = task[key]
            if future.done():
                try:
                    result = future.result(timeout=0)
                    logger.debug('[MemPrefetch] Using prefetched %s', key)
                    return result
                except Exception as e:
                    logger.debug('[MemPrefetch] %s failed, falling back: %s',
                                 key, e)
            else:
                logger.debug('[MemPrefetch] %s not done yet, falling back', key)
        return fallback_fn()

    # ── Turn-boundary head freeze: while the previous turn's cache entry
    #    is still alive, reuse the BYTE-FROZEN carrier body / memory-count
    #    hint from last turn — the journal digest and memory count change
    #    every turn in an active project, and both sit in the cached prefix
    #    head, so a fresh render every turn re-keyed the whole prefix
    #    (measured: 2026-08-01, the dominant turn_boundary_rebill driver).
    _frozen_head = {}
    if _cid:
        try:
            _frozen_head = _ctx_freeze.get_frozen_head(
                _cid, project_path if project_enabled else '') or {}
        except Exception as _fhe:
            logger.debug('[CtxFreeze] head lookup failed conv=%s: %s',
                         (_cid or '?')[:8], _fhe)
    _frozen_carrier_body = _frozen_head.get('carrier_body')

    # ── Load project context (CLAUDE.md) once ──
    proj_ctx = ''
    if project_enabled and not _frozen_carrier_body:
        def _load_project():
            from lib.project_mod import get_context_for_prompt
            return get_context_for_prompt(project_path, conv_id=_cid or None)

        # FUSE-slow load is absorbed by the prefetch future (_prefetch_project);
        # _get_prefetched consumes it when ready, else falls back to a sync
        # load. No conv-scoped result cache — each task gets fresh messages and
        # ALWAYS injects, so a hash cache could not skip the compute anyway.
        # A frozen carrier body makes the whole load unnecessary (the OTHER
        # win: the FUSE read is skipped entirely on warm reuse).
        proj_ctx = _get_prefetched('_prefetch_project', _load_project) or ''

    logger.info('[Inject] conv=%s proj_enabled=%s proj_ctx_len=%d '
                'has_real_tools=%s',
                (_cid or '?')[:8], project_enabled, len(proj_ctx or ''),
                has_real_tools)

    # ★ 1. Static Claude-Code block — append as separate cache-stable block.
    #      Injected ONCE; marker guards against endpoint-mode re-entry.
    #      Skipped entirely in replace mode (user prompt is the base).
    if _replace_static:
        logger.info('[Inject] conv=%s system_prompt_mode=replace — built-in '
                    'static block suppressed (user prompt is the base)',
                    (_cid or '?')[:8])
        _ctx_suppressed('static', 'replace_mode')
    elif _CC_STATIC_MARKER in _existing:
        _ctx_suppressed('static', 'marker_present')
    if _CC_STATIC_MARKER not in _existing and not _replace_static:
        # When project mode is OFF, suppress the working-directory bullet
        # entirely — leaking the server's cwd in the system prompt has caused
        # the model to chase ghost paths (the path is the server's runtime
        # location, not anything the user provided).
        _cwd = project_path if project_enabled else ''
        try:
            import os as _os
            _is_git = bool(_cwd and _os.path.isdir(_os.path.join(_cwd, '.git')))
        except Exception as e:
            logger.debug('[SysPrompt] is_git probe failed: %s', e)
            _is_git = False

        # Extra-roots (multi-root workspace) — reuse project_mod snapshot.
        # Same suppression rule: if there's no primary cwd to anchor them
        # against (project mode off), don't expose extras either.
        _extra_roots = []
        if project_enabled:
            try:
                # Source from the per-conv registry (when known) so the
                # advertised roots match what resolve_namespaced_path will
                # accept at tool-call time.  Reading the global _roots here
                # leaks concurrent tasks' roots into this conv's prompt and
                # causes "Unknown workspace root" refusals — see
                # get_context_for_prompt's conv_id docstring.
                from lib.project_mod.config import get_conv_roots
                for _rn, _rs in get_conv_roots(_cid or None).items():
                    if _rs.get('path') and _rs['path'] != _cwd:
                        _extra_roots.append(f"{_rn} → {_rs['path']}")
            except Exception as e:
                logger.debug('[SysPrompt] extra-roots probe failed: %s', e)

        _static_block = system_prompt_cc.build_static_prompt(
            cwd=_cwd, is_git=_is_git, model=model,
            extra_roots=_extra_roots or None,
            has_real_tools=has_real_tools,
            # SWE-bench-shaped guidance (code-hygiene bullets, git/CI
            # examples, file_path:line_number style) only ships when
            # project mode is on. For chat-only / paper-Q&A / translation
            # turns, the prompt becomes a generic-assistant prompt.
            is_code_context=project_enabled,
            # Only name dedicated tools in "# Using your tools" that are
            # actually registered this turn — otherwise the model is told
            # write_file / apply_diff / grep_search exist when project mode
            # is off and tries to call a tool absent from the schema.
            tool_names=tool_names,
            # User-disabled blocks from the per-block system-prompt editor.
            disabled_blocks=disabled_blocks,
            # Current date is NO LONGER baked into the cached static block —
            # it rides the true tail (see ★4.5). Keeping it here re-billed the
            # whole system prefix at every UTC-day rollover (Anthropic's named
            # "don't inject timestamps into the cached prompt" anti-pattern).
            include_date=False,
        )
        _append_to_system_message(messages, _static_block,
                                   as_separate_block=True)
        _existing = _system_text(messages)
        _ctx_injected('static', len(_static_block))

    # ★ 2. Project CLAUDE.md → prepended user _isMeta message (cache-friendly).
    #    Warm-window reuse: the FROZEN body from last turn keeps the carrier
    #    (index 1, inside the cached prefix) byte-identical across the turn
    #    boundary; a fresh render only happens on a cold window (cache dead
    #    anyway) or a project switch.
    if _frozen_carrier_body and '[PROJECT CO-PILOT MODE]' not in _existing:
        _insert_user_context_message(messages, _frozen_carrier_body)
        logger.debug('[CtxFreeze] conv=%s CLAUDE.md carrier reused FROZEN '
                     'body (%d chars)',
                     (_cid or '?')[:8], len(_frozen_carrier_body))
        _ctx_injected('claude_md', len(_frozen_carrier_body))
    elif proj_ctx and '[PROJECT CO-PILOT MODE]' not in _existing:
        _reminder = system_prompt_cc.build_user_context_reminder(
            claude_md=proj_ctx, current_date=None,
        )
        if _reminder:
            _insert_user_context_message(messages, _reminder)
            logger.info('[Inject] conv=%s CLAUDE.md inserted as user '
                        '_isMeta msg (len=%d)',
                        (_cid or '?')[:8], len(_reminder))
            _ctx_injected('claude_md', len(_reminder))
            if _cid:
                try:
                    _ctx_freeze.store_head(
                        _cid, project_path if project_enabled else '',
                        carrier_body=_reminder)
                except Exception as _fse:
                    logger.debug('[CtxFreeze] carrier store failed conv=%s: %s',
                                 (_cid or '?')[:8], _fse)
    elif proj_ctx:
        # CLAUDE.md is already IN the system message — shouldn't happen under
        # the single-layout design.  Left as a warning so stale snapshots /
        # external injections are surfaced.
        logger.warning('[Inject] conv=%s CLAUDE.md marker found in system '
                       'text — something is placing it in system instead of '
                       'as a user _isMeta msg. Check endpoint re-entry / '
                       'stale legacy code paths.',
                       (_cid or '?')[:8])

    # ★ 2.5 Personal preference profile → appended to the prepended _isMeta
    #   user message (NOT the system prefix). This is the cache-safe seam: the
    #   profile is small + bounded but DOES change when the consolidation pass
    #   rewrites it, so it must ride the BP4 5-min-TTL tail like CLAUDE.md /
    #   <relevant_memories> — never messages[0] (that would re-write the whole
    #   cached prefix on every preference edit; see the memory-count-hint and
    #   timestamp-placement A/B lessons). Gated on memory_enabled so the
    #   Memory toggle's "off → no proactive memory plumbing" semantics hold.
    # Preferences are a DISTINCT personal capability from the memory store
    # (see lib/agent_core/personal_scope). The UI keeps the historical
    # behaviour (profile rides the Memory toggle) via the memory_enabled
    # fallback; a headless caller that enabled the memory store does NOT get
    # the operator's personal profile unless it ALSO opts into preferences.
    from lib.agent_core.personal_scope import resolve_preferences_enabled
    _prefs_enabled = resolve_preferences_enabled(
        task.get('config') if task else None, memory_enabled=memory_enabled)
    if not _prefs_enabled:
        _ctx_suppressed('pref_core', 'preferences_disabled')
    elif _PROFILE_MARKER in _existing:
        _ctx_suppressed('pref_core', 'marker_present')
    if _prefs_enabled and _PROFILE_MARKER not in _existing:
        # Identity scope: captured onto the task at creation from the request's
        # AuthContext.user_id (multi-user tenant) — '' for open/private mode →
        # the single global profile, so personal installs are byte-identical.
        _profile_scope = (task.get('_profileScope', '') if task else '') or ''
        # TIERED injection (relevance gating). The CORE tier (work-style /
        # standing instructions, e.g. ## Preferences) is byte-stable across
        # turns and always injected — the always-on, cache-friendly block. The
        # DETAIL tier (identity / project-specific facts, e.g. ## About the
        # user) is scored by BM25 against THIS turn's last-user text and only
        # the relevant bullets are injected, as a SEPARATE block. Both ride the
        # same _isMeta tail (the BP4 5-min-TTL seam) — the core stays
        # byte-stable there so it keeps its cache hit, while the detail block
        # varying per turn is exactly the cache-safe pattern <relevant_memories>
        # already uses. See render_profile_tiers + lib/memory/relevance.score_items.
        try:
            from lib.memory.user_profile import load_profile, render_profile_tiers
            _profile_body = load_profile(_profile_scope)
            _query = _extract_last_user_text(messages)
            _core_block, _detail_block = render_profile_tiers(
                _profile_body, _profile_scope, query=_query)
        except Exception as e:
            logger.warning('[Inject] conv=%s user-profile load failed: %s',
                           (_cid or '?')[:8], e)
            _profile_body, _core_block, _detail_block = '', None, None
        _profile_injected = False
        if _core_block:
            _profile_injected = _append_user_profile_block(
                messages, _core_block, marker=_PROFILE_MARKER)
        # Detail tier rides the TRUE tail (the LAST user message) with its OWN
        # marker — the SAME cache-safe seam <relevant_memories> uses — NOT the
        # _isMeta carrier the core rides. The carrier is at index 1 (CLAUDE.md)
        # and sits INSIDE the cached prefix; the detail selection changes per
        # turn, so placing it on the carrier would rewrite the cached prefix
        # every turn (bug B4). It is RELEVANCE-GATED PER TURN, so within a task
        # (endpoint re-entry on the same last user message) it must be
        # REFRESHED, not append-once: _refresh_detail_block strips any stale
        # detail block on that message and re-appends this turn's selection (or
        # removes it entirely when this turn has no relevant detail). A prior
        # turn's frozen detail (on a now-historical user message) is left
        # untouched — same tradeoff <relevant_memories> makes.
        _detail_action = _refresh_detail_block(
            messages, _detail_block, marker=_PROFILE_DETAIL_MARKER)
        _detail_injected = _detail_action in ('replaced', 'added', 'removed')
        # ── Chip stash: fire whenever the profile is IN CONTEXT this turn,
        #   not only when THIS call mutated the message list. _append_*
        #   returns False when the marker is already present — which happens
        #   on every turn after the first when the _isMeta carrier is reused
        #   from the server-side message store (keepToolHistory) or rebuilt
        #   history. The preferences ARE in context on those turns (carried
        #   over), so the "preferences applied" chip must still appear — gating
        #   it on a fresh mutation is exactly what made the chip flicker in/out
        #   between turns of the same conversation. So: stash the chip payload
        #   whenever there's a non-empty profile body; only the cache
        #   notify_compaction + the INFO log are gated on an actual mutation.
        if _core_block and task is not None:
            try:
                from lib.memory.user_profile import applied_profile_items
                _applied = applied_profile_items(
                    _profile_body, _profile_scope, query=_query)
                # The chip shows EXACTLY what was injected: the full
                # always-on core + only the relevance-selected detail
                # bullets — never an arbitrary first-N slice. `items` is
                # the flat union (back-compat for the existing chip);
                # `core`/`detail` let the UI group them.
                task['_appliedPreferences'] = {
                    'chars': len(_profile_body),
                    'items': _applied['core'] + _applied['detail'],
                    'core': _applied['core'],
                    'detail': _applied['detail'],
                    'detail_injected': bool(_detail_block),
                }
            except Exception as e:
                logger.debug('[Inject] profile summary stash failed: %s', e)
        if _profile_injected or _detail_injected:
            _existing = _system_text(messages)
            # CRITICAL: we mutated a message that, after the first tool
            # round, sits INSIDE the cached prefix (messages[0:N-2]).
            # Signal a HISTORY REWRITE (not a compaction): this NAMES the
            # cause so detect_cache_break does NOT emit the anonymous false
            # `PREFIX MUTATION DETECTED` alarm — but, unlike notify_compaction,
            # it does NOT blanket-suppress break detection. A profile splice is
            # a genuine prefix mutation that RE-BILLS the whole body uncached;
            # notify_compaction would launder that cost into a false
            # "server-side — PROVEN" verdict and hide it from the metrics.
            # notify_history_rewrite keeps the wire diff live so the re-bill is
            # still detected and attributed.
            # See .tofu/skills/cache-tracking-prefix-mutation-mutators.md.
            if _cid:
                try:
                    from lib.tasks_pkg.cache_tracking import notify_history_rewrite
                    notify_history_rewrite(_cid)
                except Exception as e:
                    logger.debug('[Inject] notify_history_rewrite unavailable: %s', e)
            logger.info('[Inject] conv=%s user-profile applied '
                        '(%d chars, core=%s on _isMeta carrier, detail=%s on '
                        'true tail)',
                        (_cid or '?')[:8], len(_profile_body),
                        _profile_injected, _detail_injected)
        # Record the bytes that ACTUALLY landed: core on the _isMeta carrier,
        # detail on the true tail. They are two distinct seams with their own
        # real lengths (the summary total must sum what was spliced). 'removed'
        # subtracts a stale block (endpoint re-entry) → not a fresh-assembly
        # inject, so it is not counted as added bytes.
        if _profile_injected and _core_block:
            _ctx_injected('pref_core', len(_core_block))
        if _detail_action in ('added', 'replaced') and _detail_block:
            _ctx_injected('pref_detail', len(_detail_block))

    # ★ 3. Compact memory accumulation instructions + memory count hint
    #   Both the HOW-TO-USE instructions and the dynamic count hint
    #   ("You have N accumulated memories...") go into the system message.
    #   Suppressed when the user disables Memory — keeps the toggle's
    #   "off → no proactive memory plumbing" semantics consistent with the
    #   per-turn prefetch gate in orchestrator.py.
    if not (has_real_tools and memory_enabled):
        _ctx_suppressed('memory_accum',
                        'no_tools' if not has_real_tools else 'memory_disabled')
    if has_real_tools and memory_enabled:
        if '<memory_accumulation>' in _existing:
            logger.debug('[Inject] Memory instructions already present, skipping '
                         'append (conv=%s)', _cid[:8] if _cid else '?')
            _ctx_suppressed('memory_accum', 'marker_present')
        else:
            from lib.memory import MEMORY_ACCUMULATION_INSTRUCTIONS_COMPACT

            # Build memory count hint (dynamic, changes on CRUD). Cheap
            # in-process count — no conv-scoped cache (each task injects fresh).
            _pp = project_path if project_enabled else None
            def _load_memory_hint():
                from lib.memory import build_memory_context
                return build_memory_context(project_path=_pp)

            _mem_hint = _frozen_head.get('mem_hint')
            if _mem_hint is None:
                _mem_hint = _load_memory_hint() or ''
                if _cid:
                    try:
                        _ctx_freeze.store_head(
                            _cid, project_path if project_enabled else '',
                            mem_hint=_mem_hint)
                    except Exception as _fse:
                        logger.debug('[CtxFreeze] mem-hint store failed '
                                     'conv=%s: %s', (_cid or '?')[:8], _fse)
            else:
                logger.debug('[CtxFreeze] conv=%s memory-count hint reused '
                             'FROZEN (%d chars)', (_cid or '?')[:8],
                             len(_mem_hint or ''))

            _mem_block = MEMORY_ACCUMULATION_INSTRUCTIONS_COMPACT
            if _mem_hint:
                _mem_block = _mem_hint + '\n\n' + _mem_block

            # Separate cache-block: the memory count in _mem_hint changes
            # whenever memories are CRUD'd, so we want its BP independent
            # from the static CC block's BP.
            _mem_spliced = _wrap_system_reminder(_mem_block)
            _append_to_system_message(
                messages,
                _mem_spliced,
                as_separate_block=True)
            _existing = _system_text(messages)
            # Full spliced length (hint + instructions, <system-reminder>
            # wrapper included) — that is the byte delta this seam adds.
            _ctx_injected('memory_accum', len(_mem_spliced))

    # ★ 3.5. Available-skills index — the always-visible skills channel.
    #   Gated on has_real_tools ONLY: skills are USER-installed capability
    #   packs, a different noun from memories, so they do NOT follow the
    #   memory toggle. The block is byte-stable for a fixed install set
    #   (sorted by id) and splices as its OWN cache block, same rule as
    #   the memory count hint.
    if has_real_tools:
        # The idempotency marker MUST be the listing's closing tag, not the
        # bare noun: the static memory_accumulation prose itself mentions
        # `<available_skills>` (backticked, no close tag), so checking the
        # noun suppressed the skills index on EVERY memory-enabled turn —
        # installed skills were never advertised. A real listing (from a
        # prior assembly of the same messages) always carries the close tag.
        if '</available_skills>' in _existing:
            _ctx_suppressed('skills_index', 'marker_present')
        else:
            from lib.skills import build_skills_index
            _skills_block = build_skills_index(
                project_path=project_path if project_enabled else None) or ''
            if _skills_block:
                _skills_spliced = _wrap_system_reminder(_skills_block)
                _append_to_system_message(
                    messages,
                    _skills_spliced,
                    as_separate_block=True)
                _existing = _system_text(messages)
                _ctx_injected('skills_index', len(_skills_spliced))
            else:
                _ctx_suppressed('skills_index', 'none_installed')
    else:
        _ctx_suppressed('skills_index', 'no_tools')

    # ★ 3.6. Credential-vault index — the model's discovery seam for
    #   operator credentials (NAMES + env vars ONLY, never values; the
    #   values ride the run_command child env, see
    #   lib.credentials_vault.exec_env_overlay). Gated on has_real_tools:
    #   without run_command there is nothing to reference them with.
    #   Byte-stable for a fixed entry set (sorted by name), own cache
    #   block — same rule as the skills index.
    if has_real_tools:
        if '</credential_vault>' in _existing:
            _ctx_suppressed('vault_index', 'marker_present')
        else:
            from lib.credentials_vault import build_vault_index
            _vault_block = build_vault_index() or ''
            if _vault_block:
                _vault_spliced = _wrap_system_reminder(_vault_block)
                _append_to_system_message(
                    messages,
                    _vault_spliced,
                    as_separate_block=True)
                _existing = _system_text(messages)
                _ctx_injected('vault_index', len(_vault_spliced))
            else:
                _ctx_suppressed('vault_index', 'empty_vault')
    else:
        _ctx_suppressed('vault_index', 'no_tools')

    # ★ 4. Swarm system prompt injection — gated ONLY on swarm_enabled.
    #   Decoupled from project_enabled because a bare-conversation research
    #   swarm is a valid use case (mirrors the read_files decoupling done
    #   on 2026-04-20: see read-files-tool-always-on-decoupled memory).
    if swarm_enabled and '<parallel_execution>' not in _existing:
        from lib.swarm.registry import format_role_catalogue
        swarm_prompt = f"""
<parallel_execution>
You have an **async parallel execution** system: `spawn_agents`, `await_agents`, `get_agent_result`. **Read this carefully — these are first-class tools, not advanced extras.**

## When you SHOULD reach for spawn_agents

These cases come up *constantly* and are exactly the situation spawn_agents is for:

- The user's question naturally splits into 2+ independent investigations (e.g. "is this branch ready to ship?" → git audit + test audit + flag audit).
- You're about to do multiple unrelated greps / file reads / web searches that do not feed into each other.
- A research task spans multiple URLs / docs / repos that are independent.
- A refactor or audit touches multiple unrelated subsystems.
- You want a "second opinion" on code or design — spawn a `reviewer` so it doesn't see your analysis.
- You're about to import a lot of low-value tool output (huge greps, big files) into your own context — fork that work so only the conclusion comes back.

## When NOT to spawn

- Trivial single-step questions (one tool call would do it).
- Tasks that are inherently sequential — each step needs the previous answer.
- Reading a single file you already know the path of.
- A grep you can do in one call.

## Available roles

When you spawn, set `role` to one of these (default `general`):

{format_role_catalogue()}

## Mechanics

- `spawn_agents` is **fire-and-forget**: returns a handle immediately, your turn ends. Sub-agent results land on later turns as `<swarm-update>` user messages.
- To run N agents in parallel, send a **single `spawn_agents` call with N entries in `agents`**. Do NOT issue several spawn_agents calls in a row — that just queues them up serially in the protocol and defeats the parallelism.
- **Never poll**, never sleep, never call back to "check status". Updates land automatically.
- **Never fabricate** sub-agent results before their `<swarm-update>` arrives. If the user asks mid-wait, give status, not a guess: "the audit is still running, should land shortly."
- **Never read `output_file`** unless the user explicitly asks for a progress check. Trust the notification.
- If you genuinely have nothing else useful to do while waiting, call `await_agents(mode='any')` to block on the next completion (capped at 120 s). If you have other work or the user is talking to you, prefer doing that over awaiting.
- `await_agents` returns `{{completed:[...], still_running:[...], timed_out, note}}`. When `timed_out` is true it did NOT fail — the cap elapsed before your `mode` condition was met; `note` tells you how many finished and which are still running (they keep running in the background). Agents listed in `completed` are handed to you right here, so you will NOT get a duplicate `<swarm-update>` for them later.
- If a `<preview>` was too short, call `get_agent_result(id)` for the full body. This also consumes that agent's pending `<swarm-update>`, so you won't see it twice.
- Sub-agents cannot spawn further sub-agents and cannot ask the user. Don't write objectives that assume they can.

## Writing the objective

Treat the sub-agent like a smart colleague who just walked in — it has none of your conversation context. Brief it with:

- What you're trying to accomplish AND why.
- The context it needs to do the job (file paths, URLs, constraints, prior findings).
- The output format you expect ("report a punch list, under 200 words").

Vague terse prompts produce shallow generic work. **Don't write "based on your findings, fix the bug"** — that pushes synthesis onto the sub-agent. You synthesise in your own next turn.

## `<swarm-update>` shape

When a sub-agent finishes you'll see something like this prepended to your next turn:

```
<swarm-update>
  <agent-id>a1</agent-id>
  <role>researcher</role>
  <status>completed</status>
  <elapsed-seconds>12.4</elapsed-seconds>
  <preview>...up to 200 chars of the final answer...</preview>
  <output-file>data/swarm/&lt;task_id&gt;/a1.log</output-file>
  <remaining running="2" pending="0"/>
</swarm-update>
```

## Worked example

User: "Is this branch ready to ship?"

Your round N (a single tool call, three parallel agents):

```
spawn_agents({{agents:[
  {{"objective":"Audit git status: uncommitted, commits ahead of main",       "role":"coder"}},
  {{"objective":"Audit tests: coverage of recent changes, all passing",        "role":"coder"}},
  {{"objective":"Check whether new feature flags appear in build_flags.yaml",  "role":"reviewer"}}
]}})
```

Tool result: `{{status:"async_launched", agents:[a1, a2, a3]}}`. You reply: "Three audits running — git status, tests, and feature flags."

(Round ends. You DO NOT predict the findings.)

Round N+1, user (impatient): "tests bit?" — Reply: "Test audit is still running, should land shortly." (NO fabricated answer.)

Round N+2, two `<swarm-update>` blocks arrive (a1 + a3 completed, a2 still running). You now have partial findings; report them honestly and note a2 is still pending. Or, if the user is waiting on you, call `await_agents(mode='any', ids=['a2'])` to block on the last one.

Round N+3, a2's update lands. Synthesise the full picture for the user.
</parallel_execution>
"""
        _swarm_spliced = _wrap_system_reminder(swarm_prompt)
        _append_to_system_message(messages, _swarm_spliced,
                                   as_separate_block=True)
        _ctx_injected('swarm', len(_swarm_spliced))
    elif not swarm_enabled:
        _ctx_suppressed('swarm', 'swarm_disabled')
    else:
        _ctx_suppressed('swarm', 'marker_present')

    # ★ 4.4 Cross-conversation project digest (Layer 2) — always-on in project
    #   mode. A bounded list (top DIGEST_MAX_SIBLINGS) of the most recent OTHER
    #   conversations of this project, each as "title — summary [id]", so the
    #   model is AMBIENTLY aware that siblings exist and can get_conversation()
    #   into one. Read-only/cached (never generates a summary on this hot path);
    #   own cache-block because the sibling list changes as the project grows.
    # Marker is the substring BOTH header variants share (tool-enabled and
    # tool-free), so the idempotency probe matches regardless of which header
    # build_project_digest emitted — see its conv_tools_available docstring.
    _DIGEST_MARKER = 'related conversation(s)'
    if not (project_enabled and project_path):
        _ctx_suppressed('digest', 'project_off')
    elif _DIGEST_MARKER in _existing:
        _ctx_suppressed('digest', 'marker_present')
    if project_enabled and project_path and _DIGEST_MARKER not in _existing:
        # The digest's header advertises list_conversations / get_conversation
        # ONLY when those tools are actually registered this turn. They register
        # only once the user @-attached a conversation (registry._build_conv_ref
        # gates on has_conv_ref), so on a plain project turn we must NOT tell the
        # model to call tools absent from its schema — mirrors the
        # using-tools-section-filters-by-registered-tools guardrail.
        _conv_tools_available = bool(
            tool_names and {'list_conversations', 'get_conversation'} & tool_names)
        _digest_query = _extract_last_user_text(messages)
        try:
            from lib.conversations.project_summary import build_project_digest
            _digest = build_project_digest(
                project_path, current_conv_id=_cid or None,
                conv_tools_available=_conv_tools_available,
                query=_digest_query)
        except Exception as e:
            logger.debug('[Inject] project digest build failed conv=%s: %s',
                         (_cid or '?')[:8], e)
            _digest = ''
        if _digest:
            _digest_spliced = _wrap_system_reminder(_digest)
            # Volatile (re-orders as siblings evolve) → ride the TRUE tail, NOT
            # the system floor. Keeping it in system re-bills the whole body
            # uncached every turn (prefix-cache 29298-pin bug). See
            # _refresh_tail_block.
            _refresh_tail_block(messages, _digest_spliced, _DIGEST_MARKER)
            _ctx_injected('digest', len(_digest_spliced))
        else:
            _ctx_suppressed('digest', 'empty')
            # Stash the SAME siblings (structured) so the frontend can show a
            # "related conversations" provenance segment — making the ambient
            # context the model received auditable, mirroring the prefs chip.
            if task is not None:
                try:
                    from lib.conversations.project_summary import (
                        project_digest_entries)
                    _entries = project_digest_entries(
                        project_path, current_conv_id=_cid or None,
                        query=_digest_query)
                    if _entries:
                        task['_relatedConversations'] = {
                            'count': len(_entries),
                            'items': _entries,
                            'toolsAvailable': _conv_tools_available,
                        }
                except Exception as e:
                    logger.debug('[Inject] related-convs stash failed: %s', e)

    # ★ 4.45 Project Charter (the shared "north star" — Pillar #2).
    #   Its OWN cache-stable block next to the digest: the charter changes far
    #   less often than the sibling list, and it's the mechanism that makes
    #   every conversation of the project share one intent. Injected ONLY when
    #   a charter exists (an empty project adds no prompt weight). Keyed
    #   STRICTLY on the explicit project_path — never the global singleton.
    _CHARTER_MARKER = '[PROJECT CHARTER]'
    if not (project_enabled and project_path):
        _ctx_suppressed('charter', 'project_off')
    elif _CHARTER_MARKER in _existing:
        _ctx_suppressed('charter', 'marker_present')
    if project_enabled and project_path and _CHARTER_MARKER not in _existing:
        try:
            from lib.conversations.project_charter import (
                render_charter_injection_block)
            _charter_block = render_charter_injection_block(project_path)
        except Exception as e:
            logger.debug('[Inject] charter build failed conv=%s: %s',
                         (_cid or '?')[:8], e)
            _charter_block = ''
        if _charter_block:
            _charter_spliced = _wrap_system_reminder(_charter_block)
            # Semi-volatile (grows on a commit) → ride the TRUE tail so a
            # charter change never re-bills the cached system floor.
            _refresh_tail_block(messages, _charter_spliced, _CHARTER_MARKER)
            _ctx_injected('charter', len(_charter_spliced))
        else:
            _ctx_suppressed('charter', 'empty')

    # ★ 4.455 Project GOALS (Status & Focus, Pillar #7) — the human's standing
    #   intent, injected DIRECTLY from the watch lane.
    #
    #   Deliberately its OWN block rather than a copy inside the charter
    #   (owner-directed 2026-07-30: "goals are goals, they should work without
    #   being in the charter"). The morning's design promoted a goal into
    #   charter.content, which meant TWO copies of one sentence and therefore a
    #   diverged state, a replacement preview and a version gate to reconcile
    #   them. One copy needs none of that.
    #
    #   Only kind='goal' + status='open' ships — concerns/questions remain
    #   human-facing-only (see the source-grep guard in
    #   tests/test_project_watch_lane.py). Empty lane ⇒ '' ⇒ zero prompt weight.
    #   Rides the TRUE tail on the SAME cache-safe seam as the charter/board so
    #   editing a goal never re-bills the cached system floor.
    _GOALS_MARKER = '[PROJECT GOALS]'
    if not (project_enabled and project_path):
        _ctx_suppressed('goals', 'project_off')
    elif _GOALS_MARKER in _existing:
        _ctx_suppressed('goals', 'marker_present')
    if project_enabled and project_path and _GOALS_MARKER not in _existing:
        try:
            from lib.conversations.project_watch import (
                render_goals_injection_block)
            _goals_block = render_goals_injection_block(project_path)
        except Exception as e:
            logger.debug('[Inject] goals build failed conv=%s: %s',
                         (_cid or '?')[:8], e)
            _goals_block = ''
        if _goals_block:
            _goals_spliced = _wrap_system_reminder(_goals_block)
            _refresh_tail_block(messages, _goals_spliced, _GOALS_MARKER)
            _ctx_injected('goals', len(_goals_spliced))
        else:
            _ctx_suppressed('goals', 'empty')

    # ★ 4.46 Project Board (auto-coordination — Pillar #3).
    #   The mechanism that makes conversations STOP colliding/duplicating: the
    #   board's open/claimed/done epics injected as their own cache block, with
    #   an explicit "avoid duplicating — X is being advanced by conversation …"
    #   hint per epic another conversation holds an UNEXPIRED lease on. NOT a
    #   passive display: this is what a reading conversation acts on to step
    #   aside. Injected only when the board is non-empty; keyed STRICTLY on the
    #   explicit project_path. render_board_injection_block evaluates lease
    #   expiry at read time (an abandoned claim reads open → never deadlocks).
    _BOARD_MARKER = '[PROJECT BOARD]'
    if not (project_enabled and project_path):
        _ctx_suppressed('board', 'project_off')
    elif _BOARD_MARKER in _existing:
        _ctx_suppressed('board', 'marker_present')
    if project_enabled and project_path and _BOARD_MARKER not in _existing:
        try:
            from lib.conversations.project_board import render_board_injection_block
            _board_block = render_board_injection_block(project_path, current_conv_id=_cid or '')
        except Exception as e:
            logger.debug('[Inject] board build failed conv=%s: %s',
                         (_cid or '?')[:8], e)
            _board_block = ''
        if _board_block:
            _board_spliced = _wrap_system_reminder(_board_block)
            # Volatile (epics move every turn) → ride the TRUE tail, never the
            # cached system floor.
            _refresh_tail_block(messages, _board_spliced, _BOARD_MARKER)
            _ctx_injected('board', len(_board_spliced))
        else:
            _ctx_suppressed('board', 'empty')

    # ★ 4.47 Peer messaging protocol (Pillar #6 — agent-to-agent register).
    #   Ambient guidance so project_message / project_intervene are composed as
    #   coordination acts TO ANOTHER AGENT (claim / boundary / hand-off /
    #   overlap-warning) instead of status reports to a human. Without it the
    #   ONLY steer was the tool description → the model defaulted to report-to-
    #   human prose (the reported symptom). Cache-stable fixed protocol (no
    #   per-turn state). Injected only in project mode; keyed on project_path.
    _PEER_MARKER = '[PEER MESSAGING PROTOCOL]'
    if not (project_enabled and project_path):
        _ctx_suppressed('peer_protocol', 'project_off')
    elif _PEER_MARKER in _existing:
        _ctx_suppressed('peer_protocol', 'marker_present')
    if project_enabled and project_path and _PEER_MARKER not in _existing:
        try:
            from lib.conversations.project_peer import render_peer_protocol_block
            _peer_block = render_peer_protocol_block(project_path)
        except Exception as e:
            logger.debug('[Inject] peer-protocol build failed conv=%s: %s',
                         (_cid or '?')[:8], e)
            _peer_block = ''
        if _peer_block:
            _peer_spliced = _wrap_system_reminder(_peer_block)
            _append_to_system_message(messages, _peer_spliced,
                                       as_separate_block=True)
            _existing = _system_text(messages)
            _ctx_injected('peer_protocol', len(_peer_spliced))
        else:
            _ctx_suppressed('peer_protocol', 'empty')

    # ★ 4.5 Current date → the TRUE tail, never the cached system floor.
    #   The date changes once per UTC day. Previously it was inlined in the
    #   static system block (append mode) or appended as its own system block
    #   (replace mode) — either way it sat INSIDE the cached prefix, so the
    #   daily rollover rewrote a cached block and re-billed the whole body
    #   uncached at the UTC boundary (Anthropic names this exact anti-pattern:
    #   "don't inject timestamps into the cached prompt"). Riding the true tail
    #   — the SAME cache-safe seam the digest / charter / board use — keeps the
    #   system prefix byte-stable across the day boundary and confines the date
    #   bytes to the already-volatile 5m tail (which is re-billed every round
    #   regardless, so the date rides for free). build_static_prompt() is now
    #   always called with include_date=False, so this is the sole date source
    #   in both append and replace modes.
    _DATE_MARKER = 'Current date:'
    _date_spliced = _wrap_system_reminder(system_prompt_cc.section_current_date())
    _refresh_tail_block(messages, _date_spliced, _DATE_MARKER)
    _ctx_injected('date', len(_date_spliced))

    # ── Record the last user message's final content for the NEXT turn's
    #    restore (see the entry hook). Runs AFTER every tail mutation in this
    #    assembly so the snapshot is the exact wire form.
    if _cid and _tail_entry_content is not None:
        try:
            _ctx_freeze.record_tail_block(_cid, _tail_entry_content, messages)
        except Exception as _fre:
            logger.debug('[CtxFreeze] tail record failed conv=%s: %s',
                         (_cid or '?')[:8], _fre)

    # ── Per-assembly trace: ONE INFO line naming every block spliced this
    #   assembly + the total bytes. _inject_system_contexts runs ONCE per task
    #   (round 0), so this is a per-assembly summary, NOT a per-round line —
    #   `round` reflects the tool-round count at assembly (0 on a fresh task).
    #   The model sees these same system blocks on every round of the task.
    _emit_context_summary()


def _extract_last_user_text(messages: list) -> str:
    """Extract the text content of the last user message.

    Used by memory-prefetch to build a BM25 query from the conversational
    surface of the last turn.
    """
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get('role') == 'user':
            content = messages[i].get('content', '')
            if isinstance(content, str):
                return content
            elif isinstance(content, list):
                parts = []
                for block in content:
                    if isinstance(block, dict) and block.get('type') == 'text':
                        parts.append(block.get('text', ''))
                return ' '.join(parts)
    return ''
