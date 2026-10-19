"""Intent-first action stream through Surface, real presenters and shipped CSS.

No persisted conversation or command is touched: the live isolated test app
serves assets and an inert iframe mounts synthetic accepted tool results.
"""
from __future__ import annotations

import json

import pytest

from tests._runtime_sections import native_module_path, runtime_section_path
from tests.support.frontend_visual import ROOT, frontend_visual_origin, frontend_visual_page  # noqa: F401

pytestmark = pytest.mark.visual


@pytest.mark.parametrize('width,language,theme', [
    (1100, 'en', 'light'), (1100, 'zh', 'tofu'),
    (390, 'en', 'dark'), (390, 'zh', 'tofu'),
])
def test_actions_have_one_summary_full_intent_and_independent_evidence(
    frontend_visual_page, tmp_path, width, language, theme,  # noqa: F811
):
    page = frontend_visual_page
    page.set_viewport_size({'width': width, 'height': 1100})
    styles = page.locator('link[rel="stylesheet"]').evaluate_all('(nodes)=>nodes.map(n=>n.href)')
    page.evaluate("""() => {
      const iframe=document.createElement('iframe');iframe.id='action-reading-fixture';
      iframe.style.cssText='position:fixed;inset:0;width:100%;height:100%;z-index:999999;border:0';
      document.body.appendChild(iframe);
    }""")
    frame = page.frames[-1]
    frame.set_content(f'<html lang="{language}" data-theme="{theme}"><body>'
                      '<main id="surface"></main></body></html>')
    for url in styles:
        frame.add_style_tag(url=url)
    # Host geometry only: tool, turn and control styles come from shipped CSS.
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
    frame.add_script_tag(path=native_module_path(
        '.native/intent-first-surface.js', ROOT / 'frontend/src/conversation/index.ts'))
    frame.evaluate(r"""language => {
      const scope={llmRound:0,attemptId:'fixture-attempt',taskId:'fixture-task'};
      const intent=language==='zh'
        ? '在整合后的主工作区重跑变更边界与视觉矩阵，确保不是只在隔离区可用，并检查桌面、手机、键盘焦点与浅色和深色主题的展示。'
        : 'Recheck the integrated workspace boundaries and visual matrix, including desktop, mobile, keyboard focus and both light and dark themes—not only the isolated workspace.';
      const browserIntent=language==='zh'
        ? '只读校验正在提供的新版浏览器卡片脚本与样式，不刷新或修改正在运行的会话，确认用户能看到完整的操作目的。'
        : 'Read-only verification of the browser card scripts and styles, without refreshing or changing the current conversation; confirm the full purpose of the action remains visible.';
      const round=(id,toolName,description,metadata,args={})=>({
        ...scope,roundNum:1,toolCallId:id,toolName,status:'done',query:description,
        toolArgs:{description,...args},results:[metadata],toolContent:'Synthetic result only',
      });
      window.rounds=[
        round('checklist','todo_write','Checklist: 4 steps (3 done)',{
          todos:['Inspect','Design','Verify','Integrate'].map((content,i)=>({content,status:i<3?'completed':'pending'})),
          todoExplanation:'Complete rationale stays in the single checklist body.',todoUpdateCount:4,
        }),
        round('command','run_command',intent,{command:'printf "synthetic-only"\n'.repeat(5),description:intent,output:'All synthetic checks passed',exitCode:0}),
        round('browser','browser_execute_js',browserIntent,{tabTitle:'Tofu'},
          {code:'document.title',tab_id:333777}),
        round('failure','run_command',language==='zh'?'失败不会被成功标签或折叠包装隐藏':'Failure remains distinct and inspectable',
          {command:'synthetic-failure',description:language==='zh'?'失败不会被成功标签或折叠包装隐藏':'Failure remains distinct and inspectable',output:'Synthetic failure evidence',exitCode:2}),
        round('background','run_command',language==='zh'?'后台工作不等于已经完成':'Background work is not command completion',
          {command:'synthetic-detached',description:language==='zh'?'后台工作不等于已经完成':'Background work is not command completion',backgrounded:true,exitCode:'background'}),
      ];
      const blocks=rounds.map((r,i)=>({kind:'tool',blockId:r.toolCallId,ordinal:i+1,source:scope,round:r}));
      blocks.push({kind:'text',blockId:'answer',ordinal:6,markdown:language==='zh'?'总结与下一步保持可读。':'Outcome and next step stay readable.',
        text:'Outcome',deliverable:true,terminal:true,source:{}});
      window.turn={turnId:'reading-turn',presentationId:'reading-turn',actor:'assistant',role:'assistant',laneId:'main',ordinal:1,
        status:'completed',terminal:true,blocks,queuedTurns:[],branches:[],actions:[],metadata:{translation:{pending:false,streaming:false}},
        source:{projection:{timestamp:'2026-10-01T09:00:00Z'},createdAt:'2026-10-01T09:00:00Z'},finish:null};
      window.model={conversationId:'fixture',conversationRevision:1,transport:'ready',mainLane:{laneId:'main',title:'Main',kind:'main',expanded:true,turns:[turn]},
        orphanLanes:[],queue:[],planDecision:null,transportNotice:null};
      window.surface=createConversationSurface(document.getElementById('surface'),createClassicConversationRenderers({
        renderSafeMarkdownHtml:markdown=>'<p>'+escapeHtml(markdown)+'</p>',localizedText:(k,f)=>t(k)||f,
        renderToolBlockHtml:block=>_renderUnifiedToolLine(block.round,false),
      }));
      window.replay=()=>surface.render(JSON.parse(JSON.stringify(model)));
      replay();
      document.addEventListener('click',event=>{
        const button=event.target.closest('.ptool-cmd-fold');if(!button)return;
        if(button.classList.contains('ptool-cmd-fold-output'))_cmdOutputOpenToggle(button,event);
        else _cmdBodyToggle(button,event);
      });
    }""", language)

    assert frame.locator('.conversation-round-header').count() == 0
    assert not frame.locator('.conversation-round-toolbar').is_visible()
    cards = frame.locator('[data-tool-summary="self-contained"]')
    assert cards.count() == 5
    checklist = frame.locator('.ptool-todo-block')
    assert checklist.locator('summary').count() == 1
    assert checklist.locator('summary .ptool-todo-count').inner_text() == '3/4'
    assert checklist.locator('summary .ptool-todo-revisions').count() == 0
    assert not checklist.locator('.ptool-todo-list').is_visible()
    checklist.locator('summary').focus()
    page.keyboard.press('Enter')
    assert checklist.locator('.ptool-todo-list').is_visible()
    frame.evaluate('replay()')
    assert checklist.get_attribute('open') is not None
    page.keyboard.press('Enter')
    assert not checklist.locator('.ptool-todo-list').is_visible()

    descriptions = frame.locator('.ptool-cmd-desc-inline')
    assert descriptions.count() == 4
    assert descriptions.evaluate_all("""nodes=>nodes.every(n=>{
      const s=getComputedStyle(n);return s.whiteSpace==='pre-wrap'
        && s.textOverflow!=='ellipsis' && s.userSelect==='text'
        && n.scrollWidth<=n.clientWidth+1 && n.scrollHeight<=n.clientHeight+1;
    })""")
    browser = frame.locator('.ptool-cmd-js')
    # The shared command chrome already uses the icon and observed page title;
    # it intentionally omits the redundant Terminal/Browser text label.
    assert browser.locator('.ptool-cmd-context').count() == 0
    assert browser.locator('.ptool-cmd-tab').inner_text() == 'Tofu'
    assert browser.locator('.ptool-cmd-label').count() == 0
    assert '333777' not in browser.inner_text()
    assert frame.locator('.ptool-cmd-finish').count() == 0
    assert frame.locator('.ptool-cmd-err .ptool-cmd-status').inner_text() == locale['toolCommandExecution.failed']
    assert frame.locator('.ptool-cmd-background .ptool-cmd-status').inner_text() == locale['bgCommand.moved']

    command = frame.locator('[data-block-id="command"] .ptool-cmd-block')
    assert not command.locator('.ptool-cmd-code').is_visible()
    code = command.locator('.ptool-cmd-fold-cmd')
    output = command.locator('.ptool-cmd-fold-output')
    code.focus()
    page.keyboard.press('Enter')
    assert command.locator('.ptool-cmd-code').is_visible()
    assert not command.locator('.ptool-cmd-output-wrap').is_visible()
    frame.evaluate('replay()')
    assert code.evaluate('(el)=>el===document.activeElement')
    assert code.get_attribute('aria-expanded') == 'true'
    page.keyboard.press('Enter')
    assert not command.locator('.ptool-cmd-code').is_visible()
    output.focus()
    page.keyboard.press('Space')
    assert command.locator('.ptool-cmd-output-wrap').is_visible()
    assert not command.locator('.ptool-cmd-code').is_visible()
    page.keyboard.press('Space')
    assert not command.locator('.ptool-cmd-output-wrap').is_visible()
    assert frame.locator('[data-block-id="answer"]').is_visible()
    assert frame.locator('body').evaluate('(el)=>el.scrollWidth<=window.innerWidth+1')
    assert cards.evaluate_all('(nodes)=>nodes.every(n=>n.getBoundingClientRect().right<=window.innerWidth+1)')
    frame.evaluate('document.activeElement?.blur()')
    screenshot = tmp_path / f'intent-first-{width}-{language}-{theme}.png'
    page.locator('#action-reading-fixture').screenshot(path=str(screenshot))
    print(f'Intent-first action stream screenshot: {screenshot}')
