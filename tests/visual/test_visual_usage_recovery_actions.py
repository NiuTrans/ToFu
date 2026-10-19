"""Human-only recovery through real HTTP, Turn storage and the shipped UI.

Only executor startup is held: no paid model, fabricated success or old request
replay is part of this journey. The API cases own accounting/transaction detail.
"""
from copy import deepcopy
import json
from pathlib import Path

import pytest
from playwright.sync_api import expect

from tests.conversation import test_usage_recovery as recovery

# Register these fixtures locally without changing the suite's shared plugin.
_chat_sidecar_runtime = recovery._chat_sidecar_runtime
recovery_source = recovery.recovery_source
executor_starts = recovery.executor_starts

pytestmark = [pytest.mark.visual, pytest.mark.auth_mode('open')]
pytest_plugins = ('tests._chat_sidecar',)
ROOT = Path(__file__).resolve().parents[2]


def load_source(page, source, language):
    from tests.conftest import _dismiss_onboarding_modals
    page.evaluate('(lang) => localStorage.setItem("tofu_ui_lang", lang)', language)
    page.reload(wait_until='domcontentloaded')
    _dismiss_onboarding_modals(page)
    page.wait_for_function("typeof window.TofuModules?.resolveAction?.('loadConversation') === 'function'")
    page.evaluate("id => window.TofuModules.resolveAction('loadConversation')(id)", source.conversation_id)
    notice = page.locator('[data-usage-recovery]')
    expect(notice).to_be_visible()
    labels = json.loads((ROOT / f'frontend/src/i18n/locales/{language}.json').read_text())
    return notice, labels


@pytest.mark.parametrize(('width', 'language'), [(1280, 'en'), (390, 'zh')])
def test_confirmed_new_budget_preserves_old_output_and_survives_reload(
        page, recovery_source, executor_starts, assert_no_js_errors, tmp_path, width, language):
    source = recovery_source()
    original = deepcopy(source.turn())
    page.set_viewport_size({'width': width, 'height': 1000})
    notice, labels = load_source(page, source, language)
    assert executor_starts == []
    with page.expect_response(lambda r: r.request.method == 'GET' and r.url.endswith('/usage-recovery')) as response:
        notice.get_by_role('button', name=labels['usageRecovery.check'], exact=True).click()
    assert response.value.status == 200, response.value.text()
    budget = response.value.json()['budget']
    expect(notice.locator('[data-usage-recovery-budget]')).to_be_visible()

    expect(notice.get_by_role('button', name=labels['usageRecovery.confirm'], exact=True)).to_be_focused()
    for number in budget.values():
        expect(notice.locator('[data-usage-recovery-budget]')).to_contain_text(str(number))
    for key in ('usageRecovery.cost', 'usageRecovery.preserve', 'usageRecovery.scope'):
        expect(notice).to_contain_text(labels[key])
    assert executor_starts == [] and source.turn() == original
    notice.get_by_role('button', name=labels['usageRecovery.cancel'], exact=True).click()
    expect(notice).to_have_attribute('data-usage-recovery', 'idle')
    assert executor_starts == []
    notice.get_by_role('button', name=labels['usageRecovery.check'], exact=True).click()
    expect(notice).to_have_attribute('data-usage-recovery', 'ready')
    assert notice.evaluate('(el) => el.scrollWidth <= el.clientWidth + 1')
    page.screenshot(path=str(tmp_path / f'budget-confirmation-{width}-{language}.png'), full_page=True)
    with page.expect_response(lambda r: r.request.method == 'POST' and r.url.endswith('/usage-recovery')) as response:
        notice.get_by_role('button', name=labels['usageRecovery.confirm'], exact=True).click()
    assert response.value.status == 200, response.value.text()
    accepted = response.value.json()
    assert len(executor_starts) == 1
    assert source.turn() == original
    assert accepted['turn']['turnId'] != source.turn_id
    recovery._finish_success(source, accepted, executor_starts[0]['taskId'])
    expect(page.locator('#chatInner')).to_contain_text('Fresh work completed.')
    load_source(page, source, language)
    expect(page.locator('#chatInner')).to_contain_text('Fresh work completed.')
    expect(page.locator('#chatInner')).to_contain_text(original['projection']['content'])
    assert source.turn()['projection']['usageComplete'] is False
    assert source.turn()['projection']['usage'] == original['projection']['usage']
    assert len(executor_starts) == 1
    assert page.locator('body').evaluate('(el) => el.scrollWidth <= innerWidth + 1')


def test_unknown_operation_refuses_new_budget_in_the_browser(
        page, recovery_source, executor_starts, assert_no_js_errors):
    source = recovery_source(tool_rounds=[recovery._unknown_effect()])
    original = deepcopy(source.snapshot())
    notice, labels = load_source(page, source, 'zh')
    notice.get_by_role('button', name=labels['usageRecovery.check'], exact=True).click()
    expect(notice).to_have_attribute('data-usage-recovery', 'blocked')
    expect(notice).to_contain_text(labels['usageRecovery.operationUnconfirmed'])
    expect(notice.get_by_role('button', name=labels['usageRecovery.confirm'], exact=True)).to_have_count(0)
    assert source.snapshot() == original and executor_starts == []
