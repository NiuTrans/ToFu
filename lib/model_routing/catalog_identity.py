"""Bind exact discovered catalog identities without inventing probe evidence.

Entry point: reconcile_catalog_identities, a pure owner aggregate mutation used
by catalog install/sync. Unknown or ambiguous names remain provider scoped.
Credential grants preserve the reachability of the previously pending wires.
"""
from collections import defaultdict
from copy import deepcopy
from .offering_metadata import apply_inherited_model_fields, inherited_model_fields, declared_context_window


def inherit_discovered_metadata(rows, models):
    """Fill absent listing facts from unique public Models, retaining overrides."""
    by_name = defaultdict(list)
    for model in models:
        by_name[model['model_id']].append(model)
    result = []
    for row in rows:
        row = deepcopy(row)
        inherit = set(inherited_model_fields(row))
        if row.get('capabilities') is None:
            inherit.add('capabilities')
        window = declared_context_window(row)
        if window is None:
            inherit.add('context_window')
        else:
            row['context_window'] = window
        row['inherited_model_fields'] = sorted(inherit)
        matches = by_name.get(row.get('model_id'), [])
        if len(matches) == 1:
            apply_inherited_model_fields(row, matches[0])
        result.append(row)
    return result


def reconcile_catalog_identities(document: dict, *, access_id: str | None = None) -> int:
    models = defaultdict(list)
    for model in document.get('models', []):
        models[model['model_id']].append(model)
    deployments = defaultdict(list)
    for row in document.get('deployments', []):
        deployments[row['offering_id']].append(row)
    confirmed = []
    from lib.llm_dispatch.config._pricing import MANAGED_TIER_TAGS
    for offering in document.get('offerings', []):
        if (offering.get('identity_state') != 'pending_identity'
                or (access_id is not None and offering['provider_access_id'] != access_id)):
            continue
        matches = models.get(offering.get('pending_model_id'), [])
        if len(matches) != 1:
            continue
        model = matches[0]
        ref = {key: model[key] for key in ('creator_id', 'model_id')}
        offering.update(model=ref, identity_state='confirmed')
        offering.pop('pending_model_id')
        apply_inherited_model_fields(offering, model)
        offering['capabilities'] = sorted(set(offering['capabilities']) & (
            set(model['capabilities']) | MANAGED_TIER_TAGS))
        offering['context_window'] = min(offering['context_window'], model['context_window'])
        for deployment in deployments[offering['offering_id']]:
            deployment['identity_confidence'] = 'high'
        confirmed.append(offering)
    groups = defaultdict(list)
    for offering in confirmed:
        ref = offering['model']
        groups[(offering['provider_access_id'], ref['creator_id'], ref['model_id'])].extend(
            deployments[offering['offering_id']])
    for (provider_access_id, creator_id, model_id), newly_bound in groups.items():
        ref = {'creator_id': creator_id, 'model_id': model_id}
        all_wires = {dep['wire_model_id'] for offering in document['offerings']
                     if offering['provider_access_id'] == provider_access_id and offering.get('model') == ref
                     for dep in deployments[offering['offering_id']]}
        for credential in document.get('credentials', []):
            if credential['provider_access_id'] != provider_access_id:
                continue
            auth = credential['authorization']
            if any(all(grant[key] == value for key, value in ref.items()) for grant in auth['models']):
                continue  # Owner grants and explicit wire exclusions win.
            reachable = {dep['wire_model_id'] for dep in newly_bound
                         if dep['connection_id'] in auth['connection_ids']}
            if not reachable:
                continue
            grant = dict(ref)
            excluded = sorted(all_wires - reachable)
            if excluded:
                grant['excluded_wire_model_ids'] = excluded
            auth['models'].append(grant)
    return len(confirmed)
