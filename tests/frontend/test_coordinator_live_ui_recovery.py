"""Live UI recovery and selection against an isolated project, never owner data."""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit, urlunsplit
from urllib.request import urlopen
import uuid

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.visual


def test_project_coordinator_retry_stream_proxy_and_selection(
    browser, live_server, tmp_path, monkeypatch,
):
    from tests._seed import seed_conversation, seed_coordinator_binding
    from tests.conftest import _dismiss_onboarding_modals
    import routes.api_v1.folders as folders

    monkeypatch.setattr(folders, '_FOLDERS_PATH', str(tmp_path / 'folders.json'))
    project = str(tmp_path / 'project')
    Path(project).mkdir()
    suffix = uuid.uuid4().hex[:8]
    leader = 'coordinator-live-ui-' + suffix
    workers = ['worker-live-ui-' + suffix + '-' + str(index) for index in range(3)]
    context = browser.new_context(service_workers='block', viewport={'width': 1440, 'height': 960})
    context.add_init_script("localStorage.setItem('tofu_ui_lang', 'en'); localStorage.setItem('tofu_onboarding_v1_done', '1')")
    page = context.new_page()
    folder = context.request.post(live_server + '/api/v4/folders', data={
        'name': 'Live UI fixture', 'projectPaths': [project],
    })
    assert folder.ok, folder.text()
    folder_id = folder.json()['data']['id']
    seed_conversation(leader, title='Synthetic Coordinator', settings={
        'projectPath': project, 'folderId': folder_id}, messages=[{'role': 'user', 'content': 'Coordinator'}])
    seed_coordinator_binding(project, leader)
    for index, conversation_id in enumerate(workers):
        seed_conversation(conversation_id, title=f'Synthetic worker {index}', settings={
            'projectPath': project, 'folderId': folder_id, '_coordinatorSource': leader},
            messages=[{'role': 'user', 'content': f'Worker {index}'},
                      {'role': 'assistant', 'content': f'Worker {index} answer'}])
    binding_url = '**/api/v4/project/coordination?path=' + quote(project, safe='')
    failed = []
    stream_requests = []
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('request', lambda request: stream_requests.append(request.url)
            if '/api/v4/conversation-events?' in request.url else None)
    page.route(binding_url, lambda route: failed.append(1) or route.abort() if not failed else route.fallback())
    output = Path(__file__).resolve().parents[2] / 'output/playwright/live-ui-recovery'
    output.mkdir(parents=True, exist_ok=True)
    try:
        page.goto(live_server, wait_until='domcontentloaded')
        _dismiss_onboarding_modals(page)
        page.locator(f'.folder-tab[data-folder-id="{folder_id}"]').click()
        entry = page.locator('#projectCoordinatorEntry')
        expect(entry.locator('.coordination-read-error')).to_be_visible(timeout=20000)
        expect(entry.locator('.coordination-leader-meta')).not_to_have_text('Loading…')
        page.screenshot(path=str(output / 'coordinator-retry.png'), animations='disabled')
        entry.get_by_role('button', name='Read again', exact=True).click()
        expect(entry.locator('[data-coordinator-id]')).to_have_attribute('data-coordinator-id', leader)
        expect(entry.locator('.coordination-read-error')).to_have_count(0)
        page.screenshot(path=str(output / 'coordinator-recovered.png'), animations='disabled')

        for index, conversation_id in enumerate(workers):
            toggle = page.locator('.coordinator-conversations-toggle')
            if index == 0:
                expect(toggle).to_be_visible(timeout=20000)
                toggle.click()
            else:
                expect(toggle).to_have_attribute('aria-expanded', 'true')
            row = page.locator(f'.conv-item[data-conv-id="{conversation_id}"]')
            expect(row).to_be_visible(timeout=20000)
            row.locator('.conv-title').click(position={'x': 4, 'y': 4})
            expect(row).to_have_class(__import__('re').compile(r'\bactive\b'))
            other = workers[(index + 1) % len(workers)]
            seed_conversation(other + '-update-' + str(index), title='Background update', settings={
                'projectPath': project}, messages=[{'role': 'user', 'content': 'Background update'}])
            page.wait_for_timeout(1000)
            expect(row).to_have_class(__import__('re').compile(r'\bactive\b'))
            expect(page.locator('.conv-item.active')).to_have_count(1)
        page.screenshot(path=str(output / 'selection-after-updates.png'), animations='disabled')
        assert not errors, errors

        source = next(url for url in reversed(stream_requests) if 'streams=' in url)
        parsed = urlsplit(source)
        subscriptions = parse_qs(parsed.query)['streams'][0]
        json.loads(subscriptions)
        query = '&'.join(part if not part.startswith('streams=') else 'streams=' + quote(subscriptions, safe='')
                         for part in parsed.query.split('&'))
        proxy_url = urlunsplit(parsed._replace(query=query))
        with urlopen(proxy_url, timeout=15) as response:
            assert response.status == 200
            assert response.headers.get_content_type() == 'text/event-stream'
            response.close()
    finally:
        page.unroute(binding_url)
        context.close()
