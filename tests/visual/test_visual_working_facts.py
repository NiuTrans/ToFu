"""Real app owner approval exposes the fact claim and preserves reject semantics."""
from concurrent.futures import ThreadPoolExecutor
import json
from types import SimpleNamespace

import pytest

from tests.turn_lifecycle.test_task_event_delivery import owner as owner, _task, _discard

pytestmark = [pytest.mark.visual, pytest.mark.auth_mode('open')]


@pytest.mark.parametrize('width,approved', [(1280, True), (390, False)])
def test_working_fact_owner_approval_shows_claim_and_source(page, owner, monkeypatch, tmp_path, width, approved):
    from playwright.sync_api import expect
    from lib.storage import get_storage_client
    from lib import turn_lifecycle
    from lib.artifacts import core
    from lib.identity import PrincipalContext
    from lib.memory.working_facts import service
    from lib.tools.working_facts import WORKING_FACT_CONTRACTS
    from lib.tools.registry import all_specs
    from lib.tools.contracts import compile_execution_contract_documents
    from lib.tasks_pkg.tool_dispatch._parse import parse_tool_calls
    from lib.tasks_pkg.tool_dispatch._pipeline import execute_tool_pipeline
    from tests.conftest import _dismiss_onboarding_modals
    client = get_storage_client(write=True)
    monkeypatch.setenv('TOFU_TURN_DELTA_RECORD_MS', '0')
    _, task = _task(turn_lifecycle, SimpleNamespace(client=client), 'browser-working-fact')
    principal = PrincipalContext.user(subject_id='browser-owner', owner_user_id=1)
    task.update(aborted=False, _attended=True, messages=[], model='fixture', _principalContext=principal.to_payload())
    definitions = [contract.provider_schema() for contract in WORKING_FACT_CONTRACTS]
    spec = next(spec for spec in all_specs() if spec.key == 'working_facts')
    task['_executable_tool_catalog'] = definitions
    task['_toolContractDocumentsByName'] = compile_execution_contract_documents(definitions)
    task['_tool_env'] = SimpleNamespace(resolve=lambda name: spec.handler if name in spec.provides else None,
        write_names=spec.write_tools, idempotent_names=spec.cacheable_tools, confirmation_names=set())
    artifact = core.create_artifact(conv_id=task['convId'], content='Customer total is 42.', format='markdown', source='write_file')
    args = {'revision': 0, 'text': 'Customer total is 42.', 'kind': 'observation', 'sources': [
        {'kind': 'artifact', 'id': artifact['id'], 'version': artifact['content_sha256'], 'quote': 'Customer total is 42.'}]}
    def invoke():
        message = {'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'save-fact', 'type': 'function',
            'function': {'name': 'record_working_fact', 'arguments': json.dumps(args)}}]}
        task['messages'].append(message)
        parsed, _ = parse_tool_calls(message, task, 0, 0, True)
        execute_tool_pipeline(task, parsed, {'autoApply': False}, '', False, definitions,
                              task['messages'], [], 0, 'fixture')
        return parsed[0][5], task['messages'][-1]['content']
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        page.set_viewport_size({'width': width, 'height': 1000})
        page.reload(wait_until='domcontentloaded'); _dismiss_onboarding_modals(page)
        page.locator(f'.conv-item[data-conv-id="{task["convId"]}"]').wait_for()
        page.wait_for_function("typeof window.TofuModules?.resolveAction?.('loadConversation')==='function'")
        page.evaluate("id=>window.TofuModules.resolveAction('loadConversation')(id)", task['convId'])
        future = pool.submit(invoke)
        notice = page.locator(f'[data-turn-id="{task["_turnId"]}"] .ptool-pending-wrap')
        try:
            expect(notice).to_be_visible(timeout=15000)
        except AssertionError:
            if future.done():
                future.result()
            raise
        expect(notice).to_contain_text('Customer total is 42.')
        expect(notice).to_contain_text(artifact['id'])
        expect(notice).to_contain_text(artifact['content_sha256'])
        assert service.query_facts(task['convId'], principal=principal)['facts'] == []
        assert not future.done()
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
        page.screenshot(path=str(tmp_path / f'working-fact-approval-{width}.png'), full_page=True)
        notice.locator('.ptool-approve-btn' if approved else '.ptool-reject-btn').click()
        row, result = future.result(timeout=15)
        assert row['status'] == ('done' if approved else 'rejected'), result
        view = service.query_facts(task['convId'], principal=principal)
        assert len(view['facts']) == int(approved)
        if approved:
            assert view['facts'][0]['text'] == 'Customer total is 42.'
        expect(notice).to_have_count(0)
    finally:
        task['aborted'] = True
        pool.shutdown(wait=True, cancel_futures=True)
        from lib.tasks_pkg.event_delivery import flush_task_events
        flush_task_events(task, timeout=10)
        _discard(task)
        client.command('conversation.delete', {'conv_id': task['convId'], 'user_id': 1}, task['id'] + '-cleanup')


@pytest.mark.parametrize('width,theme,language,source_kind', [(1280, 'light', 'en', 'artifact'), (390, 'dark', 'en', 'artifact'), (390, 'tofu', 'zh', 'artifact'), (390, 'tofu', 'zh', 'physical_file'), (390, 'dark', 'zh', 'tool_result'), (1280, 'light', 'en', 'tool_result'), (1280, 'light', 'en', 'turn_criterion'), (390, 'tofu', 'zh', 'turn_criterion')])
def test_working_facts_owner_correction_and_task_impact(page, owner, tmp_path, width, theme, language, source_kind):
    from playwright.sync_api import expect
    from lib import turn_lifecycle
    from lib.artifacts import create_artifact
    from lib.identity import PrincipalContext
    from lib.memory.working_facts import service, consumers
    from lib.storage import get_storage_client
    from tests.conftest import _dismiss_onboarding_modals
    client = get_storage_client(write=True)
    _, task = _task(turn_lifecycle, SimpleNamespace(client=client), 'browser-working-facts')
    principal = PrincipalContext.user(subject_id='browser-owner', owner_user_id=1)
    conv = task['convId']
    artifact = create_artifact(conv_id=conv, task_id=task['id'], content='Budget: 100\nRegion: east',
                               format='markdown', source='write_file', title='Project constraints')

    file_resolver = tool_resolver = None
    selected_source = None
    if source_kind == 'turn_criterion':
        from lib.acceptance import turns
        scope = turns.update_review(conv, task['_turnId'], {'action': 'requirements',
            'requirementsRevision': 0, 'objective': 'Plan within the current budget',
            'criteria': [{'id': 'budget', 'kind': 'requirement', 'text': 'Budget: 100'}],
            'outputs': []}, principal=principal)
        criterion = scope['criteria'][0]
        selected_source = {'kind': 'criterion', 'targetKind': 'turn', 'id': task['_turnId'],
            'criterionId': criterion['id'], 'version': criterion['digest']}
    if source_kind == 'tool_result':
        from lib.tasks_pkg.executor import _build_simple_meta, _finalize_tool_round
        from lib.tasks_pkg.tool_dispatch._parse import parse_tool_calls
        from lib.tasks_pkg.tool_dispatch._pipeline import execute_tool_pipeline
        from lib.tasks_pkg.working_tool_sources import available_tool_sources, tool_quote_resolver
        task.update(_principalContext=principal.to_payload(), aborted=False, messages=[], model='fixture')
        text = 'Budget: 100\nRegion: east'
        def observe(task, tc, name, call_id, args, rn, row, *rest, **kw):
            _finalize_tool_round(task, rn, row, [_build_simple_meta(name, text, source='fixture')], status='done')
            return call_id, text, False
        definitions = [{'type': 'function', 'function': {'name': 'observe_budget', 'description': 'Observe budget',
            'parameters': {'type': 'object', 'properties': {}}}}]
        task['_executable_tool_catalog'] = definitions
        task['_tool_env'] = SimpleNamespace(resolve=lambda name: observe, write_names=set(), idempotent_names=set(), confirmation_names=set())
        message = {'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'budget-observation', 'type': 'function',
            'function': {'name': 'observe_budget', 'arguments': '{}'}}]}
        parsed, _ = parse_tool_calls(message, task, 0, 0, True)
        execute_tool_pipeline(task, parsed, {'autoApply': True}, '', False, definitions, task['messages'], [], 0, 'fixture')
        selected_source = available_tool_sources(task)['toolSources'][0]
        tool_resolver = tool_quote_resolver(task)
    if source_kind == 'physical_file':
        import re
        from lib.project_mod.tools import execute_tool
        from lib.tasks_pkg.working_file_sources import FileReadCapture, file_witness_resolver
        task.update(_principalContext=principal.to_payload(), aborted=False)
        task.setdefault('config', {})['projectPath'] = str(tmp_path)
        file_path = tmp_path / 'constraints.txt'; file_path.write_text('Budget: 100\nRegion: east')
        capture = FileReadCapture(task, 'browser-file-read')
        result = execute_tool('read_files', {'path': 'constraints.txt', 'bind_sources': True}, str(tmp_path),
            task=task, conv_id=conv, task_id=task['id'], file_source_capture=capture)
        file_source = json.loads(re.search(r'\[File source: (\{.*\})\]', result)[1])
        capture.publish({'status': 'done'})
        file_resolver = file_witness_resolver(task)

    def record(text, sources, kind='observation'):
        revision = service.query_facts(conv, principal=principal)['revision']
        view = service.record_fact(conv, {'action': 'record', 'revision': revision, 'text': text,
            'kind': kind, 'sources': sources}, principal=principal, task_id=task['id'], file_sources=file_resolver, tool_sources=tool_resolver)
        return next(fact for fact in view['facts'] if fact['text'] == text)

    source = {'kind': 'artifact', 'id': artifact['id'], 'version': artifact['content_sha256']}
    fact = record('Budget: 100', [{**(selected_source or (file_source if source_kind == 'physical_file' else source)), 'quote': 'Budget: 100'}])
    derived = record('Plan within the budget', [{'kind': 'fact', 'id': fact['id'], 'version': fact['revision']}], 'derived')
    independent = record('Region: east', [{**source, 'quote': 'Region: east'}])
    for name, basis, confirm in [('dependent', derived, True), ('independent', independent, True), ('uncertain', fact, False)]:
        refs = [{'id': basis['id'], 'revision': basis['revision']}]
        consumers.prepare(conv, principal=principal, task_id=name, facts=refs, turn_id=task['_turnId'])
        if confirm:
            consumers.confirm(conv, principal=principal, task_id=name, facts=refs)
    en = language == 'en'
    labels = {
        'open': 'Current basis' if en else '当前依据', 'facts': 'Current facts' if en else '当前事实',
        'impact': 'Task impact' if en else '任务影响', 'correction': 'Corrected text' if en else '纠正后的内容',
        'note': 'Reason or review note' if en else '原因或核验说明', 'save': 'Save correction' if en else '保存纠正',
        'close': 'Close' if en else '关闭', 'revalidate': 'Needs revalidation' if en else '需要重新核验',
        'unchanged': 'Recorded basis unchanged' if en else '已记录的依据未变',
        'uncertain': 'Use not fully confirmed' if en else '尚未完全确认使用情况',
        'reload': 'Reload latest facts and impact' if en else '重新读取事实和影响',
        'saved': 'Owner decision saved' if en else '用户决定已保存',
        'error': 'Decision was not confirmed' if en else '尚未确认决定已保存',
        'stale': 'Source changed' if en else '来源已变更',
    }
    page.add_init_script("window.__workingFactsAppReady = false; window.addEventListener('tofu:app-ready', () => { window.__workingFactsAppReady = true; });")

    def reload_application():
        page.reload(wait_until='domcontentloaded')
        page.wait_for_function('window.__workingFactsAppReady === true')
        _dismiss_onboarding_modals(page)

    try:
        page.set_viewport_size({'width': width, 'height': 1000})
        page.evaluate('language=>localStorage.setItem("tofu_ui_lang",language)', language)
        reload_application()
        page.evaluate('theme=>document.documentElement.dataset.theme=theme', theme)
        page.wait_for_function("typeof window.TofuModules?.resolveAction?.('loadConversation')==='function'")
        page.locator(f'.conv-item[data-conv-id="{conv}"]').wait_for(state="attached")
        page.evaluate("id=>window.TofuModules.resolveAction('loadConversation')(id)", conv)
        page.wait_for_function('id => document.querySelector(".conversation-surface")?.dataset.conversationId === id', arg=conv)

        def open_basis():
            page.locator('#contextHealthBar').click()
            page.get_by_role('button', name=labels['open'], exact=True).click()
            dialog = page.locator('[data-working-facts-dialog]')
            expect(dialog).to_be_visible()
            expect(dialog.locator('[data-working-fact]')).to_have_count(3)
            return dialog

        dialog = open_basis()
        entry = dialog.locator(f'[data-working-fact="{fact["id"]}"]')
        entry.locator('summary').click()
        if source_kind == 'turn_criterion':
            expect(entry).to_contain_text(task['_turnId'] + ' / budget')
            expect(entry).to_contain_text('Turn · Acceptance criterion' if en else '轮次 · 验收条件')
            turns.update_review(conv, task['_turnId'], {'action': 'requirements',
                'requirementsRevision': scope['requirementsRevision'], 'objective': scope['objective'],
                'criteria': [{'id': 'budget', 'kind': 'requirement', 'text': 'Budget: 150'}],
                'outputs': []}, principal=principal)
            dialog.get_by_role('button', name=labels['reload'], exact=True).click()
            expect(entry.locator('summary')).to_contain_text(labels['stale'])
            expect(dialog.locator(f'[data-working-fact="{derived["id"]}"] summary')).to_contain_text(labels['stale'])
            expect(dialog.locator(f'[data-working-fact="{independent["id"]}"] summary')).not_to_contain_text(labels['stale'])
            page.screenshot(path=str(tmp_path / f'working-turn-criterion-{language}-{width}.png'), full_page=True)
        if source_kind == 'tool_result':
            expect(entry).to_contain_text('observe_budget')
            expect(entry).to_contain_text('Historical tool observation' if en else '历史工具观察')
            expect(entry).to_contain_text('Original source coverage' if en else '原始来源覆盖范围')
            expect(entry.locator('time')).to_be_visible()
            if not en:
                import re
                expect(entry.locator('time')).to_have_text(re.compile(r'^\d{4}/\d{1,2}/\d{1,2}'))
            page.screenshot(path=str(tmp_path / f'working-tool-evidence-{language}-{width}.png'), full_page=True)
        if source_kind == 'physical_file':
            expect(entry).to_contain_text(str(file_path))
            expect(entry).to_contain_text(file_source['version'])
            file_path.write_text('Budget: 150\nRegion: east')
            dialog.get_by_role('button', name=labels['reload'], exact=True).click()
            expect(entry.locator('summary')).to_contain_text(labels['stale'])
            expect(dialog.locator(f'[data-working-fact="{derived["id"]}"] summary')).to_contain_text(labels['stale'])
            expect(entry.get_by_label(labels['correction'], exact=True)).to_be_visible()
        entry.get_by_label(labels['correction'], exact=True).fill('Budget: 200')
        entry.get_by_label(labels['note']).fill('Owner approved the revised budget')
        entry.get_by_role('button', name=labels['save'], exact=True).click()
        expect(dialog).to_contain_text(labels['saved'])
        expect(dialog.locator('[data-working-fact]')).to_have_count(3)
        expect(dialog.locator(f'[data-working-fact="{derived["id"]}"] summary')).to_contain_text(labels['stale'])
        expect(entry.locator('summary')).to_contain_text('Budget: 200')
        dialog.get_by_role('tab', name=labels['impact'], exact=True).click()
        expect(dialog.get_by_role('tab', name=labels['impact'], exact=True)).to_have_attribute('aria-selected', 'true')
        expect(dialog.get_by_role('tab', name=labels['facts'], exact=True)).to_have_attribute('aria-selected', 'false')
        expect(dialog.locator('[data-fact-consumer="dependent"]')).to_contain_text(labels['revalidate'])
        expect(dialog.locator('[data-fact-consumer="independent"]')).to_contain_text(labels['unchanged'])
        expect(dialog.locator('[data-fact-consumer="uncertain"] summary')).to_contain_text(labels['uncertain'])
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
        assert dialog.evaluate('el=>el.scrollWidth <= el.clientWidth + 1')
        page.screenshot(path=str(tmp_path / f'working-facts-impact-{theme}-{width}.png'), full_page=True)
        dialog.get_by_role('button', name=labels['close'], exact=True).click()
        expect(dialog).to_have_count(0)
        # Fresh application load reads selected durable storage, never a local UI cache.
        reload_application()
        page.evaluate('theme=>document.documentElement.dataset.theme=theme', theme)
        page.wait_for_function("typeof window.TofuModules?.resolveAction?.('loadConversation')==='function'")
        page.locator(f'.conv-item[data-conv-id="{conv}"]').wait_for(state="attached")
        page.evaluate("id=>window.TofuModules.resolveAction('loadConversation')(id)", conv)
        page.wait_for_function('id => document.querySelector(".conversation-surface")?.dataset.conversationId === id', arg=conv)
        dialog = open_basis()
        entry = dialog.locator(f'[data-working-fact="{fact["id"]}"]')
        expect(entry.locator('summary')).to_contain_text('Budget: 200')
        entry.locator('summary').click()
        entry.get_by_label(labels['correction'], exact=True).fill('My unsaved correction')
        entry.get_by_label(labels['note']).fill('My unsaved reason')
        url = page.evaluate('location.origin') + f'/api/v4/conversations/{conv}/working-facts'
        latest = page.request.get(url).json()['data']
        changed = page.request.post(url, data={'action': 'correct', 'revision': latest['revision'], 'id': fact['id'],
            'text': 'Changed in another tab', 'note': 'Concurrent owner update'})
        assert changed.status == 200
        entry.get_by_role('button', name=labels['save'], exact=True).click()
        expect(dialog).to_contain_text(labels['error'])
        expect(entry.get_by_label(labels['correction'], exact=True)).to_have_value('My unsaved correction')
        expect(entry.get_by_role('button', name=labels['save'], exact=True)).to_be_disabled()
        dialog.get_by_role('button', name=labels['reload'], exact=True).click()
        expect(entry.locator('summary')).to_contain_text('Changed in another tab')
        expect(entry.get_by_label(labels['correction'], exact=True)).to_have_value('My unsaved correction')
        page.screenshot(path=str(tmp_path / f'working-facts-correction-{theme}-{width}.png'), full_page=True)
        # Native Escape and context navigation retire the same mount and its reads.
        page.keyboard.press('Escape'); expect(dialog).to_have_count(0)
        dialog = open_basis()
        page.evaluate("window.TofuModules.resolveAction('newChat')()")
        expect(dialog).to_have_count(0)
    finally:
        task['aborted'] = True; _discard(task)
        client.command('conversation.delete', {'conv_id': conv, 'user_id': 1}, task['id'] + '-cleanup')
