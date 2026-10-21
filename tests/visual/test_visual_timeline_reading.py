"""Mixed chronology through the production Surface and tool presenters.

No conversation writes or tool execution. Real Chromium proves common reading
geometry, wrapping, contrast, native keyboard disclosure and replay continuity.
"""
import json

import pytest

from tests._runtime_sections import ROOT, native_module_path, runtime_section
from tests.frontend.test_frontend_tool_rounds_wire_parity import _run_harness
from tests.support.frontend_visual import frontend_visual_origin as frontend_visual_origin, frontend_visual_page as frontend_visual_page

pytestmark = pytest.mark.visual


def test_answer_title_waits_for_turn_settlement(frontend_visual_page, timeline_bundle):
    """Actual Surface replay: translation and tool boundaries cannot title narration."""
    from playwright.sync_api import expect

    page = frontend_visual_page
    page.set_content('<html data-theme="tofu"><head><link rel="stylesheet" href="/static/styles.css">'
                     '</head><body class="cv-off"><main id="surface"></main></body></html>')
    page.add_style_tag(path=str(ROOT / 'frontend/src/conversation/ui/conversation-surface.css'))
    page.add_script_tag(path=timeline_bundle)
    page.evaluate("""async ()=>{
      await setReadingLanguage('zh');
      let revision=0;
      window.surface=createConversationSurface(document.getElementById('surface'),createClassicConversationRenderers({
        renderSafeMarkdownHtml:text=>{const p=document.createElement('p');p.textContent=text;return p.outerHTML;},
        localizedText:(key,fallback)=>key==='conversation.answer'?'答复':fallback,
      }));
      window.stage=(status,segments,extra={})=>{
        const turn={turnId:'turn',conversationId:'answer-lifecycle',actor:'assistant',role:'assistant',
          laneId:'main',ordinal:0,status,projectionRevision:++revision,projection:{segments,...extra}};
        surface.render(selectConversationViewModel({...createTurnState('answer-lifecycle'),
          turnsById:{turn},laneOrder:{main:['turn']}}));
      };
      window.tail=(text,translatedText)=>({type:'text',blockId:'text:terminal',text,translatedText,
        terminal:true,deliverable:true});
      stage('running',[tail('I will inspect the file.')]);
    }""")
    expect(page.locator('.md-content')).to_have_text('I will inspect the file.')
    expect(page.locator('.conversation-answer-label')).to_have_count(0)
    # Translation is already durable, but this content still precedes a tool.
    page.evaluate("""()=>stage('running',[tail('I will inspect the file.','我会检查文件。')],
      {translation:{status:'completed',model:'translator'}})""")
    expect(page.locator('.md-content')).to_have_text('我会检查文件。')
    expect(page.locator('.conversation-answer-label, .bilingual-block')).to_have_count(0)
    page.evaluate("""()=>{
      window.readingHistory=[{type:'text',blockId:'narration-1',text:'I will inspect the file.',
        translatedText:'我会检查文件。',terminal:false,deliverable:false},
        {type:'tool_use',blockId:'read',id:'read',name:'read_files',input:{path:'example.txt'},
          _round:{toolName:'read_files',toolCallId:'read',status:'done',results:[{text:'Verified.'}]}}];
      stage('running',readingHistory);
    }""")
    expect(page.locator('.conversation-answer-label')).to_have_count(0)
    expect(page.locator('[data-block-id="narration-1"] .md-content')).to_have_text('我会检查文件。')
    page.evaluate("""()=>{
      window.answerSegments=[...readingHistory,tail('The file is correct.')];
      stage('running',answerSegments);
    }""")
    expect(page.locator('.conversation-answer-label')).to_have_count(0)
    # Only status changes; semantic block equality must still repaint the title.
    page.evaluate("()=>stage('completed',answerSegments)")
    expect(page.locator('.conversation-answer-title')).to_have_text('答复')
    expect(page.locator('[data-block-id="text:terminal"] > .md-content')).to_have_text('The file is correct.')
    process = page.locator('.conversation-history-process > summary')
    process.focus()
    page.evaluate("""()=>stage('completed',answerSegments,{translatedContent:'文件没有问题。',
      translation:{status:'completed',model:'translator'}})""")
    expect(process).to_be_focused()
    expect(page.locator('.conversation-answer-title')).to_have_count(1)
    expect(page.locator('[data-block-id="text:terminal"] > .md-content')).to_have_text('文件没有问题。')
    expect(page.locator('.bilingual-block')).to_have_count(1)
    page.evaluate("()=>stage('running',answerSegments)")
    expect(page.locator('.conversation-answer-label, .bilingual-block')).to_have_count(0)
    expect(page.locator('[data-block-id="narration-1"] .md-content')).to_have_text('我会检查文件。')
    page.evaluate('surface.dispose()')


@pytest.fixture(scope='module')
def reading_tools(tmp_path_factory):
    rounds = [
        {'_name': 'read', 'toolName': 'read_files', 'query': '阅读对话渲染与组件规范',
         'toolArgs': {'path': 'docs/RENDER_CONTRACT.md'}, 'status': 'done', 'roundNum': 1,
         'toolContent': 'Chronology preserves narration, reasoning and tool evidence in their original order.',
         'results': [{'text': 'The renderer keeps stable block identity.'}]},
        {'_name': 'command', 'toolName': 'run_command', 'query': 'npm run check:styles',
         'status': 'done', 'roundNum': 2, 'results': [{'command': 'npm run check:styles',
         'description': '检查组件样式与主题一致性', 'output': 'Style checks passed.', 'exitCode': '0'}]},
        {'_name': 'checklist', 'toolName': 'todo_write', 'status': 'done', 'roundNum': 3,
         'results': [{'todos': [{'id': 'read', 'text': 'Review the reading hierarchy', 'status': 'completed'},
                               {'id': 'verify', 'text': 'Verify keyboard and mobile layouts', 'status': 'in_progress'}]}]},
        {'_name': 'failure', 'toolName': 'read_files', 'query': 'Read the optional preview',
         'status': 'error', 'roundNum': 4, 'toolContent': 'Preview unavailable. The saved source remains accessible.',
         'results': [{'text': 'Preview unavailable. The saved source remains accessible.', 'exitCode': 1}]},
    ]
    path = tmp_path_factory.mktemp('reading-rounds') / 'rounds.json'
    path.write_text(json.dumps(rounds))
    rendered = _run_harness(path)
    assert not any(row.get('err') for row in rendered), rendered
    return {row['name']: row['html'] for row in rendered}


@pytest.fixture(scope='module')
def timeline_bundle(tmp_path_factory):
    entry = tmp_path_factory.mktemp('timeline-module') / 'entry.ts'
    entry.write_text(f"""
export * from '{ROOT}/frontend/src/conversation/index.ts';
export {{createTurnState}} from '{ROOT}/frontend/src/core/turn-state.ts';
export {{setLanguage as setReadingLanguage}} from '{ROOT}/frontend/src/i18n/index.ts';
""")
    return native_module_path('.native/mixed-reading.js', entry)


@pytest.mark.parametrize('theme', ['light', 'dark', 'tofu'])
@pytest.mark.parametrize('width', [1180, 390, 320])
def test_mixed_timeline_reading(frontend_visual_page, reading_tools, timeline_bundle, tmp_path, theme, width):
    from playwright.sync_api import expect

    page = frontend_visual_page
    page.set_viewport_size({'width': width, 'height': 1100})
    page.emulate_media(reduced_motion='reduce')
    page.set_content(f'<!doctype html><html data-theme="{theme}"><head>'
                     '<link rel="stylesheet" href="/static/styles.css"></head><body class="cv-off">'
                     '<div class="chat-inner"><main id="surface"></main></div></body></html>')
    page.add_style_tag(path=str(ROOT / 'frontend/src/conversation/ui/conversation-surface.css'))
    # The phone shell fixes body and delegates scrolling to chatContainer. This
    # standalone transcript has no shell until the last capture, so it must
    # override that host geometry to paint the entire reading column.
    host_style = page.add_style_tag(content='html,body{position:static!important;height:auto!important;overflow:visible!important}'
                               'body{display:block;margin:0;padding:var(--space-6) var(--space-4)}'
                               '.chat-inner{margin-inline:auto;min-height:0;padding:0}')
    for source in ['node_modules/marked/lib/marked.umd.js', 'node_modules/dompurify/dist/purify.min.js']:
        page.add_script_tag(path=str(ROOT / source))
    locale = json.loads((ROOT / 'frontend/src/i18n/locales/zh.json').read_text())
    page.evaluate(r"""locale => {
      window.BASE_PATH=''; window.projectState={active:false}; window._i18nLang='zh';
      window.t=(key,values={})=>(locale[key]||key).replace(/\{(\w+)\}/g,(m,k)=>values[k]??m);
      window.escapeHtml=value=>String(value??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;');
    }""", locale)
    page.add_script_tag(content=runtime_section('core/markdown.js', scope_prelude=False))
    page.add_script_tag(path=timeline_bundle)
    page.evaluate(r"""async ({locale,tools})=>{
      await setReadingLanguage('zh');
      const scope={taskId:'reading-task',attemptId:'reading-attempt'};
      const text=(blockId,text,llmRound,extra={})=>({type:'text',blockId,text,llmRound,...scope,...extra});
      const thinking=(blockId,llmRound)=>({type:'thinking',blockId,llmRound,...scope,text:'需要保留真实顺序，让读者能理解每一步的原因，也能随时查看原始证据。'});
      const tool=(blockId,name,llmRound)=>({type:'tool_use',blockId,id:blockId,name,llmRound,...scope,
        _round:{toolName:name,toolCallId:blockId,roundNum:llmRound+1,llmRound,...scope,status:'done',results:[{text:'Evidence retained.'}]}});
      const segments=[
        text('intro','我会先检查渲染结构，再统一正文、思考和工具调用的阅读层级。',0),
        thinking('thinking-0',0),tool('read','read_files',0),
        text('narration','目前的主要问题是重复的卡片边框和不一致的缩进。接下来会让过程更紧凑，同时保留必要的状态提示。',1),
        thinking('thinking-1',1),tool('command','run_command',1),
        {type:'system_note',blockId:'note',noteKind:'context-injection',reason:'active_checklist',text:'检查项：阅读层级、键盘交互、窄屏布局。',...scope,llmRound:1},
        tool('checklist','todo_write',1),
        tool('program','execute_program',2),
        text('answer','### 让阅读沿着内容自然前进\n\n正文保持清晰的阅读节奏；思考和工具使用统一的折叠入口，需要时再展开查看。\n\n- 时间顺序保持完整。\n- 过程标题对齐，展开详情共用缩进。\n- 失败与待确认操作继续明确显示。\n\n### 过程随时可查\n\n在答复旁打开过程面板，可以沿着原始时间线查看证据。\n\n### 留住豆腐的温度\n\n清淡的底色、少量暖色点缀和柔软的圆角，让阅读轻松一点。',3,{deliverable:true,terminal:true})];
      segments.find(s=>s.blockId==='program')._round={toolName:'execute_program',toolCallId:'program',status:'done',
        toolArgs:{code:'return await tools.read_files({path:"docs/RENDER_CONTRACT.md"});'},
        toolContent:{summary:'Renderer contract inspected'},...scope,llmRound:2};
      const request={turnId:'request',conversationId:'timeline-reading',role:'user',actor:'human',laneId:'main',ordinal:0,
        status:'completed',projectionRevision:1,createdAt:'2026-10-19T09:42:00Z',
        projection:{content:'保留真实时间线，把聊天界面的阅读体验重新设计一下。希望过程清楚，结果突出，整体更有美感。'}};
      const turn={turnId:'turn',conversationId:'timeline-reading',role:'assistant',actor:'assistant',laneId:'main',ordinal:1,
        status:'running',projectionRevision:1,createdAt:'2026-10-19T09:42:05Z',projection:{segments}};
      window.state={...createTurnState('timeline-reading'),turnsById:{request,turn},laneOrder:{main:['request','turn']}};
      const localize=(key,fallback,values={})=>(locale[key]||fallback).replace(/\{(\w+)\}/g,(m,k)=>values[k]??m);
      window.surface=createConversationSurface(document.getElementById('surface'),createClassicConversationRenderers({
        renderSafeMarkdownHtml:renderMarkdown,localizedText:localize,roleLabel:turn=>turn.role==='user'?'你':'Tofu',
        renderToolBlockHtml:block=>tools[block.blockId]||''}));
      window.render=()=>surface.render(selectConversationViewModel(state));render();
      window.initialOrder=[...document.querySelectorAll('.conversation-block[data-block-id]')].map(n=>n.dataset.blockId);
    }""", {'locale': locale, 'tools': reading_tools})
    # Full-page captures opt out of the production offscreen paint optimization;
    # virtualized painting otherwise clips the tail to the original viewport.
    page.screenshot(path=str(tmp_path / f'timeline-{theme}-{width}.png'), full_page=True)
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
    # Visual runs follow exact adjacency and never absorb authored narration.
    assert page.locator('[data-block-id="thinking-0"]').get_attribute('data-process-edge') == 'start'
    assert page.locator('[data-block-id="read"]').get_attribute('data-process-edge') == 'end'
    assert page.locator('[data-block-id="thinking-1"]').get_attribute('data-process-edge') == 'start'
    assert page.locator('[data-block-id="program"]').get_attribute('data-process-edge') == 'end'
    assert page.locator('[data-block-id="narration"]').get_attribute('data-process-edge') is None
    assert not page.locator('.conversation-answer-label').count()
    assert not page.locator('.user-msg .conversation-answer-label').count()
    # Role, narration, process labels and the answer share an actual text edge.
    alignment = page.evaluate("""()=>['.conversation-turn:not(.user-msg) .message-role',
      '[data-block-id="intro"] .md-content', '.thinking-label',
      '[data-block-id="answer"] > .md-content > h3']
      .map(s=>({selector:s,x:document.querySelector(s).getBoundingClientRect().left}))""")
    assert max(x['x'] for x in alignment)-min(x['x'] for x in alignment) <= 1, alignment
    # Native keyboard access and mounted reader choices survive a revision.
    summary = page.locator('.thinking-header').first
    summary.focus()
    summary.press('Enter')
    expect(page.locator('.thinking-text').first).to_be_visible()
    page.evaluate("""()=>{
      window.readingSummary=document.querySelector('.thinking-header');
      const turn=state.turnsById.turn;
      state={...state,turnsById:{...state.turnsById,turn:{...turn,projectionRevision:2}}};render();
    }""")
    expect(summary).to_be_focused()
    assert summary.evaluate('node=>node===readingSummary')
    assert page.evaluate("JSON.stringify(initialOrder)===JSON.stringify([...document.querySelectorAll('.conversation-block[data-block-id]')].map(n=>n.dataset.blockId))")
    # A process row has one shared inset and readable type; details do not add
    # arbitrary layers of card borders. Compare actual labels, not CSS text.
    measurements = page.evaluate(r"""()=>{
      const selectors=['.thinking-label','.ptool-result-header .ptool-text','.ptool-cmd-desc-inline','.ptool-todo-header .ptool-text','.ptc-title'];
      const ctx=document.createElement('canvas').getContext('2d');
      const rgb=value=>{ctx.clearRect(0,0,1,1);ctx.fillStyle=value;ctx.fillRect(0,0,1,1);return [...ctx.getImageData(0,0,1,1).data];};
      const luminance=value=>rgb(value).slice(0,3).map(n=>{n/=255;return n<=.04045?n/12.92:((n+.055)/1.055)**2.4;})
        .reduce((v,n,i)=>v+n*[.2126,.7152,.0722][i],0);
      return selectors.map(selector=>{const n=document.querySelector(selector);if(!n)return {selector,missing:true};
        const block=n.closest('.conversation-block'),s=getComputedStyle(n),ink=luminance(s.color);
        let backdrop=n;while(backdrop.parentElement&&rgb(getComputedStyle(backdrop).backgroundColor)[3]<255)backdrop=backdrop.parentElement;
        const canvas=luminance(getComputedStyle(backdrop).backgroundColor);
        return {selector,inset:n.getBoundingClientRect().left-block.getBoundingClientRect().left,size:parseFloat(s.fontSize),visible:n.getBoundingClientRect().width>0,
          contrast:(Math.max(canvas,ink)+.05)/(Math.min(canvas,ink)+.05)};});
    }""")
    assert all(not row.get('missing') and row['visible'] for row in measurements), measurements
    assert max(row['inset'] for row in measurements) - min(row['inset'] for row in measurements) <= 2, measurements
    assert all(row['size'] >= 13 for row in measurements), measurements
    assert all(row['contrast'] >= 4.5 for row in measurements), measurements
    (tmp_path / f'timeline-{theme}-{width}.json').write_text(json.dumps(measurements, indent=2))
    summary.press('Enter')
    expect(page.locator('.thinking-text').first).not_to_be_visible()
    for selector, body in [('.ptool-result-header', '.ptool-result-body'),
                           ('.ptool-todo-header', '.ptool-todo-body'), ('.ptc-head', '.ptc-body')]:
        control = page.locator(selector).first
        control.focus(); control.press('Enter')
        expect(page.locator(body).first).to_be_visible()
        if width < 720:
            assert control.bounding_box()['height'] >= 42
        control.press('Enter')
        expect(page.locator(body).first).not_to_be_visible()
    # Long labels remain selectable and wrap at phone widths and browser zoom.
    page.locator('.ptool-cmd-desc-inline').evaluate("n=>n.textContent='检查前端组件与主题的一致性，并保留完整的操作说明。'.repeat(5)")
    assert page.locator('.ptool-cmd-desc-inline').evaluate('n=>n.scrollWidth<=n.clientWidth+1')
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
    # Settlement uses the existing final-answer/process owner, preserving the
    # real order when reopened rather than regrouping by content kind.
    page.evaluate("""()=>{const turn=state.turnsById.turn;state={...state,turnsById:{...state.turnsById,turn:{...turn,status:'completed',projectionRevision:3}}};render();}""")
    expect(page.locator('[data-block-id="answer"]')).to_be_visible()
    assert page.locator('.conversation-answer-title').inner_text() == '答复'
    answer_edges = page.evaluate("""()=>['.conversation-turn:not(.user-msg) .message-role',
      '.conversation-answer-title','[data-block-id="answer"] > .md-content > h3']
      .map(s=>document.querySelector(s).getBoundingClientRect().left)""")
    assert max(answer_edges)-min(answer_edges) <= 1, answer_edges
    outline = page.locator('.conversation-answer-outline')
    outline.locator('summary').focus(); outline.locator('summary').press('Enter')
    expect(outline.locator('nav')).to_be_visible()
    outline.get_by_role('button', name='留住豆腐的温度').click()
    expect(page.locator('[data-block-id="answer"] h3').last).to_be_focused()
    expect(outline.locator('nav')).not_to_be_visible()
    process = page.locator('.conversation-history-process > summary')
    answer_y = page.locator('[data-block-id="answer"] > .md-content').bounding_box()['y']
    process.focus(); process.press('Enter')
    expect(page.locator('[data-block-id="read"]')).to_be_visible()
    expect(page.locator('.conversation-history-process__controls')).not_to_be_visible()
    pane = page.locator('.conversation-history-process__body')
    expect(pane).to_have_attribute('popover', 'manual')
    expect(pane.get_by_role('button', name='回到答复')).to_be_focused()
    assert abs(page.locator('[data-block-id="answer"] > .md-content').bounding_box()['y']-answer_y) <= 1
    assert pane.evaluate('n=>n.scrollWidth<=n.clientWidth+1')
    pane.get_by_role('button', name='回到答复').press('Escape')
    expect(process).to_be_focused()
    expect(pane).to_have_count(0)
    process.press('Enter')
    expect(page.locator('.conversation-history-process__body:popover-open')).to_be_visible()
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
    page.screenshot(path=str(tmp_path / f'timeline-history-{theme}-{width}.png'), full_page=True)
    # A failure stays in its original slot with visible summary evidence even
    # when its raw diagnostic output is folded. The new quiet success styles
    # must not erase this distinction.
    page.evaluate("""html=>{
      const block=document.createElement('section');block.className='conversation-block conversation-block--tool';
      block.id='failure-proof';block.innerHTML=html;document.querySelector('.conversation-turn-blocks').append(block);
    }""", reading_tools['failure'])
    failure = page.locator('#failure-proof')
    expect(failure).to_contain_text('Preview unavailable')
    expect(failure.locator('summary')).to_be_visible()
    assert failure.evaluate('n=>n.scrollWidth<=n.clientWidth+1')
    # Review the settled answer in the real application shell markup. Scripts
    # are deliberately absent: this is geometry, not an app/backend boot test.
    # The same mounted Surface moves into chatInner; no screenshot-only cards.
    failure.evaluate('n=>n.remove()')
    process.press('Enter')
    host_style.evaluate('n=>n.remove()')
    page.evaluate(r"""html=>{
      const surface=document.getElementById('surface');
      const shell=new DOMParser().parseFromString(html,'text/html');
      shell.querySelectorAll('script').forEach(n=>n.remove());
      document.body.replaceChildren(...shell.body.childNodes);
      document.getElementById('chatInner').replaceChildren(surface);
      // The normal shell initializer starts the phone drawer closed.
      if (innerWidth < 768) document.querySelector('.sidebar').classList.add('collapsed');
      document.querySelectorAll('[data-i18n]').forEach(n=>n.textContent=t(n.dataset.i18n));
      document.querySelectorAll('[data-i18n-placeholder]').forEach(n=>n.setAttribute('placeholder',t(n.dataset.i18nPlaceholder)));
      document.getElementById('userInput').setAttribute('placeholder','继续讨论，或提出新的问题…');
    }""", (ROOT / 'index.html').read_text())
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
    page.screenshot(path=str(tmp_path / f'timeline-shell-{theme}-{width}.png'))
    primary_fill = page.locator('.new-chat-btn').evaluate('n=>getComputedStyle(n).backgroundImage')
    assert primary_fill == 'none'
    if theme == 'tofu':
        paper = page.evaluate('getComputedStyle(document.documentElement).getPropertyValue("--bg-primary").trim().toUpperCase()')
        assert paper == '#F8FAFC'
    assert page.locator('.conversation-turn-actions button').evaluate_all(
        'nodes=>nodes.every(n=>n.getAttribute("aria-label") && n.title)')
    page.locator('#userInput').focus()
    expect(page.locator('#userInput')).to_be_focused()
    # One Surface has one active inspector, even when several answers expose
    # Process. Opening another releases the previous lazy body immediately.
    page.evaluate("""()=>{
      const second={...state.turnsById.turn,turnId:'second',ordinal:2};
      state={...state,turnsById:{...state.turnsById,second},laneOrder:{main:['request','turn','second']}};render();
    }""")
    first_process = page.locator('[data-turn-id="turn"] .conversation-history-process')
    second_process = page.locator('[data-turn-id="second"] .conversation-history-process')
    first_process.locator(':scope > summary').press('Enter')
    expect(first_process.locator('.conversation-history-process__body:popover-open')).to_be_visible()
    second_process.locator(':scope > summary').press('Enter')
    expect(second_process.locator('.conversation-history-process__body:popover-open')).to_be_visible()
    expect(first_process.locator('.conversation-history-process__body')).to_have_count(0)
    assert not first_process.evaluate('n=>n.open')
    expect(page.locator(':popover-open')).to_have_count(1)
    page.evaluate('surface.dispose()')
    expect(page.locator(':popover-open')).to_have_count(0)


@pytest.mark.parametrize('theme', ['light', 'dark', 'tofu'])
@pytest.mark.parametrize('width', [1180, 390, 320])
def test_tool_svg_slots_are_transparent(frontend_visual_page, reading_tools, timeline_bundle, theme, width):
    """Live and historical tool identities have no opaque tile or shadow halo."""
    from playwright.sync_api import expect

    page = frontend_visual_page
    page.set_viewport_size({'width': width, 'height': 900})
    page.set_content(f'<html data-theme="{theme}"><head><link rel="stylesheet" href="/static/styles.css">'
                     '</head><body class="cv-off"><main id="surface"></main></body></html>')
    page.add_style_tag(path=str(ROOT / 'frontend/src/conversation/ui/conversation-surface.css'))
    page.add_script_tag(path=timeline_bundle)
    page.evaluate("""tools=>{
      const scope={taskId:'icon-task',attemptId:'icon-attempt',llmRound:0};
      const tool=(blockId,name)=>({type:'tool_use',blockId,id:blockId,name,...scope,
        _round:{toolName:name,toolCallId:blockId,status:'done',...scope}});
      const turn={turnId:'turn',conversationId:'icon-reading',actor:'assistant',role:'assistant',
        laneId:'main',ordinal:0,status:'running',projectionRevision:1,projection:{segments:[
          tool('read','read_files'),tool('command','run_command'),
          {type:'text',blockId:'answer',text:'Verified.',deliverable:true,terminal:true}]}};
      window.iconState={...createTurnState('icon-reading'),turnsById:{turn},laneOrder:{main:['turn']}};
      window.iconSurface=createConversationSurface(document.getElementById('surface'),
        createClassicConversationRenderers({
          renderSafeMarkdownHtml:text=>{const p=document.createElement('p');p.textContent=text;return p.outerHTML;},
          renderToolBlockHtml:block=>tools[block.blockId]||''}));
      window.renderIcons=()=>iconSurface.render(selectConversationViewModel(iconState));renderIcons();
    }""", reading_tools)

    def assert_unpainted():
        for selector in ['[data-block-id="read"] .ptool-icon',
                         '[data-block-id="command"] .ptool-cmd-icon']:
            icon = page.locator(selector).first
            expect(icon).to_be_visible()
            expect(icon.locator('svg')).to_have_count(1)
            expect(icon).to_have_css('background-color', 'rgba(0, 0, 0, 0)')
            expect(icon).to_have_css('background-image', 'none')
            expect(icon).to_have_css('box-shadow', 'none')

    expect(page.locator('[data-block-id="read"]')).to_have_attribute('data-process-edge', 'start')
    expect(page.locator('[data-block-id="command"]')).to_have_attribute('data-process-edge', 'end')
    assert_unpainted()
    page.evaluate("""()=>{const turn=iconState.turnsById.turn;
      iconState={...iconState,turnsById:{turn:{...turn,status:'completed',projectionRevision:2}}};renderIcons();}""")
    page.locator('.conversation-history-process > summary').click()
    expect(page.locator('.conversation-history-process__body:popover-open')).to_be_visible()
    assert_unpainted()
    page.evaluate('iconSurface.dispose()')
