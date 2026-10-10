"""Reconcile provider request names after a successful, owner-scoped probe.

Entry points: reconcile_probe_identities (pure aggregate edit), identity_probe_runner
(manual probe completion). Catalog identity and live reachability are separate
evidence; ambiguous names remain provider-scoped. No network or new worker pool.
"""
from copy import deepcopy

from lib.model_catalog._creator_identity import strip_routing_decoration


def reconcile_probe_identities(document, bundle, cells):
    """Match successful pending supplies to unique existing catalog identities.

    Only exact names, known publisher decoration, and confirmed request names
    are identity evidence. Preview/date/version suffixes are preserved. Failed
    or rate-limited calls never establish identity or change existing grants.
    """
    pending = [row for row in bundle['offerings']
               if row.get('identity_state') == 'pending_identity' and row.get('enabled', True)]
    if not pending:
        return 0
    models = {(row['creator_id'], row['model_id']): row
              for row in document.get('models', [])}
    names = {}

    def index(name, ref):
        key = strip_routing_decoration(name).casefold()
        if key:
            names.setdefault(key, set()).add(ref)

    for ref in models:
        index(ref[1], ref)
    offerings = {row['offering_id']: row for row in document.get('offerings', [])}
    scoped_ids = {row.get('offering_id') for row in bundle['offerings']}
    deployments_by_offering = {}
    for deployment in document.get('deployments', []):
        if deployment['offering_id'] not in scoped_ids:
            continue
        deployments_by_offering.setdefault(deployment['offering_id'], []).append(deployment)
        offering = offerings[deployment['offering_id']]
        ref = offering.get('model')
        if ref:
            index(deployment['wire_model_id'], (ref['creator_id'], ref['model_id']))
    successes = {}
    for cell in cells:
        idx = cell.get('key_idx')
        if (cell.get('status') == 'ok' and type(idx) is int
                and 0 <= idx < len(bundle['credentials'])):
            successes.setdefault(cell.get('model_id'), set()).add(
                bundle['credentials'][idx]['credential_id'])
    credentials = {row['credential_id']: row for row in document.get('credentials', [])}
    confirmed = []
    for source in pending:
        offering = offerings.get(source.get('offering_id'))
        if (not offering or offering.get('identity_state') != 'pending_identity'
                or not offering.get('enabled', True)):
            continue
        deployments = deployments_by_offering.get(offering['offering_id'], [])
        passed = [row for row in deployments if row['wire_model_id'] in successes]
        if not passed:
            continue
        candidates = set()
        for name in [offering['pending_model_id']] + [row['wire_model_id'] for row in deployments]:
            candidates.update(names.get(strip_routing_decoration(name).casefold(), set()))
        if len(candidates) != 1:
            continue
        ref = next(iter(candidates))
        official = models[ref]
        offering['model'] = {'creator_id': ref[0], 'model_id': ref[1]}
        offering['identity_state'] = 'confirmed'
        offering.pop('pending_model_id')
        from .offering_metadata import apply_inherited_model_fields
        apply_inherited_model_fields(offering, official)
        # Discovery metadata cannot expand official capability/context limits.
        from lib.llm_dispatch.config._pricing import MANAGED_TIER_TAGS
        offering['capabilities'] = sorted(set(offering.get('capabilities', []))
            & (set(official['capabilities']) | MANAGED_TIER_TAGS))
        offering['context_window'] = min(offering['context_window'], official['context_window'])
        for deployment in deployments:
            deployment['identity_confidence'] = 'high'
            if deployment in passed:
                deployment['probe_status'] = 'passed'
        confirmed.append((offering, passed))
    groups = {}
    for offering, passed in confirmed:
        key = (offering['provider_access_id'], offering['model']['creator_id'], offering['model']['model_id'])
        group = groups.setdefault(key, {'offering': offering, 'passed': [], 'wires': set()})
        group['passed'].extend(passed)
    for offering in offerings.values():
        ref = offering.get('model')
        if not ref:
            continue
        group = groups.get((offering['provider_access_id'], ref['creator_id'], ref['model_id']))
        if group is not None:
            group['wires'].update(row['wire_model_id'] for row in
                                  deployments_by_offering.get(offering['offering_id'], []))
    for group in groups.values():
        offering, passed, all_wires = group['offering'], group['passed'], group['wires']
        ref = (offering['model']['creator_id'], offering['model']['model_id'])
        # Preserve owner grants/exclusions. A new grant reaches only successful
        # names on connections already authorized for that credential.
        for cid, credential in credentials.items():
            if credential['provider_access_id'] != offering['provider_access_id']:
                continue
            authorization = credential['authorization']
            reachable = {row['wire_model_id'] for row in passed
                         if cid in successes[row['wire_model_id']]
                         and row['connection_id'] in authorization['connection_ids']}
            grants = authorization['models']
            if not reachable or any((g['creator_id'], g['model_id']) == ref for g in grants):
                continue
            grant = dict(offering['model'])
            excluded = sorted(all_wires - reachable)
            if excluded:
                grant['excluded_wire_model_ids'] = excluded
            grants.append(grant)
    return len(confirmed)


def identity_probe_runner(repository, boundary, authority, bundle, *, runner):
    """Commit before publishing done; stale configuration fails its original CAS.

    Scoped runs reconcile only their fresh cells, never seeded historical pips.
    """
    def execute(task, work, timeout):
        fresh_keys = {(row[0], row[3]) for row in work}

        def complete(snapshot):
            document = deepcopy(authority.document)
            cells = [cell for cell in snapshot['cells'].values()
                     if (cell.get('key_idx'), cell.get('model_id')) in fresh_keys]
            if reconcile_probe_identities(document, bundle, cells):
                repository.compare_and_swap(boundary, document,
                                            expected_revision=authority.revision)

        return runner(task, work, timeout, on_complete=complete)

    return execute
