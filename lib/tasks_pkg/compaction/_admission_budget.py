"""Shared rendered-prompt ceiling for proactive recovery and final dispatch."""

from lib.tasks_pkg.compaction._tokens import _compaction_trigger_threshold, _get_context_limit


def resolve_admission_budget(task, *, round_num=0):
    """Return route-derived hard safety ceiling, distinct from economic targets."""
    from lib.llm_dispatch.context_policy import task_context_profile
    routed = task_context_profile(task)
    window = int(routed['window']) if routed else _get_context_limit(task)
    effective, window_threshold, working_set = _compaction_trigger_threshold(
        task, context_limit=window)
    # A rejected economic rewrite must not be forced again at dispatch.
    # Working-set targets belong to the ROI gate; only physical safety
    # requires a complete summary (or a refused dispatch) here.
    ceiling = max(1, window_threshold)

    # Honor an explicit output reservation even on a small-window route.
    # The shared usable-context floor is only a fallback, not permission to
    # dispatch input + requested output beyond physical capacity.
    requested_output = (routed.get('output_tokens') if routed and 'output_tokens' in routed
                        else ((task or {}).get('config') or {}).get('maxTokens'))
    if (isinstance(requested_output, int)
            and not isinstance(requested_output, bool) and requested_output > 0):
        ceiling = min(ceiling, max(1, window - requested_output))
    if task is not None:
        task['_contextBudgetDecision'] = {
            'contextWindowTokens': window,
            'contextWindowSource': routed['source'] if routed else 'model_knowledge_or_learned',
            'economicTargetTokens': min(effective, working_set) if working_set > 0 else 0,
            'windowSafetyTokens': window_threshold,
            'hardCeilingTokens': ceiling,
        }
    # Economic and first-dispatch targets are not physical route limits.
    # Rendered-prompt measurement and window reserves apply to every round.
    return ceiling, ceiling
