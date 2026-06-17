"""Pipeline entry point + post-compact context re-injection.

Public surface:
  * ``run_compaction_pipeline``             — called from the orchestrator
    before each LLM API call.
  * ``_reinject_system_contexts_after_compact`` — re-injects system contexts
    after L2 compaction drops the system message.
"""

from lib.log import get_logger
from lib.tasks_pkg.compaction._layer1 import micro_compact
from lib.tasks_pkg.compaction._layer2 import force_compact_if_needed

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
#  Post-compact context re-injection
#  Inspired by Claude Code: after compaction replaces old messages, the system
#  context (project context, memory, swarm prompt) is re-injected to ensure
#  the model doesn't lose critical instructions.
# ═══════════════════════════════════════════════════════════════════════════════

def _reinject_system_contexts_after_compact(messages: list, task: dict | None = None):
    """Re-inject system contexts after compaction.

    After force_compact replaces old messages, the system message may have
    been rebuilt from only the archived system messages.  This ensures
    project context, memory, and swarm prompts are still present.

    Only runs if the task has the necessary config to re-inject.
    """
    if not task:
        return

    cfg = task.get('config', {})
    project_path = cfg.get('projectPath', '')
    project_enabled = bool(project_path)
    memory_enabled = cfg.get('memoryEnabled', True)
    search_enabled = cfg.get('searchMode', '') in ('single', 'multi')
    swarm_enabled = cfg.get('swarmEnabled', False)

    # Check if system contexts are already present (avoid double-injection)
    if messages and messages[0].get('role') == 'system':
        sys_content = messages[0].get('content', '')
        if isinstance(sys_content, list):
            sys_text = ''.join(
                b.get('text', '') for b in sys_content
                if isinstance(b, dict) and b.get('type') == 'text'
            )
        else:
            sys_text = sys_content or ''

        # The CC static block lives IN the system message; CLAUDE.md lives
        # in a separate user _isMeta msg. Use the static-block marker as
        # the trigger — if it's gone, compaction stripped the system msg
        # and we need to rebuild everything.  (Do NOT use
        # '[PROJECT CO-PILOT MODE]' here — that string is in the user
        # _isMeta msg, not in sys_text, so it would fire every compaction.)
        from lib.tasks_pkg.system_context import _CC_STATIC_MARKER
        if _CC_STATIC_MARKER not in sys_text:
            from lib.tasks_pkg.system_context import _inject_system_contexts
            # Re-inject from scratch — the system_context module handles dedup
            _inject_system_contexts(
                messages, project_path, project_enabled,
                memory_enabled, search_enabled, swarm_enabled,
                has_real_tools=True,
                conv_id=task.get('convId', ''),
                task=task,
                model=cfg.get('model', ''),
                system_prompt_mode=cfg.get('systemPromptMode', 'append'),
            )
            logger.info('[PostCompact] Re-injected system contexts after compaction')


# ═══════════════════════════════════════════════════════════════════════════════
#  Pipeline entry point
# ═══════════════════════════════════════════════════════════════════════════════

def run_compaction_pipeline(messages: list, current_round: int,
                            task: dict | None = None):
    """Run the compaction pipeline.

    Called from the orchestrator before each LLM API call.

    Layer 0 (budget_tool_result):
        Applied at tool-result entry time (in tool_dispatch.py).
        Truncates oversized results immediately.  Zero LLM cost.

    Layer 1 (micro_compact):
        Archives and compacts cold tool results every round.
        Also strips old thinking/reasoning_content.
        Zero LLM cost.  Runs unconditionally.

    Force compact (force_compact_if_needed):
        Fires only when estimated tokens approach the context limit.
        Injects a context_compact tool_call/result pair.
        After compaction, re-injects system contexts if needed.

    Layer 3 (reactive_compact):
        Emergency compaction — called from orchestrator on API 400
        prompt_too_long errors.  Not called here (called from
        llm_fallback.py on error).
    """
    conv_id = task.get('convId', '') if task else ''

    logger.debug('[Pipeline] round=%d  conv=%s  messages=%d',
                 current_round, conv_id[:8] if conv_id else '?',
                 len(messages))

    # ── PreCompact hooks (Claude Agent SDK parity) ──
    # Fire BEFORE any compaction layer touches the messages.  Hooks may
    # snapshot / archive the full transcript; they MUST treat messages as
    # read-only.
    if task is not None:
        try:
            from lib.tasks_pkg.tool_hooks import run_pre_compact_hooks
            run_pre_compact_hooks(messages, task)
        except Exception as e:
            logger.debug('[Pipeline] PreCompact hooks failed: %s', e)

    # Layer 1: compact cold tool results + strip old thinking.
    # An optional ``task['config']['compaction']`` dict selects a
    # non-default strategy WITHOUT mutating any global state — so A/B
    # experiment arms can run concurrently (see compaction step registry).
    # Recognised keys: ``steps`` (explicit ordered step-name list),
    # ``ignore_cache_prefix`` (aggressive arm), ``constant_overrides``
    # (per-call tunable overlay), ``enable_paired_assistant_compact`` /
    # ``enable_assistant_compact`` (gated builtins).  Absent ⇒ defaults
    # ⇒ byte-identical to shipped behavior.
    # ── Experiment isolation flags (REPLACEMENT-mode arms) ──
    # An external-method arm (OpenCode/Hermes/OpenClaw/no-compaction) must
    # run ONLY its own compaction, NOT chatui's default L1+L2 underneath —
    # otherwise it's a confounded 'chatui + method' hybrid. These two
    # flags let an arm opt out of the built-in layers:
    #   disableDefaultL1   → skip the unconditional micro_compact default pass
    #   disableForceCompact → skip chatui's L2 smart-summary force-compact
    # Absent ⇒ both run (this IS the chatui 'tofu'/baseline arm). The
    # arm's OWN steps/advanced_steps still run regardless of these flags.
    _disable_l1 = False
    _disable_force = False
    _l1_kwargs = {}
    if task:
        _comp_cfg = (task.get('config') or {}).get('compaction')
        if isinstance(_comp_cfg, dict):
            _disable_l1 = bool(_comp_cfg.get('disableDefaultL1', False))
            _disable_force = bool(_comp_cfg.get('disableForceCompact', False))
            for _k in ('steps', 'ignore_cache_prefix', 'constant_overrides',
                       'enable_paired_assistant_compact',
                       'enable_assistant_compact'):
                if _k in _comp_cfg:
                    _l1_kwargs[_k] = _comp_cfg[_k]
            if _l1_kwargs or _disable_l1 or _disable_force:
                logger.info('[Pipeline] conv=%s  compaction override: %s '
                            'disableL1=%s disableForce=%s',
                            conv_id[:8] if conv_id else '?',
                            sorted(_l1_kwargs), _disable_l1, _disable_force)
    # L1 runs unless explicitly disabled. When an arm supplies its own
    # ``steps`` we still go through micro_compact (it routes those steps);
    # disableDefaultL1 is for arms that want NO L1 at all (no-compaction).
    if _disable_l1 and 'steps' not in _l1_kwargs:
        saved = 0
    else:
        saved = micro_compact(messages, conv_id=conv_id, task=task, **_l1_kwargs)

    if saved > 0:
        logger.debug('[Pipeline] L1 saved ~%d tokens, now %d messages',
                     saved, len(messages))

    # Force compact if context near capacity (chatui L2) — unless the arm
    # opted out to run its own summarizer as the sole context manager.
    compacted = False if _disable_force else force_compact_if_needed(messages, task=task)

    # Post-compact: re-inject system contexts if compaction dropped them
    if compacted:
        _reinject_system_contexts_after_compact(messages, task=task)

    # Stage B — advanced host: structural / LLM-allowed compaction methods.
    # Opt-in via task['config']['compaction']['advanced_steps'] (default
    # off ⇒ shipped behavior unchanged). Runs on the api-form messages
    # like L2, recomputed each round, so no durable-placeholder work here.
    adv_saved = 0
    if task:
        _comp_cfg = (task.get('config') or {}).get('compaction')
        if isinstance(_comp_cfg, dict):
            _adv_steps = _comp_cfg.get('advanced_steps')
            if isinstance(_adv_steps, list) and _adv_steps:
                try:
                    from lib.tasks_pkg.compaction._advanced import advanced_compact
                    adv_saved = advanced_compact(
                        messages, conv_id=conv_id, task=task,
                        advanced_steps=_adv_steps,
                        constant_overrides=_comp_cfg.get('constant_overrides'),
                        ignore_cache_prefix=bool(
                            _comp_cfg.get('ignore_cache_prefix', False)),
                    )
                except Exception as e:
                    logger.error('[Pipeline] advanced_compact failed: %s',
                                 e, exc_info=True)

    # Notify cache tracker that compaction occurred so the expected
    # cache_read token drop isn't flagged as a cache break.
    if (saved > 0 or compacted or adv_saved > 0) and conv_id:
        try:
            from lib.tasks_pkg.cache_tracking import notify_compaction
            notify_compaction(conv_id)
        except Exception as e:
            logger.debug('[Pipeline] notify_compaction failed: %s', e)
