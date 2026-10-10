"""Real Chromium layout/focus smoke using retained sources, no production state."""
from pathlib import Path
import json

import pytest

from tests._runtime_sections import ROOT, runtime_section, native_module_path

pytestmark = [pytest.mark.visual, pytest.mark.slow]


def test_compaction_drawer_post_stage_and_summary_focus(browser, tmp_path):
    context = browser.new_context(viewport={"width": 1100, "height": 800})
    page = context.new_page()
    try:
        page.set_content('<!doctype html><html><body></body></html>')
        page.add_style_tag(path=str(ROOT / 'frontend/src/styles/application/17-compaction-context-rail.css'))
        locale = json.loads((ROOT / 'frontend/src/i18n/locales/en.json').read_text())
        page.evaluate('(locale) => { window.t = key => locale[key] || key; }', locale)
        page.add_script_tag(content='''
          var runtimeScope = {};
          var escapeHtml = s => String(s ?? '').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;');
          var _applyI18n = () => document.querySelectorAll('[data-i18n],[data-i18n-html]').forEach(el => {
            if (el.dataset.i18nHtml) el.innerHTML = t(el.dataset.i18nHtml);
            else el.textContent = t(el.dataset.i18n);
          });
          var archive = {id:'visual-archive', createdAt:1724587200000, receipt:{implementation:'deterministic_recovery_receipt'}};
          var messages = [
            {role:'system',content:'Stored system'},
            {role:'user',content:'<retained_user_messages>ordinary user text</retained_user_messages>'},
            {role:'assistant',content:null,tool_calls:[{id:'c1',function:{name:'context_compact',arguments:'{}'}}]},
            {role:'tool',tool_call_id:'c1',content:'Deterministic task recovery'}
          ];
          var Api = {compactions:{
            list:async()=>({compactions:[archive]}),
            getSummary:async()=>({archive}),
            getPost:async()=>({archive,postMessages:messages})
          }};
        ''')
        helper = native_module_path('.native/visual-compaction-snapshot.js', ROOT / 'frontend/src/features/compaction-snapshot.ts')
        page.add_script_tag(content=Path(helper).read_text())
        page.add_script_tag(content=runtime_section('compaction-viewer.js'))
        page.evaluate("runtimeScope.openCompactionViewer('visual-conversation')")
        drawer = page.locator('#compactionViewerDrawer')
        assert drawer.locator('[data-tab="messages"]').get_attribute('class').endswith('is-active')
        assert 'not the first API request' in drawer.inner_text()
        assert drawer.locator('[data-compaction-kind="recovery"]').count() == 1
        assert drawer.locator('.cd-msg').nth(1).get_attribute('data-compaction-kind') == 'unknown'
        drawer.locator('[data-jump-summary]').click()
        assert page.evaluate("document.activeElement.dataset.compactionKind") == 'recovery'
        assert drawer.locator('.cd-msg').nth(3).inner_text().endswith('Deterministic task recovery')
        drawer.screenshot(path=str(tmp_path / 'compaction-result.png'))
    finally:
        context.close()


@pytest.mark.parametrize('manual', [False, True])
def test_built_app_archive_projection_and_export(page, monkeypatch, tmp_path, manual):
    """Real app/lazy bundle/HTTP owner; synthetic archive, no provider or user data."""
    import routes.conversations_compaction as route

    archive = {'id': 'visual-archive', 'createdAt': 1724587200000,
               'summary': 'accepted continuation', 'trigger': 'manual' if manual else 'force',
               'receipt': {'implementation': 'model_summary'}}
    summary = ([{'role': 'assistant', '_isCompactionSummary': True,
                 'content': 'accepted continuation'}] if manual else [
        {'role': 'user', '_isMeta': True,
         '_compactionHandoff': {'version': 'tofu.compaction-handoff/v1', 'phase': 'mid_turn'},
         'content': '<compaction_handoff>accepted continuation</compaction_handoff>'}])
    messages = [
        {'role': 'system', 'content': '  native policy\n'},
        {'role': 'user', '_isMeta': True, '_retainedUserHistoryVersion': 2,
         '_retainedUserHistory': [{'kind': 'opening_request', 'content': 'historical ask'}],
         'content': '<retained_user_messages>historical ask</retained_user_messages>'},
        *summary,
        {'role': 'user', 'content': '<retained_user_messages>not a producer marker</retained_user_messages>'},
        {'role': 'user', '_isMeta': True, 'content': 'surviving runtime context'},
    ]
    reads = []

    class ArchiveStore:
        def list_compaction_archives(self, conv_id, *, user_id):
            assert conv_id == 'visual-conversation' and user_id > 0
            return [archive]

        def get_compaction_archive(self, conv_id, archive_id, *, user_id,
                                   include_messages, include_post_messages):
            assert conv_id == 'visual-conversation' and archive_id == archive['id']
            assert user_id > 0
            reads.append((include_messages, include_post_messages))
            document = {'archive': archive}
            if include_post_messages:
                document['postMessages'] = messages
            return document

    monkeypatch.setattr(route, 'get_conversation_store', ArchiveStore)
    page.set_viewport_size({'width': 650 if manual else 1100, 'height': 800})
    page.evaluate('''() => {
      window.__copiedCompaction = null;
      Object.defineProperty(navigator, 'clipboard', { configurable:true, value:{
        writeText: async text => { window.__copiedCompaction = JSON.parse(text); }
      }});
    }''')
    page.evaluate("window.TofuModules.invokeFeature('openCompactionViewer', ['visual-conversation'], () => {})")
    drawer = page.locator('#compactionViewerDrawer')
    assert drawer.locator('.compaction-msg-list > li').count() == len(messages)
    assert drawer.locator('[data-compaction-kind="summary"]').count() == 1
    assert drawer.locator('[data-compaction-kind="history"]').count() == 1
    assert drawer.locator('[data-compaction-kind="runtime"]').count() == 1
    assert drawer.locator('.cd-msg').nth(len(messages) - 2).get_attribute('data-compaction-kind') == 'unknown'
    assert drawer.locator('.cd-msg').nth(0).locator('.cd-content code').inner_text() == '  native policy\n'
    drawer.locator('[data-jump-summary]').click()
    assert page.evaluate('document.activeElement.dataset.compactionKind') == 'summary'
    drawer.locator('[data-action="copy-json"]').click()
    page.wait_for_function('window.__copiedCompaction !== null')
    copied = page.evaluate('window.__copiedCompaction')
    with page.expect_download() as download_info:
        drawer.locator('[data-action="download"]').click()
    downloaded = json.loads(Path(download_info.value.path()).read_text())
    assert downloaded == copied
    assert copied['postMessages'] == messages
    assert copied['postSnapshot'] == {
        'stage': 'compaction_result', 'status': 'captured',
        'includesRuntimeRecomposition': False, 'includesProviderAdaptation': False,
    }
    # Copy/download reuse the loaded selected snapshot without a second full read.
    assert reads == [(False, False), (False, True)]
    screenshot = tmp_path / ('manual-result.png' if manual else 'automatic-result.png')
    drawer.screenshot(path=str(screenshot))
    print(f'Compaction screenshot: {screenshot}')
    drawer.locator('button[data-close]').click()


@pytest.mark.parametrize('language,width', [('en', 1100), ('zh', 390)])
def test_built_app_receipt_certainty_and_completion(page, monkeypatch, tmp_path, language, width):
    """Serve synthetic receipts through real routes and the built lazy drawer.

    These fixtures verify presentation, not real provider quality, latency or savings.
    The shared live_server/page fixtures enforce disposable storage and disable LLMs.
    """
    from copy import deepcopy
    from playwright.sync_api import expect
    import routes.conversations_compaction as route

    locale = json.loads((ROOT / f'frontend/src/i18n/locales/{language}.json').read_text())
    receipt = json.loads((ROOT / 'tests/fixtures/compaction/receipt_v1.json').read_text())
    known = deepcopy(receipt)
    known['status'] = 'completed'
    known['summary']['accepted'] = True
    known['summary']['cost'].update(known=True, costUsd=0.002, unknownCallCount=0)
    known['summary']['cost']['calls'] = known['summary']['cost']['calls'][:1]
    known['summary']['cost']['callCount'] = 1
    known['economics'].update(pricingKnown=True, summaryCostUsd=0.002, paybackRounds=0.5)
    known['contextBudget']['decisionKind'] = 'safety'
    failed = deepcopy(receipt)
    failed['status'] = 'failed'
    failed['summary'].update(generated=False, complete=False, accepted=False)
    failed['summary']['cost'].update(known=True, costUsd=0.0000002, calls=[])
    legacy = deepcopy(receipt)
    for key in ('complete', 'route', 'cost'):
        legacy['summary'].pop(key)
    legacy['contextBudget'] = {'decisionKind': 'future_decision'}
    legacy['economics'] = {'summaryCostTokens': 100, 'summaryCostUsd': 0, 'paybackRounds': 0.5}
    archives = [
        {'id': name, 'createdAt': 1724587200000 + index, 'trigger': 'working_set',
         'model': 'main-task-not-summary', 'tokensBefore': 20000, 'tokensAfter': 8000,
         'receipt': value, 'summary': 'UNUSABLE_PARTIAL_OUTPUT' if name == 'failed' else 'Complete fixture summary'}
        for index, (name, value) in enumerate([
            ('unknown', receipt), ('known', known), ('failed', failed), ('legacy', legacy)])
    ]
    reads = []

    class ArchiveStore:
        def list_compaction_archives(self, conv_id, *, user_id):
            assert conv_id == 'visual-receipt-conversation' and user_id > 0
            return archives

        def get_compaction_archive(self, conv_id, archive_id, *, user_id,
                                   include_messages, include_post_messages):
            assert conv_id == 'visual-receipt-conversation' and user_id > 0
            reads.append((archive_id, include_messages, include_post_messages))
            result = {'archive': next(a for a in archives if a['id'] == archive_id)}
            if include_post_messages:
                result['postMessages'] = [{'role': 'user', 'content': 'fixture original context'}]
            return result

    monkeypatch.setattr(route, 'get_conversation_store', ArchiveStore)
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.set_viewport_size({'width': width, 'height': 900})
    page.evaluate('(language) => localStorage.setItem("tofu_ui_lang", language)', language)
    # Live push/polling has no network-idle boundary; wait for the feature and locale.
    page.reload(wait_until='domcontentloaded')
    page.wait_for_function('window.TofuModules?.invokeFeature !== undefined')
    expect(page.locator('html')).to_have_attribute('lang', 'zh-CN' if language == 'zh' else 'en')
    drawer = page.locator('#compactionViewerDrawer')

    def label(key):
        return locale['compactionViewer.' + key]

    def metric(key):
        return drawer.locator('.cd-receipt-metric').filter(
            has=page.get_by_text(label(key), exact=True)).locator('strong').first

    def open_archive(archive_id):
        page.evaluate('''archiveId => window.TofuModules.invokeFeature(
            'openCompactionViewer', ['visual-receipt-conversation', archiveId], () => {})''', archive_id)
        expect(drawer).to_be_visible()
        drawer.locator('[data-tab="summary"]').click()
        expect(drawer.locator('.cd-receipt')).to_be_visible()

    open_archive('unknown')
    expect(metric('receiptModel')).to_have_text('fixture-summary-model')
    expect(metric('receiptProvider')).to_have_text('fixture-summary-provider')
    expect(metric('receiptRoutePolicy')).to_have_text(label('policy.qualifiedCheapest'))
    expect(metric('receiptComplete')).to_have_text(label('yes'))
    expect(metric('receiptAccepted')).to_have_text(label('no'))
    expect(metric('receiptUsd')).to_have_text(label('receiptUnknown'))
    expect(metric('receiptKnownSubtotal')).to_have_text('$0.002000')
    expect(metric('receiptPayback')).to_have_text(label('receiptUnknown'))
    expect(metric('receiptWindow')).to_have_text('128.0k')
    expect(metric('receiptCeiling')).to_have_text('120.0k')
    expect(metric('receiptReserve')).to_have_text('8.0k')
    expect(metric('receiptRendered')).to_have_text('96.0k')
    expect(metric('receiptEconomicTarget')).to_have_text('64.0k')
    expect(metric('receiptDecision')).to_have_text(label('decision.economic'))
    expect(drawer.get_by_text(label('receiptCostCaveat'), exact=True)).to_be_visible()

    expect(drawer.get_by_text(label('receiptBudgetCaveat'), exact=True)).to_be_visible()
    expect(metric('receiptPricingKnown')).to_have_text(label('no'))
    expect(drawer.locator('.cd-summary')).to_have_text('Complete fixture summary')
    drawer.locator('.cd-receipt-files > summary').filter(has_text=label('receiptAttempts')).click()
    expect(drawer.get_by_text('fixture-first-provider', exact=True)).to_be_visible()
    assert drawer.evaluate('el => el.scrollWidth <= el.clientWidth + 1')
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
    screenshot = tmp_path / f'compaction-receipt-{language}-{width}.png'
    drawer.screenshot(path=str(screenshot))
    print(f'Compaction receipt screenshot: {screenshot}')

    open_archive('known')
    expect(metric('receiptAccepted')).to_have_text(label('yes'))

    expect(metric('receiptPricingKnown')).to_have_text(label('yes'))
    expect(metric('receiptUsd')).to_have_text('$0.002000')
    expect(metric('receiptSummaryCost')).to_have_text('$0.002000')
    expect(metric('receiptPayback')).to_have_text(label('receiptRounds').replace('{n}', '0.5'))
    expect(metric('receiptDecision')).to_have_text(label('decision.safety'))

    open_archive('failed')
    expect(metric('receiptComplete')).to_have_text(label('no'))
    expect(metric('receiptAccepted')).to_have_text(label('no'))
    expect(metric('receiptUsd')).to_have_text('< $0.000001')
    expect(drawer.locator('.cd-summary')).to_have_count(0)
    expect(drawer.get_by_text(label('incompleteSummaryHidden'), exact=True)).to_be_visible()
    assert 'UNUSABLE_PARTIAL_OUTPUT' not in drawer.inner_text()

    open_archive('legacy')
    for key in ('receiptComplete', 'receiptModel', 'receiptUsd', 'receiptPayback',
                'receiptWindow', 'receiptCeiling', 'receiptReserve', 'receiptDecision'):
        expect(metric(key)).to_have_text(label('receiptUnknown'))
    assert 'compactionViewer.' not in drawer.inner_text()
    drawer.locator('button[data-close]').click()
    assert {row[0] for row in reads} == {'unknown', 'known', 'failed', 'legacy'}
    assert not errors
