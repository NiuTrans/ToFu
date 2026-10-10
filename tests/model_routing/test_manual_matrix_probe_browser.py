"""Real Settings clicks probe draft request names without saving or granting.

Serve the production frontend from live_server; isolate only HTTP authority and
probe responses (no provider traffic). Completion goes through the real poller.
"""
from copy import deepcopy
import json
from urllib.parse import urlparse

import pytest

pytestmark = pytest.mark.visual


def test_manual_detection_preserves_disabled_and_unsaved_settings(browser, live_server, tmp_path):
    from playwright.sync_api import expect
    from lib.model_routing import normalize_document
    from tests.model_routing.test_model_routing_contract import _full_document

    document = _full_document()
    document['offerings'][0]['enabled'] = False
    document['credentials'][0]['authorization']['models'] = []
    document = normalize_document(document)
    original = deepcopy(document)
    state = {'starts': [], 'writes': [], 'reads': 0, 'snapshot': {'status': 'none'}}
    context = browser.new_context(viewport={'width': 1440, 'height': 1100}, service_workers='block')
    config = {'model': 'beta', 'modelRef': {'creator_id': 'creator', 'model_id': 'beta'},
              'preferredProviderId': '', 'thinkingDepth': 'medium'}
    context.add_init_script('localStorage.setItem("claude_client_config", '+json.dumps(json.dumps(config))+');')

    def route_request(route):
        request = route.request
        url = urlparse(request.url)
        if url.netloc != urlparse(live_server).netloc:
            return route.abort()
        if request.method == 'PUT':
            state['writes'].append(url.path)
            return route.fulfill(status=500, json={'error': 'Detect must not save'})
        if url.path == '/api/v4/model-routing':
            state['reads'] += 1
            return route.fulfill(json={'ok': True, 'revision': 1, 'model_routing': document,
                                       'metadata_inheritance_supported': True})
        if url.path.endswith('/probe-cells/start'):
            body = request.post_data_json
            state['starts'].append(body)
            wire = body['only']['model_ids'][0]
            state['snapshot'] = {'status': 'done', 'probe_schema_version': 3,
                'total': 1, 'done_count': 1, 'summary': {'ok': 1, 'disable': 0},
                'cells': {'0::' + wire: {'status': 'ok', 'detail': 'HTTP 200; valid content',
                    'recommend_disable': False, 'http_status': 200, 'content_validated': True,
                    'model_id': wire, 'root_model_id': 'alpha', 'key_idx': 0}}}
            return route.fulfill(json={'status': 'running', 'cells': {}, 'total': 1, 'done_count': 0})
        if url.path.endswith('/probe-cells/status'):
            return route.fulfill(json=state['snapshot'])
        if url.path == '/api/v4/model-intelligence/aa':
            return route.fulfill(json={'ok': True, 'aa': {'status': 'ok', 'scores': {}}})
        if url.path == '/api/v4/update/check':
            return route.fulfill(json={'update_available': False})
        if '/chat' in url.path and request.method == 'POST':
            raise AssertionError('Unexpected provider traffic')
        return route.continue_()

    context.route('**/*', route_request)
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        page.goto(live_server, wait_until='domcontentloaded')
        page.wait_for_function('window.TofuModules && window.TofuModules.version === 3')
        page.wait_for_selector('#presetDropdownList [data-value="beta"]', state='attached')
        if page.locator('#obSkip').is_visible():
            page.locator('#obSkip').click()
        page.locator('[data-tofu-action="openSettings()"]').first.click()
        page.locator('.settings-tab[data-tab="api"]').click()
        provider = page.locator('.stg-provider-card-v2[data-provider-id="provider-a"]')
        if not provider.evaluate('(el) => el.open'):
            provider.locator('> summary').click()
        modal = page.locator('#settingsModal')
        root = provider.locator('.is-logical[data-id="alpha"]')
        toggle = root.locator('.stg-mx-gtoggle input')
        expect(toggle).not_to_be_checked()
        reads_before = state['reads']

        def detect(wire, *, enabled, expected_wires):
            row = provider.locator('.is-alias[data-id="' + wire + '"]')
            expect(row.locator('.stg-mx-cell.off')).to_have_count(1)
            row.hover()
            expect(row.locator('.stg-mx-zap.row')).to_be_enabled()
            with page.expect_response(lambda r: urlparse(r.url).path.endswith('/probe-cells/status')
                                      and r.json().get('status') == 'done'):
                with page.expect_request(lambda r: urlparse(r.url).path.endswith('/probe-cells/start')):
                    row.locator('.stg-mx-zap.row').click()
            expect(row.locator('.stg-mx-probe-pip')).to_be_visible(timeout=15000)
            expect(row.locator('.stg-mx-zap.row')).to_be_enabled()
            expect(modal).to_be_visible()
            expect(toggle).to_be_checked(checked=enabled)
            # Success is observational: the disabled credential grant remains off.
            expect(row.locator('.stg-mx-cell.off')).to_have_count(1)
            assert state['reads'] == reads_before, 'Probe completion must not reload routing authority'
            assert state['writes'] == [], 'Detect must never PUT Settings or routing authority'
            command = state['starts'][-1]
            assert command['only'] == {'model_ids': [wire]}
            assert command['force'] is False
            assert command['credential_ids'] == ['credential-a']
            assert {target['wire_model_id'] for target in command['targets']} == set(expected_wires)
            assert all(set(target) == {'offering_id', 'connection_id', 'wire_model_id'}
                       for target in command['targets'])

            saved_offerings = {item['offering_id'] for item in original['offerings']}
            saved_connections = {item['connection_id'] for item in original['connections']}
            assert all(item['offering_id'] in saved_offerings and item['connection_id'] in saved_connections
                       for item in command['targets'])
            assert set(command) == {'targets', 'credential_ids', 'attempts', 'force', 'only'}
            assert document == original

        detect('alpha-wire-a', enabled=False, expected_wires=['alpha-wire-a', 'beta-wire-a'])
        # The design-system native checkbox is visually hidden; click its track.
        root.locator('.stg-mx-gtoggle .stg-toggle-track').click()
        expect(toggle).to_be_checked()
        detect('alpha-wire-a', enabled=True, expected_wires=['alpha-wire-a', 'beta-wire-a'])

        wire = provider.locator('.is-alias[data-id="alpha-wire-a"]')
        wire.get_by_role('button', name='alpha-wire-a', exact=True).click()
        page.locator('.app-dialog-input').fill('renamed-wire')
        page.locator('.app-dialog-input').press('Enter')
        detect('renamed-wire', enabled=True, expected_wires=['renamed-wire', 'beta-wire-a'])

        root.locator('.stg-mx-act.add').click()
        page.locator('.app-dialog-input').fill('new-wire')
        page.locator('.app-dialog-input').press('Enter')
        detect('new-wire', enabled=True, expected_wires=['renamed-wire', 'new-wire', 'beta-wire-a'])
        expect(provider.locator('.is-alias[data-id="renamed-wire"]')).to_be_visible()
        expect(provider.locator('.is-alias[data-id="new-wire"]')).to_be_visible()
        assert len(state['starts']) == 4
        assert errors == []
        page.screenshot(path=str(tmp_path / 'manual-matrix-draft-preserved.png'))
    finally:
        context.close()
