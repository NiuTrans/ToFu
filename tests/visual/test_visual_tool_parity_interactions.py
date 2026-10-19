"""Reviewed wire-parity changes remain usable through the real app actions."""
from __future__ import annotations

import pytest

from tests.frontend.test_frontend_tool_rounds_wire_parity import _run_harness

pytestmark = pytest.mark.visual


def test_reviewed_tool_parity_disclosures_and_navigation(browser, live_server, tmp_path):
    from playwright.sync_api import expect

    context = browser.new_context(service_workers='block')
    context.add_init_script("localStorage.setItem('tofu_onboarding_v1_done','1');")
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        page.goto(live_server, wait_until='domcontentloaded')
        page.wait_for_function('typeof renderToolRoundsHTML === "function"')
        names = ['search-results', 'search-multiq-vertical', 'badge-tokens',
                 'badge-fetchedchars', 'compaction-L1', 'error-failed-tool',
                 'error-failed-tool-long-multiline']
        rows = {row['name']: row['html'] for row in _run_harness()}
        page.evaluate('''rows => {
            const host = document.createElement('section');
            host.id = 'parity-interactions';
            for (const [name, html] of rows) {
                const slot = document.createElement('section');
                slot.dataset.parityCase = name;
                slot.innerHTML = html;
                const header = slot.querySelector('.ptool-results-header');
                if (header) {
                    const anchor = document.createElement('a');
                    anchor.className = 'ri-tool-anchor';
                    anchor.href = '#tool-card-navigation-check';
                    anchor.innerHTML = '<span>Navigate without folding</span>';
                    header.appendChild(anchor);
                }
                host.appendChild(slot);
            }
            document.body.appendChild(host);
        }''', [[name, rows[name]] for name in names])
        for name in names[:5]:
            slot = page.locator(f'[data-parity-case="{name}"]')
            header = slot.locator('.ptool-results-header')
            for opened in (False, True):
                if opened:
                    header.locator('.ptool-text').click()
                expect(header).to_have_attribute('aria-expanded', str(opened).lower())
                page.evaluate("history.replaceState(null, '', location.pathname + location.search)")
                slot.locator('.ri-tool-anchor span').click()
                page.wait_for_function("location.hash === '#tool-card-navigation-check'")
                expect(header).to_have_attribute('aria-expanded', str(opened).lower())
                assert header.evaluate('node => node.parentElement.classList.contains("expanded")') == opened
        engines = page.locator('[data-parity-case="search-multiq-vertical"] .eb-toggle')
        engines.click()
        expect(engines).to_have_attribute('aria-expanded', 'true')
        engines.click()
        expect(engines).to_have_attribute('aria-expanded', 'false')
        for name, evidence in [
            ('error-failed-tool', 'Tool execution timed out: get_conversation'),
            ('error-failed-tool-long-multiline', '3 running trials, 12 queued trials across 2 searchers.'),
        ]:
            slot = page.locator(f'[data-parity-case="{name}"]')
            details = slot.locator('details')
            assert not details.evaluate('node => node.open')
            details.locator(':scope > summary').click()
            expect(details.locator('code')).to_be_visible()
            expect(details.locator('code')).to_contain_text(evidence)
            details.locator(':scope > summary').click()
            assert not details.evaluate('node => node.open')
        page.screenshot(path=str(tmp_path / 'reviewed-tool-parity-interactions.png'), full_page=True)
        assert not errors, errors
    finally:
        context.close()
