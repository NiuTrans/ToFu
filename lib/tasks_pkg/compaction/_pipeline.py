"""Pipeline entry point + post-compact context re-injection.

Public surface:
  * ``run_compaction_pipeline``             — called from the orchestrator
    before each LLM API call.
  * ``recompose_context_after_compaction`` — rebuilds managed tail context
    after L2 compaction changes the request-local working set.
"""

from lib.log import audit_log, get_logger

from lib.tasks_pkg.compaction._ownership import compaction_owner_key
from lib.tasks_pkg.compaction._constants import _CONTEXT_BUDGET_DIAGNOSTIC_RATIO
from lib.tasks_pkg.compaction._layer1 import micro_compact
from lib.tasks_pkg.compaction._layer2 import force_compact_if_needed
from lib.tasks_pkg.compaction._timing import compaction_stage_timing
from lib.tasks_pkg.compaction._tokens import (
    _estimate_total_tokens,
    _get_context_limit,
    _usable_context,
)

logger = get_logger(__name__)

def _emit_compaction_analytics(task: dict, current_round: int,
                               tokens_before: int, msgs_before: int,
                               messages: list, trigger: str) -> int:
    """Post-compaction observability: audit event + PostCompact hooks.

    Codex-inspired (codex-rs ``compact.rs::CompactionAnalyticsAttempt``):
    every successful compaction leaves a structured trail — trigger, round,
    token/message counts before and after — so a production compaction can
    be diagnosed from the logs alone. Hooks receive a read-only summary
    dict, never the message list itself.
    """
    tokens_after = _estimate_total_tokens(messages)
    info = {
        'conv_id': compaction_owner_key(task),
        'round': current_round,
        'layer': 'L2',
        'trigger': trigger,
        'tokens_before': tokens_before,
        'tokens_after': tokens_after,
        'token_count_kind': 'estimated',
        'messages_before': msgs_before,
        'messages_after': len(messages),
        'reduction_pct': round(
            (1 - tokens_after / max(1, tokens_before)) * 100, 1),
    }
    try:
        audit_log('context_compact', **info)
    except Exception as e:
        logger.debug('[Compact] audit_log failed: %s', e)
    if task.get('aborted'):
        return tokens_after
    try:
        from lib.tasks_pkg.tool_hooks import run_post_compact_hooks
        run_post_compact_hooks(info, task)
    except Exception as e:
        logger.warning('[Compact] PostCompact hooks failed: %s', e, exc_info=True)
    return tokens_after


def _maybe_record_context_budget(
    messages: list,
    task: dict | None,
    *,
    used_tokens: int | None = None,
    current_round: int | None = None,
) -> None:
    """Record content-free budget pressure, never model-facing instructions.

    Automatic compaction owns capacity. Emit at most once per context window
    through the existing best-effort audit seam; retain only one task latch.
    Counts describe this working-message estimate, not final wire admission
    or the UI's last provider-reported prompt divided by the full window.
    """
    if not task or task.get('_contextBudgetDiagnosticRecorded'):
        return
    try:
        used = (int(used_tokens) if used_tokens is not None
                else _estimate_total_tokens(messages))
        context_limit = _get_context_limit(task)
        usable = _usable_context(context_limit)
    except Exception as e:
        logger.debug('[TokenBudget] estimate failed: %s', e)
        return
    if usable <= 0:
        return
    remaining = usable - used
    if used < int(usable * _CONTEXT_BUDGET_DIAGNOSTIC_RATIO):
        return
    task['_contextBudgetDiagnosticRecorded'] = True
    from lib.tasks_pkg.context_composer import record_injection_diagnostic
    record_injection_diagnostic(task, 'context_budget',
        reason='automatic_compaction_owns_capacity; estimate=working_messages',
        metrics={
            'contextLimitTokens': context_limit, 'usableTokens': usable,
            'usedTokens': used, 'remainingTokens': max(0, remaining),
            'overBudgetTokens': max(0, -remaining),
            'diagnosticRatio': _CONTEXT_BUDGET_DIAGNOSTIC_RATIO,
        })
    try:
        audit_log(
            'context_budget',
            round=(current_round if isinstance(current_round, int)
                   and not isinstance(current_round, bool)
                   and current_round > 0 else None),
            context_limit_tokens=context_limit,
            usable_tokens=usable,
            used_tokens=used,
            remaining_tokens=max(0, remaining),
            over_budget_tokens=max(0, -remaining),
            remaining_usable_pct=max(0, round(remaining * 100 / usable)),
            diagnostic_ratio=_CONTEXT_BUDGET_DIAGNOSTIC_RATIO,
            token_count_kind='estimated',
            measurement_scope='working_messages',
            capacity_owner='automatic_compaction',
        )
    except Exception as e:
        logger.debug('[TokenBudget] diagnostic audit skipped: %s', e)


# ═══════════════════════════════════════════════════════════════════════════════
#  Post-compact context re-injection
#  After compaction replaces old messages, the system
#  context (project context, memory, swarm prompt) is re-injected to ensure
#  the model doesn't lose critical instructions.
# ═══════════════════════════════════════════════════════════════════════════════

def recompose_context_after_compaction(messages: list, task: dict | None = None):
    """Recompose all managed context after compaction.

    A compactor may retain the static system block while dropping a managed
    head/tail block. Marker probing therefore cannot prove that context is
    complete. The Context Composer is content-addressed and append-only: it
    retains surviving carriers, restores only blocks actually removed by
    compaction, and appends a newer version only when content changed. Run it
    after every successful compaction.

    Only runs if the task has the necessary config to re-inject.
    """
    if not task or task.get('aborted'):
        return

    cfg = task.get('config', {})
    project_path = cfg.get('projectPath', '')
    project_enabled = bool(project_path)
    memory_enabled = cfg.get('memoryEnabled', True)
    search_enabled = cfg.get('searchMode', '') in ('single', 'multi')

    from lib.tasks_pkg.context_composer import (
        compose_task_context, disabled_context_blocks,
    )
    from lib.tasks_pkg.manager import task_user_id
    compose_task_context(
        messages,
        user_id=task_user_id(task),
        project_path=project_path,
        project_enabled=project_enabled,
        memory_enabled=memory_enabled,
        search_enabled=search_enabled,
        has_real_tools=bool(task.get('_contextHasRealTools', True)),
        conv_id=task.get('convId', ''),
        task=task,
        model=cfg.get('model', ''),
        system_prompt_mode=cfg.get('systemPromptMode', 'append'),
        tool_names=set(task.get('_contextToolNames') or ()),
        disabled_blocks=disabled_context_blocks(cfg),
    )
    logger.info('[PostCompact] Re-composed managed context after compaction')


# ═══════════════════════════════════════════════════════════════════════════════
#  Pipeline entry point
# ═══════════════════════════════════════════════════════════════════════════════

def run_compaction_pipeline(
    messages: list,
    current_round: int,
    task: dict | None = None,
    *,
    remaining_api_rounds: int | None = None,
):
    """Run the compaction pipeline.

    Called from the orchestrator before each LLM API call.

    Layer 0 (budget_tool_result):
        Applied at tool-result entry time (in tool_dispatch.py).
        Truncates oversized results immediately.  Zero LLM cost.

    Layer 1 (micro_compact):
        Compacts cold tool results in the request-local working copy.
        Also strips old thinking/reasoning_content from that copy.
        Zero LLM cost.  Runs unconditionally.

    Force compact (force_compact_if_needed):
        Fires only when estimated tokens approach the context limit.
        Installs a typed continuation handoff, not a synthetic tool execution.
        After compaction, re-injects managed tail contexts if needed.

    Layer 3 (reactive_compact):
        Emergency compaction — called from orchestrator on API 400
        prompt_too_long errors.  Not called here (called from
        llm_fallback.py on error).
    """
    # Cancellation is cooperative at stage boundaries. Work already accepted
    # stays historical; never start a later hook/transform/provider read after Stop.
    if task and task.get('aborted'):
        return
    conv_id = compaction_owner_key(task)

    logger.debug('[Pipeline] round=%d  conv=%s  messages=%d',
                 current_round, conv_id[:8] if conv_id else '?',
                 len(messages))

    # ── PreCompact hooks ──
    # Fire BEFORE any compaction layer touches the messages.  Hooks may
    # snapshot / archive the full transcript; they MUST treat messages as
    # read-only.
    if task is not None:
        try:
            from lib.tasks_pkg.tool_hooks import run_pre_compact_hooks
            run_pre_compact_hooks(messages, task)
        except Exception as e:
            logger.warning('[Pipeline] PreCompact hooks failed: %s', e, exc_info=True)

    if task and task.get('aborted'):
        return

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
        with compaction_stage_timing('l1', task, current_round):
            saved = micro_compact(messages, conv_id=conv_id, task=task, **_l1_kwargs)

    if task and task.get('aborted'):
        return

    if saved > 0:
        logger.debug('[Pipeline] L1 saved ~%d tokens, now %d messages',
                     saved, len(messages))

    # Force compact if context near capacity (chatui L2) — unless the arm
    # opted out to run its own summarizer as the sole context manager.
    #
    # ``_allow_head_truncate_fallback=True`` is the deterministic OOM guard:
    # when the L2 summary LLM can't run (no 'cheap' slot / saturated single
    # model / input too big) AND the context is critically over the usable
    # window, force_compact falls through to _head_truncate right here rather
    # than returning False and looping the oversized prompt (the reactive
    # net never fires proactively — the max_tokens clamp prevents the API
    # rejection that would trigger it). Only the PROACTIVE pipeline passes
    # this; reactive_compact keeps its own Phase-4 head-truncate and must NOT
    # double-truncate, so it does not set the flag.
    _l2_measurement: dict = {}
    compacted = False
    if not _disable_force:
        with compaction_stage_timing('l2', task, current_round):
            compacted = force_compact_if_needed(
                messages, task=task, _allow_head_truncate_fallback=True,
                _compaction_round=current_round,
                _compaction_remaining_api_rounds=remaining_api_rounds,
                _measurement_out=_l2_measurement)

    if task and task.get('aborted'):
        return

    # Post-compact: re-inject managed tail contexts if compaction dropped them
    _post_l2_tokens: int | None = None
    if compacted:
        with compaction_stage_timing('recompose', task, current_round):
            recompose_context_after_compaction(messages, task=task)
            if task and task.get('aborted'):
                return
            if task is not None:
                # A fresh context window earns one fresh budget diagnostic.
                task['_contextBudgetDiagnosticRecorded'] = False
                _post_l2_tokens = _emit_compaction_analytics(
                    task, current_round,
                    int(_l2_measurement.get('message_tokens') or 0),
                    int(_l2_measurement.get('message_count') or 0),
                    messages, trigger='auto')

    if task and task.get('aborted'):
        return

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
                    with compaction_stage_timing('advanced', task, current_round):
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

    if task and task.get('aborted'):
        return

    # Experiment-only mutation counter. This observes actual context changes
    # (L1/L2/advanced), not merely summary-model calls, and stays absent for
    # every non-enrolled request so the default execution path is unchanged.
    _assignment = task.get('_costExperiment') if task else None
    if (isinstance(_assignment, dict)
            and _assignment.get('status') == 'assigned'):
        _mutations = int(saved > 0) + int(bool(compacted)) + int(adv_saved > 0)
        if _mutations:
            task['_costExperimentCompactions'] = (
                int(task.get('_costExperimentCompactions') or 0) + _mutations)

    # Notify cache tracker ONLY for mutations that actually touch the cached
    # PREFIX, so the expected cache_read drop isn't flagged as a break.
    #
    # Default L1 (micro_compact, saved>0) is cache-SAFE by construction:
    #   every built-in step gates on ``ctx.is_in_cache_prefix(idx)`` and skips
    #   messages[0:get_cache_prefix_count]. It edits only COLD results that
    #   have NOT yet been cached (or, idempotently, ones already byte-identical
    #   in the prefix). So it does NOT cause a drop and must NOT raise
    #   compaction_pending — doing so blanket-suppresses detect_cache_break on
    #   exactly the transient rounds (cold start, post-eviction, big fan-out)
    #   where a REAL break is most likely, masking it. (See memory
    #   l1-compaction-notify-masks-detection.)
    #
    #   We DO notify for:
    #     * L2 force-compact (``compacted``) — rebuilds/drops prefix messages.
    #     * advanced structural compaction (``adv_saved``) — drops whole turns.
    #     * the aggressive arm (``ignore_cache_prefix``) — L1 then edits INSIDE
    #       the prefix, so a drop is genuinely expected.
    _ignore_prefix = False
    if task:
        _cc = (task.get('config') or {}).get('compaction')
        if isinstance(_cc, dict):
            _ignore_prefix = bool(_cc.get('ignore_cache_prefix', False))
    _touched_prefix = bool(compacted) or adv_saved > 0 or (saved > 0 and _ignore_prefix)
    if _touched_prefix and conv_id:
        try:
            from lib.tasks_pkg.cache_tracking._roi import notify_compaction
            from lib.tasks_pkg.manager import task_user_id
            notify_compaction(conv_id, user_id=task_user_id(task))
        except Exception as e:
            logger.debug('[Pipeline] notify_compaction failed: %s', e)

    # Content-free telemetry: one diagnostic per context window. Runs
    # AFTER every mutation step above so the estimate reflects the final
    # per-round context, and AFTER a successful compaction resets the claim.
    if adv_saved > 0:
        _diagnostic_tokens = None
    elif compacted:
        _diagnostic_tokens = _post_l2_tokens
    else:
        _measured = _l2_measurement.get('message_tokens')
        _diagnostic_tokens = int(_measured) if _measured is not None else None
    _maybe_record_context_budget(
        messages, task, used_tokens=_diagnostic_tokens,
        current_round=current_round)
