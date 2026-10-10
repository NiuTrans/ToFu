"""Served native-primary time presentation, live hint, reload and narrow layout."""
import os
import time
from pathlib import Path

import pytest

pytestmark=[pytest.mark.visual,pytest.mark.auth_mode('open')]


@pytest.mark.parametrize('width',[1280,390])
def test_actual_task_start_is_visible_and_survives_reload(page,monkeypatch,tmp_path,width):
    from playwright.sync_api import expect
    from lib.storage import get_storage_client
    from tests.automations.test_time_publication import prepared
    from tests.conftest import _dismiss_onboarding_modals

    assert os.environ.get('TOFU_PERSONAL_STORAGE_BACKEND')=='tofudb'
    factory,repo,_,dispatch,completed,fence,created,attempt=prepared(
        (get_storage_client,1,None),monkeypatch,tenant=None)
    conv_id=created['turn']['conversationId']
    expected=completed['steps']['show']['output']['text']
    try:
        page.set_viewport_size({'width':width,'height':900})
        page.reload(wait_until='domcontentloaded')
        _dismiss_onboarding_modals(page)
        page.locator(f'.conv-item[data-conv-id="{conv_id}"]').wait_for()
        page.wait_for_function("typeof window.TofuModules?.resolveAction?.('loadConversation') === 'function'")
        page.evaluate("id=>window.TofuModules.resolveAction('loadConversation')(id)",conv_id)
        turn=page.locator(f'[data-turn-id="{created["turn"]["turnId"]}"]')
        expect(turn).to_be_visible()
        record=turn.locator('[data-automation-presentation-id]')
        expect(record).to_have_count(0)
        # Only the run output commit and its owned wake can make this visible.
        repo.advance(dispatch,completed,fence,now=time.time())
        expect(record).to_have_count(1,timeout=20000)
        expect(record).to_have_text(expected)
        page.evaluate('Date.now=()=>9999999999999')
        expect(record).to_have_text(expected)
        box=record.bounding_box()
        assert box and box['x']>=0 and box['x']+box['width']<=width+1
        screenshot=tmp_path/f'automation-task-time-{width}.png'
        destination=Path(os.environ.get('TOFU_AUTOMATION_SCREENSHOT_DIR') or tmp_path)
        destination.mkdir(parents=True,exist_ok=True)
        page.screenshot(path=str(destination/screenshot.name))
        page.reload(wait_until='domcontentloaded')
        _dismiss_onboarding_modals(page)
        page.evaluate("id=>window.TofuModules.resolveAction('loadConversation')(id)",conv_id)
        expect(page.locator(f'[data-turn-id="{created["turn"]["turnId"]}"] [data-automation-presentation-id]')).to_have_text(expected)
    finally:
        factory(write=True).command('conversation.delete',{'conv_id':conv_id,'user_id':1},conv_id+':cleanup')
