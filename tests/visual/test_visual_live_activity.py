"""Running reply live row on the actual Surface graph, presenters and shipped CSS.

A long agent reply renders reasoning above its tool panel and receipts below
its prose, so the live row at the tail is the surface a following reader sees.
It must name the call that is running, with an elapsed clock from the call's
backend start, and report streaming reasoning instead of the empty-phase
placeholder. Synthetic Turn graph in an inert iframe; no execution writes.
"""
from __future__ import annotations

import json
import re

import pytest

from tests._runtime_sections import native_module_path, runtime_section_path
from tests.support.frontend_visual import ROOT, frontend_visual_origin, frontend_visual_page  # noqa: F401

pytestmark = pytest.mark.visual


@pytest.fixture(scope='module')
def live_activity_bundle(tmp_path_factory):
    entry = tmp_path_factory.mktemp('live-activity') / 'live-activity.ts'
    entry.write_text(
        f"export * from '{ROOT}/frontend/src/conversation/index.ts';\n"
        f"export {{ setLanguage }} from '{ROOT}/frontend/src/i18n/index.ts';\n")
    return native_module_path('.native/live-activity-surface.js', entry)


def _seconds(clock: str) -> int:
    match = re.fullmatch(r'(\d{2,}):(\d{2})', clock)
    assert match, clock
    return int(match.group(1)) * 60 + int(match.group(2))


@pytest.mark.parametrize('width,language,theme', [(1100, 'zh', 'tofu'), (1100, 'en', 'light'), (390, 'zh', 'tofu')])
def test_running_reply_reports_its_call_and_reasoning_below_receipts(
    frontend_visual_page, live_activity_bundle, tmp_path, width, language, theme,  # noqa: F811
):
    page = frontend_visual_page
    page.set_viewport_size({'width': width, 'height': 1100})
    styles = page.locator('link[rel="stylesheet"]').evaluate_all('(nodes)=>nodes.map(n=>n.href)')
    page.evaluate("""() => {
      const iframe=document.createElement('iframe');iframe.id='live-activity-fixture';
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
    frame.add_script_tag(path=live_activity_bundle)
    frame.evaluate(r"""async ({language, locale}) => {
      await setLanguage(language);
      const scope={attemptId:'fixture-attempt',taskId:'fixture-task'};
      const zh=language==='zh';
      window.command=(id,llmRound,intent,status,tStart)=>({
        type:'tool_use',blockId:id,id,name:'run_command',llmRound,...scope,
        input:{command:'python3 -m pytest -q tests/slides',description:intent},result:{content:'',status},
        _round:{...scope,llmRound,roundNum:llmRound+1,toolCallId:id,toolName:'run_command',status,tStart,
          query:intent,toolArgs:{command:'python3 -m pytest -q tests/slides',description:intent},
          results:status==='done'?[{command:'python3 -m pytest -q tests/slides',exitCode:0}]:[]},
      });
      window.intent=zh?'运行幻灯片测试':'Run the slides tests';
      const state=createTurnState('fixture');
      state.turnsById.input={turnId:'input',presentationId:'input',runId:'',conversationId:'fixture',laneId:'main',
        ordinal:0,actor:'human',kind:'input',status:'completed',currentAttemptId:null,projectionRevision:1,
        createdAt:1,updatedAt:1,settlement:{},projection:{content:zh?'把材料做成 PPT。':'Turn the material into slides.'}};
      state.turnsById.reply={turnId:'reply',presentationId:'reply',runId:'',conversationId:'fixture',laneId:'main',
        ordinal:1,actor:'assistant',kind:'reply',status:'running',currentAttemptId:'fixture-attempt',
        projectionRevision:2,createdAt:2,updatedAt:3,settlement:{},projection:{content:'',
          fileChanges:{blockId:'file-changes',count:1,state:'applied',files:[{path:'lib/slides/recipe.py'}]},
          segments:[
            {type:'thinking',blockId:'think-0',llmRound:0,...scope,text:'Check why pages fell back.'},
            command('tool-a',0,zh?'检查作者阶段日志':'Inspect the author-stage log','done',Date.now()-600000),
            {type:'text',blockId:'narrate-0',llmRound:0,...scope,text:zh?'正在修复来源标记。':'Fixing the source markers.'},
            {type:'thinking',blockId:'think-1',llmRound:1,...scope,text:'Run the focused tests.'},
            command('tool-b',1,intent,'searching',Date.now()-125000),
          ]}};
      state.attemptsById['fixture-attempt']={attemptId:'fixture-attempt',turnId:'reply',taskId:'fixture-task',status:'running'};
      state.laneOrder.main=['input','reply'];
      state.livePhase=null;
      window.state=state;
      window.surface=createConversationSurface(document.getElementById('surface'),createClassicConversationRenderers({
        renderSafeMarkdownHtml:markdown=>'<p>'+escapeHtml(markdown)+'</p>',
        localizedText:(key,fallback,values)=>(locale[key]||fallback).replace(/\{(\w+)\}/g,(_,k)=>values?.[k]??''),
        renderToolBlockHtml:(block,turn)=>renderToolRoundsHTML([block.round],
          turn.status==='pending'||turn.status==='running'),
      }));
      window.replay=()=>surface.render(selectConversationViewModel(state));
      replay();
    }""", {'language': language, 'locale': locale})

    turn = frame.locator('[data-turn-id="reply"] .conversation-turn-blocks')
    order = '(node)=>[...node.children].filter(n=>n.dataset.blockId).map(n=>n.dataset.blockId)'
    assert turn.evaluate(order)[-2:] == ['file-changes', 'live-status']
    live = frame.locator('[data-block-id="live-status"]')
    phase_text = live.locator('.stream-phase-text')
    expected = locale['stream.phase.runningToolIntent'].replace('{tool}', 'run_command').replace(
        '{intent}', '运行幻灯片测试' if language == 'zh' else 'Run the slides tests')
    assert phase_text.inner_text().startswith(expected)
    assert locale['stream.phase.waitingWorkerStatus'] not in phase_text.inner_text()
    clock = live.locator('.stream-phase-elapsed')
    assert clock.get_attribute('aria-hidden') == 'true'
    first = _seconds(clock.inner_text())
    assert 125 <= first < 140
    frame.wait_for_function(
        "start => { const t=document.querySelector('.stream-phase-elapsed')?.textContent||'';"
        " const [m,s]=t.split(':').map(Number); return m*60+s > start; }", arg=first, timeout=5000)
    receipt = frame.locator('[data-block-id="file-changes"]').bounding_box()
    row = live.bounding_box()
    assert receipt and row and row['y'] >= receipt['y'] + receipt['height'] - 1
    assert not frame.evaluate('document.documentElement.scrollWidth > innerWidth')
    running_path = tmp_path / f'live-running-{language}-{width}.png'
    page.locator('#live-activity-fixture').screenshot(path=str(running_path))

    frame.evaluate(r"""() => {
      const reply=state.turnsById.reply;
      const segments=reply.projection.segments.map(s=>s.blockId==='tool-b'
        ?command('tool-b',1,intent,'done',Date.now()-130000):s);
      segments.push({type:'thinking',blockId:'think-2',llmRound:2,attemptId:'fixture-attempt',
        taskId:'fixture-task',text:'Tests pass; plan the deck rebuild.'});
      state.turnsById.reply={...reply,projectionRevision:3,projection:{...reply.projection,segments}};
      replay();
    }""")
    assert phase_text.inner_text() == locale['stream.thinking.active']
    assert live.locator('.stream-phase-elapsed').count() == 0
    thinking_path = tmp_path / f'live-thinking-{language}-{width}.png'
    page.locator('#live-activity-fixture').screenshot(path=str(thinking_path))

    frame.evaluate("""() => {state.turnsById.reply={...state.turnsById.reply,status:'completed',
      currentAttemptId:null,projectionRevision:4};replay();}""")
    assert frame.locator('[data-block-id="live-status"]').count() == 0
    print(f'Live activity screenshots: {running_path} {thinking_path}')
