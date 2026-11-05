"""Production renderer: a large round reads as compact one-line calls.

No backend execution/storage. Uses shipped CSS, Surface and retained native
presenters; synthetic calls are private to the isolated browser page.
"""
import json
import pytest
from tests._runtime_sections import ROOT
from tests.frontend.test_frontend_tool_rounds_wire_parity import _run_harness
from tests.visual.test_visual_timeline_reading import timeline_bundle as timeline_bundle
from tests.support.frontend_visual import frontend_visual_origin, frontend_visual_page as frontend_visual_page  # noqa: F401

pytestmark = pytest.mark.visual


@pytest.mark.parametrize('width', [1536, 390])
def test_warm_dark_round_overview(frontend_visual_page, timeline_bundle, tmp_path, width):
    from playwright.sync_api import expect
    page = frontend_visual_page
    page.set_viewport_size({'width': width, 'height': 1024 if width > 600 else 844})
    page.emulate_media(reduced_motion='reduce')
    calls = []
    for i in range(24):
        call = {'_name': f'call-{i}', 'toolCallId': f'call-{i}', 'toolName': 'read_files',
                'roundNum': i + 1, 'llmRound': 5, 'taskId': 'review', 'attemptId': 'attempt',
                'status': 'done', 'attentionKind': 'routine',
                'query': f'核对组件规范 {i+1}', 'toolArgs': {'path': f'frontend/src/component-{i}.ts'},
                'results': [{'text': '源码保留完整，可在原位展开核对。', 'badge': '1 file'}],
                'toolContent': 'export const verified = true;'}
        calls.append(call)
    calls[0].update(query='读取界面样式', toolArgs={'path': 'frontend/src/styles/'}, results=[{'badge': '8 files', 'text': 'Verified source files.'}])
    calls[1].update(toolName='grep_search', query='定位渲染入口', toolArgs={'pattern': 'tool-round'}, results=[{'badge': '12 matches', 'text': 'Source matches.'}])
    calls[2].update(toolName='run_command', query='运行界面测试', toolArgs={'command': 'npm run check:frontend'},
                    results=[{'command': 'npm run check:frontend', 'description': '运行界面测试', 'output': 'All checks passed.', 'exitCode': '0'}])
    calls[3].update(toolName='apply_diffs', query='应用布局调整', toolArgs={'path': 'frontend/src/styles/'},
                    results=[{'editSummaries': [{'path':'layout.css','status':'ok'},{'path':'tools.css','status':'ok'}], 'badge':'2/2 edits', 'writeOk':True}])
    calls[21].update(toolName='browser_preview_page', query='验证桌面布局', status='searching', _isSearching=True, results=[], toolArgs={'url':'http://localhost/preview'})
    calls[22].update(toolName='browser_preview_page', query='验证窄屏布局', status='searching', _isSearching=True, results=[], toolArgs={'url':'http://localhost/mobile'})
    calls[23].update(toolName='browser_screenshot', query='获取预览截图', status='error',
                     toolContent='截图服务未返回图像，其他检查仍在继续。', results=[{'text':'截图服务未返回图像，其他检查仍在继续。'}])
    source = tmp_path / 'rounds.json'; source.write_text(json.dumps(calls))
    rendered = _run_harness(source)
    assert not any(row.get('err') for row in rendered), rendered
    html = {row['name']: row['html'] for row in rendered}
    page.set_content('<html data-theme="dark"><head><link rel="stylesheet" href="/static/styles.css"></head><body class="cv-off"></body></html>')
    page.add_style_tag(path=str(ROOT / 'frontend/src/conversation/ui/conversation-surface.css'))
    page.add_script_tag(path=timeline_bundle)
    locale = json.loads((ROOT / 'frontend/src/i18n/locales/zh.json').read_text())
    page.evaluate(r"""async ({shell,locale,calls,html})=>{
      await setReadingLanguage('zh');
      const parsed=new DOMParser().parseFromString(shell,'text/html');
      parsed.querySelectorAll('script').forEach(n=>n.remove());
      document.body.replaceChildren(...parsed.body.childNodes);
      if(innerWidth<768)document.querySelector('.sidebar').classList.add('collapsed');
      window.translate=(key,n)=>(locale[key]||key).replace('{n}',n??'');
      document.querySelectorAll('[data-i18n]').forEach(n=>n.textContent=translate(n.dataset.i18n));
      document.querySelectorAll('[data-i18n-placeholder]').forEach(n=>n.setAttribute('placeholder',translate(n.dataset.i18nPlaceholder)));
      document.getElementById('userInput').placeholder='继续补充你的想法…';
      const host=document.getElementById('chatInner');host.replaceChildren();
      const blocks=[{type:'text',blockId:'intro',text:'我先核对变更，再验证桌面和窄屏表现。',terminal:false,deliverable:false},
        ...calls.map(call=>({type:'tool_use',blockId:call._name,id:call.toolCallId,name:call.toolName,
          llmRound:5,taskId:'review',attemptId:'attempt',input:call.toolArgs,_round:call}))];
      const turn={turnId:'turn',conversationId:'review',actor:'assistant',role:'assistant',laneId:'main',ordinal:1,status:'running',projectionRevision:1,projection:{segments:blocks}};
      window.state={...createTurnState('review'),turnsById:{turn},laneOrder:{main:['turn']}};
      window.surface=createConversationSurface(host,{...createClassicConversationRenderers({
        renderSafeMarkdownHtml:text=>{const p=document.createElement('p');p.textContent=text;return p.outerHTML;},
        renderToolBlockHtml:block=>html[block.blockId]||'', localizedText:(key,fallback)=>locale[key]||fallback}),
        roundOutlineLabel:translate,scrollViewport:document.getElementById('chatContainer')});
      surface.render(selectConversationViewModel(state));
    }""", {'shell': (ROOT/'index.html').read_text(), 'locale': locale, 'calls': calls, 'html': html})
    out=ROOT/'output'/'design'/'tofu-compact-reading-implemented';out.mkdir(parents=True,exist_ok=True)
    page.locator('#chatContainer').evaluate('node=>node.scrollTop=0')
    page.screenshot(path=str(out/f'overview-{width}.png'))
    (out/f'geometry-{width}.json').write_text(json.dumps(page.evaluate('''()=>Object.fromEntries(
      ['html','body','.sidebar','.topbar','.input-inner','.input-box','.conversation-round-header'].map(s=>{
        const n=document.querySelector(s),css=getComputedStyle(n),box=n.getBoundingClientRect();
        return [s,{background:css.backgroundColor,bgSecondary:css.getPropertyValue('--bg-secondary'),
          color:css.color,font:css.fontFamily,x:box.x,width:box.width}];}))'''),indent=2))
    assert page.evaluate('getComputedStyle(document.documentElement).getPropertyValue("--bg-primary").trim().toLowerCase()') == '#24221f'
    expect(page.locator('.conversation-round-header')).to_contain_text('24')
    expect(page.locator('.conversation-round-fold')).to_contain_text('17')
    assert page.locator('.conversation-block--tool:visible').count() == 7
    expect(page.locator('[data-block-id="call-23"]')).to_contain_text('截图服务未返回图像')
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
    # Every routine call is one compact row inside the tool-panel card, led by
    # its plain line glyph (no tile, wash or shadow); identity never grows a
    # second line (the V5 two-line 64px header showed one call per screen).
    glyphs = page.locator('[data-action-summary] > .ptool-icon:visible')
    assert glyphs.count() > 0
    assert glyphs.evaluate_all('''nodes=>nodes.every(n=>{const s=getComputedStyle(n);
      return n.querySelectorAll('svg').length===1 && s.backgroundImage==='none' && s.boxShadow==='none'
        && ['rgba(0, 0, 0, 0)','transparent'].includes(s.backgroundColor);})''')
    if width > 600:
        heights = page.evaluate('''()=>[...document.querySelectorAll(
          '.conversation-block:not([data-action-state="attention"]) [data-action-summary]')]
          .filter(n=>n.offsetParent).map(n=>n.getBoundingClientRect().height)''')
        assert heights and max(heights) <= 36, heights
    fold=page.locator('.conversation-round-fold');fold.focus();fold.press('Enter')
    assert page.locator('.conversation-block--tool:visible').count() == 24
    first=page.locator('[data-block-id="call-0"] summary').first
    first.focus();first.press('Enter')
    expect(page.locator('[data-block-id="call-0"] details').first).to_have_attribute('open','')
    page.evaluate('()=>surface.render(selectConversationViewModel({...state,conversationRevision:2}))')
    expect(first).to_be_focused()
    fold.click()
    search=page.locator('.conversation-round-search');search.fill('component-7')
    page.locator('.conversation-round-matches button').first.click()
    expect(page.locator('[data-block-id="call-7"]')).to_be_focused()
    # Without a context rail the column and its composer share one center.
    composer=page.locator('.input-inner').bounding_box();pane=page.locator('#chatContainer').bounding_box()
    assert abs((composer['x']+composer['width']/2)-(pane['x']+pane['width']/2))<=1
    # New-chat shell retains exactly the same main pane and centered composer.
    page.evaluate('()=>{surface.dispose();document.getElementById("chatInner").innerHTML="<div class=welcome><h2>开始新的对话</h2></div>";}')
    composer=page.locator('.input-inner').bounding_box();pane=page.locator('#chatContainer').bounding_box()
    assert abs((composer['x']+composer['width']/2)-(pane['x']+pane['width']/2))<=1
    page.screenshot(path=str(out/f'new-chat-{width}.png'))
