"""Manual HTTP detection reads saved identities without saving or granting access.

Use the real route, planner, work builder and successful cell runner. Repository,
queue admission, snapshot storage and provider transport are isolated test ports.
"""
import asyncio
from collections import OrderedDict
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest
from quart import Quart

from lib.provider import provider_probe
from lib.model_routing import OwnerBoundary, normalize_document
from lib.model_routing.repository import StoredAuthority
from tests.model_routing.test_manual_probe_plan import manual_document, target

pytestmark = pytest.mark.unit

BOUNDARY = OwnerBoundary.create(4127, 'manual-probe-test')
START = '/api/v1/providers/provider-a/probe-cells/start'
STATUS = '/api/v1/providers/provider-a/probe-cells/status'
CREDENTIAL_IDS = ['credential-a', 'credential-a-second']


class ReadOnlyRepository:
    """Record forbidden writes even if a runner catches the injected failure."""

    def __init__(self):
        document = manual_document()
        offering = document['offerings'][0]
        offering.pop('model')
        offering.update(identity_state='pending_identity', pending_model_id='alpha')
        credential = document['credentials'][0]
        credential.update(kind='api_key', secret_reference='secret-a', enabled=False)
        document['credentials'].append({
            **deepcopy(credential), 'credential_id': CREDENTIAL_IDS[1],
            'secret_reference': 'secret-a-second',
        })
        self.document = normalize_document(document)
        self.revision = 17
        self.resolved = []
        self.writes = []
        # Synthetic values, never real provider credentials.
        self.secrets = {'secret-a': 'fake-key-column-a',
                        'secret-a-second': 'fake-key-column-b'}

    def get(self, boundary):
        assert boundary == BOUNDARY
        return StoredAuthority(boundary, self.revision, self.document, 0)

    def resolve_secret(self, boundary, reference):
        assert boundary == BOUNDARY
        self.resolved.append(reference)
        return self.secrets[reference]

    def compare_and_swap(self, *args, **kwargs):
        self.writes.append(('compare_and_swap', args, kwargs))
        raise AssertionError('manual probing must not CAS routing authority')

    def put_secret(self, *args, **kwargs):
        self.writes.append(('put_secret', args, kwargs))
        raise AssertionError('manual probing must not save credentials')


@pytest.fixture
def harness(monkeypatch):
    import routes.api_v1.auth as auth
    import routes.api_v1.providers as routes
    from lib.model_routing import probe_identity

    repository = ReadOnlyRepository()
    state = SimpleNamespace(repository=repository, queued=[], persisted=[],
                            network=[], identity_calls=[], cached=None)
    context = SimpleNamespace(
        owner_user_id=BOUNDARY.owner_user_id, tenant_id=BOUNDARY.tenant_id,
        is_authenticated=True, has_scope=lambda scope: scope == 'providers')
    monkeypatch.setattr(auth, 'current_auth', lambda: context)
    monkeypatch.setattr(routes, 'current_auth', lambda: context)
    monkeypatch.setattr(routes, '_repository', lambda: repository)
    tasks = OrderedDict()
    monkeypatch.setattr(routes, 'CELL_PROBE_TASKS', tasks)
    monkeypatch.setattr(provider_probe, 'CELL_PROBE_TASKS', tasks)
    monkeypatch.setattr(routes, '_read_persisted_probe_snapshot',
                        lambda key, credential_ids=None: deepcopy(state.cached))

    def submit(task, work, timeout, *, runner):
        state.queued.append((task, work, timeout, runner))

    def persist(task):
        state.persisted.append(provider_probe.public_probe_snapshot(task))
        return False  # retain the in-memory task for HTTP status polling

    def probe_cell(*args, **kwargs):
        state.network.append((args, kwargs))
        return 'ok', 'HTTP 200'

    def forbidden_identity(*args, **kwargs):
        state.identity_calls.append((args, kwargs))
        raise AssertionError('manual probing must not construct an identity writer')

    monkeypatch.setattr(routes, 'submit_provider_probe_task', submit)
    monkeypatch.setattr(provider_probe, 'persist_probe_task', persist)
    monkeypatch.setattr(provider_probe, 'probe_cell_multi', probe_cell)
    monkeypatch.setattr(probe_identity, 'identity_probe_runner', forbidden_identity)
    # Guard the historical route alias too, if one is still imported.
    if hasattr(routes, 'identity_probe_runner'):
        monkeypatch.setattr(routes, 'identity_probe_runner', forbidden_identity)
    app = Quart(__name__, static_folder=None)
    app.config['TESTING'] = True
    app.register_blueprint(routes.api_v1_providers_bp)

    def request(method, path, body=None):
        async def go():
            client = app.test_client()
            response = await client.open(path, method=method, json=body)
            return response.status_code, await response.get_json()
        return asyncio.run(go())

    state.request = request
    return state


def assert_error(response, kind):
    status, body = response
    assert status == 400, body
    error = body.get('error')
    actual_kind = error.get('kind') if isinstance(error, dict) else body.get('kind')
    assert actual_kind == kind, body


def assert_unmodified(harness, before):
    assert harness.repository.document == before
    assert harness.repository.revision == 17
    assert harness.repository.writes == []
    assert harness.identity_calls == []


def assert_secret_free(harness, payload):
    encoded = json.dumps(payload)
    for secret in harness.repository.secrets.values():
        assert secret not in encoded
    for private in ('_base_url', '_extra_headers', 'api_keys', 'secret_reference'):
        assert private not in encoded


@pytest.mark.parametrize('all_off', [False, True])
@pytest.mark.parametrize('wire', ['alpha-wire-a', 'unsaved/request-name'])
def test_disabled_selected_cell_uses_saved_transport_and_success_is_read_only(
        harness, all_off, wire):
    repository = harness.repository
    if all_off:
        for key in ('provider_accesses', 'connections', 'offerings', 'credentials'):
            for row in repository.document[key]:
                row['enabled'] = False
    before = deepcopy(repository.document)
    status, started = harness.request('POST', START, {
        'force': True, 'targets': [target('  ' + wire + '  ')],
        'credential_ids': CREDENTIAL_IDS,
        'only': {'key_idxs': [1], 'model_ids': [wire]},
        'attempts': 1,
    })
    assert status == 200, started
    assert started['total'] == 1
    assert len(harness.queued) == 1
    task, work, timeout, runner = harness.queued[0]
    assert [(row[0], row[2], row[3], row[5], row[6]) for row in work] == [
        (1, 'alpha', wire, 'https://disabled.example/v1', 'openai')]
    assert work[0][1] == repository.secrets['secret-a-second']
    assert work[0][7]['extra_headers'] == {'X-Transport': 'disabled'}
    assert task['_owner_user_id'] == BOUNDARY.owner_user_id
    # Cell keys are positional, so the snapshot must say which credential each
    # column measured — otherwise a later key deletion re-labels the evidence.
    assert started['credential_ids'] == CREDENTIAL_IDS
    assert harness.network == []
    assert_unmodified(harness, before)
    assert_secret_free(harness, started)

    runner(task, work, timeout)
    status, completed = harness.request('GET', STATUS)
    assert status == 200, completed
    assert completed['status'] == 'done', completed.get('error')
    assert completed['done_count'] == completed['total'] == 1
    assert set(completed['cells']) == {f'1::{wire}'}
    assert completed['cells'][f'1::{wire}']['status'] == 'ok'
    assert completed['credential_ids'] == CREDENTIAL_IDS
    assert len(harness.network) == 1
    assert harness.persisted
    assert_unmodified(harness, before)
    assert repository.document['offerings'][0]['identity_state'] == 'pending_identity'
    assert repository.document['credentials'][0]['authorization']['models'] == []
    for snapshot in [completed, *harness.persisted]:
        assert_secret_free(harness, snapshot)


@pytest.mark.parametrize('all_off', [False, True])
def test_manual_route_without_targets_also_includes_disabled_rows(harness, all_off):
    if all_off:
        for key in ('connections', 'offerings'):
            for row in harness.repository.document[key]:
                row['enabled'] = False
    before = deepcopy(harness.repository.document)
    status, body = harness.request('POST', START, {'force': True})
    assert status == 200, body
    work = harness.queued[0][1]
    assert {(row[0], row[3]) for row in work} == {
        (key, wire) for key in (0, 1) for wire in ('alpha-wire-a', 'beta-wire-a')}
    assert_unmodified(harness, before)


@pytest.mark.parametrize('targets', [
    [target(offering='unknown')], [target(connection='unknown')],
    [target(offering='alpha-b')], [target(connection='connection-b')],
    [target('   ')], [target('bad\x00wire')], [target('wire\n')],
    [target('same'), target('same', offering='beta-a')],
    [target('same'), target('same', connection='connection-a')],
])
def test_invalid_ownership_or_wire_rejected_before_secret_resolution(harness, targets):
    before = deepcopy(harness.repository.document)
    response = harness.request('POST', START, {'force': True, 'targets': targets})
    assert harness.repository.resolved == []
    assert harness.queued == []
    assert harness.network == []
    assert_unmodified(harness, before)
    assert_error(response, 'provider_probe_invalid_target')


@pytest.mark.parametrize('credential_ids', [
    list(reversed(CREDENTIAL_IDS)), ['credential-a'],
    ['credential-a', 'credential-b'], ['credential-a', 'credential-a'],
    ['credential-a', 'deleted-credential'],
])
def test_stale_credential_columns_rejected_before_secret_resolution(harness, credential_ids):
    before = deepcopy(harness.repository.document)
    response = harness.request('POST', START, {
        'force': True, 'targets': [target()], 'credential_ids': credential_ids,
        'only': {'key_idxs': [1]},
    })
    assert harness.repository.resolved == []
    assert harness.queued == []
    assert_unmodified(harness, before)
    assert_error(response, 'provider_probe_stale_selection')


def test_cached_snapshot_cannot_hide_changed_credential_columns(harness):
    harness.cached = {'status': 'done', 'cells': {'0::alpha-wire-a': {
        'key_idx': 0, 'model_id': 'alpha-wire-a', 'status': 'ok'}}}
    response = harness.request('POST', START, {
        'credential_ids': list(reversed(CREDENTIAL_IDS))})
    assert_error(response, 'provider_probe_stale_selection')
    assert harness.repository.resolved == []
    assert harness.queued == []


@pytest.mark.parametrize('only', [
    {'key_idxs': [99]}, {'model_ids': ['not-selected']},
    {'model_ids': ['beta-wire-a']},
])
def test_empty_explicit_scope_errors_instead_of_probing_unselected_cells(harness, only):
    before = deepcopy(harness.repository.document)
    response = harness.request('POST', START, {
        'force': True, 'targets': [target()], 'only': only})
    assert harness.queued == []
    assert_unmodified(harness, before)
    assert_error(response, 'provider_probe_empty_scope')


@pytest.mark.parametrize('body', [
    {'only': {'credential_ids': CREDENTIAL_IDS}},
    {'only': {'targets': [target()]}},
    {'only': {'key_idxs': [True]}},
    {'targets': [{**target(), 'api_key': 'client-must-not-send-a-secret'}]},
    {'credential_ids': [17]},
])
def test_request_contract_rejects_undeclared_scope_fields_and_credentials(harness, body):
    before = deepcopy(harness.repository.document)
    status, response = harness.request('POST', START, body)
    assert status == 400, response
    assert harness.repository.resolved == []
    assert harness.queued == []
    assert_unmodified(harness, before)


def test_force_keeps_existing_cache_restart_behavior(harness):
    harness.cached = {'status': 'done', 'cells': {'0::beta-wire-a': {
        'key_idx': 0, 'model_id': 'beta-wire-a', 'status': 'ok'}}}
    status, cached = harness.request('POST', START, {})
    assert status == 200, cached
    assert cached['status'] == 'done'
    assert harness.queued == []
    status, fresh = harness.request('POST', START, {'force': True})
    assert status == 200, fresh
    assert fresh['status'] == 'running'
    assert len(harness.queued) == 1
    # A running probe is not duplicated even when force is requested again.
    status, existing = harness.request('POST', START, {'force': True})
    assert status == 200, existing
    assert existing['started_at'] == fresh['started_at']
    assert len(harness.queued) == 1


def test_explicit_draft_targets_bypass_completed_cache_without_force(harness):
    harness.cached = {'status': 'done', 'cells': {'0::beta-wire-a': {
        'key_idx': 0, 'model_id': 'beta-wire-a', 'status': 'ok'}}}
    status, body = harness.request('POST', START, {
        'targets': [target('new-draft-name')], 'credential_ids': CREDENTIAL_IDS})
    assert status == 200, body
    assert body['status'] == 'running'
    assert {row[3] for row in harness.queued[0][1]} == {'new-draft-name'}
    assert body['cells'] == {}


@pytest.mark.parametrize('selection,kind', [
    ({'targets': [target(offering='another-owner-offering')]},
     'provider_probe_invalid_target'),
    ({'credential_ids': list(reversed(CREDENTIAL_IDS))},
     'provider_probe_stale_selection'),
])
def test_running_task_does_not_bypass_explicit_selection_validation(harness, selection, kind):
    status, body = harness.request('POST', START, {'force': True})
    assert status == 200, body
    harness.repository.resolved.clear()
    response = harness.request('POST', START, selection)
    assert len(harness.queued) == 1
    assert harness.repository.resolved == []
    assert_error(response, kind)


def test_manual_route_submits_matrix_beyond_retired_cell_count(harness, monkeypatch):
    from routes.api_v1 import providers as routes
    original = routes.build_probe_work
    def large_work(*args, **kwargs):
        rows = original(*args, **kwargs)
        assert rows
        row = rows[0]
        return [(*row[:3], f'wire-{i}', *row[4:]) for i in range(5000)]
    monkeypatch.setattr(routes, 'build_probe_work', large_work)
    status, started = harness.request('POST', START, {'force': True, 'attempts': 1})
    assert status == 200, started
    assert started['total'] == 5000
    assert len(harness.queued) == 1 and len(harness.queued[0][1]) == 5000
    assert harness.network == []
    assert_secret_free(harness, started)


@pytest.mark.parametrize('cache_source', ['memory', 'disk'])
def test_retargeted_deployment_rebuilds_cached_detect_plan(
        harness, monkeypatch, cache_source):
    """An already complete face can change only a Deployment's endpoint."""
    from routes.api_v1 import providers as routes
    from lib.model_routing import empty_document
    from lib.model_routing.repository import InMemoryModelRoutingRepository
    from lib.model_routing.template_supply import sync_template_supply
    from lib.provider.provider_template_recipes import (
        ProviderTemplateRecipeError, compile_provider_template_bundle,
    )

    try:
        bundle = compile_provider_template_bundle(
            'yourprovider', selected_model_ids=['gpt-6.1-sol'])
    except ProviderTemplateRecipeError:
        pytest.skip('yourprovider provider template is not shipped in this build')
    repository = InMemoryModelRoutingRepository()
    secret = repository.put_secret(BOUNDARY, 'synthetic-probe-key')
    document = empty_document()
    document['providers'].append(bundle['provider'])
    document['provider_accesses'].append(bundle['provider_access'])
    for collection in ('creators', 'models', 'connections', 'credentials',
                       'offerings', 'deployments'):
        document[collection].extend(deepcopy(bundle[collection]))
    for credential in document['credentials']:
        credential.update(secret)
    repository.compare_and_swap(BOUNDARY, document, expected_revision=0)
    access_id = bundle['provider_access']['provider_access_id']
    sync_template_supply(boundary=BOUNDARY, repository=repository,
                         provider_access_ids=[access_id], backfill_only=True)
    before = repository.get(BOUNDARY).document
    face = next(row for row in before['connections']
                if row.get('gateway_namespace') == 'kunlun')
    wrong = next(row for row in before['connections']
                 if row.get('gateway_namespace') != 'kunlun')
    wire = 'kunlunbedrock.gpt-6.1-sol-m17'
    assert all(face['connection_id'] in row['authorization']['connection_ids']
               for row in before['credentials'])

    def misroute(document):
        alias = next(row for row in document['deployments']
                     if row['wire_model_id'] == wire)
        alias.update(connection_id=wrong['connection_id'], probe_status='failed')

    repository.mutate(BOUNDARY, misroute)
    provider_id = bundle['provider']['provider_id']
    monkeypatch.setattr(routes, '_repository', lambda: repository)
    reloads = []
    monkeypatch.setattr(routes, '_hot_reload_dispatcher_slots',
                        lambda: reloads.append(True))
    original_sync = routes._sync_one_card_from_template
    reports = []

    def sync(*args):
        stats = original_sync(*args)
        reports.append(stats)
        return stats

    monkeypatch.setattr(routes, '_sync_one_card_from_template', sync)
    cache_key = routes._probe_cell_cache_key(BOUNDARY, provider_id)
    cached = {'provider_id': cache_key, 'credential_ids': [
        row['credential_id'] for row in before['credentials']],
        'status': 'done', 'started_at': 1, 'finished_at': 2,
        'total': 1, 'done_count': 1, 'summary': {'ok': 0, 'disable': 1},
        'error': None,
        'cells': {f'0::{wire}': {'key_idx': 0, 'model_id': wire,
                              'status': 'not_found', 'detail': 'old endpoint'}}}
    if cache_source == 'memory':
        routes.CELL_PROBE_TASKS[cache_key] = cached
    else:
        harness.cached = cached
    start = f'/api/v1/providers/{provider_id}/probe-cells/start'
    status, response = harness.request('POST', start, {})
    assert status == 200, response
    assert reports and reports[0]['repaired_deployments'] == 1, reports
    assert all(reports[0][key] == 0 for key in (
        'added_connections', 'added_deployments', 'repaired_connections'))
    assert reloads == [True]
    assert response['status'] == 'running', response
    assert response['cells'] == {}
    assert len(harness.queued) == 1
    task, work, timeout, runner = harness.queued[0]
    aliases = [row for row in work if row[3] == wire]
    assert aliases and all(row[5] == face['base_url'] for row in aliases)
    assert all(row[6] == face['protocol'] for row in aliases)
    assert all(row[7]['extra_headers'] == face['extra_headers'] for row in aliases)
    runner(task, work, timeout)
    calls = [args for args, _kwargs in harness.network if args[2] == wire]
    assert calls and all(args[0] == face['base_url'] for args in calls)
    assert all(args[3] == face['extra_headers'] for args in calls)
    assert harness.identity_calls == []
    after = repository.get(BOUNDARY).document
    assert after['connections'] == before['connections']
    assert after['credentials'] == before['credentials']
    assert after['offerings'] == before['offerings']
    alias = next(row for row in after['deployments'] if row['wire_model_id'] == wire)
    assert alias['connection_id'] == face['connection_id']
    assert alias['probe_status'] == 'unprobed'
    assert 'synthetic-probe-key' not in json.dumps(response)


@pytest.mark.parametrize('cache_location', ['memory', 'disk', 'none'])
def test_retarget_only_repair_rebuilds_plan_and_skips_completed_snapshot(
        harness, monkeypatch, cache_location):
    import routes.api_v1.providers as routes

    new_url = 'https://repaired.example/v1'
    document = harness.repository.document
    source = document['deployments'][0]['connection_id']
    connection = next(row for row in document['connections']
                      if row['connection_id'] == source)
    document['connections'].append({
        **deepcopy(connection), 'connection_id': 'repaired-face',
        'base_url': new_url})
    old = deepcopy(document)
    status, started = harness.request('POST', START, {'force': True})
    assert status == 200, started
    task, work, timeout, runner = harness.queued.pop()
    runner(task, work, timeout)
    assert task['status'] == 'done'
    completed = provider_probe.public_probe_snapshot(task)
    harness.cached = completed if cache_location == 'disk' else None
    if cache_location != 'memory':
        routes.CELL_PROBE_TASKS.clear()

    def repair(boundary, repository, provider_id):
        repository.document = deepcopy(old)
        # Stand in for the sync CAS retargeting a Deployment onto an existing
        # face. No new row or Connection field is needed for this repair.
        repository.document['deployments'][0]['connection_id'] = 'repaired-face'
        repository.revision += 1
        return {'added_connections': 0, 'added_deployments': 0,
                'repaired_connections': 0, 'repaired_deployments': 1}

    monkeypatch.setattr(routes, '_sync_one_card_from_template', repair)
    status, restarted = harness.request('POST', START, {})
    assert status == 200, restarted
    assert restarted['status'] == 'running'
    assert len(harness.queued) == 1
    new_work = harness.queued[0][1]
    assert any(row[5] == new_url for row in new_work)


def test_retarget_only_sync_hot_reloads_dispatcher(harness, monkeypatch):
    from lib.model_routing import template_supply
    import routes.api_v1.providers as routes

    monkeypatch.setattr(template_supply, 'sync_template_supply', lambda **kwargs: {
        'added_connections': 0, 'added_deployments': 0,
        'repaired_connections': 0, 'repaired_deployments': 1})
    reloads = []
    monkeypatch.setattr(routes, '_hot_reload_dispatcher_slots', lambda: reloads.append(True))
    stats = routes._sync_one_card_from_template(BOUNDARY, harness.repository, 'provider-a')
    assert stats['repaired_deployments'] == 1
    assert reloads == [True]
