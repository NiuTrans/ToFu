"""Session-start context notes: one disclosure in both real browser render paths.

Synthetic Turns stay in a disposable Chromium page. The retained timeline is
loaded from its authored renderer + batching module, the typed Surface from its
production module graph, with real shipped CSS. No user data is touched;
this layout check does not require starting a second storage server.
"""
from __future__ import annotations

import json
import os

import pytest

from tests._runtime_sections import ROOT, native_module_path
from tests.context.test_system_note_context_grouping import _STUBS, _extract_timeline_fns

pytestmark = pytest.mark.visual


REASONS = (
    'session_context_committed', 'relevant_memories', 'memory_guidance',
    'current_date', 'skills_index', 'parallel_execution', 'programmatic_execution', 'project_rules',
    'credential_vault', 'environment',
)


def _notes(legacy=False):
    return [
        {
            'type': 'system_note', 'blockId': f'system-note:fixture:{index}',
            'noteKind': 'system-intervention' if legacy else ('session-context' if index == 0 else 'context-injection'),
            'reason': reason, 'action': 'prompt', 'llmRound': 0,
            'text': ('Session context <script>window.noteScriptExecuted=true</script>'
                     if index == 0 else f'Wire text for {reason}'),
        }
        for index, reason in enumerate(REASONS)
    ]


@pytest.mark.parametrize('width,language', [(1100, 'zh'), (390, 'en')])

@pytest.mark.parametrize('legacy', [False, True])
def test_session_context_is_one_expandable_row_in_both_render_paths(
        browser, request, tmp_path, width, language, legacy):
    # Use the explicit local app when supplied; otherwise boot an isolated app.
    origin = os.environ.get('TOFU_E2E_BASE_URL') or request.getfixturevalue('live_server')
    context = browser.new_context(viewport={'width': width, 'height': 960})
    page = context.new_page()
    page.goto(origin, wait_until='domcontentloaded')

    # The app origin establishes a real-server test; the synthetic document
    # needs no transport subscriptions or further API traffic.
    page.evaluate('window.stop()')
    page.route('**/api/**', lambda route: route.fulfill(status=200, content_type='application/json', body='{"data":{},"meta":{}}'))
    segments = _notes(legacy) + [
        {'type': 'thinking', 'blockId': 'thinking:fixture', 'text': 'Now reason', 'llmRound': 0},
        {'type': 'tool_use', 'blockId': 'tool:fixture', 'id': 'read-1',
         'name': 'read_files', 'llmRound': 0},
        # A reason-specialized intervention is not a context-tail member.
        {'type': 'system_note', 'blockId': 'system-note:modified-files',
         'noteKind': 'context-injection', 'reason': 'modified_files',
         'action': 'prompt', 'text': 'Separate modified-files recall', 'llmRound': 1},
        {'type': 'text', 'blockId': 'text:fixture', 'text': 'Next step', 'llmRound': 1},
        {'type': 'text', 'blockId': 'answer:fixture', 'text': 'Done',
         'deliverable': True, 'terminal': True},
    ]
    rounds = [{'roundNum': 1, 'llmRound': 0, 'toolCallId': 'read-1',
               'toolName': 'read_files', 'status': 'done',
               'toolArgs': {'path': 'synthetic.txt'},
               'results': [{'text': 'Synthetic read result'}]}]
    page.set_content('<!doctype html><html data-theme="tofu"><body>'
                     '<main id="typed" class="conversation-surface"></main>'
                     '<main id="legacy" class="conversation-surface"></main>'
                     '</body></html>')
    page.add_style_tag(path=str(ROOT / 'static/styles.css'))
    page.add_style_tag(content=(
        'body{display:block!important;margin:0;padding:12px;box-sizing:border-box;overflow-x:hidden}'
        '#typed,#legacy{width:100%;min-width:0;box-sizing:border-box;--rail-w:0px}'
        '#legacy{margin-top:24px}'
    ))
    locale = json.loads((ROOT / f'frontend/src/i18n/locales/{language}.json').read_text())
    page.evaluate(r"""locale => {
      window.fixtureLocale = locale;
      window.t = (key, values) => (locale[key] || key).replace(/\{(\w+)\}/g,
        (match, name) => values?.[name] ?? match);
      window.escapeHtml = value => String(value ?? '').replaceAll('&','&amp;')
        .replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;');
    }""", locale)
    page.add_script_tag(path=native_module_path(
        '.native/context-note-group-surface.js',
        ROOT / 'frontend/src/conversation/index.ts'))

    page.add_script_tag(path=native_module_path(
        '.native/context-provenance-visual.js',
        ROOT / 'frontend/src/conversation/presentation/turn-provenance.ts'))
    # The legacy renderer is the authored implementation plus its native
    # batching owner. Keep its shell-only ports; use the locale and HTML
    # escaping defined above instead of the unit harness's echo stubs.
    shell_ports = '\n'.join(line for line in _STUBS.splitlines()
                            if not line.startswith(('function t(', 'function escapeHtml(')))
    page.add_script_tag(content=shell_ports + '\n' + _extract_timeline_fns()
                        + '\nwindow.renderSegmentTimelineHTML = renderSegmentTimelineHTML;')
    page.evaluate(r"""data => {
      const turn = {
        turnId:'fixture-turn',conversationId:'fixture',laneId:'main',ordinal:1,
        actor:'assistant',kind:'reply',status:'completed',currentAttemptId:null,
        projectionRevision:1,createdAt:1,updatedAt:2,
        projection:{segments:data.segments,toolRounds:data.rounds,provenance:{
          blockId:'provenance:fixture',memoryPrefetch:{phase:'done',selected:2,
          memories:[{name:'FIRST MEMORY',description:'A safely escaped <img src=x onerror="window.noteScriptExecuted=true"> memory',scope:'project'},
            {name:'SECOND MEMORY',description:'A second memory',scope:'project'}]}}},
      };
      const state = {
        conversationId:'fixture',conversationRevision:1,transport:'live',
        turnsById:{'fixture-turn':turn},laneOrder:{main:['fixture-turn']},
        attemptsById:{},queueItems:[],pendingEventsByTurn:{},commandPending:{},
        liveRoundUsageByTurn:{},
      };
      const provenance = createTurnProvenancePresentation({translate:t,escapeHtml,escAttr:escapeHtml,
        iconHtml:() => '<span></span>',asMarkdownText:String,formatToolInlineMarkdown:String});
      const renderers = createClassicConversationRenderers({
        renderSafeMarkdownHtml:escapeHtml,

        renderContextProvenanceHtml:provenance.renderContextProvenanceHtml,
        renderToolBlockHtml:() => '<div>synthetic read</div>',
        localizedText:(key, fallback, values) => fixtureLocale[key]
          ? t(key, values) : fallback.replace(/\{(\w+)\}/g,
              (match, name) => values?.[name] ?? match),
      });
      const surface = createConversationSurface(document.querySelector('#typed'),renderers);
      surface.render(selectConversationViewModel(state));

      window.fixtureUpdate = () => {
        const updated = {...turn,projectionRevision:2,
          projection:{...turn.projection,provenance:{blockId:'provenance:fixture',
          memoryPrefetch:{phase:'done',selected:3,memories:[{name:'UPDATED MEMORY'}]}}}};
        surface.render(selectConversationViewModel({...state,conversationRevision:2,
          turnsById:{'fixture-turn':updated}}));
      };
      window.fixtureDispose = () => surface.dispose();
    }""", {'segments': segments, 'rounds': rounds})
    legacy_html = page.evaluate("""data => window.renderSegmentTimelineHTML(
      data.segments, {segments:data.segments,toolRounds:data.rounds}, 0)
    """, {'segments': segments, 'rounds': rounds})
    assert legacy_html
    page.evaluate("html => document.querySelector('#legacy').innerHTML = html", legacy_html)
    legacy_panel = page.locator('#legacy .ptool-panel')
    if legacy_panel.count():
        legacy_panel.evaluate("node => node.classList.remove('collapsed')")

    expected_first_text = _notes()[0]['text']
    for root in ('#typed', '#legacy'):
        groups = page.locator(f'{root} .sw-note-group')
        assert groups.count() == 1, root
        group = groups.first
        assert group.locator('.sw-note-group-count').inner_text() == (
            locale['systemNote.groupItemsTitle'].replace('{n}', str(len(REASONS))))
        assert group.locator('.sw-note-group-item').count() == len(REASONS)
        assert group.locator('.sw-note-group-item').evaluate_all(
            '(nodes) => nodes.map(node => node.dataset.reason)') == list(REASONS)
        assert page.locator(f'{root} .sw-system-note-row').count() == 2
        assert page.locator(f'{root} .sw-system-note-row[data-reason="modified_files"]').count() == 1
        assert group.get_attribute('open') is None

        assert not group.locator('.sw-note-group-body').is_visible()
        assert not group.locator('pre').first.is_visible()
        group.locator('summary').click()
        assert group.locator('pre').first.is_visible()
        assert group.locator('pre').first.inner_text() == expected_first_text
        assert group.locator('script, img, [onerror]').count() == 0
        assert page.evaluate('window.noteScriptExecuted === true') is False
        assert group.locator('.sw-note-group-item').last.locator('pre').inner_text() == (
            'Wire text for environment')
        if root == '#typed':
            assert group.locator('[data-provenance-field="memoryPrefetch"]').count() == 1
            assert group.locator('.sw-note-group-item[data-reason="relevant_memories"]').inner_text().count('FIRST MEMORY') == 1
            assert page.locator('#typed .tp-segs, #typed .turn-prov').count() == 0
            assert group.locator('img, [onerror]').count() == 0
            page.evaluate('fixtureUpdate()')
            assert group.get_attribute('open') is not None
            assert 'UPDATED MEMORY' in group.inner_text()
            assert 'FIRST MEMORY' not in group.inner_text()
        # A zero-overflow disclosure can still be unusable when its parent
        # squeezes it to one character per line. The real shell gives mobile
        # turns nearly the whole viewport; verify that contract here too.
        widths = group.evaluate('''node => [...function*(){for(let p=node,i=0;p&&i<9;p=p.parentElement,i++) yield {cls:p.className,client:p.clientWidth,rect:p.getBoundingClientRect().width,computed:getComputedStyle(p).display,grid:getComputedStyle(p).gridTemplateColumns}}()]''')
        assert widths[0]['rect'] >= width * .6, (root, '\n'.join(f"{w['cls']}={w['rect']}:{w['grid']}" for w in widths))
        label_widths = group.locator('.sw-note-group-item-head > .ptool-text').evaluate_all(
            '(nodes) => nodes.map(el => ({width:el.getBoundingClientRect().width, size:parseFloat(getComputedStyle(el).fontSize)}))')
        assert all(value['width'] >= value['size'] * 6 for value in label_widths), (root, label_widths)
        overflow = group.evaluate('''node => ({client:node.clientWidth,scroll:node.scrollWidth,
          children:[...node.querySelectorAll('summary, .sw-note-group-body, .sw-note-group-item, .sw-note-group-item-head, .sw-note-reason')]
            .filter(el => el.scrollWidth > el.clientWidth + 1)
            .slice(0,5).map(el => ({cls:el.className,client:el.clientWidth,scroll:el.scrollWidth})),
          parents:[...function*(){for(let p=node,i=0;p&&i<7;p=p.parentElement,i++)yield {cls:p.className,client:p.clientWidth,scroll:p.scrollWidth}}()]})''')
        assert overflow['scroll'] <= overflow['client'] + 1, (root, overflow)
        screenshot = tmp_path / f'context-notes-{root[1:]}-{language}-{width}.png'
        group.screenshot(path=str(screenshot))
        print(f'Context note group ({root}, {language}, {width}): {screenshot}')
    page.evaluate('fixtureDispose()')
    context.close()
