"""Folder rail journeys: nested add targets, double-click editing and geometry.

Uses the real served bundle/CSS and isolated live_server; no source-text guards.
"""
from pathlib import Path

import pytest

from tests.e2e.test_e2e_smoke import _wait_app_ready
from tests.e2e.test_folder_settings import _dialog, _list, _name, _open_create, _save

pytestmark = pytest.mark.visual


@pytest.fixture()
def rail_page(page, monkeypatch, tmp_path):
    import routes.api_v1.folders as owner
    monkeypatch.setattr(owner, '_FOLDERS_PATH', str(tmp_path / 'rail.json'))
    page.set_viewport_size({'width': 1280, 'height': 900})
    page.evaluate("localStorage.setItem('tofu_project_rail_collapsed', '0')")
    page.reload()
    _wait_app_ready(page)
    return page


def _create(page, name):
    dialog = _open_create(page)
    _name(dialog).fill(name)
    _save(dialog).click()
    page.wait_for_function("!document.querySelector('dialog:modal')")
    folder = next(row for row in _list(page) if row['name'] == name)
    page.locator(f'.folder-tab[data-folder-id="{folder["id"]}"]').wait_for(state='visible')
    return folder


def _compact(page, compact):
    if ('rail-collapsed' in (page.locator('#sidebar').get_attribute('class') or '')) != compact:
        page.locator('#folderTabs .project-rail-collapse').click()
    page.wait_for_function(
        '(compact) => document.querySelector("#sidebar").classList.contains("rail-collapsed") === compact',
        arg=compact,
    )


@pytest.mark.parametrize('compact', [False, True], ids=['expanded', 'compact'])
@pytest.mark.parametrize('target', ['svg', '.folder-tab-dot'], ids=['svg', 'inner-tile'])
def test_add_nested_click_opens_creation(rail_page, compact, target, assert_no_js_errors):
    page = rail_page
    folder = _create(page, 'Nested click anchor')
    _compact(page, compact)
    page.locator('#folderTabs .folder-tab-add').locator(target).click(timeout=5000)
    dialog = _dialog(page)
    dialog.wait_for(state='visible', timeout=5000)
    assert _name(dialog).input_value() == ''
    assert page.locator(f'.folder-tab[data-folder-id="{folder["id"]}"]').evaluate(
        '(tab) => tab.classList.contains("active")'
    )
    _name(dialog).fill('Nested click created')
    _save(dialog).click()
    page.wait_for_function("!document.querySelector('dialog:modal')")
    assert {row['name'] for row in _list(page)} == {'Nested click anchor', 'Nested click created'}


@pytest.mark.parametrize('compact', [False, True], ids=['expanded', 'compact'])
def test_double_click_edits_clicked_folder(rail_page, compact, assert_no_js_errors):
    page = rail_page
    folder = _create(page, 'Edit this folder')
    _create(page, 'Do not edit this folder')
    _compact(page, compact)
    tile = page.locator(f'.folder-tab[data-folder-id="{folder["id"]}"] .folder-tab-dot')
    tile.click()
    assert page.locator('dialog:modal').count() == 0  # single click only selects
    tile.dblclick()
    dialog = _dialog(page)
    dialog.wait_for(state='visible', timeout=5000)
    assert _name(dialog).input_value() == 'Edit this folder'
    _name(dialog).fill('Edited via double click')
    _save(dialog).click()
    page.wait_for_function("!document.querySelector('dialog:modal')")
    saved = {row['id']: row['name'] for row in _list(page)}
    assert saved[folder['id']] == 'Edited via double click'
    assert 'Do not edit this folder' in saved.values()


@pytest.mark.parametrize('theme', ['dark', 'light', 'tofu'])
def test_compact_uniform_tiles_without_gear_rows(rail_page, theme, assert_no_js_errors):
    page = rail_page
    _create(page, 'A')
    _create(page, 'Long workspace label to exercise compact layout')
    page.evaluate('(theme) => localStorage.setItem("claude_ui_theme", theme)', theme)
    page.reload()
    _wait_app_ready(page)
    page.wait_for_function('(theme) => document.documentElement.dataset.theme === theme', arg=theme)
    _compact(page, True)
    geometry = page.locator('#folderTabs').evaluate('''rail => {
        const visible = el => el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden' && Number(getComputedStyle(el).opacity) > 0;
        const box = el => {
            const r = el.getBoundingClientRect();
            return {x:r.x, y:r.y, width:r.width, height:r.height};
        };
        const tabs = [...rail.querySelectorAll('.folder-tab')].filter(visible);
        return {
            tabs: tabs.map(tab => ({...box(tab), tile:tab.querySelector('.folder-tab-dot') ? box(tab.querySelector('.folder-tab-dot')) : null})),
            extraControls: [...rail.querySelectorAll('button')].filter(visible)
                .filter(button => !button.matches('.folder-tab, .project-rail-collapse'))
                .map(button => ({label:button.getAttribute('aria-label'), ...box(button)})),
            scroll:rail.scrollWidth, client:rail.clientWidth,
        };
    }''')
    assert len(geometry['tabs']) == 4, geometry  # inbox, two folders, add
    assert geometry['extraControls'] == [], geometry
    assert geometry['scroll'] <= geometry['client'] + 1, geometry
    tabs = geometry['tabs']
    assert all(tab['tile'] is not None for tab in tabs), geometry
    for key in ('x', 'width', 'height'):
        values = [tab[key] for tab in tabs]
        assert max(values) - min(values) <= 1, (key, geometry)
        values = [tab['tile'][key] for tab in tabs]
        assert max(values) - min(values) <= 1, (key, geometry)
    assert all(tab['tile']['width'] > 0 and abs(tab['tile']['width'] - tab['tile']['height']) <= 1 for tab in tabs), geometry
    ordered = sorted(tabs, key=lambda tab: tab['y'])
    assert all(a['y'] + a['height'] <= b['y'] + 1 for a, b in zip(ordered, ordered[1:])), geometry
    out = Path(__file__).resolve().parents[2] / 'output/playwright/folder-rail'
    out.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(out / f'{theme}-compact.png'))


@pytest.mark.parametrize('access', ['Shift+F10', 'ContextMenu', 'right-click'])
def test_compact_context_settings_access(rail_page, access, assert_no_js_errors):
    page = rail_page
    folder = _create(page, 'Accessible settings')
    _compact(page, True)
    tab = page.locator(f'.folder-tab[data-folder-id="{folder["id"]}"]')
    if access == 'right-click':
        tab.click(button='right')
    else:
        tab.focus()
        tab.press(access)
    menu = page.get_by_role('menu')
    menu.wait_for(state='visible', timeout=5000)
    settings = menu.locator('[data-action="rename"]')
    assert settings.evaluate('(item) => item === document.activeElement')
    if access == 'right-click':
        settings.click()
    else:
        settings.press('Escape')
        assert page.get_by_role('menu').count() == 0
        assert tab.evaluate('(item) => item === document.activeElement')
        tab.press(access)
        page.get_by_role('menu').locator('[data-action="rename"]').press('Enter')
    dialog = _dialog(page)
    dialog.wait_for(state='visible', timeout=5000)
    assert _name(dialog).input_value() == 'Accessible settings'
