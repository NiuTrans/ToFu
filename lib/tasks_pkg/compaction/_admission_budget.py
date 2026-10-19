"""Shared rendered-prompt ceiling for proactive recovery and final dispatch.

Ordinary maxTokens is a cap, not a bound reservation. Native output_tokens is
already bound. Completion policy remains in the canonical body clamp.
"""

from lib.tasks_pkg.compaction._tokens import _compaction_trigger_threshold, _get_context_limit


def resolve_admission_budget(task, *, round_num=0, input_tokens=None):
    """Return physical safety limits, independent of economic rewrite targets.

    input_tokens must be the current full rendered prompt (including schemas),
    never a previous task receipt. Without a valid count, plan at the safety
    boundary; body construction and selected-slot recount still clamp again.
    """
    from lib.llm_dispatch.context_policy import task_context_profile
    from lib.llm.body._clamp import _clamp_completion_to_context_window
    from lib.model_info import _clamp_max_tokens
    from lib.token_counter.evidence import validated_admitted_input_tokens

    routed = task_context_profile(task)
    window = int(routed['window']) if routed else _get_context_limit(task)
    effective, window_threshold, working_set = _compaction_trigger_threshold(
        task, context_limit=window)
    ceiling = max(1, window_threshold)
    config = (task or {}).get('config') or {}
    requested_output = config.get('maxTokens')
    measured_input = validated_admitted_input_tokens(input_tokens)
    if routed and 'output_tokens' in routed:
        # Issued native output is owned by native accounting/route preparation.
        # Never shrink it to make a Python prompt appear admissible.
        output = routed['output_tokens']
        if type(output) is not int or output <= 0:
            raise ValueError('native output reservation must be a positive integer')
        policy = 'native_bound_reservation'

        cap = output
        output_source = 'native_scope'
    else:
        model = config.get('model') or (task or {}).get('model') or ''
        # Missing/invalid ordinary config retains the shared model fallback.
        cap = _clamp_max_tokens(
            model, requested_output if type(requested_output) is int else None)
        if routed and routed.get('output_cap_tokens'):
            cap = min(cap, routed['output_cap_tokens'])
        output = _clamp_completion_to_context_window(
            model, None, cap, context_window=window,
            precomputed_input_tokens=(measured_input if measured_input is not None
                                      else ceiling))
        policy = 'input_aware_output_cap'

        output_source = ('task_config' if type(requested_output) is int
                         and requested_output > 0 else 'model_default')
    ceiling = min(ceiling, max(1, window - output))
    if task is not None:
        task['_contextBudgetDecision'] = {
            'contextWindowTokens': window,
            'contextWindowSource': routed['source'] if routed else 'model_knowledge_or_learned',
            'economicTargetTokens': min(effective, working_set) if working_set > 0 else 0,
            'windowSafetyTokens': window_threshold,
            'hardCeilingTokens': ceiling,
            'outputReservationPolicy': policy,

            'requestedOutputTokens': (requested_output
                if type(requested_output) is int and 0 < requested_output <= 2**53 - 1
                else None),
            'outputCapTokens': cap,
            'outputResolutionSource': output_source,
            'resolvedOutputTokens': output,
            'outputResolutionInputTokens': (measured_input if measured_input is not None
                                           else max(1, window_threshold)),
            'outputResolutionInputSource': ('current_prompt' if measured_input is not None
                                           else 'safety_boundary'),
        }
    return ceiling, ceiling
