"""Real current-build folder/settings journey; only upstream model is stubbed."""
from pathlib import Path
import re
import pytest
from tests.e2e.test_e2e_smoke import (
    _disable_open_mode_rate_limit, _install_llm_stubs, _wait_app_ready, _select_stub_model, _SENTINEL,
)

pytestmark = pytest.mark.visual


def _dialog(page):
    return page.locator('dialog:modal').last


def _name(dialog):
    return dialog.locator('.folder-settings-identity input')


def _save(dialog):
    return dialog.locator('button[type=submit]')


def _list(page):
    response = page.request.get(f'{page.url.rstrip("/")}/api/v4/folders')
    assert response.ok, response.text()
    return response.json()['data']['items']


def _open_create(page):
    page.locator('#folderTabs .folder-tab-add:visible, #folderQuickAdd:visible').first.click()
    _dialog(page).wait_for(state='visible')
    return _dialog(page)


def test_name_only_creation_failed_submit_retry_and_pending_path(page, monkeypatch, tmp_path, assert_no_js_errors):
    import routes.api_v1.folders as owner
    monkeypatch.setattr(owner, '_FOLDERS_PATH', str(tmp_path / 'folders.json'))
    _wait_app_ready(page)
    dialog = _open_create(page)
    _name(dialog).fill('Folder retry journey')
    route = '**/api/v4/folders'
    failed = []
    def fail_once(intercept):
        if intercept.request.method == 'POST' and not failed:
            failed.append(True)
            intercept.fulfill(status=503, content_type='application/json', body='{"error":{"message":"Injected save unavailable"}}')
        else: intercept.continue_()
    page.route(route, fail_once)
    _save(dialog).click()
    page.wait_for_function("document.querySelector('.folder-settings-error:not([hidden])')")
    assert _name(dialog).input_value() == 'Folder retry journey'
    assert _save(dialog).is_enabled() and _list(page) == []

    expected_resource_error = 'console.error: Failed to load resource: the server responded with a status of 503 (Service Unavailable)'
    errors = page._tofu_js_errors
    assert errors.count(expected_resource_error) == 1 and len(failed) == 1
    errors.remove(expected_resource_error)  # only this test's deliberate HTTP fault; pageerrors stay fatal
    page.unroute(route, fail_once)
    _save(dialog).click()
    page.wait_for_function("!document.querySelector('dialog:modal')")
    folder = next(row for row in _list(page) if row['name'] == 'Folder retry journey')
    assert folder['projectPaths'] == [] and folder['readOnlyPaths'] == []
    page.locator(f'.folder-tab[data-folder-id="{folder["id"]}"]').dblclick()
    dialog = _dialog(page)
    root = tmp_path / 'workspace'; root.mkdir()
    dialog.locator('.folder-settings-path-field input').fill(str(root))
    # Save must stage typed text even without clicking Add.
    _save(dialog).click()
    page.wait_for_function("!document.querySelector('dialog:modal')")
    saved = next(row for row in _list(page) if row['id'] == folder['id'])
    assert saved['projectPaths'] == [str(root)] and saved['revision'] == 2


def test_folder_defaults_first_send_readonly_and_existing_chat_isolation(page, monkeypatch, tmp_path, assert_no_js_errors):
    import routes.api_v1.folders as owner
    monkeypatch.setattr(owner, '_FOLDERS_PATH', str(tmp_path / 'defaults.json'))
    _wait_app_ready(page)
    root = tmp_path / 'primary'; root.mkdir()
    reference = tmp_path / 'reference'; reference.mkdir()
    dialog = _open_create(page)
    _name(dialog).fill('Workspace inheritance')
    field = dialog.locator('.folder-settings-path-field input')
    field.fill(str(root)); field.press('Enter')
    field.fill(str(reference)); field.press('Enter')
    dialog.locator('.folder-settings-root').nth(1).locator('input[type=checkbox]').check()
    _save(dialog).click()
    page.wait_for_function("!document.querySelector('dialog:modal')")
    folder = next(row for row in _list(page) if row['name'] == 'Workspace inheritance')
    page.locator(f'.folder-tab[data-folder-id="{folder["id"]}"]').click()
    page.locator('.new-chat-btn').click(); _select_stub_model(page)
    calls_before = _SENTINEL['stream_calls']
    page.locator('#userInput').fill('Folder defaults browser journey')
    page.locator('#sendBtn').click()
    page.wait_for_function("document.querySelector('#chatInner')?.innerText.includes('stubbed model')")

    assert _SENTINEL['stream_calls'] > calls_before
    page.wait_for_function("window.conversations?.some(c => c.id === window.activeConvId)")
    captured = page.evaluate("() => { const c = window.conversations.find(c => c.id === window.activeConvId); return {id:c.id,projectPath:c.projectPath,projectPaths:c.projectPaths,readOnlyPaths:c.readOnlyPaths,folderId:c.folderId}; }")
    assert captured['folderId'] == folder['id'] and captured['projectPath'] == str(root)
    assert captured['projectPaths'] == [str(root), str(reference)]
    assert captured['readOnlyPaths'] == [str(reference)]
    page.locator(f'.folder-tab[data-folder-id="{folder["id"]}"]').dblclick()
    dialog = _dialog(page)
    for _ in range(2): dialog.locator('.folder-settings-root').first.locator('button').last.click()
    _save(dialog).click(); page.wait_for_function("!document.querySelector('dialog:modal')")
    after = page.evaluate("() => { const c = window.conversations.find(c => c.id === window.activeConvId); return {projectPaths:c.projectPaths,readOnlyPaths:c.readOnlyPaths}; }")
    assert after['projectPaths'] == captured['projectPaths'] and after['readOnlyPaths'] == captured['readOnlyPaths']


@pytest.mark.parametrize('theme,width', [('dark', 1280), ('light', 1280), ('tofu', 1280), ('dark', 390), ('light', 390), ('tofu', 390)])
def test_folder_settings_theme_focus_and_narrow_layout(page, theme, width, assert_no_js_errors):

    recent_paths = ['/mnt/workspace/' + ('long-parent/' * 8) + name for name in ('tofu', 'llm-mcp', 'lumix', 'autoharness')]
    page.route('**/api/v4/project/recent', lambda route: route.fulfill(
        json={'data': {'projects': [{'path': path} for path in recent_paths]}}))
    page.evaluate('(theme) => { localStorage.setItem("claude_ui_theme", theme); localStorage.setItem("tofu_ui_lang", "en"); }', theme)
    page.reload(); _wait_app_ready(page)
    page.wait_for_function('(theme) => document.documentElement.dataset.theme === theme', arg=theme)
    page.set_viewport_size({'width': 1280, 'height': 900})
    dialog = _open_create(page)
    assert 'folder.createTitle' not in dialog.inner_text()
    page.set_viewport_size({'width': width, 'height': 900})
    _name(dialog).fill('Example workspace')

    path = dialog.locator('.folder-settings-path-field input')
    path.fill('/workspace/primary/shared'); path.press('Enter')
    path.fill('/workspace/reference/shared'); path.press('Enter')
    dialog.locator('.folder-settings-root').nth(1).locator('input[type=checkbox]').check()
    dialog.locator('.folder-settings-recents summary').click()
    cards = dialog.locator('.folder-settings-recent-list button')
    cards.first.wait_for(state='visible')
    assert cards.count() == 4
    assert cards.first.get_attribute('aria-label') == recent_paths[0]
    assert cards.first.locator('.folder-settings-recent-name').inner_text() == 'tofu'
    assert cards.first.locator('.folder-settings-recent-path').evaluate('(el) => getComputedStyle(el).whiteSpace') == 'nowrap'
    layout = dialog.evaluate('''(d) => {
      const workspace = d.querySelector('.folder-settings-workspace').getBoundingClientRect();
      const recents = d.querySelector('.folder-settings-recents').getBoundingClientRect();
      const save = d.querySelector('button[type=submit]').getBoundingClientRect();
      return { sideBySide: recents.left >= workspace.right, saveTop: save.top, saveBottom: save.bottom,
        border: parseFloat(getComputedStyle(d).borderTopWidth), background: getComputedStyle(d).backgroundColor };
    }''')
    assert layout['sideBySide'] == (width > 700)
    assert layout['border'] == 3
    assert layout['background'] != 'rgba(0, 0, 0, 0)'
    assert layout['saveTop'] >= 0 and layout['saveBottom'] <= 900
    geometry = dialog.evaluate('(d) => ({width:d.getBoundingClientRect().width,scroll:d.scrollWidth,client:d.clientWidth,focus:d.contains(document.activeElement),named:!!document.getElementById(d.getAttribute("aria-labelledby"))})')
    assert geometry['width'] <= width and geometry['scroll'] <= geometry['client'] + 1
    assert geometry['focus'] and geometry['named']

    assert dialog.evaluate('(d) => [...d.querySelectorAll("button[hidden]")].every(button => getComputedStyle(button).display === "none" && button.getClientRects().length === 0)')
    dialog.evaluate('(d) => { d.scrollTop = 0; d.querySelector(".ui-dialog__body").scrollTop = 0; }')
    out = Path(__file__).resolve().parents[2] / 'output/playwright/folder-settings'
    out.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(out / f'{theme}-{width}.png'))
    page.keyboard.press('Escape')
    assert page.locator('dialog:modal').count() == 0


def test_directory_browser_selects_acknowledged_directory_without_binding_active_chat(page, monkeypatch, tmp_path, assert_no_js_errors):
    import routes.api_v1.folders as owner
    monkeypatch.setattr(owner, '_FOLDERS_PATH', str(tmp_path / 'browser.json'))
    _wait_app_ready(page)
    dialog = _open_create(page); _name(dialog).fill('Browse directory journey')
    directory = tmp_path / 'selectable'; directory.mkdir()
    (directory / 'child').mkdir()
    dialog.locator('.folder-settings-actions').first.locator('button').nth(1).click()
    browser = dialog.locator('.folder-settings-browser')
    location = browser.locator('input[type=text]'); location.fill(str(directory))
    location.press('Enter')
    try:
        browser.get_by_role('button', name='child', exact=True).wait_for(state='visible')
    except Exception as error:
        raise AssertionError({'location': location.input_value(), 'browser': browser.inner_text()}) from error
    use = browser.locator('.folder-settings-actions button').nth(3)
    assert use.is_enabled()
    use.click()
    assert dialog.locator('.folder-settings-root').first.get_attribute('data-path') == str(directory)
    assert _list(page) == []  # selection is a local editor choice, not a write
    _save(dialog).click(); page.wait_for_function("!document.querySelector('dialog:modal')")
    assert _list(page)[0]['projectPaths'] == [str(directory)]
