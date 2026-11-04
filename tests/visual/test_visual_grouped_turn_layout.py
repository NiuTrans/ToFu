"""Grouped reply reading through the Surface, real presenters and shipped CSS.

A reply reads as one reasoning summary, ONE tool panel and one fold of interim
notes joined in a single activity card, then its answer. The panel holds every
call plus the failures between them, each failure directly below the call it
followed, in one issue style. No persisted conversation is touched: an inert
iframe mounts a synthetic Turn graph.
"""
from __future__ import annotations

import json

import pytest

from tests._runtime_sections import native_module_path, runtime_section_path
from tests.support.frontend_visual import ROOT, frontend_visual_origin, frontend_visual_page  # noqa: F401

pytestmark = pytest.mark.visual


@pytest.fixture(scope='module')
def grouped_surface_bundle(tmp_path_factory):
    entry = tmp_path_factory.mktemp('grouped-surface') / 'grouped-surface.ts'
    entry.write_text(
        f"export * from '{ROOT}/frontend/src/conversation/index.ts';\n"
        f"export {{ setLanguage }} from '{ROOT}/frontend/src/i18n/index.ts';\n")
    return native_module_path('.native/grouped-reading-surface.js', entry)


@pytest.mark.parametrize('width,language,theme', [
    (1100, 'en', 'light'), (1100, 'zh', 'tofu'), (1100, 'en', 'dark'), (390, 'zh', 'tofu'),
])
def test_reply_groups_reasoning_tools_and_prose_with_inline_failures(
    frontend_visual_page, grouped_surface_bundle, tmp_path, width, language, theme,  # noqa: F811
):
    page = frontend_visual_page
    page.set_viewport_size({'width': width, 'height': 1100})
    styles = page.locator('link[rel="stylesheet"]').evaluate_all('(nodes)=>nodes.map(n=>n.href)')
    page.evaluate("""() => {
      const iframe=document.createElement('iframe');iframe.id='grouped-reading-fixture';
      iframe.style.cssText='position:fixed;inset:0;width:100%;height:100%;z-index:999999;border:0';
      document.body.appendChild(iframe);
    }""")
    frame = page.frames[-1]
    frame.set_content(f'<html lang="{language}" data-theme="{theme}"><body>'
                      '<main id="surface"></main></body></html>')
    for url in styles:
        frame.add_style_tag(url=url)
    frame.add_style_tag(content='body{display:block;margin:0;padding:var(--space-4);height:auto;overflow:auto}'
                               '#surface{max-width:900px;margin:auto}.conversation-turn{--rail-w:0px}')
    locale = json.loads((ROOT / f'frontend/src/i18n/locales/{language}.json').read_text())
    frame.evaluate(r"""locale => {
      window.t=(key,params)=>(locale[key]||key).replace(/\{(\w+)\}/g,(_,k)=>params?.[k]??'');
      window.escapeHtml=value=>String(value??'').replace(/[&<>"']/g,
        ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
      window._featureFlags={debug_mode:false};window.projectState={extraRoots:[]};
      window.getActiveConv=()=>null;window.renderMarkdown=value=>escapeHtml(value);
      window.Icon=()=>'<svg viewBox="0 0 24 24" width="14" height="14"><path d="m5 6 6 6-6 6m8 0h6" stroke="currentColor" fill="none"/></svg>';
    }""", locale)
    frame.add_script_tag(path=runtime_section_path('ui/tool_rounds.js'))
    frame.add_script_tag(path=runtime_section_path('ui/tool_rounds_rich.js'))
    frame.add_script_tag(path=grouped_surface_bundle)
    frame.evaluate(r"""async language => {
      await setLanguage(language);
      const scope={attemptId:'fixture-attempt',taskId:'fixture-task'};
      const command=(id,llmRound,intent,exitCode,status='done')=>({
        type:'tool_use',blockId:id,id,name:'run_command',llmRound,...scope,
        input:{command:'printf synthetic',description:intent},result:{content:'Synthetic output',status},
        _round:{...scope,llmRound,roundNum:llmRound+1,toolCallId:id,toolName:'run_command',status,query:intent,
          toolArgs:{command:'printf synthetic',description:intent},
          results:[{command:'printf synthetic',description:intent,output:'Synthetic output',exitCode}],
          toolContent:'Synthetic output'},
      });
      const issue=(id,anchor,extra)=>({id,spanId:id,seq:1,occurredAt:Date.UTC(2026,9,28,9,0,1),count:1,
        ...scope,anchorBlockId:anchor,...extra});
      const zh=language==='zh';
      const state=createTurnState('fixture');
      state.turnsById.input={turnId:'input',presentationId:'input',runId:'',conversationId:'fixture',laneId:'main',
        ordinal:0,actor:'human',kind:'input',status:'completed',currentAttemptId:null,projectionRevision:1,
        createdAt:1,updatedAt:1,settlement:{},projection:{content:zh?'清理过时的引擎接口。':'Clean up stale engine interfaces.'}};
      state.turnsById.reply={turnId:'reply',presentationId:'reply',runId:'',conversationId:'fixture',laneId:'main',
        ordinal:1,actor:'assistant',kind:'reply',status:'completed',currentAttemptId:null,projectionRevision:1,
        createdAt:2,updatedAt:3,settlement:{},projection:{
          content:zh?'三个引擎现在共享一个固定接口。':'All three engines now share one fixed interface.',
          segments:[
            {type:'thinking',blockId:'think-0',llmRound:0,...scope,text:'List engines, then compare interfaces.'},
            {type:'text',blockId:'narrate-0',llmRound:0,...scope,text:zh?'先盘点现有引擎。':'Inventorying the engines first.'},
            command('tool-a',0,zh?'列出引擎目录':'List engine modules',0),
            {type:'thinking',blockId:'think-1',llmRound:1,...scope,text:'Run the interface checks.'},
            command('tool-b',1,zh?'运行接口一致性检查':'Run interface conformance checks',0),
            command('tool-c',1,zh?'运行旧基线测试':'Run legacy baseline tests',2,'error'),
            {type:'text',blockId:'narrate-2',llmRound:2,...scope,text:zh?'基线测试已修复。':'Baseline tests repaired.'},
            {type:'text',blockId:'answer',llmRound:3,...scope,terminal:true,deliverable:true,
              text:zh?'三个引擎现在共享一个固定接口。':'All three engines now share one fixed interface.'},
          ],
          activityTimeline:{entries:[
            issue('request-failed','tool-a',{kind:'model',status:'failed',severity:'error',summary:'Model request failed',
              model:'fixture-model',statusCode:502}),
            issue('rate-wait','tool-b',{kind:'status',status:'waiting',severity:'warning',phase:'retrying',
              summary:'Rate limited, retrying',statusCode:429}),
          ]},
        }};
      state.laneOrder.main=['input','reply'];
      window.state=state;
      window.surface=createConversationSurface(document.getElementById('surface'),createClassicConversationRenderers({
        renderSafeMarkdownHtml:markdown=>'<p>'+escapeHtml(markdown)+'</p>',localizedText:(k,f)=>t(k)||f,
        renderToolBlockHtml:block=>_renderUnifiedToolLine(block.round,false),
      }));
      window.replay=()=>surface.render(selectConversationViewModel(state));
      replay();
    }""", language)

    turn = frame.locator('[data-turn-id="reply"] .conversation-turn-blocks')
    order = """(node)=>[...node.children]
      .map(n=>n.dataset.blockId||('narrationWell' in n.dataset?'notes':''))
      .filter(Boolean)"""
    assert turn.evaluate(order) == ['think-0', 'layout:tool-panel', 'notes', 'answer']
    assert frame.locator('[data-block-kind="tool"]').count() == 0
    toggle = frame.locator('.conversation-tool-panel__toggle')
    assert toggle.get_attribute('aria-expanded') == 'false'
    assert ('3' in toggle.inner_text()) and frame.locator('.conversation-tool-panel__errors').is_visible()
    # Reasoning and tools are two independent disclosures in one activity
    # card: identical full-width rows, shared glyph/chevron columns, no gap
    # or second pill width. The reasoning body starts at the card inset.
    reasoning = frame.locator('[data-block-id="think-0"] .thinking-block')
    assert reasoning.locator('.thinking-icon svg').count() == 1
    assert reasoning.locator('.thinking-state-dot').count() == 0
    activity_geometry = frame.evaluate("""()=>{
      const box=selector=>document.querySelector(selector).getBoundingClientRect();
      const style=selector=>getComputedStyle(document.querySelector(selector));
      const thinking=box('.thinking-header'),tools=box('.conversation-tool-panel__toggle');
      const card=box('.conversation-tool-panel'),reasoning=box('.thinking-block');
      return {widths:[thinking.width,tools.width,card.width,reasoning.width],
        heights:[thinking.height,tools.height],lefts:[thinking.left,tools.left,card.left,reasoning.left],
        joined:Math.abs(card.top-reasoning.bottom),
        iconOffset:box('.thinking-icon').left-box('.conversation-tool-panel__glyph').left,
        chevronOffset:box('.thinking-toggle').right-box('.conversation-tool-panel__chevron').right,
        labelOffset:box('.thinking-label').left-box('.conversation-tool-panel__label').left,
        rowPadding:style('.thinking-header').padding,buttonPadding:style('.conversation-tool-panel__toggle').padding};
    }""")
    assert activity_geometry['widths'][0] == activity_geometry['widths'][1], activity_geometry
    assert activity_geometry['widths'][2] == activity_geometry['widths'][3], activity_geometry
    assert activity_geometry['lefts'][0] == activity_geometry['lefts'][1], activity_geometry
    assert activity_geometry['lefts'][2] == activity_geometry['lefts'][3], activity_geometry
    assert max(activity_geometry['heights']) - min(activity_geometry['heights']) <= 1, activity_geometry
    assert abs(activity_geometry['joined']) <= 1, activity_geometry
    for measurement in ('iconOffset', 'chevronOffset', 'labelOffset'):
        assert abs(activity_geometry[measurement]) <= 1, activity_geometry
    assert activity_geometry['rowPadding'] == activity_geometry['buttonPadding'], activity_geometry
    # Interim notes are the card's last row: a settled reply folds them like
    # its calls, on the same columns, joined by the card's rule.
    notes = frame.locator('[data-narration-well]')
    notes_toggle = notes.locator(':scope > .conversation-narration__toggle')
    notes_body = notes.locator(':scope > [data-narration-well-body]')
    assert notes_body.evaluate(
        '(node)=>[...node.querySelectorAll(":scope > [data-block-id]")].map(n=>n.dataset.blockId)',
    ) == ['narrate-0', 'narrate-2']
    assert notes_toggle.get_attribute('aria-expanded') == 'false'
    assert not notes_body.is_visible()
    notes_geometry = frame.evaluate("""()=>{
      const box=selector=>document.querySelector(selector).getBoundingClientRect();
      const style=selector=>getComputedStyle(document.querySelector(selector));
      const tools=box('.conversation-tool-panel__toggle'),notes=box('.conversation-narration__toggle');
      const panel=box('.conversation-tool-panel'),well=box('.conversation-narration');
      return {widths:[tools.width,notes.width,panel.width,well.width],
        lefts:[tools.left,notes.left,panel.left,well.left],heights:[tools.height,notes.height],
        joined:well.top-panel.bottom,
        iconOffset:box('.conversation-narration__glyph').left-box('.conversation-tool-panel__glyph').left,
        labelOffset:box('.conversation-narration__label').left-box('.conversation-tool-panel__label').left,
        chevronOffset:box('.conversation-narration__chevron').right-box('.conversation-tool-panel__chevron').right,
        seam:[style('.conversation-tool-panel').borderBottomLeftRadius,
          style('.conversation-narration').borderTopLeftRadius,style('.conversation-narration').borderTopWidth],
        foot:style('.conversation-narration').borderBottomLeftRadius};
    }""")
    assert notes_geometry['widths'][0] == notes_geometry['widths'][1], notes_geometry
    assert notes_geometry['widths'][2] == notes_geometry['widths'][3], notes_geometry
    assert notes_geometry['lefts'][0] == notes_geometry['lefts'][1], notes_geometry
    assert notes_geometry['lefts'][2] == notes_geometry['lefts'][3], notes_geometry
    assert max(notes_geometry['heights']) - min(notes_geometry['heights']) <= 1, notes_geometry
    assert abs(notes_geometry['joined']) <= 1, notes_geometry
    for measurement in ('iconOffset', 'labelOffset', 'chevronOffset'):
        assert abs(notes_geometry[measurement]) <= 1, notes_geometry
    assert notes_geometry['seam'] == ['0px', '0px', '0px'], notes_geometry
    assert notes_geometry['foot'] != '0px', notes_geometry
    assert not reasoning.evaluate('(node)=>node.open')
    frame.locator('.thinking-header').click()
    assert reasoning.evaluate('(node)=>node.open')
    assert toggle.get_attribute('aria-expanded') == 'false'
    assert frame.locator('[data-block-kind="tool"]').count() == 0
    reasoning_body = frame.locator('.thinking-text')
    assert reasoning_body.is_visible()
    reasoning_geometry = reasoning_body.evaluate("""(node)=>{
      const body=node.getBoundingClientRect(),card=document.querySelector('.conversation-tool-panel').getBoundingClientRect();
      return {inside:body.left>=card.left && body.right<=card.right+1,
        paddingLeft:parseFloat(getComputedStyle(node).paddingLeft),
        cardInset:parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--space-3')),
        bodyLeft:body.left,contentLeft:node.querySelector('p').getBoundingClientRect().left};
    }""")
    assert reasoning_geometry['inside'], reasoning_geometry
    assert reasoning_geometry['paddingLeft'] == reasoning_geometry['cardInset'], reasoning_geometry
    assert reasoning_geometry['contentLeft'] > reasoning_geometry['bodyLeft'], reasoning_geometry
    reasoning_shot = tmp_path / f'activity-reasoning-open-{width}-{language}-{theme}.png'
    page.locator('#grouped-reading-fixture').screenshot(path=str(reasoning_shot))
    frame.locator('.thinking-header').click()
    assert not reasoning.evaluate('(node)=>node.open')
    assert toggle.get_attribute('aria-expanded') == 'false'
    # One translation receipt per Turn: narration never repeats it.
    assert frame.locator('[data-block-id^="narrate"] .bilingual-attribution').count() == 0

    toggle.focus()
    page.keyboard.press('Enter')
    assert toggle.get_attribute('aria-expanded') == 'true'
    assert toggle.evaluate('(el)=>el===document.activeElement')
    assert turn.evaluate(order) == ['think-0', 'layout:tool-panel', 'tool-a', 'activity:request-failed',
                                    'tool-b', 'activity:rate-wait', 'tool-c', 'notes', 'answer']
    # Open calls still end on the notes row: the last call's rule divides them.
    open_seam = frame.evaluate("""()=>{
      const well=document.querySelector('.conversation-narration'),last=well.previousElementSibling;
      return {joined:well.getBoundingClientRect().top-last.getBoundingClientRect().bottom,
        foot:getComputedStyle(last).borderBottomLeftRadius,rule:getComputedStyle(last).borderBottomWidth};
    }""")
    assert abs(open_seam['joined']) <= 1 and open_seam['foot'] == '0px', open_seam
    assert open_seam['rule'] != '0px', open_seam
    rows = frame.locator('[data-turn-zone="process"]')
    assert rows.evaluate_all("""nodes=>nodes.every(n=>{
      const s=getComputedStyle(n);return parseFloat(s.borderLeftWidth)>=1 && parseFloat(s.paddingLeft)>0;
    })""")
    failure = frame.locator('[data-block-id="activity:request-failed"] > .activity-event')
    warning = frame.locator('[data-block-id="activity:rate-wait"] > .activity-event')
    styles_of = """(el)=>{const s=getComputedStyle(el);
      const call=document.querySelector('[data-block-id="tool-a"] .ptool-cmd-icon').getBoundingClientRect();
      return {wash:s.backgroundColor, rule:s.boxShadow,
              glyphEdge:el.querySelector('.activity-event__marker').getBoundingClientRect().left-call.left,
              ink:getComputedStyle(el.querySelector('.activity-event__summary')).color};}"""
    failed, warned = failure.evaluate(styles_of), warning.evaluate(styles_of)
    for issue in (failed, warned):
        assert issue['wash'] not in ('rgba(0, 0, 0, 0)', 'transparent')
        assert 'inset' in issue['rule']
        # The callout keeps its row's column: its marker sits on the calls'
        # glyph column, its wash hung into the gutter.
        assert abs(issue['glyphEdge']) <= 1, issue
    assert failed['ink'] != warned['ink'] and failed['rule'] != warned['rule']
    assert frame.locator('body').evaluate('(el)=>el.scrollWidth<=window.innerWidth+1')

    frame.evaluate('replay()')
    assert toggle.evaluate('(el)=>el===document.activeElement')
    assert toggle.get_attribute('aria-expanded') == 'true'
    frame.evaluate('document.activeElement?.blur()')
    open_shot = tmp_path / f'grouped-open-{width}-{language}-{theme}.png'
    page.locator('#grouped-reading-fixture').screenshot(path=str(open_shot))

    toggle.focus()
    page.keyboard.press('Space')
    assert toggle.get_attribute('aria-expanded') == 'false'
    assert frame.locator('[data-turn-zone="process"]').count() == 0
    frame.evaluate('document.activeElement?.blur()')
    closed_shot = tmp_path / f'grouped-closed-{width}-{language}-{theme}.png'
    page.locator('#grouped-reading-fixture').screenshot(path=str(closed_shot))

    # The notes fold is its own disclosure: it opens at the newest note and
    # never touches the calls.
    notes_toggle.focus()
    page.keyboard.press('Enter')
    assert notes_toggle.get_attribute('aria-expanded') == 'true'
    assert notes_toggle.evaluate('(el)=>el===document.activeElement')
    assert notes_body.is_visible() and toggle.get_attribute('aria-expanded') == 'false'
    assert notes_body.evaluate('(node)=>node.scrollTop+node.clientHeight>=node.scrollHeight-1')
    frame.evaluate('document.activeElement?.blur()')
    notes_shot = tmp_path / f'grouped-notes-open-{width}-{language}-{theme}.png'
    page.locator('#grouped-reading-fixture').screenshot(path=str(notes_shot), animations='disabled')
    notes_toggle.focus()
    page.keyboard.press('Space')
    assert notes_toggle.get_attribute('aria-expanded') == 'false' and not notes_body.is_visible()

    # A later live round preserves both disclosures' reader choices. Its
    # readable label and shared glyph replace the old pulsing status dot.
    toggle.focus()
    page.keyboard.press('Enter')
    assert toggle.get_attribute('aria-expanded') == 'true'
    frame.evaluate("""()=>{
      const old=state.turnsById.reply;
      state.turnsById.reply={...old,status:'running',currentAttemptId:'fixture-attempt',projectionRevision:2,
        projection:{...old.projection,content:'',segments:[...old.projection.segments,
          {type:'thinking',blockId:'think-live',llmRound:4,attemptId:'fixture-attempt',taskId:'fixture-task',
            text:'Streaming reasoning keeps the same activity row.'},
        ]}};
      replay();
    }""")
    assert not reasoning.evaluate('(node)=>node.open')
    assert reasoning.get_attribute('data-state') == 'active'
    assert reasoning.locator('.thinking-icon svg').count() == 1
    assert reasoning.locator('.thinking-state-dot').count() == 0
    assert toggle.get_attribute('aria-expanded') == 'true'
    assert frame.locator('[data-block-kind="tool"]').count() == 3
    frame.locator('.thinking-header').click()
    assert reasoning.evaluate('(node)=>node.open')
    # The reader's fold outlives the reply resuming work.
    assert notes_toggle.get_attribute('aria-expanded') == 'false'
    streaming_shot = tmp_path / f'activity-streaming-{width}-{language}-{theme}.png'
    page.locator('#grouped-reading-fixture').screenshot(path=str(streaming_shot))
    print(f'Grouped reading screenshots: {closed_shot} {open_shot} {notes_shot} {streaming_shot}')


def test_open_panel_shows_newest_calls_and_pages_only_on_click(
    frontend_visual_page, grouped_surface_bundle,  # noqa: F811
):
    """Owner report 2026-11-02: an opened panel showed its oldest calls, then
    scrolling silently mounted later calls and retired earlier ones under a
    row that said "click to show". The panel opens at its newest calls,
    scrolling never pages them, and a click reveals earlier calls without
    dropping the ones already on screen."""
    page = frontend_visual_page
    page.set_viewport_size({'width': 1100, 'height': 700})
    styles = page.locator('link[rel="stylesheet"]').evaluate_all('(nodes)=>nodes.map(n=>n.href)')
    page.evaluate("""() => {
      const iframe=document.createElement('iframe');iframe.id='grouped-paging-fixture';
      iframe.style.cssText='position:fixed;inset:0;width:100%;height:100%;z-index:999999;border:0';
      document.body.appendChild(iframe);
    }""")
    frame = page.frames[-1]
    frame.set_content('<html lang="en" data-theme="light"><body>'
                      '<div id="viewport"><main id="surface"></main></div></body></html>')
    for url in styles:
        frame.add_style_tag(url=url)
    frame.add_style_tag(content='body{display:block;margin:0;height:100vh;overflow:hidden}'
                               '#viewport{height:100vh;overflow:auto}'
                               '#surface{max-width:900px;margin:auto}.conversation-turn{--rail-w:0px}')
    locale = json.loads((ROOT / 'frontend/src/i18n/locales/en.json').read_text())
    frame.evaluate(r"""locale => {
      window.t=(key,params)=>(locale[key]||key).replace(/\{(\w+)\}/g,(_,k)=>params?.[k]??'');
      window.escapeHtml=value=>String(value??'').replace(/[&<>"']/g,
        ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
      window._featureFlags={debug_mode:false};window.projectState={extraRoots:[]};
      window.getActiveConv=()=>null;window.renderMarkdown=value=>escapeHtml(value);
      window.Icon=()=>'<svg viewBox="0 0 24 24" width="14" height="14"><path d="m5 6 6 6-6 6m8 0h6" stroke="currentColor" fill="none"/></svg>';
    }""", locale)
    frame.add_script_tag(path=runtime_section_path('ui/tool_rounds.js'))
    frame.add_script_tag(path=runtime_section_path('ui/tool_rounds_rich.js'))
    frame.add_script_tag(path=grouped_surface_bundle)
    frame.evaluate(r"""async () => {
      await setLanguage('en');
      const scope={attemptId:'fixture-attempt',taskId:'fixture-task'};
      const read=(i)=>({type:'tool_use',blockId:'call-'+i,id:'call-'+i,name:'read_files',llmRound:i,...scope,
        input:{path:'file-'+i+'.ts'},result:{content:'ok',status:'done'},
        _round:{...scope,llmRound:i,roundNum:i+1,toolCallId:'call-'+i,toolName:'read_files',status:'done',
          query:'file-'+i+'.ts',toolArgs:{path:'file-'+i+'.ts'},toolContent:'ok'}});
      const state=createTurnState('fixture');
      state.turnsById.reply={turnId:'reply',presentationId:'reply',runId:'',conversationId:'fixture',laneId:'main',
        ordinal:0,actor:'assistant',kind:'reply',status:'completed',currentAttemptId:null,projectionRevision:1,
        createdAt:2,updatedAt:3,settlement:{},projection:{content:'Done.',segments:[
          ...Array.from({length:120},(_,i)=>read(i)),
          {type:'text',blockId:'answer',llmRound:120,...scope,terminal:true,deliverable:true,text:'Done.'},
        ]}};
      state.laneOrder.main=['reply'];
      window.surface=createConversationSurface(document.getElementById('surface'),{
        ...createClassicConversationRenderers({
          renderSafeMarkdownHtml:markdown=>'<p>'+escapeHtml(markdown)+'</p>',localizedText:(k,f)=>t(k)||f,
          renderToolBlockHtml:block=>_renderUnifiedToolLine(block.round,false),
        }),
        scrollViewport:document.getElementById('viewport'),
      });
      surface.render(selectConversationViewModel(state));
    }""")
    calls = ('()=>[...document.querySelectorAll(\'[data-block-region="process"]\')]'
             '.map(n=>n.dataset.blockId)')
    frame.locator('.conversation-tool-panel__toggle').click()
    opened = frame.evaluate(calls)
    assert opened[-1] == 'call-119' and len(opened) < 120
    assert frame.locator('[data-region-boundary="process:later"]').count() == 0
    earlier = frame.locator('[data-region-boundary="process:earlier"] button')
    assert earlier.is_visible()

    frame.locator('#viewport').hover()
    for delta in (900, 900, 900, -900, -900, -900, -900, 900):
        page.mouse.wheel(0, delta)
        frame.evaluate('()=>new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))')
    page.wait_for_timeout(300)
    assert frame.evaluate(calls) == opened

    earlier.click()
    grown = frame.evaluate(calls)
    assert len(grown) > len(opened) and grown[-len(opened):] == opened
    assert frame.locator('[data-region-boundary="process:later"]').count() == 0


def test_live_panel_keeps_newest_calls_after_the_reader_leaves_the_tail(
    frontend_visual_page, grouped_surface_bundle,  # noqa: F811
):
    """Owner report 2026-11-03: once the follow latch dropped (an upward
    nudge, or the pane settling while a conversation opened), the next calls
    landed behind a "N newer tool calls" row until a later frame arrived while
    following again. A live panel keeps its newest calls mounted however the
    reader scrolled, and the calls already on screen stay where they were."""
    page = frontend_visual_page
    page.set_viewport_size({'width': 1100, 'height': 700})
    styles = page.locator('link[rel="stylesheet"]').evaluate_all('(nodes)=>nodes.map(n=>n.href)')
    page.evaluate("""() => {
      const iframe=document.createElement('iframe');iframe.id='grouped-live-fixture';
      iframe.style.cssText='position:fixed;inset:0;width:100%;height:100%;z-index:999999;border:0';
      document.body.appendChild(iframe);
    }""")
    frame = page.frames[-1]
    frame.set_content('<html lang="en" data-theme="light"><body>'
                      '<div id="viewport"><main id="surface"></main></div></body></html>')
    for url in styles:
        frame.add_style_tag(url=url)
    frame.add_style_tag(content='body{display:block;margin:0;height:100vh;overflow:hidden}'
                               '#viewport{height:100vh;overflow:auto}'
                               '#surface{max-width:900px;margin:auto}.conversation-turn{--rail-w:0px}')
    locale = json.loads((ROOT / 'frontend/src/i18n/locales/en.json').read_text())
    frame.evaluate(r"""locale => {
      window.t=(key,params)=>(locale[key]||key).replace(/\{(\w+)\}/g,(_,k)=>params?.[k]??'');
      window.escapeHtml=value=>String(value??'').replace(/[&<>"']/g,
        ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
      window._featureFlags={debug_mode:false};window.projectState={extraRoots:[]};
      window.getActiveConv=()=>null;window.renderMarkdown=value=>escapeHtml(value);
      window.Icon=()=>'<svg viewBox="0 0 24 24" width="14" height="14"><path d="m5 6 6 6-6 6m8 0h6" stroke="currentColor" fill="none"/></svg>';
    }""", locale)
    frame.add_script_tag(path=runtime_section_path('ui/tool_rounds.js'))
    frame.add_script_tag(path=runtime_section_path('ui/tool_rounds_rich.js'))
    frame.add_script_tag(path=grouped_surface_bundle)
    frame.evaluate(r"""async () => {
      await setLanguage('en');
      const scope={attemptId:'fixture-attempt',taskId:'fixture-task'};
      const read=(i)=>({type:'tool_use',blockId:'call-'+i,id:'call-'+i,name:'read_files',llmRound:i,...scope,
        input:{path:'file-'+i+'.ts'},result:{content:'ok',status:'done'},
        _round:{...scope,llmRound:i,roundNum:i+1,toolCallId:'call-'+i,toolName:'read_files',status:'done',
          query:'file-'+i+'.ts',toolArgs:{path:'file-'+i+'.ts'},toolContent:'ok'}});
      const state=createTurnState('fixture');
      state.laneOrder.main=['reply'];
      window.surface=createConversationSurface(document.getElementById('surface'),{
        ...createClassicConversationRenderers({
          renderSafeMarkdownHtml:markdown=>'<p>'+escapeHtml(markdown)+'</p>',localizedText:(k,f)=>t(k)||f,
          renderToolBlockHtml:block=>_renderUnifiedToolLine(block.round,false),
        }),
        scrollViewport:document.getElementById('viewport'),
      });
      window.renderCalls=(count)=>{
        state.turnsById.reply={turnId:'reply',presentationId:'reply',runId:'',conversationId:'fixture',laneId:'main',
          ordinal:0,actor:'assistant',kind:'reply',status:'running',currentAttemptId:'fixture-attempt',
          projectionRevision:count,createdAt:2,updatedAt:count,settlement:{},
          projection:{content:'',segments:Array.from({length:count},(_,i)=>read(i))}};
        surface.render(selectConversationViewModel(state));
      };
      renderCalls(60);
    }""")
    settle = '()=>new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))'
    calls = ('()=>[...document.querySelectorAll(\'[data-block-region="process"]\')]'
             '.map(n=>n.dataset.blockId)')
    later = frame.locator('[data-region-boundary="process:later"]')
    frame.evaluate(settle)
    opened = frame.evaluate(calls)
    assert opened[-1] == 'call-59' and len(opened) < 60
    assert later.count() == 0

    frame.locator('#viewport').hover()
    page.mouse.wheel(0, -240)
    frame.evaluate(settle)
    page.wait_for_timeout(200)
    reading_top = frame.evaluate("()=>document.getElementById('viewport').scrollTop")
    frame.evaluate('renderCalls(72)')
    frame.evaluate(settle)
    shown = frame.evaluate(calls)
    assert shown[-1] == 'call-71', shown[-3:]
    assert shown[:len(opened)] == opened
    assert later.count() == 0
    assert abs(frame.evaluate("()=>document.getElementById('viewport').scrollTop") - reading_top) <= 1
