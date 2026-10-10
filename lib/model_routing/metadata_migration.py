"""One-way metadata provenance repair for pre-inheritance routing aggregates.

Entry point: reconcile_offering_metadata. Runs at the owner catalog migration
boundary, never during dispatch. Recognizes generated discovery IDs and exact
old materialization fingerprints; unmatched values remain explicit overrides.
An existing marker (including []) is authoritative and is never reinterpreted.
The report contains only identifiers, field names and migration reasons.
"""
from collections import defaultdict
import hashlib

from .offering_metadata import MODEL_METADATA_FIELDS, apply_inherited_model_fields


def _declared_provider_contexts(document, templates):
    """Index supplied catalog evidence by exact provider endpoint and Model ID."""
    endpoints = defaultdict(set)
    for connection in document['connections']:
        endpoints[connection['provider_access_id']].add(connection['base_url'].rstrip('/'))
    by_url = defaultdict(dict)
    for template in templates:
        for recipe in template.get('offering_recipes', []):
            source = recipe.get('context_window_source')
            if source and source not in {'family_estimate', 'vendor_official', 'repository_verified'}:
                by_url[template.get('base_url', '').rstrip('/')][recipe['model_id']] = recipe.get('context_window')
    return {access: {mid: value for url in urls for mid, value in by_url[url].items()}
            for access, urls in endpoints.items()}


def _receipt_inheritance(document, receipt):
    """Recover missing input fields from the owner's redacted cutover receipt."""
    backup = ((receipt or {}).get('receipt') or {}).get('redacted_backup') or {}
    if not backup:
        return {}
    from .migration import plan_legacy_migration
    plan = plan_legacy_migration(backup.get('server_config') or {},
        byo_providers=backup.get('byo_providers') or [], official_directory=document['models'])
    return {o['offering_id']: set(o.get('inherited_model_fields', []))
            for o in plan.document['offerings']}


def reconcile_offering_metadata(document, *, provider_templates=(), legacy_receipt=None):
    models = {(m['creator_id'], m['model_id']): m for m in document['models']}
    providers = {a['provider_access_id']: a['provider_id']
                 for a in document['provider_accesses']}
    oauth_accesses = {c['provider_access_id'] for c in document['credentials']
                      if c['kind'] == 'oauth'}
    deployments = defaultdict(list)
    for deployment in document['deployments']:
        deployments[deployment['offering_id']].append(deployment)
    changes = []
    provider_contexts = _declared_provider_contexts(document, provider_templates)
    receipt_fields = _receipt_inheritance(document, legacy_receipt)
    for offering in document['offerings']:
        if 'inherited_model_fields' in offering or offering['identity_state'] != 'confirmed':
            continue
        ref = offering['model']
        model = models[(ref['creator_id'], ref['model_id'])]
        reasons = {field: 'matches_model' for field in MODEL_METADATA_FIELDS
                   if offering[field] == model[field]}
        declared = provider_contexts.get(offering['provider_access_id'], {}).get(ref['model_id'])
        if isinstance(declared, int) and offering['context_window'] == min(declared, model['context_window']):
            reasons.pop('context_window', None)
        absent = receipt_fields.get(offering['offering_id'], set())
        if 'context_window' in absent and offering['context_window'] == 128000:
            reasons['context_window'] = 'migration_receipt_missing_field'
        if 'capabilities' in absent and offering['capabilities'] == ['text']:
            reasons['capabilities'] = 'migration_receipt_missing_field'
        imported_digest = hashlib.sha256(('\0'.join((offering['provider_access_id'],
            f"{ref['creator_id']}/{ref['model_id']}"))).encode('utf-8')).hexdigest()[:24]
        if (offering['provider_access_id'] in oauth_accesses
                and offering['offering_id'] == f'off_{imported_digest}'
                and offering['context_window'] == 128000):
            # Historical subscription projection dropped context fields;
            # migration then supplied 128000. This is not an account limit.
            reasons['context_window'] = 'legacy_subscription_default'
        wires = {d['wire_model_id'] for d in deployments[offering['offering_id']]}
        if len(wires) == 1:
            wire = next(iter(wires))
            digest = hashlib.sha256(
                ('\0'.join((offering['provider_access_id'], wire))).encode('utf-8')
            ).hexdigest()[:24]
            # The old sync parser discarded context evidence and fabricated
            # text-only capabilities. Only an exact generated row fingerprint
            # qualifies; a different user value or an explicit [] never does.
            if offering['offering_id'] == f'csync-offering-{digest}':
                from lib.model_info import context_profile
                historical = context_profile(wire, providers[offering['provider_access_id']]).get('window') or 32768
                if offering['capabilities'] == ['text'] and not offering.get('inference_profile'):
                    reasons['capabilities'] = 'legacy_catalog_default'
                    if offering['context_window'] == historical:
                        reasons['context_window'] = 'legacy_catalog_default'
        # Recording [] also completes migration for genuine provider limits.
        offering['inherited_model_fields'] = sorted(reasons)
        apply_inherited_model_fields(offering, model)
        changes.append({'offering_id': offering['offering_id'],
                        'inherited': reasons,
                        'preserved_overrides': [f for f in MODEL_METADATA_FIELDS if f not in reasons]})
    return changes
