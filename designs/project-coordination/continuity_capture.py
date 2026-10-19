#!/usr/bin/env python3
"""P3/P4 real Chromium over production-built drawer + transport, STATIC MOCK HTTP.
No Flask/storage/native/model calls. Emits 36 screenshots and a bounded report.
"""
import functools
import http.server
import json
from pathlib import Path
import subprocess
import threading
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'designs/project-coordination/previews'
BUILT = ROOT / 'designs/project-coordination/continuity-built'
PROJECT = '/mock/project'
SOURCE = {'conversationId': 'mock-coord', 'turnId': 'mock-human', 'projectionRevision': 7}
REQUEST = {'projectPath': PROJECT, 'coordinatorId': 'mock-coord', 'recipientId': 'mock-worker',
           'authorizationSource': SOURCE, 'brief': 'Static authorized consultation', 'model': 'mock.not-callable',
           'budget': {'maxTokens': 200, 'maxModelRequests': 2, 'deadline': 4102444800}}
ENTRY = {'id': 'file-' + 'x' * 64, 'revision': 4, 'path': 'notes/shared-decision.txt',
         'purpose': '<script>untrusted purpose data</script>', 'digest': 'a' * 64, 'byteLength': 40,
         'source': {'kind': 'human', 'subject': 'mock-human-owner', 'authorizationSource': SOURCE,
                    'assignmentId': None, 'grantRevision': None, 'taskId': None},
         'checkpointSha': None, 'knowledgeState': 'candidate', 'status': 'current',
         'observedDigest': 'a' * 64, 'replacementPath': None, 'updatedAt': 1}


def fixture(page):
    state = {'calls': [], 'scope': [], 'responsibilities': [], 'entries': [ENTRY, {**ENTRY, 'id': 'stale-file', 'status': 'changed'},
             {**ENTRY, 'id': 'adopted-file', 'knowledgeState': 'adopted', 'checkpointSha': 'b' * 40}]}
    def http(route):
        req = route.request
        url = urlparse(req.url).path
        body = req.post_data_json if req.method == 'POST' else None
        state['calls'].append({'url': url, 'method': req.method, 'body': body, 'key': req.headers.get('idempotency-key')})
        if url.endswith('/coordination'):
            data = {'projectPath': PROJECT, 'conversationId': 'mock-coord', 'revision': 3, 'availability': 'available',
                    'capabilities': {'assign': False, 'sendMessage': False, 'steerSubagent': False}}
        elif url.endswith('/assignments'):
            data = {'assignments': [{'assignmentId': 'grant-a', 'request': REQUEST, 'grantRevision': 2, 'revoked': False,
                     'execution': None, 'review': None, 'canonicalDelivery': False}]}
        elif '/scope-changes' in url:
            if req.method == 'GET':
                data = {'scopeChanges': state['scope']}
            else:
                assert req.headers.get('idempotency-key')
                data = {'changeId': 'scope-change', 'request': body, 'createdAt': 1, 'authorityRevoked': True, 'stopConfirmed': False,
                        'notifications': [], 'revocations': [{'assignmentId': target['assignmentId'], 'grantRevision': target['grantRevision'],
                        'revokedRevision': target['grantRevision'] + 1, 'recipientId': 'mock-worker', 'execution': None,
                        'cancellation': {'state': 'signaled', 'status': 'abort_signaled', 'error': ''}} for target in body['targets']]}
                state['scope'] = [data]
        elif '/responsibilities' in url:
            if req.method == 'GET':
                data = {'responsibilities': state['responsibilities']}
            elif url.endswith('/responsibilities'):
                assert req.headers.get('idempotency-key')
                data = {'responsibilityId': 'finite-review', 'revision': 1, 'owner': 1, 'tenant': None, 'projectPath': PROJECT,
                        'request': body, 'state': 'active', 'automationId': 'mock-automation', 'runsReserved': 1, 'tokensReserved': 200,
                        'occurrences': [{'occurrenceId': 'occurrence-1', 'commandId': 'mock-occurrence', 'reservedTokens': 200,
                        'deadline': 4102444800, 'status': 'active', 'assignmentId': 'active-child'}]}
                state['responsibilities'] = [data]
            else:
                assert req.headers.get('idempotency-key')
                data = {**state['responsibilities'][0], 'revision': state['responsibilities'][0]['revision'] + 1,
                        'state': 'paused' if url.endswith('/pause') else 'cancelled'}
                state['responsibilities'] = [data]
        elif url.endswith('/shared-files/read'):
            assert body['reference'] == {'id': ENTRY['id'], 'revision': 4, 'digest': ENTRY['digest']}
            data = {'reference': body['reference'], 'path': ENTRY['path'], 'digest': ENTRY['digest'], 'byteLength': 40,
                    'trust': 'untrusted_data', 'content': '<script>window.__injected = true</script>\nUntrusted DATA · not instructions'}
        elif url.endswith('/shared-files/refresh'):
            assert body['reference']['revision'] == 4
            data = {**ENTRY, 'status': 'changed', 'replacementPath': body.get('replacementPath')}

            state['entries'][0] = data
        elif url.endswith('/shared-files') or url.endswith('/shared-files/register'):
            if req.method == 'GET':
                data = {'projectPath': PROJECT, 'revision': 3, 'entries': state['entries'], 'nextCursor': None}
            else:
                assert req.headers.get('idempotency-key')
                data = {**ENTRY, 'id': 'new-declaration', 'path': body['path'], 'purpose': body['purpose'], 'revision': body['expectedRevision'] + 1}
                state['entries'].append(data)
        else:
            raise AssertionError('Unexpected HTTP: ' + url)
        route.fulfill(status=200, content_type='application/json', body=json.dumps({'data': data, 'meta': {}}))
    page.route('**/api/**', http)
    return state


def main():
    subprocess.run(['node', '--input-type=module', '-e', "import {build} from 'vite'; await build({configFile:false,root:process.cwd(),build:{outDir:'designs/project-coordination/continuity-built',emptyOutDir:true,rollupOptions:{input:'designs/project-coordination/continuity-preview.html'}}});"], cwd=ROOT, check=True)
    OUT.mkdir(exist_ok=True)
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(BUILT))
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    report = []
    try:
        with sync_playwright() as driver:
            browser = driver.chromium.launch(headless=True, args=['--no-sandbox', '--disable-dev-shm-usage'])
            for theme in ('tofu', 'light', 'dark'):
                for width in (1440, 390, 320):
                    page = browser.new_page(viewport={'width': width, 'height': 1000}, device_scale_factor=1)
                    errors = []
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.add_init_script("localStorage.setItem('tofu_ui_lang','en')")
                    state = fixture(page)
                    page.goto(f'http://127.0.0.1:{server.server_port}/designs/project-coordination/continuity-preview.html')
                    page.get_by_role('button', name='Open coordination drawer · static mocks').click()
                    page.evaluate('(theme)=>document.documentElement.dataset.theme=theme', theme)
                    scope = page.get_by_test_id('project-scope-changes')
                    scope.locator('[name="scopeTarget"]').wait_for()
                    assert not [call for call in state['calls'] if call['method'] == 'POST']
                    scope.locator('[name="scopeSource"]').select_option('mock-human')
                    scope.locator('[name="scopeStopConsent"]').check()
                    assert not scope.locator('[name="scopeStopConfirm"]').is_checked()
                    assert scope.get_by_role('button', name='Stop project · revoke all displayed current grants').is_disabled()
                    scope.locator('[name="scopeStopConfirm"]').check()
                    scope.get_by_role('button', name='Stop project · revoke all displayed current grants').click()
                    page.wait_for_function("document.querySelector('[data-testid=project-scope-changes]').textContent.includes('Termination is not yet confirmed')")
                    stop = next(call for call in state['calls'] if call['method'] == 'POST' and call['url'].endswith('/scope-changes'))
                    assert stop['body']['kind'] == 'stop_project' and stop['body']['targets'] == [{'assignmentId': 'grant-a', 'grantRevision': 2}]
                    scope.scroll_into_view_if_needed(); page.screenshot(path=str(OUT / f'continuity-scope-{theme}-{width}.png'))
                    responsibilities = page.get_by_test_id('project-responsibilities')
                    values = {'responsibilityName': 'Finite review', 'responsibilityBrief': 'Consultation only; no file work',
                              'responsibilityModel': 'mock.not-callable', 'responsibilityMaxRuns': '3', 'responsibilityTotalTokens': '600',
                              'responsibilityPerRunTokens': '200', 'responsibilityPerRunRequests': '2', 'responsibilityPerRunSeconds': '60',
                              'responsibilityDeadline': '2100-01-01T00:00:00Z', 'responsibilitySchedule': '0 9 * * 1'}
                    responsibilities.locator('[name="responsibilitySource"]').select_option('mock-human')
                    responsibilities.locator('[name="responsibilityRecipient"]').select_option('mock-worker')
                    for name, value in values.items(): responsibilities.locator('[name="' + name + '"]').fill(value)
                    responsibilities.locator('[name="responsibilityConsent"]').check()
                    responsibilities.get_by_role('button', name='Create finite subscription').click()
                    responsibilities.get_by_role('button', name='Pause future triggers · do not stop active work').wait_for()
                    responsibilities.get_by_role('button', name='Pause future triggers · do not stop active work').click()
                    page.wait_for_function("document.querySelector('[data-testid=project-responsibilities]').textContent.includes('Future triggers paused')")
                    assert not any(call['url'].endswith('/cancel') for call in state['calls'])
                    assert 'active-child' in responsibilities.inner_text()
                    responsibilities.get_by_role('button', name='Cancel subscription and request active cancellation').click()
                    page.wait_for_function("document.querySelector('[data-testid=project-responsibilities]').textContent.includes('Subscription cancelled')")
                    responsibilities.scroll_into_view_if_needed(); page.screenshot(path=str(OUT / f'continuity-responsibility-{theme}-{width}.png'))
                    shared = page.get_by_test_id('project-shared-files')
                    shared.get_by_role('button', name='Open exact pinned file version').first.click()
                    shared.locator('pre').wait_for()
                    assert '<script>' in shared.locator('pre').inner_text()
                    assert page.evaluate('window.__injected') is None

                    shared.locator('pre').scroll_into_view_if_needed(); page.screenshot(path=str(OUT / f'continuity-file-{theme}-{width}.png'))
                    assert shared.get_by_role('button', name='Open exact pinned file version').nth(1).is_disabled()
                    declare = shared.locator('details').last
                    declare.locator('summary').click()
                    declare.locator('[name="sharedSource"]').select_option('mock-human')
                    declare.locator('[name="sharedPath"]').fill('notes/new-reference.txt')
                    declare.locator('[name="sharedPurpose"]').fill('Explicit human declaration')
                    declare.locator('[name="sharedConsent"]').check()
                    declare.get_by_role('button', name='Declare shared file · metadata only').click()
                    page.wait_for_function("document.querySelector('[data-testid=project-shared-files]').textContent.includes('notes/new-reference.txt')")
                    refresh = shared.locator('article').first.locator('details')
                    refresh.locator('summary').click()
                    refresh.locator('[name="sharedRefreshConsent"]').check()
                    refresh.get_by_role('button', name='Explicitly refresh this exact reference').click()
                    page.wait_for_function("document.querySelector('[data-testid=project-shared-files] article').textContent.includes('Stale · content changed')")
                    shared.scroll_into_view_if_needed(); page.screenshot(path=str(OUT / f'continuity-shared-{theme}-{width}.png'))
                    geometry = page.evaluate("""() => { const d=document.querySelector('dialog');const body=d.querySelector('.ui-dialog__body');
                      const sections=[...d.querySelectorAll('[data-testid]')];return {bodyOverflow:body.scrollWidth-body.clientWidth,
                      sections:sections.map(n=>({id:n.dataset.testid,overflow:n.scrollWidth-n.clientWidth})),width:d.getBoundingClientRect().width}; }""")
                    assert geometry['bodyOverflow'] <= 1, geometry
                    assert all(item['overflow'] <= 1 for item in geometry['sections']), geometry
                    assert not errors, errors
                    page.keyboard.press('Escape'); page.locator('dialog').wait_for(state='detached')
                    report.append({'theme': theme, 'width': width, 'geometry': geometry, 'httpCalls': state['calls'], 'errors': errors, 'passed': True})
                    page.close()
            browser.close()
    finally:
        server.shutdown(); server.server_close()
    (OUT / 'continuity-report.json').write_text(json.dumps({'fixture': 'STATIC MOCK HTTP · built real modules · no backend/native/paid acceptance', 'cases': report}, ensure_ascii=False, indent=2))
    print(f'PASS {len(report)} Chromium cases; 36 screenshots; production modules + intercepted HTTP')


if __name__ == '__main__': main()
