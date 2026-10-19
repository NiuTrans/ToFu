"""Real app/browser workspace isolation; no real provider call or user files."""
from __future__ import annotations

import uuid

import pytest

pytestmark = [pytest.mark.visual, pytest.mark.auth_mode('open')]


def test_workspace_navigation_with_draft_never_restores_global_project(browser, live_server, tmp_path):
    from playwright.sync_api import expect
    from tests.conftest import _dismiss_onboarding_modals

    context = browser.new_context(service_workers='block', viewport={'width': 1280, 'height': 800})

    # This journey is an existing user's workspace, not first-run setup.
    context.add_init_script("localStorage.setItem('tofu_onboarding_v1_done', '1')")
    page = context.new_page()
    prefix = 'workspace-browser-' + uuid.uuid4().hex[:10]
    first, second = prefix + '-a', prefix + '-b'
    root_a, root_b = tmp_path / 'project-a', tmp_path / 'project-b'
    root_a.mkdir(); root_b.mkdir()
    errors, project_writes, submissions = [], [], []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('request', lambda request: project_writes.append(request.url)
            if request.method == 'POST' and '/project/set' in request.url else None)

    def seed(cid, root):
        response = context.request.post(f'{live_server}/api/v4/conversations/{cid}/turns/settled', data={
            'commandId': cid + '-seed', 'actor': 'human', 'kind': 'text',
            'projection': {'content': f'Workspace context {cid}'},
            'conversation': {'allowCreate': True, 'title': cid, 'createdAt': 100,
                'settings': {'projectPath': str(root), 'projectPaths': [str(root)],
                             'readOnlyPaths': [], 'chatMode': 'studio', 'model': 'gpt-4o'}},
        })
        assert response.status == 200, response.text()

    def capture_send(route):
        if route.request.method == 'POST':
            submissions.append(route.request.post_data_json)
            route.abort()  # No accepted attempt/provider call is allowed in this UI test.
        else:
            route.continue_()

    try:
        seed(first, root_a); seed(second, root_b)
        page.goto(live_server, wait_until='domcontentloaded')
        _dismiss_onboarding_modals(page)
        expect(page.locator(f'.conv-item[data-conv-id="{first}"]')).to_have_count(1, timeout=20000)
        page.locator(f'.conv-item[data-conv-id="{first}"]').click(position={'x': 12, 'y': 12})
        expect(page.locator('#chatInner')).to_contain_text(f'Workspace context {first}', timeout=20000)
        page.locator('#userInput').fill('unsent draft must not select a project')
        page.locator(f'.conv-item[data-conv-id="{second}"]').click(position={'x': 12, 'y': 12})
        expect(page.locator('#chatInner')).to_contain_text(f'Workspace context {second}', timeout=20000)
        expect(page.locator('#chatModeMenu')).to_have_count(0)
        expect(page.locator('#chatModeToggle')).not_to_have_attribute('aria-haspopup', 'menu')
        # Config/onboarding can finish after the initial cold conversation.
        _dismiss_onboarding_modals(page)
        # Exercise the demand-loaded picker: its eager-owner ports must resolve.
        page.locator('#chatModeToggle').click()
        expect(page.locator('#projectModal')).to_have_class(__import__('re').compile(r'.*open.*'))
        page.keyboard.press('Escape')
        expect(page.locator('#projectModal')).not_to_have_class(__import__('re').compile(r'.*open.*'))
        page.route(f'**/api/v4/conversations/{second}/turns', capture_send)
        page.locator('#userInput').fill('Inspect this project')
        with page.expect_request(lambda request: request.method == 'POST'
                and request.url.endswith(f'/api/v4/conversations/{second}/turns'), timeout=15000) as submitted:
            page.locator('#sendBtn').click()
        config = submitted.value.post_data_json['config']
        assert config['projectPath'] == str(root_b)
        assert config['projectPaths'] == [str(root_b)]
        assert config['readOnlyPaths'] == []
        assert config['chatMode'] == 'chat'
        assert project_writes == []
        assert errors == []
    finally:
        for cid in (first, second):
            context.request.delete(f'{live_server}/api/v4/conversations/{cid}')
        context.close()
