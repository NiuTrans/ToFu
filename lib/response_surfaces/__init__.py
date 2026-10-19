"""Read-only live projections for native response components.

Ownership comes from an existing Turn or Evolution run, never model-authored
URLs, paths or owner IDs. Compute providers retain credential, project and
catalog admission. There is no new business store, worker, model call or job
submission path here. Entry: read_resources(scope, provider, user_id, tenant_id).
"""
from datetime import datetime, timezone
import hashlib
import re


def resolve_scope(scope, *, user_id):
    from lib.identity import require_user_id
    require_user_id(user_id, context='response surface')
    if not isinstance(scope, dict):
        raise PermissionError('Owned surface context required')
    if set(scope) == {'runId'} and isinstance(scope['runId'], str) and scope['runId']:
        from lib.evolution.runs import get_run_snapshot
        run = get_run_snapshot(scope['runId'], user_id=user_id)
        if not run:
            raise PermissionError('Surface unavailable')
        return str(run.get('project_path') or '')
    if set(scope) == {'conversationId', 'turnId'} and all(isinstance(v, str) and v for v in scope.values()):
        from lib.turn_lifecycle import get_turn, get_attempt
        turn = get_turn(scope['conversationId'], scope['turnId'], user_id=user_id)
        attempt = get_attempt(turn['currentAttemptId'], user_id=user_id, include_execution_config=True)
        config = attempt.get('config') or {}
        return str(config.get('projectPath') or '') if config.get('projectEnabled') else ''
    raise PermissionError('Owned surface context required')


def read_catalog(provider, *, user_id, tenant_id, project_path, cancelled):
    """Borrow the optional provider SDK's trusted composition and lifecycle."""
    from tofu_environments import host
    from tofu_environments.contracts import Actor
    actor = Actor(user_id, tenant_id, project_path)
    return host.read_compute_catalog(actor, provider, cancelled=cancelled)


def resource_snapshot(catalog):
    """Normalize validated provider data without inventing unknown quantities."""
    rows = []
    identities = set()
    for queue in catalog['queues']:
        for resource in queue['resources']:
            key = '\0'.join((queue['name'], resource['kind'], resource['name'], resource['unit']))
            identity = 'resource-' + hashlib.sha256(key.encode()).hexdigest()[:32]
            if identity in identities:
                raise ValueError('Provider returned duplicate resource identities')
            identities.add(identity)
            rows.append({'id': identity, 'queue': queue['name'],
                         **{k: resource[k] for k in ('kind', 'name', 'unit', 'total', 'allocated', 'available')},
                         **({'pending': resource['pending']} if 'pending' in resource else {})})
    snapshot = {'fetchedAt': datetime.now(timezone.utc).isoformat(), 'rows': rows}
    from jsonschema import Draft202012Validator
    import json
    from pathlib import Path
    from lib.response_surfaces.validation import validate_surface_semantics
    payload = {'version': 1, 'title': 'Resources', 'blocks': [
        {'type': 'resource_panel', 'id': 'resources', 'title': 'Resources', 'provider': 'catalog', 'snapshot': snapshot}]}
    json.dumps(payload, allow_nan=False)
    schema = json.loads((Path(__file__).resolve().parents[2] / 'contracts/rich_response_v1.schema.json').read_text())
    Draft202012Validator(schema).validate(payload)
    validate_surface_semantics(payload)
    return snapshot


def available_providers(scope, *, user_id):
    resolve_scope(scope, user_id=user_id)
    try:
        from tofu_environments import host
    except ImportError:
        native = ()
    else:
        native = host.configured_compute_providers()
    from lib.response_surfaces.mcp import providers
    return {'providers': [{'id': name} for name in sorted(set(native))
                          if isinstance(name, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,64}', name)] + providers(user_id)}


def read_resources(scope, provider, *, user_id, tenant_id='', cancelled=lambda: False, connection=None, queue=None):
    if not isinstance(provider, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', provider):
        raise ValueError('Invalid provider')
    project = resolve_scope(scope, user_id=user_id)
    if connection is not None:
        from lib.response_surfaces.mcp import read_catalog as read_mcp
        catalog = read_mcp(connection, provider, user_id=user_id, cancelled=cancelled, queue=queue)
    else:
        if queue is not None:
            raise ValueError('Queue resources require an explicit connection')
        catalog = read_catalog(provider, user_id=user_id, tenant_id=tenant_id,
                               project_path=project, cancelled=cancelled)
    # Recheck access after the remote read; deletion/revocation cannot deliver
    # a late result into another ownership scope.
    if resolve_scope(scope, user_id=user_id) != project:
        raise PermissionError('Surface context changed')
    return {'version': 1, 'provider': provider, 'snapshot': resource_snapshot(catalog)}
