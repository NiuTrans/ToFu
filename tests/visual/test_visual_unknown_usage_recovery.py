"""Actual persisted native recovery refusal renders in the shared error card."""
import json
from pathlib import Path

import pytest

from tests.server.test_native_production_shutdown import (
    test_shutdown_joins_active_native_model_before_storage_release as rehearse_shutdown,
)

pytestmark = [pytest.mark.visual, pytest.mark.slow]


@pytest.mark.parametrize(('width', 'language'), [(1280, 'en'), (390, 'zh')])
def test_unknown_usage_is_localized_after_live_refusal_and_cold_restart(browser, tmp_path, width, language):
    from playwright.sync_api import expect
    from tests.conftest import _dismiss_onboarding_modals

    labels = json.loads((Path(__file__).resolve().parents[2]
        / f'frontend/src/i18n/locales/{language}.json').read_text())
    kind = 'execution_token_usage_unknown'
    page = browser.new_page(viewport={'width': width, 'height': 1000})
    page.add_init_script('if (window === window.top) localStorage.setItem("tofu_ui_lang", ' + json.dumps(language) + ')')
    page.add_init_script("window.__usageAppReady=false; window.addEventListener('tofu:app-ready',()=>{window.__usageAppReady=true})")
    errors, stages = [], []
    page.on('pageerror', lambda error: errors.append(str(error)))

    def load(conversation):
        page.wait_for_function('window.__usageAppReady === true')
        _dismiss_onboarding_modals(page)
        page.wait_for_function("typeof window.TofuModules?.resolveAction?.('loadConversation')==='function'")
        page.evaluate("id=>window.TofuModules.resolveAction('loadConversation')(id)", conversation)

    def observe(stage, client, conversation, error):
        if stage == 'before_shutdown':
            page.goto(str(client.base_url), wait_until='domcontentloaded')
            load(conversation)
            expect(page.locator('#chatInner')).to_contain_text('Partial model result')
        else:
            if stage == 'cold_replay':
                page.reload(wait_until='domcontentloaded')
                load(conversation)
            assert error['kind'] == kind and error['retryable'] is False
            card = page.locator(f'#chatInner .error-block[data-error-kind="{kind}"]')
            expect(card).to_be_visible()
            expect(card.locator('.error-block-kind')).to_have_text(labels[f'err.k.{kind}.chip'])
            expect(card.locator('.error-block-message')).to_have_text(labels[f'err.k.{kind}.title'])
            expect(card.locator('.error-block-hint')).to_have_text(labels['err.k._howToFix'] + '\n' + labels[f'err.k.{kind}.hint'])
            expect(card.locator('.error-block-recover-btn, .error-block-signin-btn')).to_have_count(0)

            expect(page.locator('[data-usage-recovery]').get_by_role('button',
                name=labels['usageRecovery.check'], exact=True)).to_be_visible()
            expect(page.locator('.activity-event__terminal-envelope')).not_to_be_visible()
            assert card.evaluate('el=>el.scrollWidth<=el.clientWidth+1')
            page.screenshot(path=str(tmp_path / f'{stage}.png'), full_page=True, animations='disabled')
        stages.append(stage)

    try:
        rehearse_shutdown(tmp_path, browser_observer=observe)
        assert stages == ['before_shutdown', 'recovery_refused', 'cold_replay']
        assert not errors, errors
        (tmp_path / 'browser-evidence.json').write_text(json.dumps({
            'width': width, 'language': language, 'stages': stages,
            'same_page_across_service_restarts': True, 'page_errors': errors,
        }, indent=2))
    except BaseException:
        if not page.is_closed():
            page.screenshot(path=str(tmp_path / 'failure.png'), full_page=True)
            (tmp_path / 'failure.html').write_text(page.content())
        raise
    finally:
        page.close()
