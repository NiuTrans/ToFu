"""Real settings journeys: metadata inheritance and observational provider probes."""
from copy import deepcopy
from pathlib import Path

import pytest

from tests.model_routing.test_probe_identity import fixture, success
from tests.visual.test_visual_surfaces import _invoke_action, _wait_ready

pytestmark = [pytest.mark.visual, pytest.mark.slow]


def test_provider_metadata_inherits_context_and_labels_reference_price(page, screenshot_dir):
    from lib.model_routing import normalize_document
    from tests.model_routing.test_model_routing_contract import _full_document
    from playwright.sync_api import expect

    document = _full_document()
    document['models'][0].update(context_window=500000,
        list_pricing={'currency': 'USD', 'input': 2, 'output': 6, 'unit': 'per_million_tokens'})
    document['offerings'][0].update(context_window=32768,
        inherited_model_fields=['context_window', 'capabilities'])
    document['offerings'][0].pop('actual_pricing', None)
    document = normalize_document(document)
    page.route('**/api/v4/model-routing', lambda route: route.fulfill(json={
        'ok': True, 'revision': document['revision'], 'model_routing': document}))
    page.route('**/probe-cells/status', lambda route: route.fulfill(json={'ok': True, 'status': 'none'}))
    _wait_ready(page)
    page.add_locator_handler(page.locator('#obCloseX'), lambda locator: locator.click())
    _invoke_action(page, 'openSettings')
    _invoke_action(page, 'switchSettingsTab', 'api')
    card = page.locator('.stg-provider-card-v2[data-provider-id="provider-a"]')
    card.wait_for(state='visible')
    if not card.evaluate('(node) => node.open'):
        card.locator(':scope > summary').click()
    row = card.locator('.stg-mx-row[data-id="alpha"]')
    expect(row).to_contain_text('500K')
    expect(row).not_to_contain_text('32.8K')
    price = row.locator('.stg-mx-price')
    expect(price).to_contain_text(__import__('re').compile('参考价|Reference'))
    expect(price).to_have_attribute('title', __import__('re').compile('每百万|million'))
    expect(price).to_have_attribute('title', __import__('re').compile('尚未配置|not configured'))
    expect(row.locator('.stg-mx-meta-bit').filter(has_text='500K')).to_have_attribute(
        'title', __import__('re').compile('未通过容量探测|not been verified'))
    page.screenshot(path=str(Path(screenshot_dir) / 'inherited-model-context.png'))


@pytest.mark.parametrize('dirty', [False, True])
def test_provider_scoped_probe_preserves_pending_identity_and_draft(page, screenshot_dir, dirty):
    screenshot_dir = Path(screenshot_dir)
    document, _ = fixture()
    document['providers'] = [dict(document['providers'][0], name='CatPaw', brand='catpaw')]
    for collection in ('provider_accesses', 'connections', 'credentials', 'offerings', 'deployments'):
        document[collection] = document[collection][:1]
    document['creators'] = [{'creator_id': 'moonshot', 'name': 'moonshot'}]
    document['models'] = [dict(document['models'][0], creator_id='moonshot',
                               model_id='kimi-k3', display_name='Kimi K3')]
    document['offerings'][0]['pending_model_id'] = 'kimi-k3'
    document['deployments'][0]['wire_model_id'] = 'kimi-k3'
    private = deepcopy(document['offerings'][0])
    private.update(offering_id='private', pending_model_id='private-route')
    document['offerings'].append(private)
    deployment = deepcopy(document['deployments'][0])
    deployment.update(deployment_id='private', offering_id='private', wire_model_id='private-route')
    document['deployments'].append(deployment)
    document['revision'] = 1
    requests, writes = [], []
    authority_before = deepcopy(document)
    snapshot = {'status': 'none'}

    def read_authority(route):
        if route.request.method != 'GET':
            writes.append(route.request.method)
        route.fulfill(json={'ok': True, 'revision': document['revision'],
                            'model_routing': document})

    def start_probe(route):
        nonlocal snapshot
        requests.append(route.request.post_data_json)
        cell = success('kimi-k3', root_model_id='kimi-k3', recommend_disable=False,
                       detail='HTTP 200', http_status=200, probe_surface='chat', proof='generated_text')
        snapshot = {'status': 'done', 'total': 1, 'done_count': 1,
                    'probe_schema_version': 2, 'cells': {'0::kimi-k3': cell},
                    'summary': {'ok': 1, 'disable': 0}, 'error': None}
        route.fulfill(json={'ok': True, **snapshot})

    page.route('**/api/v4/model-routing', read_authority)
    page.route('**/api/v4/providers/provider-a/probe-cells/status',
               lambda route: route.fulfill(json={'ok': True, **snapshot}))
    page.route('**/api/v4/providers/provider-a/probe-cells/start', start_probe)
    _wait_ready(page)
    page.add_locator_handler(page.locator('#obCloseX'), lambda locator: locator.click())
    _invoke_action(page, 'openSettings')
    _invoke_action(page, 'switchSettingsTab', 'api')
    card = page.locator('.stg-provider-card-v2[data-provider-id="provider-a"]')
    card.wait_for(state='visible')
    if page.locator('#obCloseX').is_visible():
        page.locator('#obCloseX').click()
    if not card.evaluate('(node) => node.open'):
        card.locator(':scope > summary').click()
    # The logical offering and its callable wire route have distinct rows.
    root = card.locator('.stg-mx-row.is-root[data-id="kimi-k3"]')
    row = card.locator('.stg-mx-row.is-alias[data-id="kimi-k3"]')
    row.wait_for(state='visible')
    assert root.locator('.stg-mx-chip.pending').count() == 1
    assert row.locator('.stg-mx-cell.logical').count() == 0
    assert row.locator('.stg-mx-toggle').count() == 0
    if dirty:
        root.locator('.stg-mx-gtoggle').click()
        assert not root.locator('.stg-mx-gtoggle input').is_checked()
    row.hover()
    with page.expect_response('**/api/v4/providers/provider-a/probe-cells/start', timeout=10000):
        row.locator('.stg-mx-zap.row').click()
    row.locator('.stg-mx-probe-pip.ok').wait_for(state='attached')
    assert requests, getattr(page, '_tofu_js_errors', [])
    page.screenshot(path=str(screenshot_dir / ('provider-probe-dirty.png' if dirty else 'provider-probe-clean.png')))
    assert row.locator('.stg-mx-probe-pip.ok').count() == 1, card.inner_text()
    # Detection is observational even when the settings draft is clean:
    # MODEL_REGISTRATION.md forbids implicit admission, enablement or save.
    assert root.locator('.stg-mx-chip.pending').count() == 1
    assert row.locator('.stg-mx-toggle').count() == 0
    assert root.locator('.stg-mx-gtoggle input').is_checked() == (not dirty)
    assert document == authority_before
    assert not writes, writes
    assert requests == [{
        'attempts': 3, 'force': False, 'only': {'model_ids': ['kimi-k3']},
        'credential_ids': [credential['credential_id'] for credential in document['credentials']],
        'targets': [{'offering_id': deployment['offering_id'],
                     'connection_id': deployment['connection_id'],
                     'wire_model_id': deployment['wire_model_id']}
                    for deployment in document['deployments']],
    }]
