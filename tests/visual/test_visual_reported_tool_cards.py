"""Reported tool cards through a real local app and persisted v4 history.

No model/provider calls. Disposable owner-scoped turns exercise actual reload,
program ownership, failure disclosure and historical experiment navigation.
"""
from __future__ import annotations

import json
import uuid

import pytest

pytestmark = pytest.mark.visual


@pytest.mark.parametrize('width', [1280, 390])
def test_reported_tool_cards_survive_reload(browser, live_server, tmp_path, width):
    from playwright.sync_api import expect
    from tests.conftest import _dismiss_onboarding_modals

    context = browser.new_context(service_workers='block', viewport={'width': width, 'height': 900})
    context.add_init_script("localStorage.setItem('tofu_onboarding_v1_done','1');localStorage.setItem('tofu_ui_lang','en');")
    page = context.new_page()
    cid = 'tool-card-regression-' + uuid.uuid4().hex[:12]
    identity = {'llmRound': 15, 'taskId': 'synthetic-task', 'attemptId': 'synthetic-attempt'}
    code = "const q=await tools.inputs({name:'mcp__llm__experiment_eval_jobs'});return q;"
    returned = {'inputs': [], 'nextOffset': None, 'total': 0}
    program = {**identity, 'roundNum': 15, '_programSynthetic': True, '_programCallId': 'gateway-15',
               'status': 'done', 'programStatus': 'completed', 'programCode': code,
               'programResult': returned, 'childCallIds': [], 'childToolNames': [],
               'programStats': {'tool_calls': 0, 'successful_tool_calls': 0, 'input_queries': 1,
                                'result_reads': 0, 'outcome': 'completed'}}
    gateway = {**identity, 'roundNum': 15, 'toolCallId': 'gateway-15', 'toolName': 'execute_program',
               'toolArgs': {'code': code}, 'status': 'done', 'query': 'Programmatic tool call',
               'toolContent': returned}
    private_ref = 'tool-result:' + 'f' * 64
    failure = {'artifactRef': private_ref, 'status': 'error', 'summary': 'MCP Error: ' + json.dumps({
        'ok': False, 'error': 'experiment has not run yet', 'deferred_tool': 'experiment_eval_jobs',
        'error_detail': {'type': 'error', 'message': 'experiment has not run yet'}})}
    failed = {**identity, 'llmRound': 14, 'roundNum': 14, 'toolCallId': 'eval-failed',
              'toolName': 'mcp__llm__experiment_eval_jobs',
              'toolArgs': {'experiment_id_or_name_or_url': '381153'}, 'status': 'error',
              'query': 'llm/experiment_eval_jobs · experiment_id_or_name_or_url=381153',
              'toolContent': json.dumps(failure),
              '_repaired': {'label': 'auto-fixed', 'detail': 'Recovered malformed JSON',
                            'patterns': ['json_repaired', 'stringified_primitive']}}
    fork = {**identity, 'llmRound': 16, 'roundNum': 16, 'toolCallId': 'fork-done',
            'toolName': 'mcp__llm__experiment_fork', 'status': 'done',
            'query': 'llm/experiment_fork · base_id=381153',
            'toolArgs': {'base_id': '381153', 'name': 'Synthetic fork', 'dry_run': False},
            'toolContent': json.dumps({'ok': True, 'data': {'experimentId': '381154'}})}
    raw_code = "const old = await tools.inputs({name:'mcp__llm__experiment_eval_jobs'});return old;"
    raw = {**identity, 'llmRound': 17, 'roundNum': 17, 'toolCallId': 'raw-old-17',
           'toolName': 'execute_program', 'toolArgs': json.dumps({'code': raw_code}),
           'status': 'done', 'query': 'Programmatic tool call',
           'toolContent': json.dumps(returned)}
    rounds = [failed, gateway, program, fork, raw]
    segments = [
        {'type': 'tool_use', 'blockId': row['toolCallId'], 'id': row['toolCallId'],
         'name': row['toolName'], 'input': row['toolArgs'],
         'result': {'status': row['status'], 'content': row['toolContent']},
         **{key: row[key] for key in ('llmRound', 'taskId', 'attemptId')}}
        for row in (failed, gateway, fork, raw)
    ] + [{'type': 'text', 'blockId': 'answer', 'text': 'Synthetic tool-card verification complete.',
          'terminal': True, 'deliverable': True}]
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))

    def append(command, actor, projection, create=False):
        body = {'commandId': cid + command, 'actor': actor, 'kind': 'text', 'projection': projection}
        if create:
            body['conversation'] = {'allowCreate': True, 'title': cid, 'createdAt': 100, 'settings': {}}
        response = context.request.post(f'{live_server}/api/v4/conversations/{cid}/turns/settled', data=body)
        assert response.status == 200, response.text()

    try:
        append('-human', 'human', {'content': 'Synthetic reported tool-card regression'}, create=True)
        append('-assistant', 'assistant', {'content': 'Synthetic tool-card verification complete.',
                                         'toolRounds': rounds, 'segments': segments})
        context.add_init_script(f"sessionStorage.setItem('tofu_activeConvId',{json.dumps(cid)});")
        page.goto(live_server, wait_until='domcontentloaded')
        _dismiss_onboarding_modals(page)
        item = page.locator(f'.conv-item[data-conv-id="{cid}"]')
        expect(item).to_have_count(1, timeout=20000)
        # The persisted active conversation is restored at startup. Its drawer
        # item is intentionally offscreen on phones; do not force-click it.
        if width >= 768:
            item.click(position={'x': 12, 'y': 12})
        for iteration in range(2):
            chat = page.locator('#chatInner')
            expect(chat).to_contain_text('Synthetic tool-card verification complete.', timeout=20000)

            page.screenshot(path=str(tmp_path / f'reported-tool-cards-initial-{width}-{iteration}.png'), full_page=True)
            process = chat.locator('.conversation-history-process')
            expect(process).to_have_count(1)
            if not process.evaluate('node => node.open'):
                process.locator(':scope > summary').click()
            expect(chat.locator('.ptc-card')).to_have_count(2)
            raw_card = chat.locator('.ptc-card').filter(has_text='not recorded / unavailable')
            expect(raw_card).to_have_count(1)
            raw_head = raw_card.locator(':scope > summary')
            expect(raw_head).to_contain_text('Returned')
            expect(raw_head).to_contain_text('Script: look up prior results')
            expect(raw_head).to_contain_text('Retained inputs: 0 returned / 0 total')
            assert 'No new tool calls' not in raw_head.inner_text()
            assert 'Completed' not in raw_head.inner_text()
            raw_head.click()
            raw_card.locator('.ptc-code > summary').click()
            expect(raw_card.locator('.ptc-code code')).to_have_text(raw_code)
            raw_card.locator('.ptc-result > summary').click()
            expect(raw_card.locator('.ptc-result pre')).to_contain_text('"inputs"')
            assert raw_head.evaluate('node=>node.scrollWidth<=node.clientWidth+1')
            parent_card = chat.locator('.ptc-card').filter(has_text='No source tools called again')
            expect(parent_card).to_have_count(1)
            head = parent_card.locator(':scope > summary')
            expect(head).to_contain_text('Script: look up prior results')
            expect(head).to_contain_text('Completed')
            head.click()
            parent_card.locator('.ptc-technical > summary').click()
            expect(parent_card.locator('.ptc-measurements')).to_contain_text('No source tools called again')
            expect(parent_card.locator('.ptc-measurements')).to_contain_text('0 successful tool calls · 0 attempts')

            expect(head).to_contain_text('Retained inputs: 0 returned / 0 total')
            expect(head).to_contain_text('inputs')
            expect(head).to_contain_text('total')
            assert 'experiment jobs' not in head.inner_text().lower()
            assert chat.get_by_text('Programmatic tool call', exact=True).count() == 0
            assert all(private_ref not in text for text in chat.locator('summary').all_inner_texts())
            reason = chat.locator('summary').filter(has_text='experiment has not run yet')
            expect(reason).to_have_count(1)
            # Argument repair and the downstream MCP business failure stay distinct.
            expect(reason.locator('.ptool-badge-repaired')).to_have_text('auto-fixed')
            expect(reason.locator('.ptool-repair-kind')).to_have_count(2)
            expect(reason).to_contain_text('recovered JSON')
            expect(reason).to_contain_text('coerced primitive')
            assert all(private_ref not in value for value in chat.locator('[title]').evaluate_all(
                'nodes=>nodes.map(node=>node.title)'))
            details = reason.locator('..')
            disclosure_state = details.evaluate('node=>({tag:node.tagName,open:node.open,html:node.outerHTML.slice(0,2400)})')
            assert disclosure_state['tag'] == 'DETAILS' and not disclosure_state['open'], disclosure_state
            reason.click()
            expect(details.locator('pre')).to_contain_text('MCP Error:')
            reason.click()
            canvas_links = chat.locator('a[href*="/user/modelManage/experimentalTaskDetail/"]')
            expect(canvas_links).to_have_count(3)
            hrefs = canvas_links.evaluate_all('nodes=>nodes.map(node=>node.href)')
            assert sum(href.endswith('/381153') for href in hrefs) == 2
            assert sum(href.endswith('/381154') for href in hrefs) == 1
            assert chat.locator('a[data-mcp-canvas-role="source"]').count() == 1
            assert chat.locator('a[data-mcp-canvas-role="new"]').count() == 1
            assert head.evaluate('node=>node.scrollWidth<=node.clientWidth+1')
            page.screenshot(path=str(tmp_path / f'reported-tool-cards-{width}-{iteration}.png'), full_page=True)
            if iteration == 0:
                page.reload(wait_until='domcontentloaded')
                _dismiss_onboarding_modals(page)
        assert not errors, errors
    finally:
        context.request.delete(f'{live_server}/api/v4/conversations/{cid}')
        context.close()
