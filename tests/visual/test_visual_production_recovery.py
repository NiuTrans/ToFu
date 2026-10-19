"""Shipped browser keeps review/evidence coherent across whole service loss."""
import json
from pathlib import Path

import pytest

from tests.tasks_pkg.test_native_production_recovery import (
    test_pending_file_survives_full_service_loss_and_public_continue as rehearse_recovery,
)

pytestmark = [pytest.mark.visual, pytest.mark.slow]


@pytest.mark.parametrize(('width', 'language'), [(1280, 'en'), (390, 'zh')])
def test_production_restart_and_requirement_revision_in_existing_browser(browser, tmp_path, width, language):
    from playwright.sync_api import expect
    from tests.conftest import _dismiss_onboarding_modals

    labels = json.loads((Path(__file__).resolve().parents[2] /
        f'frontend/src/i18n/locales/{language}.json').read_text())
    page = browser.new_page(viewport={'width': width, 'height': 1000})
    page.add_init_script('if (window === window.top) localStorage.setItem("tofu_ui_lang", ' + json.dumps(language) + ')')
    page.add_init_script("window.__recoveryAppReady=false; window.addEventListener('tofu:app-ready',()=>{window.__recoveryAppReady=true})")
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    observed = []
    streams = set()
    connected = []

    def response_started(response):
        if response.headers.get('content-type', '').startswith('text/event-stream'):
            streams.add(response.request)

    page.on('response', response_started)
    page.on('requestfinished', lambda request: streams.discard(request))
    page.on('requestfailed', lambda request: streams.discard(request))

    def button(host, key):
        return host.get_by_role('button', name=labels[key], exact=True)

    def turn_review():
        page.locator('[data-conversation-action="review"]').click()
        return page.locator('[data-turn-review-dialog]'), page.locator('[data-turn-review]')

    def observe(stage, client, conversation, turn_id, evidence):
        if stage == 'accepted_before_crash':
            page.goto(str(client.base_url), wait_until='domcontentloaded')
            page.wait_for_function('window.__recoveryAppReady === true')
            _dismiss_onboarding_modals(page)
            page.wait_for_function("typeof window.TofuModules?.resolveAction?.('loadConversation')==='function'")
            page.evaluate("id=>window.TofuModules.resolveAction('loadConversation')(id)", conversation)
            dialog, review = turn_review()
            expect(review).to_contain_text(labels['turnReview.executionPending'])
            expect(review).to_contain_text('Report region: west')
            expect(review.locator('[data-review-criterion]')).to_have_count(2)
            page.screenshot(path=str(tmp_path / 'accepted-before-crash.png'), full_page=True, animations='disabled')
        elif stage == 'requirement_revised':
            # The original page and origin survive the process restart.
            dialog = page.locator('[data-turn-review-dialog]')
            review = dialog.locator('[data-turn-review]')
            button(review, 'artifactReview.reload').click()
            expect(review).to_contain_text('Report region: east')
            expect(review.locator('[data-review-criterion]').first).to_contain_text(labels['artifactReview.pass'])
            button(dialog, 'workingFacts.close').click()
            artifact = evidence['artifacts']['report']['id']
            page.locator(f'[data-conversation-action="open-artifact"][data-operation="{artifact}"]').click()
            panel = page.locator('#artifact-panel')
            artifact_review = panel.locator('[data-art-review]')
            button(artifact_review, 'artifactReview.title').click()
            expect(artifact_review).to_contain_text(labels['artifactReview.stale'])
            page.screenshot(path=str(tmp_path / 'requirement-revised.png'), full_page=True, animations='disabled')
            panel.locator('[data-art-action="close"]').click()
        else:
            expect(page.locator('#chatInner')).to_contain_text('Report region: east. Original ledger preserved and verified.')
            dialog, review = turn_review()
            expect(review).to_contain_text(labels['artifactReview.accepted'])
            expect(review.locator('[data-review-output]')).to_have_count(3)
            assert dialog.evaluate('el=>el.scrollWidth<=el.clientWidth+1')
            page.screenshot(path=str(tmp_path / 'accepted-after-recovery.png'), full_page=True, animations='disabled')
            button(dialog, 'workingFacts.close').click()
            assert streams, 'No live browser SSE consumer at production shutdown'
            connected.extend(sorted(request.url for request in streams))
        observed.append(stage)

    try:
        rehearse_recovery(tmp_path, True, browser_observer=observe)
        assert observed == ['accepted_before_crash', 'requirement_revised', 'accepted_after_recovery']
        assert not errors, errors
        (tmp_path / 'browser-evidence.json').write_text(json.dumps({
            'width': width, 'language': language, 'stages': observed,
            'same_page_across_service_restart': True, 'page_open_during_shutdown': not page.is_closed(),
            'streams_before_shutdown': connected, 'page_errors': errors}, indent=2))
    except BaseException:
        if not page.is_closed():
            page.screenshot(path=str(tmp_path / 'browser-failure.png'), full_page=True)
            (tmp_path / 'browser-failure.txt').write_text(page.locator('body').inner_text())
        raise
    finally:
        page.close()
