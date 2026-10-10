"""Import missing catalog identities without rewriting owner Model settings.

Entry points: merge_missing_catalog_entities and defer_incomplete_discovered_models,
used by provider install, subscription refresh and legacy recovery. Provider bundles may introduce a
new identity; an existing Model is exclusively owned by the model registry.
Offering normalization separately validates scoped limits against that Model.
"""
from copy import deepcopy
from collections.abc import Mapping

from .domain import ModelRoutingError, public_projection


def defer_incomplete_discovered_models(incoming, *, existing_models):
    """Keep unverified new supplier identities pending outside the Model registry.

    The existing Offering/Deployment is the retry source; no second durable
    queue is needed. Existing owner drafts and exact complete models survive.
    """
    from .registration import registration_missing_fields
    known = {(r['creator_id'], r['model_id']) for r in existing_models}
    deferred = {(r['creator_id'], r['model_id']) for r in public_projection(incoming)['models']
                if (r['creator_id'], r['model_id']) not in known and registration_missing_fields(r)}
    if not deferred:
        return
    incoming['models'] = [r for r in incoming['models'] if (r['creator_id'], r['model_id']) not in deferred]
    pending_ids = set()
    for offering in incoming['offerings']:
        ref = offering.get('model') or {}
        if (ref.get('creator_id'), ref.get('model_id')) in deferred:
            offering.update(identity_state='pending_identity', pending_model_id=ref['model_id'])
            offering.pop('model')
            pending_ids.add(offering['offering_id'])
    for deployment in incoming['deployments']:
        if deployment['offering_id'] in pending_ids:
            deployment['identity_confidence'] = 'pending'
    for credential in incoming['credentials']:
        auth = credential['authorization']
        auth['models'] = [r for r in auth['models'] if (r['creator_id'], r['model_id']) not in deferred]


def merge_missing_catalog_entities(document, incoming):
    for collection, keys in (
        ('creators', ('creator_id',)),
        ('models', ('creator_id', 'model_id')),
    ):
        known = {tuple(row[key] for key in keys) for row in document[collection]}
        for row in incoming.get(collection, []):
            if not isinstance(row, Mapping) or any(
                not isinstance(row.get(key), str) or not row[key] for key in keys
            ):
                raise ModelRoutingError('invalid catalog identity', field=collection)
            identity = tuple(row[key] for key in keys)
            if identity not in known:
                document[collection].append(deepcopy(row))
                known.add(identity)
