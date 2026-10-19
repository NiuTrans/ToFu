"""Enabled Offerings are probeable without a request-name enablement state.

Disabled Offerings are omitted from the plan. Fresh probes never emit
deployment enablement flags. Historical discovery snapshots retain their
non-actionable meaning instead of gaining authority to revoke model grants
when the retired request-name gate is removed.
"""
from copy import deepcopy

import pytest

from lib.provider import provider_probe as probe
from lib.model_routing import probe_automation
from lib.model_routing.domain import ModelRoutingError
from lib.model_routing.probe_plan import matrix_probe_plan

pytestmark = pytest.mark.unit


def _bundle(deployments):
    return {
        'provider': {'provider_id': 'p'},
        'provider_access': {'provider_access_id': 'a', 'enabled': True},
        'connections': [{'connection_id': 'c', 'enabled': True,
                         'base_url': 'https://gw.example/v1',
                         'protocol': 'openai', 'extra_headers': {}}],
        'credentials': [{'credential_id': 'k', 'kind': 'api_key',
                         'secret_reference': ''}],
        'offerings': [{'offering_id': 'o', 'identity_state': 'confirmed',
                       'model': {'creator_id': 'm', 'model_id': 'root'},
                       'capabilities': ['text']}],
        'deployments': deployments,
    }


def _deployment(wire_id, **extra):
    return {'deployment_id': 'd-' + wire_id, 'offering_id': 'o',
            'connection_id': 'c', 'wire_model_id': wire_id,
            'identity_confidence': 'high', 'probe_status': 'unprobed',
            'priority': 100, **extra}


def test_plan_includes_all_names_in_document_order_without_mutation():
    bundle = _bundle([
        _deployment('root-m17', enabled=False),
        _deployment('root', enabled=True, probe_status='failed'),
        _deployment('root-east'),
        _deployment('root-m17', enabled=True),
    ])
    before = deepcopy(bundle)
    plan = matrix_probe_plan(None, None, bundle)
    assert plan['models'][0]['request_ids'] == ['root-m17', 'root', 'root-east']
    assert 'disabled_cells' not in plan
    assert bundle == before


def test_plan_never_invents_a_canonical_request_when_aliases_exist():
    plan = matrix_probe_plan(None, None, _bundle([
        _deployment('root-m17', enabled=False),
    ]))
    assert plan['models'][0]['model_id'] == 'root'
    assert plan['models'][0]['request_ids'] == ['root-m17']
    assert 'disabled_cells' not in plan



def test_plan_skips_disabled_offerings_and_keeps_enabled_request_names():
    bundle = {
        'provider': {'provider_id': 'p'},
        'provider_access': {'provider_access_id': 'a', 'enabled': True},
        'connections': [{'connection_id': 'c', 'enabled': True,
                         'base_url': 'https://gw.example/v1',
                         'protocol': 'openai', 'extra_headers': {}}],
        'credentials': [{'credential_id': 'k', 'kind': 'api_key',
                         'secret_reference': ''}],
        'offerings': [
            {'offering_id': 'on', 'identity_state': 'confirmed', 'enabled': True,
             'model': {'creator_id': 'm', 'model_id': 'live'},
             'capabilities': ['text']},
            {'offering_id': 'off', 'identity_state': 'confirmed', 'enabled': False,
             'model': {'creator_id': 'm', 'model_id': 'dead'},
             'capabilities': ['text']},
        ],
        'deployments': [
            {'deployment_id': 'd-live', 'offering_id': 'on', 'connection_id': 'c',
             'wire_model_id': 'live-east', 'identity_confidence': 'high',
             'probe_status': 'unprobed', 'priority': 100, 'enabled': False},
            {'deployment_id': 'd-dead', 'offering_id': 'off', 'connection_id': 'c',
             'wire_model_id': 'dead-east', 'identity_confidence': 'high',
             'probe_status': 'unprobed', 'priority': 100},
        ],
    }
    plan = matrix_probe_plan(None, None, bundle)
    assert [row['model_id'] for row in plan['models']] == ['live']
    assert plan['models'][0]['request_ids'] == ['live-east']


def test_plan_raises_when_every_confirmed_offering_is_disabled():
    bundle = _bundle([_deployment('dead')])
    bundle['offerings'][0]['enabled'] = False
    with pytest.raises(ModelRoutingError) as captured:
        matrix_probe_plan(None, None, bundle)
    assert captured.value.kind == 'provider_probe_no_offerings'

def test_fresh_probe_reports_verdict_without_deployment_enablement(tmp_path, monkeypatch):
    monkeypatch.setattr(probe, 'probe_cache_path',
                        lambda pid: str(tmp_path / (pid + '.json')))
    monkeypatch.setattr(probe, 'CELL_PROBE_TASKS', {})
    monkeypatch.setattr(probe, 'probe_cell_multi',
                        lambda *a, **kw: ('rate_limited', 'HTTP 429'))
    task = {
        'provider_id': 'request-names', 'status': 'running',
        'started_at': 0, 'finished_at': None, 'total': 2, 'done_count': 0,
        'cells': {}, 'summary': {'ok': 0, 'disable': 0},
        'error': None, '_abort': False,
        '_base_url': 'https://gw.example/v1', '_extra_headers': {},
    }
    probe.CELL_PROBE_TASKS[task['provider_id']] = task
    work = [(0, 'synthetic-key', 'root', 'root'),
            (0, 'synthetic-key', 'root', 'root-m17')]
    probe.run_cell_probe_task(task, work, timeout=5)
    assert task['status'] == 'done'
    assert task['done_count'] == 2
    assert task['summary']['disable'] == 2
    for cell in task['cells'].values():
        assert cell['status'] == 'rate_limited'
        assert cell['recommend_disable'] is True
        assert 'deployment_enabled' not in cell
    assert '_disabled_cells' not in task


@pytest.mark.parametrize('status', ['rate_limited', 'not_found'])
def test_historical_discovery_snapshots_remain_non_actionable(status):
    original = {'cells': {'0::root-m17': {
        'status': status, 'detail': 'HTTP 429' if status == 'rate_limited' else 'HTTP 404',
        'probe_surface': 'chat', 'recommend_disable': True,
        'deployment_enabled': False,
    }}}
    before = deepcopy(original)
    snapshot = probe.normalize_probe_snapshot(original)
    assert snapshot['cells']['0::root-m17']['status'] == status
    assert snapshot['cells']['0::root-m17']['recommend_disable'] is False
    assert snapshot['summary']['disable'] == 0
    assert original == before


def test_daily_apply_does_not_reinterpret_historical_discovery_failure():
    ref = {'creator_id': 'm', 'model_id': 'root'}
    credential = {'credential_id': 'k', 'authorization': {'models': [dict(ref)]}}
    bundle = {'credentials': [credential],
              'offerings': [{'identity_state': 'confirmed', 'model': ref}]}
    document = {'credentials': [deepcopy(credential)]}
    snapshot = {'status': 'done', 'total': 1, 'done_count': 1, 'cells': {
        '0::root-m17': {'key_idx': 0, 'root_model_id': 'root',
                        'model_id': 'root-m17', 'status': 'not_found',
                        'recommend_disable': True, 'deployment_enabled': False}}}
    assert probe_automation.apply_recommendations(document, bundle, snapshot) == 0
    assert document['credentials'][0]['authorization']['models'] == [ref]


def test_v2_probe_uses_each_deployments_connection_and_credential_headers(monkeypatch):
    from types import SimpleNamespace
    bundle = _bundle([_deployment('root'), _deployment('second', connection_id='c2')])
    bundle['connections'][0]['extra_headers'] = {'x-connection': 'one'}
    bundle['connections'].append({
        'connection_id': 'c2', 'enabled': True,
        'base_url': 'https://messages.example/v1', 'protocol': 'anthropic',
        'extra_headers': {'x-connection': 'two', 'x-credential': 'default'},
        'adapter': {'agent_id': 'agent-two', 'port': 9999}})
    bundle['credentials'][0]['secret_reference'] = 'secret:one'
    monkeypatch.setattr('lib.model_routing.dispatch_adapter.decode_credential_secret',
                        lambda *a, **kw: ('key-one', '', {'x-credential': 'one'}))
    plan = matrix_probe_plan(SimpleNamespace(resolve_secret=lambda *a: 'encoded'), None, bundle)
    work = probe.build_probe_work(plan['provider'], plan['models'], plan['api_keys'],
                                 request_contexts=plan['request_contexts'],
                                 credential_contexts=plan['credential_contexts'])
    captured = {}
    def capture(base_url, api_key, model_id, headers, timeout, **kwargs):
        captured[model_id] = (base_url, api_key, headers, kwargs)
        return 'ok', 'generated text'
    monkeypatch.setattr(probe, 'probe_cell_multi', capture)
    monkeypatch.setattr(probe, 'persist_probe_task', lambda task: True)
    task = dict(provider_id='unit', status='running', total=len(work), done_count=0,
                cells={}, summary={}, error=None, started_at=0, finished_at=None,
                attempts=1, _base_url=plan['provider']['base_url'],
                _protocol=plan['provider']['protocol'], _extra_headers={}, _owner_user_id=23)
    probe.run_cell_probe_task(task, work, 5)
    assert task['status'] == 'done'
    assert task['done_count'] == 2
    assert captured['root'][:3] == ('https://gw.example/v1', 'key-one',
                                     {'x-connection': 'one', 'x-credential': 'one'})
    assert captured['second'][:3] == ('https://messages.example/v1', 'key-one',
                                       {'x-connection': 'two', 'x-credential': 'one'})
    assert captured['second'][3]['protocol'] == 'anthropic'
    assert captured['second'][3]['adapter'] == {'agent_id': 'agent-two', 'port': 9999}
    assert captured['second'][3]['owner_user_id'] == 23
    public = probe.public_probe_snapshot(task)
    assert 'key-one' not in str(public)
    assert 'x-credential' not in str(public)


def test_disabled_connection_wire_is_not_probed_on_another_connection():
    bundle = _bundle([_deployment('root'), _deployment('off', connection_id='off')])
    bundle['connections'].append({'connection_id': 'off', 'enabled': False,
                                  'base_url': 'https://off.example', 'protocol': 'responses'})
    plan = matrix_probe_plan(None, None, bundle)
    work = probe.build_probe_work(plan['provider'], plan['models'], plan['api_keys'],
                                 request_contexts=plan['request_contexts'],
                                 credential_contexts=plan['credential_contexts'])
    assert [item[3] for item in work] == ['root']


def test_unreadable_credential_aborts_plan_instead_of_probing_blank_key():
    from types import SimpleNamespace
    bundle = _bundle([_deployment('root')])
    bundle['credentials'][0]['secret_reference'] = 'missing-reference'
    def fail(*args):
        raise ModelRoutingError('secret unavailable', kind='model_secret_unavailable')
    with pytest.raises(ModelRoutingError, match='secret unavailable'):
        matrix_probe_plan(SimpleNamespace(resolve_secret=fail), None, bundle)


def test_mixed_oauth_and_api_credentials_keep_their_own_auth_mode(monkeypatch):
    from types import SimpleNamespace
    bundle = _bundle([_deployment('root')])
    bundle['credentials'] = [
        {'credential_id': 'oauth', 'kind': 'oauth', 'secret_reference': 'oauth'},
        {'credential_id': 'key', 'kind': 'api_key', 'secret_reference': 'key'}]
    monkeypatch.setattr('lib.model_routing.dispatch_adapter.decode_credential_secret',
        lambda secret, **kw: ('', 'codex', {'x-account': 'one'}) if secret == 'oauth'
        else ('plain-key', '', {'x-account': 'two'}))
    plan = matrix_probe_plan(SimpleNamespace(resolve_secret=lambda b, ref: ref), None, bundle)
    work = probe.build_probe_work(plan['provider'], plan['models'], plan['api_keys'],
                                 request_contexts=plan['request_contexts'],
                                 credential_contexts=plan['credential_contexts'])
    assert [(w[1], w[7]['oauth'], w[7]['extra_headers']) for w in work] == [
        ('oauth-managed', 'codex', {'x-account': 'one'}),
        ('plain-key', '', {'x-account': 'two'})]
