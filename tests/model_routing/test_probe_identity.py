"""Probe discovery confirms unambiguous catalog matches without guessing grants."""
from copy import deepcopy

import pytest

from lib.model_routing import InMemoryModelRoutingRepository, OwnerBoundary, ModelRoutingError, normalize_document
from lib.model_routing.probe_identity import identity_probe_runner, reconcile_probe_identities
from lib.model_routing.probe_plan import provider_bundle, matrix_probe_plan
from tests.model_routing.test_model_routing_contract import _full_document

pytestmark = pytest.mark.unit


def fixture():
    document = _full_document()
    offering = document['offerings'][0]
    offering.pop('model')
    offering.update(identity_state='pending_identity', pending_model_id='alpha')
    document['deployments'][0].update(identity_confidence='pending', probe_status='unprobed')
    document['credentials'][0]['authorization']['models'] = []
    return document, provider_bundle(document, 'provider-a')


def success(wire='alpha-wire-a', **extra):
    return {'key_idx': 0, 'model_id': wire, 'root_model_id': 'alpha', 'status': 'ok', **extra}


def test_pending_names_participate_in_probe_without_canonical_wire_guess():
    document, bundle = fixture()
    plan = matrix_probe_plan(None, None, bundle)
    assert plan['models'][0]['model_id'] == 'alpha'
    assert plan['models'][0]['request_ids'] == ['alpha-wire-a']
    assert document['offerings'][0]['identity_state'] == 'pending_identity'


def test_pending_model_matrix_is_not_truncated_by_retired_probe_quota():
    from lib.provider.provider_probe import build_probe_work
    _, bundle = fixture()
    offering, deployment, credential = bundle['offerings'][0], bundle['deployments'][0], bundle['credentials'][0]
    bundle['offerings'] = [dict(offering, offering_id=f'o{i}', pending_model_id=f'm{i}') for i in range(1000)]
    bundle['deployments'] = [dict(deployment, offering_id=f'o{i}', wire_model_id=f'm{i}') for i in range(1000)]
    bundle['credentials'] = [dict(credential, credential_id=f'c{i}') for i in range(10)]
    plan = matrix_probe_plan(None, None, bundle)
    work = build_probe_work(plan['provider'], plan['models'], plan['api_keys'])
    assert len(work) == 10000


def test_success_confirms_and_grants_only_reachable_wire():
    document, bundle = fixture()
    sibling = deepcopy(document['deployments'][0])
    sibling.update(deployment_id='alias', wire_model_id='alpha-other')
    document['deployments'].append(sibling)
    assert reconcile_probe_identities(document, bundle, [success()]) == 1
    assert document['offerings'][0]['model'] == {'creator_id': 'creator', 'model_id': 'alpha'}
    assert document['deployments'][0]['probe_status'] == 'passed'
    assert document['credentials'][0]['authorization']['models'] == [{
        'creator_id': 'creator', 'model_id': 'alpha', 'excluded_wire_model_ids': ['alpha-other']}]
    normalize_document(document)
    assert reconcile_probe_identities(document, bundle, [success()]) == 0


@pytest.mark.parametrize('status', ['not_found', 'rate_limited', 'unauthorized', 'error', 'unverified'])
def test_failure_is_not_identity_evidence(status):
    document, bundle = fixture()
    before = deepcopy(document)
    assert reconcile_probe_identities(document, bundle, [success(status=status)]) == 0
    assert document == before


@pytest.mark.parametrize('name', ['alpha-preview', 'alpha-20260911', 'private-route'])
def test_unknown_and_distinct_variants_stay_provider_scoped(name):
    document, _ = fixture()
    document['offerings'][0]['pending_model_id'] = name
    bundle = provider_bundle(document, 'provider-a')
    assert reconcile_probe_identities(document, bundle, [success()]) == 0


def test_ambiguous_creator_is_not_guessed():
    document, bundle = fixture()
    document['models'].append(dict(document['models'][0], creator_id='other'))
    assert reconcile_probe_identities(document, bundle, [success()]) == 0


def test_existing_exclusions_and_other_access_grants_survive():
    document, bundle = fixture()
    grant = {'creator_id': 'creator', 'model_id': 'alpha',
             'excluded_wire_model_ids': ['alpha-wire-a']}
    document['credentials'][0]['authorization']['models'] = [grant]
    before = deepcopy(document['credentials'])
    assert reconcile_probe_identities(document, bundle, [success()]) == 1
    assert document['credentials'] == before


def test_unauthorized_connection_is_not_granted_by_probe():
    document, bundle = fixture()
    document['credentials'][0]['authorization']['connection_ids'] = []
    assert reconcile_probe_identities(document, bundle, [success()]) == 1
    assert document['credentials'][0]['authorization']['models'] == []


@pytest.mark.parametrize('conflict', [False, True])
def test_manual_completion_commits_with_original_owner_revision(conflict):
    document, _ = fixture()
    repository = InMemoryModelRoutingRepository()
    boundary = OwnerBoundary.create(19)
    authority = repository.compare_and_swap(boundary, document, expected_revision=0)
    bundle = provider_bundle(authority.document, 'provider-a')
    if conflict:
        edited = deepcopy(authority.document)
        edited['offerings'][0]['enabled'] = False
        repository.compare_and_swap(boundary, edited, expected_revision=authority.revision)

    def runner(task, work, timeout, *, on_complete):
        on_complete({'cells': {'0::alpha-wire-a': success()}})

    run = identity_probe_runner(repository, boundary, authority, bundle, runner=runner)
    if conflict:
        with pytest.raises(ModelRoutingError, match='concurrently'):
            run({}, [(0, '', 'alpha', 'alpha-wire-a')], 1)
        assert repository.get(boundary).document['offerings'][0]['enabled'] is False
    else:
        run({}, [(0, '', 'alpha', 'alpha-wire-a')], 1)
        assert repository.get(boundary).document['offerings'][0]['identity_state'] == 'confirmed'
    assert repository.get(OwnerBoundary.create(20)).revision == 0


def test_scoped_probe_never_confirms_from_seeded_cells():
    document, _ = fixture()
    repository = InMemoryModelRoutingRepository()
    boundary = OwnerBoundary.create(19)
    authority = repository.compare_and_swap(boundary, document, expected_revision=0)
    bundle = provider_bundle(authority.document, 'provider-a')

    def runner(task, work, timeout, *, on_complete):
        on_complete({'cells': {'0::alpha-wire-a': success()}})

    identity_probe_runner(repository, boundary, authority, bundle, runner=runner)(
        {}, [(0, '', 'beta', 'beta-wire-a')], 1)
    assert repository.get(boundary).revision == authority.revision


@pytest.mark.parametrize('aborted,conflict', [(False, False), (True, False), (False, True)])
def test_probe_worker_publishes_completion_only_after_identity_commit(monkeypatch, tmp_path, aborted, conflict):
    from lib.provider import provider_probe as probe
    calls = []
    monkeypatch.setattr(probe, 'probe_cache_path', lambda _: str(tmp_path / 'probe.json'))
    monkeypatch.setattr(probe, 'probe_cell_multi', lambda *a, **kw: ('ok', 'HTTP 200'))
    task = dict(provider_id='isolated-identity', status='running', started_at=0,
                finished_at=None, total=1, done_count=0, cells={},
                summary={'ok': 0, 'disable': 0}, error=None, _abort=aborted,
                _base_url='https://provider.invalid/v1', _extra_headers={})

    def complete(snapshot):
        assert snapshot['status'] == 'running'
        calls.append(snapshot['done_count'])
        if conflict:
            raise ModelRoutingError('model-routing aggregate changed concurrently')

    probe.run_cell_probe_task(task, [(0, '', 'alpha', 'alpha', ['text'])], 1,
                             on_complete=complete)
    assert calls == ([] if aborted else [1])
    assert task['status'] == ('error' if aborted or conflict else 'done')
    if aborted or conflict:
        assert task['error']
