"""Invocation-scoped native context and issued-parent compaction readiness.

The application binds compiled route metadata after output clamping. Explicit
owner/task/model identity prevents unrelated work from borrowing that window.
This module grants no credential, provider, tool or accounting authority.
"""
from collections.abc import Mapping
from contextlib import contextmanager, nullcontext
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


def native_task_local_inference(task):
    """Read serving locality only from this task's issued route binding."""
    record = _binding.get()
    if record is None:
        return None
    owner, identity, metadata, _, _ = record
    if type(task.get('_userId')) is not int or task['_userId'] != owner or task.get('id') != identity:
        return None
    model = (task.get('config') or {}).get('model') or task.get('model')
    if model and model not in (metadata['logical_model'], metadata['wire_model']):
        return None
    if type(metadata.get('local_inference')) is bool:
        return metadata['local_inference']
    from lib.model_routing.serving_locality import is_local_inference
    return is_local_inference(protocol=metadata.get('protocol', ''),
        credential_kind=metadata.get('credential_kind', ''),
        base_url=metadata.get('base_url', ''))


def _issued_group_route_evidence(task, metadata):
    """Validate every original issued member, not just the selected model subset."""
    from lib.identity import tenant_scope
    from lib.model_routing.dispatch_adapter import RoutedSlotGroup, issued_route_identity
    from lib.model_routing.repository import OwnerBoundary
    from lib.llm_dispatch.ephemeral import EphemeralSlotHandle
    from lib.llm_dispatch.factory import get_dispatcher
    from lib.tasks_pkg.manager import task_user_id

    group = task.get('_model_routing_group')
    if (not isinstance(group, RoutedSlotGroup) or group.disposed
            or not isinstance(group.route_identity, Mapping)
            or type(group.route_identity.get('owner_user_id')) is not int
            or type(task.get('_userId')) is not int):
        return False
    try:
        view = dict(task)
        owner = task_user_id(view)  # Canonical principal validation, without stamping the task.
        tenant = view['_principalContext'].get('tenant_id', task.get('_tenant_id'))
        if (task.get('_tenant_id') is not None
                and tenant_scope(task['_tenant_id']) != tenant_scope(tenant)):
            return False
        boundary = OwnerBoundary.create(owner, tenant)
        revision = group.route_identity.get('revision')
        config = task.get('config') or {}
        model = task.get('model') or config.get('model')
        pin = task.get('_pinned_provider_id') or config.get('_pinned_provider_id')
        if (type(revision) is not int or revision < 0 or not pin or pin != group.pin_id
                or config.get('_pinned_provider_id') not in (None, '', pin)
                or not isinstance(model, str) or not model):
            return False
        dispatcher = get_dispatcher()
        with dispatcher._lock:
            handles, candidates = list(group.handles), list(group.candidates)
            if (not handles or len(handles) != len(candidates)
                    or any(not isinstance(handle, EphemeralSlotHandle) or handle.disposed
                           for handle in handles)):
                return False
            slots = [handle.slot for handle in handles]
            scope = dispatcher._selection_scope(pin)
            slot_ids = {id(slot) for slot in slots}
            if (len(slot_ids) != len(slots) or len(scope) != len(slots)
                    or {id(slot) for slot in scope} != slot_ids):
                return False
            matcher = dispatcher._prefer_matcher(model)
            if not callable(matcher) or not matcher(slots[0]):
                return False
            if config.get('model') and not dispatcher._prefer_matcher(config['model'])(slots[0]):
                return False
            identities = []
            fields = {'routing_provider_id': 'providerId', 'route_offering_id': 'offeringId',
                      'route_deployment_id': 'deploymentId', 'route_connection_id': 'connectionId',
                      'route_credential_id': 'credentialId'}
            for slot, candidate in zip(slots, candidates):
                witness = issued_route_identity(candidate, boundary, revision)
                logical = (candidate.model['model_id'] if candidate.model is not None
                           else candidate.offering['pending_model_id'])
                if (slot.provider_id != pin or slot.request_scoped is not True
                        or type(slot.routing_owner_user_id) is not int
                        or slot.routing_owner_user_id != owner
                        or tenant_scope(slot.routing_tenant_id) != boundary.tenant_id
                        or slot.logical_model != logical
                        or slot.model != candidate.deployment['wire_model_id']
                        or not isinstance(getattr(slot, 'route_witness', None), Mapping)
                        or type(slot.route_witness.get('owner_user_id')) is not int
                        or type(slot.route_witness.get('revision')) is not int
                        or dict(slot.route_witness) != witness
                        or any(getattr(slot, field, '') != witness[key]
                               for field, key in fields.items())):
                    return False
                identities.append(witness)  # Detached, secret-free primitive values only.
            if dict(group.route_identity) != identities[0]:
                return False
            if metadata is not None:
                primary = identities[0]
                fields = {'owner_user_id': 'owner_user_id', 'revision': 'revision',
                          'provider_id': 'providerId', 'provider_access_id': 'providerAccessId',
                          'offering_id': 'offeringId', 'deployment_id': 'deploymentId',
                          'connection_id': 'connectionId', 'credential_id': 'credentialId'}
                if (tenant_scope(metadata.get('tenant_id')) != boundary.tenant_id
                        or metadata.get('logical_model') != slots[0].logical_model
                        or metadata.get('wire_model') != slots[0].model
                        or any(metadata.get(field) != primary[key] for field, key in fields.items())):
                    return False
            return {'pinId': pin, 'routeIdentity': identities[0],
                    '_parentRouteIdentities': tuple(identities)}
    except (AttributeError, KeyError, TypeError, ValueError, PermissionError):
        return False


def native_compaction_route_evidence(task):
    """One readiness owner: None for legacy, False for refusal, or issued evidence.

    An absent ContextVar does not erase a task's issued group. Native metadata
    binds its primary only; the complete original group must remain in the
    current picker scope, including nonprimary models and credentials. No
    repository, plaintext, health filter or auxiliary policy is consulted here.
    """
    record = _binding.get()
    if not isinstance(task, dict):
        return None if record is None else False
    metadata = None
    if record is not None:
        owner, identity, metadata, _, deadline = record
        if type(task.get('_userId')) is not int or task['_userId'] != owner or task.get('id') != identity:
            return False
        if deadline is not None and time.monotonic_ns() >= deadline:
            event = task.get('abort_event')
            if event is not None:
                event.set()
            raise TimeoutError('native context deadline expired before summary')
    if '_model_routing_group' in task:
        return _issued_group_route_evidence(task, metadata)
    if record is None:
        return None
    # Legacy scoped callers have no issued-group port. Preserve their exact
    # owner/primary checks; only current dispatchers use the indexed scope.
    config = task.get('config') or {}
    model = config.get('model') or task.get('model')
    pin = task.get('_pinned_provider_id') or config.get('_pinned_provider_id')
    if not pin or model not in (metadata['logical_model'], metadata['wire_model']):
        return False
    fields = {'route_offering_id': 'offering_id', 'route_deployment_id': 'deployment_id',
              'route_connection_id': 'connection_id', 'route_credential_id': 'credential_id'}
    if any(not metadata.get(key) for key in fields.values()):
        return False
    from lib.llm_dispatch.factory import get_dispatcher
    dispatcher = get_dispatcher()
    with getattr(dispatcher, '_lock', nullcontext()):
        scope = (dispatcher._selection_scope(pin) if hasattr(dispatcher, '_selection_scope')
                 else dispatcher.slots)
        slots = [slot for slot in scope
                 if slot.provider_id == pin and model in (slot.model, slot.logical_model)]
        ready = bool(slots) and all(
            slot.routing_owner_user_id == owner
            and slot.routing_provider_id == metadata['provider_id']
            and all(getattr(slot, field, '') == metadata[key] for field, key in fields.items())
            for slot in slots)
    return None if ready else False


def native_compaction_route_ready(task):
    """Native capacity metadata alone cannot authorize a summary request."""
    return native_compaction_route_evidence(task) is not False
