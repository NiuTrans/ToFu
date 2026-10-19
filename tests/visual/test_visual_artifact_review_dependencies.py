"""Actual source read → browser owner review → changed file → targeted stale judgment."""
from types import SimpleNamespace

import pytest
from tests.turn_lifecycle.test_task_event_delivery import owner as owner, _task, _discard

pytestmark = [pytest.mark.visual, pytest.mark.auth_mode('open')]


@pytest.mark.parametrize(('width', 'language'), [(1280, 'en'), (390, 'zh')])
def test_review_of_source_bound_facts_in_actual_application(page, owner, tmp_path, width, language):
    from playwright.sync_api import expect
    from lib import turn_lifecycle
    from lib.artifacts import create_artifact
    from lib.storage import get_storage_client
    from tests.conftest import _dismiss_onboarding_modals
    from tests.memory.test_working_file_sources import read_source, save
    from tests.memory.test_working_facts import record
    from tests.artifacts.test_artifact_review import requirements
    client = get_storage_client(write=True)
    _, task = _task(turn_lifecycle, SimpleNamespace(client=client), 'browser-review-facts')
    artifact = create_artifact(conv_id=task['convId'], task_id=task['id'], content='# Report 1',
        format='markdown', source='write_file', source_ref={'path': 'report.md'}, title='Report with evidence')
    file_task, _, source = read_source(artifact, tmp_path)
    bound = save(artifact, file_task, source)
    independent = record(artifact)['facts'][0]
    requirements(artifact)
    turn_lifecycle.record_task_event(task, {'type': 'delta', 'content': 'Report with source evidence'})
    snapshot = client.query('turn.sync.snapshot', {'conversation_id': task['convId'], 'user_id': 1,
        'turn_limit': 100, 'include_artifact_hint': True})
    assert snapshot.get('hasArtifacts') is not False, type(client).__name__
    labels = {
        'review': ('Acceptance review', '验收'), 'choose': ('Choose supporting facts', '选择验收依据'),
        'fact': ('Supporting fact', '工作事实'), 'use': ('Use this fact version', '采用此事实版本'),
        'note': ('Evidence and review notes', '核验依据与说明'), 'pass': ('I verified this', '我已核验通过'),
        'accepted': ('Accepted by owner', '用户已验收通过'), 'reload': ('Reload latest review', '重新加载最新记录'),
        'stale': ('Previous review is stale', '原有判断已失效'), 'passed': ('Passed', '已通过'),
        'changed': ('Supporting fact or source changed', '依据或来源已变化'),
        'error': ('Review was not confirmed', '验收记录尚未确认'),
    }
    def text(key): return labels[key][language == 'zh']
    browser_messages = []
    requests = []
    page.on('console', lambda message: browser_messages.append(message.text) if 'Artifact' in message.text else None)
    def observe_response(response):
        if '/sync?' in response.url:
            data = response.json().get('data', {})
            requests.append((response.url, response.status, data.get('hasArtifacts', 'omitted')))
        elif '/artifacts' in response.url:
            requests.append((response.url, response.status))
    page.on('response', observe_response)
    try:
        page.set_viewport_size({'width': width, 'height': 1000})
        page.evaluate('lang=>localStorage.setItem("tofu_ui_lang",lang)', language)
        page.add_init_script("window.__reviewAppReady=false; window.addEventListener('tofu:app-ready',()=>{window.__reviewAppReady=true})")
        page.reload(wait_until='domcontentloaded')
        page.wait_for_function('window.__reviewAppReady === true'); _dismiss_onboarding_modals(page)
        page.wait_for_function("typeof window.TofuModules?.resolveAction?.('loadConversation')==='function'")
        page.locator(f'.conv-item[data-conv-id="{task["convId"]}"]').wait_for(state='attached')
        page.evaluate("id=>window.TofuModules.resolveAction('loadConversation')(id)", task['convId'])
        action = page.locator(f'[data-conversation-action="open-artifact"][data-operation="{artifact["id"]}"]')
        try:
            action.wait_for(timeout=5000)
        except Exception:
            import json
            origin = page.evaluate('location.origin')
            diagnostic = {'body': page.locator('body').inner_text(),
                'messages': browser_messages, 'requests': requests,
                'artifacts': page.request.get(origin + '/api/v4/artifacts?conv=' + task['convId']).json(),
                'turn': turn_lifecycle.get_turn(task['convId'], task['_turnId'], user_id=1)}
            (tmp_path / 'artifact-ui-diagnostic.json').write_text(json.dumps(diagnostic, ensure_ascii=False, indent=2))
            page.screenshot(path=str(tmp_path / 'artifact-ui-diagnostic.png'))
            raise
        action.click()
        panel = page.locator('#artifact-panel')
        review = panel.locator('[data-art-review]')
        review.get_by_role('button', name=text('review'), exact=True).click()
        items = review.locator('[data-review-criterion]')
        expect(items).to_have_count(2)
        for index, fact in enumerate((bound, independent)):
            item = items.nth(index)
            item.get_by_role('button', name=text('choose'), exact=True).click()
            item.get_by_label(text('fact'), exact=True).select_option(fact['id'])
            item.get_by_role('button', name=text('use'), exact=True).click()
            item.get_by_label(text('note'), exact=True).fill(f'Checked source for criterion {index + 1}')
            item.get_by_role('button', name=text('pass'), exact=True).click()
            expect(item.locator('[data-review-fact-evidence]')).to_contain_text(fact['text'])
        expect(review).to_contain_text(text('accepted'))
        panel.locator('[data-art-action="close"]').click()
        page.evaluate("window.TofuModules.resolveAction('newChat')()")
        page.evaluate("id=>window.TofuModules.resolveAction('loadConversation')(id)", task['convId'])
        action.click()
        review.get_by_role('button', name=text('review'), exact=True).click()
        expect(review).to_contain_text(text('accepted'))
        (tmp_path / 'basis.txt').write_text('Budget: 200\nRegion: east\n')
        review.get_by_role('button', name=text('reload'), exact=True).click()
        expect(items.nth(0)).to_contain_text(text('stale'))
        expect(items.nth(0)).to_contain_text(text('changed'))
        expect(items.nth(0)).to_contain_text('Budget: 100')
        expect(items.nth(1)).to_contain_text(text('passed'))
        expect(review).not_to_contain_text(text('accepted'))
        # The server rejects a decision that still carries the old source; UI retains the draft.
        items.nth(0).get_by_label(text('note'), exact=True).fill('Preserve this unsaved review note')
        items.nth(0).get_by_role('button', name=text('pass'), exact=True).click()
        expect(review.get_by_role('alert').first).to_contain_text(text('error'))
        expect(items.nth(0).get_by_label(text('note'), exact=True)).to_have_value('Preserve this unsaved review note')
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
        review.evaluate('el=>{el.scrollTop=0}')
        page.screenshot(path=str(tmp_path / f'artifact-review-dependencies-{language}-{width}.png'), full_page=True)
        panel.locator('[data-art-action="close"]').click()
        expect(panel).to_have_attribute('aria-hidden', 'true')
    finally:
        task['aborted'] = True
        _discard(task)
        client.command('conversation.delete', {'conv_id': task['convId'], 'user_id': 1}, task['id'] + '-cleanup')
