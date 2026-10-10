"""Search settings render the real registry through the lazy feature owner."""
from urllib.parse import urljoin
import pytest
from tests.e2e.test_e2e_smoke import (
    _disable_open_mode_rate_limit, _install_llm_stubs, _wait_app_ready,
)

pytestmark = [pytest.mark.visual]


def test_search_settings_loads_site_pack_registry(page, assert_no_js_errors):
    _wait_app_ready(page)
    page.add_locator_handler(page.locator('#onboardingModal.open'),
        lambda overlay: overlay.locator('#obCloseX').click())
    response = page.request.get(urljoin(page.url, '/api/v4/site-packs'))
    assert response.ok
    body = response.json()
    packs = body.get('packs', body.get('data', {}).get('packs', []))
    assert packs, 'the shipped core registry must not be empty'
    page.locator('[data-tofu-action="openSettings()"]').first.click()
    page.wait_for_selector('#settingsModal.open', timeout=10000)
    page.locator('.settings-tab[data-tab="search"]').click()
    disclosure = page.locator('details.settings-disclosure').filter(
        has=page.locator('#sitePacksList'))
    if disclosure.get_attribute('open') is None:
        disclosure.locator('summary').click()
    for pack in packs:
        page.locator('#sitePacksList').get_by_text(
            f"{pack['name']} · {pack['id']}", exact=True).wait_for(timeout=15000)
    assert page.locator('#sitePacksList .site-pack-row').count() == len(packs)
