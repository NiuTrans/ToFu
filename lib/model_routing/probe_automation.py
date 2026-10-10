"""Daily matrix maintenance: shared probe lane, fresh evidence, one owner CAS.

Entry point run_daily_probe is invoked by the durable scheduler. No HTTP or
ambient owner dependency; incomplete/conflicting runs never change grants.
"""
from concurrent.futures import TimeoutError
import copy
import time

from lib.identity import PrincipalContext
from lib import provider_probe as probe
from .authorization_diff import (
    derive_cell_state,
    derive_proposed_authorizations,
)
from .repository import ModelRoutingRepository, OwnerBoundary
from .domain import ModelRoutingError
from .probe_plan import provider_bundle, matrix_probe_plan
from .probe_identity import reconcile_probe_identities

DAILY_PROBE_MAX_RUNTIME = 1800


def apply_recommendations(document, bundle, snapshot):
    """Apply only positive, complete observations to automated authorization.

    Transport failure, quota exhaustion and expired credentials are not owner
    revocation. Negative cells stay visible in probe diagnostics and manual
    review, but cannot remove a grant or add a persistent exclusion here.
    """
    if (snapshot.get('status') != 'done'
            or snapshot.get('done_count') != snapshot.get('total')
            or not snapshot.get('total')
            or len(snapshot.get('cells') or {}) != snapshot.get('total')):
        raise ValueError('incomplete provider probe')
    models = {}
    for row in bundle['offerings']:
        if row.get('identity_state') == 'confirmed' and row.get('model'):
            ref = row['model']
            models.setdefault(ref['model_id'], set()).add(
                (ref['creator_id'], ref['model_id']))
    credentials = {r['credential_id']: r for r in document['credentials']}
    cells = []
    for cell in snapshot['cells'].values():
        refs = models.get(cell.get('root_model_id'), set())
        # Canonical names without Creator are ambiguous; fail closed.
        if len(refs) != 1:
            continue
        index = cell.get('key_idx')
        if type(index) is not int or not 0 <= index < len(bundle['credentials']):
            raise ValueError('invalid probe credential index')
        credential = credentials[bundle['credentials'][index]['credential_id']]
        creator_id, model_id = next(iter(refs))
        evidence = {'state': 'ok' if cell.get('status') == 'ok' else 'pending'}
        cells.append(derive_cell_state(
            credential=credential,
            wire_model_id=str(cell.get('model_id') or ''),
            model_ref={'creator_id': creator_id, 'model_id': model_id},
            evidence=evidence,
        ))
    # Disabled Connections are omitted from fresh network work. Keep their
    # wires neutral: absence supplies no positive reachability evidence.
    # One representative per credential/model suffices, bounding added cells
    # by the number of actual observations rather than the catalog product.
    offerings = {r.get('offering_id'): r for r in bundle['offerings']}
    known_wires = {}
    for deployment in bundle.get('deployments', ()):
        offering = offerings.get(deployment.get('offering_id'), {})
        ref = offering.get('model')
        if offering.get('identity_state') == 'confirmed' and ref:
            key = (ref['creator_id'], ref['model_id'])
            known_wires.setdefault(key, []).append(deployment['wire_model_id'])
    observed = {}
    for cell in cells:
        ref = cell['model_ref']
        key = (cell['credential_id'], ref['creator_id'], ref['model_id'])
        observed.setdefault(key, set()).add(cell['wire_model_id'])
    for (cid, creator, model), seen in observed.items():
        unseen = next((wire for wire in known_wires.get((creator, model), ())
                       if wire not in seen), None)
        if unseen is not None:
            cells.append(derive_cell_state(
                credential=credentials[cid], wire_model_id=unseen,
                model_ref={'creator_id': creator, 'model_id': model},
                evidence={'state': 'pending'},
            ))
    touched = {c['credential_id'] for c in cells}
    proposals = derive_proposed_authorizations(
        cells=cells,
        current_grants_by_credential={
            cid: credentials[cid]['authorization']['models'] for cid in touched
        },
    )
    successful_wires = {}
    observed_wires = {}
    for cell in cells:
        ref = cell['model_ref']
        key = (cell['credential_id'], ref['creator_id'], ref['model_id'])
        wire = cell['wire_model_id']
        if wire:
            observed_wires.setdefault(key, set()).add(wire)
            if cell['probe'] == 'ok':
                successful_wires.setdefault(key, set()).add(wire)
    changed = 0
    for cid, proposal in proposals.items():
        if not proposal.changed:
            continue
        target = credentials[cid]
        before = {(_g['creator_id'], _g['model_id']): frozenset(
            _g.get('excluded_wire_model_ids') or ())
            for _g in target['authorization']['models']}
        # A newly discovered grant must not authorize untested sibling wires.
        # Existing policy only heals a wire that has fresh successful evidence.
        for grant in proposal.models:
            model_key = (grant['creator_id'], grant['model_id'])
            if model_key in before:
                continue
            key = (cid, *model_key)
            all_wires = set(known_wires.get(model_key, ())) | observed_wires.get(key, set())
            excluded = sorted(all_wires - successful_wires.get(key, set()))
            if excluded:
                grant['excluded_wire_model_ids'] = excluded
        after = {(_g['creator_id'], _g['model_id']): frozenset(
            _g.get('excluded_wire_model_ids') or ())
            for _g in proposal.models}
        if before != after:
            changed += 1
        target['authorization']['models'] = [dict(m) for m in proposal.models]
    return changed


def run_daily_probe(*, principal, max_runtime=DAILY_PROBE_MAX_RUNTIME,
                    cancel_event=None, repository=None):
    if not isinstance(principal, PrincipalContext) or 'providers' not in principal.scopes:
        raise PermissionError('provider maintenance scope required')
    boundary = OwnerBoundary.create(principal.require_owner(context='daily provider probe'))
    repository = repository or ModelRoutingRepository()
    deadline = time.monotonic() + min(DAILY_PROBE_MAX_RUNTIME, max(1, max_runtime))
    authority = repository.get(boundary)
    document = copy.deepcopy(authority.document)
    changed = completed = total_cells = identities = 0
    for provider in document['providers']:
        if time.monotonic() >= deadline or (cancel_event and cancel_event.is_set()):
            raise TimeoutError('daily probe cancelled or timed out')
        bundle = provider_bundle(document, provider['provider_id'])
        if not bundle['provider_access'] or not bundle['provider_access'].get('enabled', True):
            continue
        if not bundle['credentials'] or not bundle['offerings']:
            continue
        try:
            plan = matrix_probe_plan(repository, boundary, bundle)
        except ModelRoutingError as exc:
            if exc.kind == 'provider_probe_no_offerings':
                continue
            raise
        work = probe.build_probe_work(plan['provider'], plan['models'], plan['api_keys'],
                                      request_contexts=plan.get("request_contexts"),
                                      credential_contexts=plan.get("credential_contexts"))
        total_cells += len(work)
        if not work:
            continue
        key = f"u{boundary.owner_user_id}_{provider['provider_id']}"
        task = dict(provider_id=key, status='running', started_at=time.time(),
                    finished_at=None, total=len(work), done_count=0, cells={},
                    summary={'ok': 0, 'disable': 0}, error=None, attempts=3,
                    _abort=False, _base_url=plan['provider']['base_url'],
                    _protocol=plan['provider']['protocol'], _extra_headers=plan['extra_headers'],
                    _oauth=plan['oauth'], _adapter=plan['adapter'],
                    _owner_user_id=boundary.owner_user_id)
        with probe.CELL_PROBE_LOCK:
            existing = probe.CELL_PROBE_TASKS.get(key)
            if existing and existing.get('status') == 'running':
                raise RuntimeError('provider probe already running')
            probe.CELL_PROBE_TASKS[key] = task
            try:
                future = probe.submit_provider_probe_task(task, work, 30)
            except Exception:
                probe.CELL_PROBE_TASKS.pop(key, None)
                raise
        work.clear()
        plan.clear()
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or (cancel_event and cancel_event.is_set()):
                    raise TimeoutError('daily probe cancelled or timed out')
                try:
                    future.result(timeout=min(1, remaining))
                    break
                except TimeoutError:
                    if future.done():
                        raise
        except BaseException:
            task['_abort'] = True
            raise
        changed += apply_recommendations(document, bundle, probe.public_probe_snapshot(task))
        identities += reconcile_probe_identities(document, bundle, task['cells'].values())
        completed += 1
    if time.monotonic() >= deadline or (cancel_event and cancel_event.is_set()):
        raise TimeoutError('daily probe cancelled or timed out')
    if changed or identities:
        repository.compare_and_swap(boundary, document, expected_revision=authority.revision)
    return {'ok': True, 'providers': completed, 'cells': total_cells, 'grant_changes': changed}
