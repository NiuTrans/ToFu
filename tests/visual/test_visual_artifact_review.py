"""Actual application artifact preview, owner decisions, version switching and conflict UI."""
from types import SimpleNamespace

import pytest
from tests.turn_lifecycle.test_task_event_delivery import owner as owner, _task, _discard

pytestmark = [pytest.mark.visual, pytest.mark.auth_mode('open')]


@pytest.mark.parametrize('width', [1280, 390])
def test_artifact_owner_review_in_actual_application(page, owner, tmp_path, width):
    from playwright.sync_api import expect
    from lib import turn_lifecycle
    from lib.artifacts import create_artifact
    from lib.storage import get_storage_client
    from tests.conftest import _dismiss_onboarding_modals
    client = get_storage_client(write=True)
    _, task = _task(turn_lifecycle, SimpleNamespace(client=client), 'browser-artifact-review')
    artifacts = [create_artifact(conv_id=task['convId'], task_id=task['id'], content=f'# Report {i}',
                    format='markdown', source='write_file', source_ref={'path': 'report.md'}, title=f'Report {i}')
                 for i in (1, 2)]
    turn_lifecycle.record_task_event(task, {'type': 'delta', 'content': 'Reports ready for review'})
    try:
        page.set_viewport_size({'width': width, 'height': 1000})
        page.evaluate("localStorage.setItem('tofu_ui_lang','en')")
        page.add_init_script("window.__reviewAppReady=false; window.addEventListener('tofu:app-ready',()=>{window.__reviewAppReady=true})")
        page.reload(wait_until='domcontentloaded')
        page.wait_for_function('window.__reviewAppReady === true'); _dismiss_onboarding_modals(page)
        page.wait_for_function("typeof window.TofuModules?.resolveAction?.('loadConversation')==='function'")
        page.locator(f'.conv-item[data-conv-id="{task["convId"]}"]').wait_for(state='attached')
        page.evaluate("id=>window.TofuModules.resolveAction('loadConversation')(id)", task['convId'])
        page.locator(f'[data-conversation-action="open-artifact"][data-operation="{artifacts[1]["id"]}"]').click()
        panel = page.locator('#artifact-panel')
        review = panel.locator('[data-art-review]')
        review.get_by_role('button', name='Acceptance review', exact=True).click()
        expect(review).to_contain_text('No acceptance criteria yet')
        review.get_by_role('button', name='Edit objective and criteria').click()
        review.get_by_label('Objective', exact=True).fill('Report for the customer')
        review.get_by_role('button', name='Add requirement', exact=True).click()
        review.get_by_label('Requirement 1', exact=True).fill('Totals agree with source')
        review.get_by_role('button', name='Add requirement', exact=True).click()
        review.get_by_label('Requirement 2', exact=True).fill('Readable headings')
        review.get_by_role('button', name='Save requirements').click()
        items = review.locator('[data-review-criterion]')
        expect(items).to_have_count(2)
        for index in range(2):
            items.nth(index).get_by_label('Evidence and review notes').fill(f'Checked criterion {index + 1} against the published report')
            items.nth(index).get_by_role('button', name='I verified this').click()
            expect(items.nth(index)).to_contain_text('Passed')
        expect(review).to_contain_text('Accepted by owner')
        # Existing immutable version selector changes the acceptance target.
        panel.locator('[data-art-version]').select_option(artifacts[0]['id'])
        review.get_by_role('button', name='Acceptance review', exact=True).click()
        expect(review).to_contain_text('Awaiting review')
        expect(review.get_by_text('Not reviewed', exact=False)).to_have_count(2)
        panel.locator('[data-art-version]').select_option(artifacts[1]['id'])
        review.get_by_role('button', name='Acceptance review', exact=True).click()
        expect(review).to_contain_text('Accepted by owner')
        review.get_by_role('button', name='Edit objective and criteria').click()
        review.get_by_label('Requirement 1', exact=True).fill('Totals and tax agree with source')
        review.get_by_role('button', name='Save requirements').click()
        expect(items.nth(0)).to_contain_text('Previous review is stale')
        expect(items.nth(1)).to_contain_text('Passed')
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
        review.evaluate('el=>{el.scrollTop=0}')
        page.screenshot(path=str(tmp_path / f'artifact-review-{width}.png'), full_page=True)
        # A second editor wins the CAS; the visible unsaved draft must survive.
        review.get_by_role('button', name='Edit objective and criteria').click()
        review.get_by_label('Objective', exact=True).fill('My unsaved objective')
        url = '/api/v4/artifacts/' + artifacts[1]['id'] + '/review'
        origin = page.evaluate('location.origin')
        current = page.request.get(origin + url).json()['data']
        response = page.request.post(origin + url, data={
            'action': 'requirements', 'requirementsRevision': current['requirementsRevision'],
            'objective': 'Changed by another editor',
            'criteria': [{key: item[key] for key in ('id', 'text', 'kind')} for item in current['criteria']],
        })
        assert response.status == 200
        review.get_by_role('button', name='Save requirements').click()
        expect(review.get_by_role('alert')).to_contain_text('Review was not confirmed')
        expect(review.get_by_label('Objective', exact=True)).to_have_value('My unsaved objective')
        panel.locator('[data-art-action="close"]').click()
        expect(panel).to_have_attribute('aria-hidden', 'true')
    finally:
        task['aborted'] = True
        _discard(task)
        client.command('conversation.delete', {'conv_id': task['convId'], 'user_id': 1}, task['id'] + '-cleanup')
