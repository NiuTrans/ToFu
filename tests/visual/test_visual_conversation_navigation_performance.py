"""Sidebar snapshots, stale-read ownership and viewport-sized first paint.

Real local app and browser, synthetic owner-scoped history; no provider calls.
The test asserts work budgets rather than host-dependent wall-clock thresholds.
"""
from __future__ import annotations

from collections import Counter
import threading
import uuid

import pytest

pytestmark = pytest.mark.visual


def test_sidebar_single_read_viewport_budget_and_late_snapshot(browser, live_server, monkeypatch):
    from playwright.sync_api import expect
    from routes.conversation_sync_v3 import _service
    from tests.conftest import _dismiss_onboarding_modals

    context = browser.new_context(service_workers='block', viewport={'width': 1280, 'height': 800})
    page = context.new_page()
    prefix = 'navigation-perf-' + uuid.uuid4().hex[:10]
    first, second, startup = [prefix + suffix for suffix in ('-a', '-b', '-start')]
    errors = []
    snapshots = Counter()
    started, release = threading.Event(), threading.Event()
    original_snapshot = _service.snapshot

    def slow_first_snapshot(conversation_id, *args, **kwargs):
        if conversation_id == first and not release.is_set():
            started.set()
            if not release.wait(15):
                raise TimeoutError('Navigation test did not release its snapshot')
        return original_snapshot(conversation_id, *args, **kwargs)

    monkeypatch.setattr(_service, 'snapshot', slow_first_snapshot)
    page.on('pageerror', lambda error: errors.append(str(error)))

    def record_snapshot(request):
        if '/sync?' in request.url:
            for cid in (first, second, startup):
                if f'/conversations/{cid}/sync?' in request.url:
                    snapshots[cid] += 1

    page.on('request', record_snapshot)
    context.add_init_script(f"sessionStorage.setItem('tofu_activeConvId', '{startup}');")

    def append(cid, command, actor, projection, create=False):
        body = {'commandId': prefix + command, 'actor': actor, 'kind': 'text', 'projection': projection}
        if create:
            body['conversation'] = {'allowCreate': True, 'title': cid, 'createdAt': 100, 'settings': {}}
        response = context.request.post(f'{live_server}/api/v4/conversations/{cid}/turns/settled', data=body)
        assert response.status == 200, response.text()

    try:
        for cid in (first, second, startup):
            append(cid, cid + '-input', 'human', {'content': 'Synthetic navigation question'}, create=True)
            if cid == startup:
                continue
            segments, rounds = [], []
            for index in range(80):
                call_id = f'call-{index}'
                identity = {'llmRound': index, 'attemptId': 'synthetic-attempt', 'taskId': 'synthetic-task'}
                round_record = {**identity, 'roundNum': index + 1, 'toolCallId': call_id,
                                'toolName': 'read_files', 'status': 'done', 'toolArgs': {'path': 'synthetic.txt'},
                                'toolContent': 'Synthetic historical result ' * 100,
                                'results': [{'title': 'Synthetic file', 'fetched': 'Synthetic file contents'}]}
                rounds.append(round_record)
                segments.extend([
                    {'type': 'thinking', 'blockId': f'thinking-{index}', 'text': 'Synthetic thought', **identity},
                    {'type': 'tool_use', 'blockId': f'tool-{index}', 'id': call_id, 'name': 'read_files',
                     'input': round_record['toolArgs'], 'result': {'status': 'done', 'content': round_record['toolContent']},
                     **identity},
                ])
            segments.append({'type': 'text', 'blockId': 'answer', 'text': f'Navigation answer {cid}',
                             'terminal': True, 'deliverable': True})
            append(cid, cid + '-reply', 'assistant', {'content': f'Navigation answer {cid}',
                                                      'segments': segments, 'toolRounds': rounds})
        page.goto(live_server, wait_until='domcontentloaded')
        _dismiss_onboarding_modals(page)
        for cid in (first, second):
            expect(page.locator(f'.conv-item[data-conv-id="{cid}"]')).to_have_count(1, timeout=20000)
        page.locator(f'.conv-item[data-conv-id="{first}"]').click(position={'x': 12, 'y': 12})
        assert started.wait(5)
        page.locator(f'.conv-item[data-conv-id="{second}"]').click(position={'x': 12, 'y': 12})
        expect(page.locator('#chatInner')).to_contain_text(f'Navigation answer {second}', timeout=20000)
        release.set()
        page.wait_for_timeout(500)
        expect(page.locator('#chatInner .conversation-surface')).to_have_attribute('data-conversation-id', second)
        assert snapshots[first] == snapshots[second] == 1, snapshots
        budget = page.evaluate("""() => {
          const root=document.querySelector('#chatInner');
          const hidden=[...root.querySelectorAll('[data-round-member][hidden]')];
          return {blocks:root.querySelectorAll('[data-block-id]').length,
            emptyHidden:hidden.length>0 && hidden.every(n=>n.childNodes.length===0),
            earlier:root.querySelectorAll('[data-conversation-block-window-action="earlier"]:enabled').length};
        }""")
        assert budget['blocks'] < 60, budget
        assert budget['emptyHidden'] and budget['earlier'] >= 1, budget
        _dismiss_onboarding_modals(page)
        page.locator('#chatInner [data-conversation-block-window-action="earlier"]:enabled').first.click()
        assert page.locator('#chatInner [data-block-id]').count() > budget['blocks']
        page.locator('#chatInner .conversation-round-toolbar:not([hidden]) [data-round-action="expandAll"]').click()
        expect(page.locator('#chatInner')).to_contain_text('Synthetic historical result')
        page.locator(f'.conv-item[data-conv-id="{first}"]').click(position={'x': 12, 'y': 12})
        expect(page.locator('#chatInner')).to_contain_text(f'Navigation answer {first}', timeout=20000)
        page.wait_for_timeout(300)
        assert snapshots[first] == snapshots[second] == 1, snapshots
        assert not errors, errors
    finally:
        release.set()
        for cid in (first, second, startup):
            context.request.delete(f'{live_server}/api/v4/conversations/{cid}')
        context.close()


def test_lazy_tool_output_fetches_on_expand_retries_and_aborts_on_navigation(live_server, browser, monkeypatch):
    from playwright.sync_api import expect
    from routes.conversation_sync_v3 import _service
    from tests.conftest import _dismiss_onboarding_modals

    prefix = 'lazy-tool-browser-' + uuid.uuid4().hex[:10]
    target, startup = prefix + '-target', prefix + '-start'
    context = browser.new_context(service_workers='block', viewport={'width': 1280, 'height': 800})
    context.add_init_script(f"sessionStorage.setItem('tofu_activeConvId', '{startup}');")
    page = context.new_page(); errors, detail_requests, cancelled = [], [], []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('request', lambda request: detail_requests.append(request.url)
            if '/tool-results/' in request.url else None)
    page.on('requestfailed', lambda request: cancelled.append(request.url)
            if '/tool-results/' in request.url else None)
    started, release = threading.Event(), threading.Event()
    original_read = _service.turn_tool_result
    attempts = Counter()

    def controlled_read(conversation_id, *args, **kwargs):
        index = kwargs['round_index']
        if conversation_id == target:
            attempts[index] += 1
            if index == 0 and attempts[index] == 1:
                from lib.storage.errors import StorageError
                raise StorageError('database_unavailable', 'Synthetic transient detail failure')
            if index == 1:
                started.set()
                if not release.wait(15):
                    raise TimeoutError('Navigation did not release lazy detail')
        return original_read(conversation_id, *args, **kwargs)

    monkeypatch.setattr(_service, 'turn_tool_result', controlled_read)

    def append(cid, suffix, actor, projection, create=False):
        body = {'commandId': prefix + suffix, 'actor': actor, 'kind': 'text', 'projection': projection}
        if create: body['conversation'] = {'allowCreate': True, 'title': cid, 'createdAt': 100, 'settings': {}}
        response = context.request.post(f'{live_server}/api/v4/conversations/{cid}/turns/settled', data=body)
        assert response.status == 200, response.text()

    try:
        append(startup, '-startup', 'human', {'content': 'Unrelated navigation target'}, True)
        append(target, '-input', 'human', {'content': 'Read large historical results'}, True)
        rounds, segments = [], []
        for index in range(2):
            content = ('historical line \n' * 450) + f'Exact tool output ending {index} <script>window.lazyOutputExecuted=true</script>'
            round_record = {'toolCallId': f'call-{index}', 'toolName': 'read_files', 'status': 'done',
                            'toolArgs': {'files': ['synthetic.txt']}, 'toolContent': content,
                            'llmRound': index, 'roundNum': index + 1, 'attemptId': 'fixture', 'taskId': 'fixture'}
            rounds.append(round_record)
            segments.append({'type': 'tool_use', 'blockId': f'tool-{index}', 'id': f'call-{index}',
                             'name': 'read_files', 'input': round_record['toolArgs'],
                             'result': {'status': 'done', 'content': content}, 'llmRound': index,
                             'attemptId': 'fixture', 'taskId': 'fixture'})
        segments.append({'type': 'text', 'blockId': 'answer', 'text': 'Lazy output answer', 'terminal': True, 'deliverable': True})
        append(target, '-output', 'assistant', {'content': 'Lazy output answer', 'toolRounds': rounds, 'segments': segments})
        page.goto(live_server, wait_until='domcontentloaded'); _dismiss_onboarding_modals(page)
        expect(page.locator(f'.conv-item[data-conv-id="{target}"]')).to_have_count(1, timeout=20000)
        page.locator(f'.conv-item[data-conv-id="{target}"]').click(position={'x': 12, 'y': 12})
        expect(page.locator('#chatInner')).to_contain_text('Lazy output answer', timeout=20000)
        _dismiss_onboarding_modals(page)
        # Two tool sections use their own disclosures; the bulk toolbar is
        # intentionally hidden until four sections are present.
        toggles = page.locator('#chatInner .conversation-round-header button[aria-expanded="false"]')
        while toggles.count():
            toggles.first.click()
        viewers = page.locator('#chatInner details[data-lazy-tool-result]')
        expect(viewers).to_have_count(2)
        assert not detail_requests, detail_requests
        viewers.nth(0).locator('summary').click()
        expect(viewers.nth(0).get_by_role('button')).to_be_visible(timeout=10000)
        viewers.nth(0).get_by_role('button').click()
        expect(page.locator('#chatInner')).to_contain_text('Exact tool output ending 0', timeout=10000)
        assert page.evaluate('window.lazyOutputExecuted === undefined')
        assert attempts[0] == 2
        loaded = page.locator('#chatInner details[data-tool-result-authority="toolContent"]')
        expect(loaded).to_have_count(1)
        assert loaded.evaluate('(details)=>details.open')
        loaded.locator('summary').click(); loaded.locator('summary').click()
        page.wait_for_timeout(100)
        assert attempts[0] == 2
        page.locator('#chatInner details[data-lazy-tool-result] summary').click()
        assert started.wait(5)
        page.locator(f'.conv-item[data-conv-id="{startup}"]').click(position={'x': 12, 'y': 12})
        expect(page.locator('#chatInner')).to_contain_text('Unrelated navigation target', timeout=10000)
        release.set(); page.wait_for_timeout(400)
        assert any('/tool-results/1' in url for url in cancelled), cancelled
        expect(page.locator('#chatInner')).not_to_contain_text('Exact tool output ending 1')
        assert not errors, errors
    finally:
        release.set()
        for cid in (target, startup): context.request.delete(f'{live_server}/api/v4/conversations/{cid}')
        context.close()



def test_sidebar_large_completed_reasoning_materializes_on_keyboard_open(browser, live_server):
    from playwright.sync_api import expect
    from tests.conftest import _dismiss_onboarding_modals

    prefix='reasoning-perf-'+uuid.uuid4().hex[:10]
    cid, startup=prefix+'-large',prefix+'-start'
    context=browser.new_context(service_workers='block')
    context.add_init_script(f"sessionStorage.setItem('tofu_activeConvId', '{startup}');")
    page=context.new_page()
    errors=[]
    page.on('pageerror',lambda error:errors.append(str(error)))
    thought=('## Historical reasoning\n\nFull evidence remains inspectable.\n\n'
             '```python\nvalue = 42\nprint(value)\n```\n\n')*180
    try:
        for conv in (cid,startup):
            response=context.request.post(f'{live_server}/api/v4/conversations/{conv}/turns/settled',data={
                'commandId':conv+'-question','actor':'human','kind':'text',
                'conversation':{'allowCreate':True,'title':conv,'createdAt':100,'settings':{}},
                'projection':{'content':'Synthetic reasoning navigation test'},
            })
            assert response.status==200,response.text()
        response=context.request.post(f'{live_server}/api/v4/conversations/{cid}/turns/settled',data={
            'commandId':cid+'-answer','actor':'assistant','kind':'text',
            'projection':{'content':'The final answer is immediately available.',
                'segments':[{'type':'thinking','blockId':'large-thinking','text':thought,'llmRound':0},
                            {'type':'text','blockId':'final-answer','text':'The final answer is immediately available.',
                             'terminal':True,'deliverable':True}]},
        })
        assert response.status==200,response.text()
        page.goto(live_server,wait_until='domcontentloaded')
        _dismiss_onboarding_modals(page)
        row=page.locator(f'.conv-item[data-conv-id="{cid}"]')
        expect(row).to_have_count(1,timeout=20000)
        _dismiss_onboarding_modals(page)
        row.click(position={'x':12,'y':12})
        expect(page.locator('#chatInner')).to_contain_text('The final answer is immediately available.',timeout=20000)
        details=page.locator('#chatInner [data-block-id="large-thinking"] details.thinking-block')
        expect(details).to_have_attribute('data-state','complete')
        assert not details.evaluate('(n)=>n.open')
        body=details.locator('.thinking-text')
        assert body.evaluate('(n)=>n.childNodes.length')==0
        assert body.get_attribute('data-deferred-reasoning')=='true'
        details.locator('summary').focus()
        details.locator('summary').press('Enter')
        expect(body).to_contain_text('Full evidence remains inspectable.')
        assert body.locator('pre code').count()==180
        assert details.evaluate('(n)=>n.open')
        assert not errors,errors
    finally:
        for conv in (cid,startup):
            context.request.delete(f'{live_server}/api/v4/conversations/{conv}')
        context.close()
