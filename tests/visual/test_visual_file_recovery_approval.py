"""Actual app approval discloses unresolved file writes and owns the decision."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.turn_lifecycle.test_task_event_delivery import owner as owner,_task,_discard
from tests.turn_lifecycle.test_file_effect_inspection import project as project,write
from tests.turn_lifecycle.test_file_recovery_admission import prepare,invoke

pytestmark=[pytest.mark.visual,pytest.mark.auth_mode('open')]


@pytest.mark.parametrize('width,approved',[(1280,True),(390,False)])
def test_unknown_file_recovery_uses_real_owner_approval(page,project,owner,monkeypatch,tmp_path,width,approved):
    from playwright.sync_api import expect
    from lib.storage import get_storage_client
    from lib import turn_lifecycle
    from lib.project_mod.write_tools import _ops
    from tests.conftest import _dismiss_onboarding_modals
    client=get_storage_client(write=True)
    monkeypatch.setenv('TOFU_TURN_DELTA_RECORD_MS','0')
    _,task=_task(turn_lifecycle,SimpleNamespace(client=client),'browser-file-takeover')
    original=_ops._record_modification_checked
    monkeypatch.setattr(_ops,'_record_modification_checked',lambda *a,**kw:(_ for _ in ()).throw(OSError('lost ACK')))
    pool=ThreadPoolExecutor(max_workers=1);future=None
    try:
        source,result=write(task,project,append=True)
        assert source['status']=='error',result
        operation=source['executionEffect']['operationId']
        # Model a lost journal while preserving its artifacts outside the active
        # authority; takeover never overrides a real file-recovery conflict.
        from lib.project_mod import modifications
        session=Path(modifications._get_session_dir(str(project),create=False))
        if approved:
            (session/'write-intents-v1').rename(tmp_path/'unavailable-prior-intents')
        (project/'new.txt').write_text('human version\n')
        definitions=prepare(task,project)
        _,result=invoke(task,project,definitions,'inspect_tool_effect',{'operation_id':operation},1)
        assert '"outcome":"unknown"' in result
        monkeypatch.setattr(_ops,'_record_modification_checked',original)
        page.set_viewport_size({'width':width,'height':1000})
        page.reload(wait_until='domcontentloaded');_dismiss_onboarding_modals(page)
        page.locator(f'.conv-item[data-conv-id="{task["convId"]}"]').wait_for()
        page.wait_for_function("typeof window.TofuModules?.resolveAction?.('loadConversation')==='function'")
        page.evaluate("id=>window.TofuModules.resolveAction('loadConversation')(id)",task['convId'])
        future=pool.submit(invoke,task,project,definitions,'write_file',
            {'path':'new.txt','content':'approved new work\n','append':True},2)
        notice=page.locator(f'[data-turn-id="{task["_turnId"]}"] .ptool-pending-wrap')
        expect(notice).to_be_visible(timeout=15000)
        expect(notice).to_contain_text('Earlier writes may already have taken effect')
        expect(notice).to_contain_text(operation)
        expect(notice).to_contain_text('approved new work')
        assert (project/'new.txt').read_text()=='human version\n'
        assert not future.done()
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
        page.screenshot(path=str(tmp_path/f'file-recovery-approval-{width}.png'),full_page=True)
        notice.locator('.ptool-approve-btn' if approved else '.ptool-reject-btn').click()
        row,result=future.result(timeout=15)
        assert row['status']==('done' if approved else 'rejected'),result
        assert (project/'new.txt').read_text()==('human version\napproved new work\n' if approved else 'human version\n')
        assert source['status']=='error'
        expect(notice).to_have_count(0)
    finally:
        task['aborted']=True
        pool.shutdown(wait=True,cancel_futures=True)
        from lib.tasks_pkg.event_delivery import flush_task_events
        flush_task_events(task,timeout=10)
        _discard(task)
        client.command('conversation.delete',{'conv_id':task['convId'],'user_id':1},task['id']+'-cleanup')
