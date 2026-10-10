"""Declared route context limits for compaction and selected-slot admission.

Read initialized slots or scoped compiled native metadata; no second registry.
Pins and explicit owner identity bound pre-dispatch candidate reads. Exact-slot
admission remains authoritative after fallback. No retained cache or lifecycle.
"""

def slot_context_window(slot) -> int:
    """Offering restrictions win; learned shrink is scoped to the wire route."""
    declared = int(getattr(slot, 'context_window', 0) or 0)
    if declared <= 0:
        return 0
    from lib.context_limits import resolve_learned_context_limit
    provider = getattr(slot, 'routing_provider_id', '') or slot.provider_id
    # Successful probes cannot expand a configured provider restriction.
    return min(declared, resolve_learned_context_limit(
        provider, slot.model, declared))


def pinned_context_profile(model, *, provider_id='', owner_user_id=None, pin=''):
    """Conservative intersection of matching authorized candidates, no I/O.

    With no owner or execution pin, use model knowledge rather than inspecting
    another owner's slots. Missing declarations keep the knowledge fallback.
    """
    from lib.llm_dispatch.provider_pin import get_pinned_provider
    pin = pin or get_pinned_provider() or ''
    if not model or (not pin and not owner_user_id):
        return None
    from lib.llm_dispatch.factory import get_dispatcher
    candidates = []
    exact = True
    for slot in list(get_dispatcher().slots):
        if pin and slot.provider_id != pin:
            continue
        if owner_user_id and getattr(slot, 'routing_owner_user_id', 0) != owner_user_id:
            continue
        if not pin and getattr(slot, 'request_scoped', False):
            continue
        if provider_id and not pin and provider_id not in (
                slot.provider_id, getattr(slot, 'routing_provider_id', '')):
            continue
        if model not in (slot.model, slot.logical_model):
            continue
        window = slot_context_window(slot)
        if not window:
            from lib.model_info import resolved_context_profile
            fallback = resolved_context_profile(
                slot.model, getattr(slot, 'routing_provider_id', '') or slot.provider_id)
            window = fallback.get('window')
            exact = exact and bool(fallback.get('exact'))
        if not window:
            return None
        candidates.append(int(window))
    if not candidates:
        return None
    return {'window': min(candidates), 'source': 'model_routing_v2', 'exact': exact}


def task_context_profile(task):
    """Resolve the configured logical model within this task's route scope."""
    task = task or {}
    from .native_context_scope import native_task_context_profile
    native = native_task_context_profile(task)
    if native is not None:
        return native
    config = task.get('config') or {}
    return pinned_context_profile(
        config.get('model') or task.get('model') or '',
        provider_id=task.get('provider_id') or config.get('providerId') or '',
        owner_user_id=task.get('_userId'), pin=task.get('_pinned_provider_id') or '')
