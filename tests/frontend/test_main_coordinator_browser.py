"""Real app + isolated storage: the main coordinator (docs/ROUTER.md R8).

One leader slot above the conversation list holds the main coordinator where
the list has no project and the project's coordinator in a project folder.
Health checks are a setting in the main coordinator's header, router chats are
never list rows and show no project location. No model or agent runs.
"""
from pathlib import Path
from urllib.parse import unquote

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.visual


def test_main_coordinator_card_header_and_health_checks(page, monkeypatch, tmp_path, assert_no_js_errors):
    from tests.conftest import _dismiss_onboarding_modals
    import routes.api_v1.folders as folders

    monkeypatch.setattr(folders, '_FOLDERS_PATH', str(tmp_path / 'folders.json'))
    monkeypatch.setattr('lib.router.home.router_home_root', lambda: str(tmp_path / 'router-homes'))
    origin = page.url.split('/?', 1)[0].rstrip('/')
    (tmp_path / 'Alpha').mkdir()
    folder = page.request.post(origin + '/api/v4/folders', data={
        'name': 'Alpha', 'projectPaths': [str(tmp_path / 'Alpha')]})
    assert folder.ok, folder.text()
    folder_id = folder.json()['data']['id']
    output = Path(__file__).resolve().parents[2] / 'output/playwright/main-coordinator'
    output.mkdir(parents=True, exist_ok=True)
    page.set_viewport_size({'width': 1440, 'height': 900})
    page.evaluate('localStorage.setItem("tofu_ui_lang", "en")')
    page.reload()
    _dismiss_onboarding_modals(page)
    card = page.locator('#globalRouterEntry .coordination-leader')
    expect(card).to_be_visible(timeout=20000)
    expect(card.locator('.coordination-leader-label')).to_have_text('Main coordinator')
    expect(page.locator('#sidebar .coordination-leader')).to_have_count(1)
    for retired in ('Global Router', 'Watchdog', 'Health checks'):
        expect(page.locator('#sidebar')).not_to_contain_text(retired)
    count_script = "document.querySelector('.folder-tab-uncat .folder-tab-count')?.textContent || ''"
    uncategorized_before = page.evaluate(count_script)
    page.screenshot(path=str(output / 'sidebar.png'), animations='disabled')

    # Every count a conversation group shows while the main coordinator is open, including mid-read.
    page.evaluate('''() => {
      const sidebar = document.querySelector('#sidebar');
      window.__groupCountsShown = [];
      window.__groupWatch = new MutationObserver(() => sidebar.querySelectorAll('.coordinator-conversations')
        .forEach(group => { if (!group.hidden) window.__groupCountsShown.push(group.textContent); }));
      window.__groupWatch.observe(sidebar, {subtree: true, childList: true, characterData: true, attributes: true});
    }''')
    with page.expect_response(lambda response: '/api/v4/project/coordination/conversations?' in response.url
                              and 'router-homes' in unquote(response.url)) as roster:
        card.click()
    assert roster.value.ok, roster.value.text()
    expect(card).to_have_attribute('aria-current', 'page', timeout=20000)
    router = page.request.get(origin + '/api/v4/router').json()['data']
    assert router['availability'] == 'available', router
    page.wait_for_function('(id) => window.activeConvId === id', arg=router['conversationId'])
    header = page.locator('.global-router-header')
    expect(header).to_be_visible()
    expect(header).to_contain_text('reach the main coordinator first')
    expect(page.locator('#topbarTitle')).to_have_text('Main coordinator')
    expect(page.locator('#projectBar')).to_be_hidden()
    expect(page.locator(f'.conv-item[data-conv-id="{router["conversationId"]}"]')).to_have_count(0)
    group = page.locator(f'#sidebar .coordinator-conversations[data-coordinator-id="{router["conversationId"]}"]')
    expect(group).to_have_count(1)

    health = header.locator('.router-watchdog')
    summary = health.locator('summary')
    expect(summary).to_have_text('Health checks: off')
    summary.click()
    health.get_by_role('button', name='Turn on health checks', exact=True).click()
    expect(summary).to_have_text('Health checks: on', timeout=20000)
    watchdog = page.request.get(origin + '/api/v4/router/watchdog').json()['data']
    assert watchdog['availability'] == 'available', watchdog
    assert page.evaluate('window.activeConvId') == router['conversationId']
    # No worker chats yet: the group stays hidden through every inventory read, so no "0" chip.
    expect(group).to_be_hidden()
    assert page.evaluate('window.__groupWatch.disconnect() || window.__groupCountsShown') == []
    page.screenshot(path=str(output / 'main-coordinator.png'), animations='disabled')

    health.get_by_role('button', name='Open the health-check chat', exact=True).click()
    page.wait_for_function('(id) => window.activeConvId === id', arg=watchdog['conversationId'])
    watchdog_header = page.locator('.router-watchdog-header')
    expect(watchdog_header).to_be_visible()
    expect(header).to_be_hidden()
    expect(page.locator('#topbarTitle')).to_have_text('Health checks')
    expect(page.locator('#projectBar')).to_be_hidden()
    expect(page.locator(f'.conv-item[data-conv-id="{watchdog["conversationId"]}"]')).to_have_count(0)
    watchdog_header.get_by_role('button', name='Back to the main coordinator', exact=True).click()
    page.wait_for_function('(id) => window.activeConvId === id', arg=router['conversationId'])
    # Both router chats are loaded now; a folder count still matches the rows it can show.
    page.wait_for_function(f'(before) => ({count_script}) === before', arg=uncategorized_before)

    page.locator(f'.folder-tab[data-folder-id="{folder_id}"]').click()
    expect(page.locator('#globalRouterEntry')).to_be_hidden()
    project_card = page.locator('#projectCoordinatorEntry .coordination-leader')
    expect(project_card.locator('.coordination-leader-label')).to_have_text('Project coordinator', timeout=20000)
    expect(page.locator('#sidebar .coordination-leader:visible')).to_have_count(1)
    page.screenshot(path=str(output / 'project-folder.png'), animations='disabled')
    page.locator('.folder-tab-uncat').click()
    expect(card).to_be_visible()

    page.evaluate('localStorage.setItem("tofu_ui_lang", "zh")')
    page.reload()
    _dismiss_onboarding_modals(page)
    expect(card.locator('.coordination-leader-label')).to_have_text('总负责人', timeout=20000)
    card.click()
    page.wait_for_function('(id) => window.activeConvId === id', arg=router['conversationId'])
    expect(page.locator('#topbarTitle')).to_have_text('总负责人')
    expect(page.locator('#projectBar')).to_be_hidden()
    expect(header.locator('.router-watchdog summary')).to_have_text('故障巡检：已开启')
    page.screenshot(path=str(output / 'main-coordinator-zh.png'), animations='disabled')
