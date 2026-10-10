"""Invocation-scoped declared native context, without slots or persisted state.

The application binds compiled route metadata after output clamping. Explicit
owner/task/model identity prevents unrelated work from borrowing that window.
This module grants no credential, provider, tool or accounting authority.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import time

_binding = ContextVar('tofu_native_context_binding', default=None)


@contextmanager
def native_context_scope(task, metadata, output_tokens, *, deadline_monotonic_ns=None):
    if deadline_monotonic_ns is not None and (type(deadline_monotonic_ns) is not int
            or not 0 <= deadline_monotonic_ns <= 18446744073709551615):
        raise ValueError('native context deadline invalid')
    owner, identity = task.get('_userId'), task.get('id')
    window = metadata.get('context_window')
    if (type(owner) is not int or owner != metadata.get('owner_user_id')
            or not isinstance(identity, str) or not identity
            or type(window) is not int or window <= 0
            or type(output_tokens) is not int or not 0 < output_tokens < window):
        raise ValueError('native context requires owned declared window and output')
    from lib.context_limits import resolve_learned_context_limit
    learned = resolve_learned_context_limit(metadata['provider_id'], metadata['wire_model'], window)
    if output_tokens >= min(window, learned):
        raise ValueError('native output reservation exceeds the effective route window')
    record = (owner, identity, dict(metadata), output_tokens, deadline_monotonic_ns)
    token = _binding.set(record)
    try:
        yield
    finally:
        _binding.reset(token)


def native_task_context_profile(task):
    record = _binding.get()
    if record is None:
        return None
    owner, identity, metadata, output, _ = record
    if (type(task.get('_userId')) is not int or task['_userId'] != owner
            or task.get('id') != identity):
        return None
    model = (task.get('config') or {}).get('model') or task.get('model')
    if model and model not in (metadata['logical_model'], metadata['wire_model']):
        return None
    from lib.context_limits import resolve_learned_context_limit
    declared = metadata['context_window']
    window = min(declared, resolve_learned_context_limit(
        metadata['provider_id'], metadata['wire_model'], declared))
    return {'window': window, 'source': 'model_routing_v2', 'exact': True,
            'output_tokens': output, 'model': metadata['logical_model']}


def native_compaction_route_ready(task):
    """Native capacity binding cannot authorize an auxiliary model request."""
    record = _binding.get()
    if record is None:
        return True
    owner, identity, metadata, _, deadline = record
    if task.get('_userId') != owner or task.get('id') != identity:
        return False
    if deadline is not None and time.monotonic_ns() >= deadline:
        event = task.get('abort_event')
        if event is not None:
            event.set()
        raise TimeoutError('native context deadline expired before summary')
    config = task.get('config') or {}
    model = config.get('model') or task.get('model')
    pin = task.get('_pinned_provider_id') or config.get('_pinned_provider_id')
    if not pin or model not in (metadata['logical_model'], metadata['wire_model']):
        return False
    fields = {'route_offering_id':'offering_id', 'route_deployment_id':'deployment_id',
              'route_connection_id':'connection_id', 'route_credential_id':'credential_id'}
    if any(not metadata.get(key) for key in fields.values()):
        return False
    from lib.llm_dispatch.factory import get_dispatcher
    slots = [slot for slot in list(get_dispatcher().slots)
             if slot.provider_id == pin and model in (slot.model, slot.logical_model)]
    return bool(slots) and all(
        slot.routing_owner_user_id == owner
        and slot.routing_provider_id == metadata['provider_id']
        and all(getattr(slot, field, '') == metadata[key] for field, key in fields.items())
        for slot in slots)
