"""Context rail reading and disclosure through its production HTML/Surface.

No backend or user data: real shell geometry, delegated clicks and the actual
rail track pass prove the expanded card stays reachable and out of prose flow.
A workspace line reads `root:branch` on one line when the pair fits its rail;
too long for its rail, it breaks only after the colon: each name stays whole
unless it alone is wider than the line.
"""
import json

import pytest

from tests._runtime_sections import ROOT, runtime_section
from tests.support.frontend_visual import frontend_visual_origin, frontend_visual_page as frontend_visual_page  # noqa: F401
from tests.visual.test_visual_timeline_reading import timeline_bundle as timeline_bundle

pytestmark = pytest.mark.visual

READING_SNAPSHOT = {
    'model': 'claude-sonnet', 'depth': 'high', 'modes': [{'label': 'Autopilot'}],
    'tools': [{'label': label, 'tone': 'mcp'} for label in (
        'Search', 'Read files', 'Run command', 'Memory', 'MCP: GitHub', 'MCP: Documents',
        'MCP: Browser', 'MCP: Calendar', 'MCP: Research', 'MCP: Workspace')],
    'roots': [{'short': short, 'path': '/workspace/' + short} for short in (
        'tofu/frontend', 'tofu/docs', 'tofu/tests', 'shared/design', 'shared/assets')],
    'git': {'branch': 'design/reading-workspace'},
    'todos': [{'content': '检查阅读与折叠层级', 'status': 'completed'},
              {'content': '统一组件的对齐和配色', 'status': 'in_progress'},
              {'content': '验证键盘与手机体验', 'status': 'pending'}],
}

#: The owner's workspace line (2026-11-01 report), captured as the shell does.
OWNER_ROOT = '/path/to/your/project'
OWNER_BRANCH = 'eval/opencode-glm53-tb21'
OWNER_SNAPSHOT = {
    'model': 'claude-sonnet',
    'roots': [{'short': 'your-username/tofu', 'path': OWNER_ROOT, 'ro': False}],
    'git': {'branch': OWNER_BRANCH, 'branches': [{'path': OWNER_ROOT, 'branch': OWNER_BRANCH}]},
}
#: Wider than a line of every rail these specs grant.
LONG_BRANCH = 'feature/a-long-branch-name-that-has-to-wrap-inside-the-rail'

#: A root line as laid out: its characters grouped by line box, how far any
#: character reaches past the text box (and the card past its own box), the
#: text box's height, and the glyph's centre measured from the first line's top.
_ROOT_LINE_LAYOUT = """n=>{
  const text=n.querySelector('.tctx-path-text'),box=text.getBoundingClientRect();
  const lineH=parseFloat(getComputedStyle(text).lineHeight),lines=[];
  let top=null,right=box.left;
  const walk=document.createTreeWalker(text,NodeFilter.SHOW_TEXT);
  for(let node=walk.nextNode();node;node=walk.nextNode())for(let i=0;i<node.data.length;i++){
    const range=document.createRange();range.setStart(node,i);range.setEnd(node,i+1);
    const rect=range.getBoundingClientRect();
    if(top===null||rect.top-top>lineH/2){lines.push('');top=rect.top;}
    lines[lines.length-1]+=node.data[i];right=Math.max(right,rect.right);
  }
  const glyph=n.querySelector('.tctx-icon svg').getBoundingClientRect(),card=n.closest('.tctx-card');
  return {lines,lineH,overflow:right-box.right,cardOverflow:card.scrollWidth-card.clientWidth,
    textHeight:box.height,glyphCentre:glyph.top+glyph.height/2-box.top};
}"""


def _mount_rail(page, timeline_bundle, theme, language, snapshot):
    """One request/answer round whose request ran with ``snapshot``, painted
    by the production shell, Surface and rail renderer."""
    page.set_viewport_size({'width': 1680, 'height': 1100})
    page.emulate_media(reduced_motion='reduce')
    page.set_content(f'<!doctype html><html data-theme="{theme}"><head>'
                     '<link rel="stylesheet" href="/static/styles.css"></head><body class="cv-off"></body></html>')
    page.add_style_tag(path=str(ROOT / 'frontend/src/conversation/ui/conversation-surface.css'))
    page.evaluate("""html=>{
      const shell=new DOMParser().parseFromString(html,'text/html');
      shell.querySelectorAll('script').forEach(n=>n.remove());
      document.body.replaceChildren(...shell.body.childNodes);
      document.getElementById('chatInner').innerHTML='<main id="surface" style="display:contents"></main>';
      // Offline shell host: relay the resolved rail grant just as the real
      // shell initializer does. The Surface itself spans both parent tracks.
      const geometry=getComputedStyle(document.getElementById('chatInner'));
      for(const key of ['--rail-w','--rail-gap'])document.querySelector('.input-inner').style.setProperty(key,geometry.getPropertyValue(key));
    }""", (ROOT / 'index.html').read_text())
    locale = json.loads((ROOT / f'frontend/src/i18n/locales/{language}.json').read_text())
    page.evaluate(r"""({locale,language})=>{
      window.runtimeScope=window; window.BASE_PATH=''; window.projectState={active:false}; window._i18nLang=language;
      window.t=(key,values={})=>(locale[key]||key).replace(/\{(\w+)\}/g,(m,k)=>values[k]??m);
      window.escapeHtml=value=>String(value??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;');
      window._modelShortName=()=> 'Claude Sonnet';
      document.querySelectorAll('[data-i18n]').forEach(n=>n.textContent=t(n.dataset.i18n));
      document.querySelectorAll('[data-i18n-placeholder]').forEach(n=>n.setAttribute('placeholder',t(n.dataset.i18nPlaceholder)));
    }""", {'locale': locale, 'language': language})
    for source in ['node_modules/marked/lib/marked.umd.js', 'node_modules/dompurify/dist/purify.min.js']:
        page.add_script_tag(path=str(ROOT / source))
    page.add_script_tag(content=runtime_section('core/markdown.js', scope_prelude=False))
    page.add_script_tag(content=runtime_section('info-rail.js', scope_prelude=False))
    page.add_script_tag(path=timeline_bundle)
    page.evaluate(r"""async ({locale,language,snapshot})=>{
      await setReadingLanguage(language);
      const request={turnId:'request',conversationId:'rail-reading',role:'user',actor:'human',laneId:'main',ordinal:0,status:'completed',projectionRevision:1,
        projection:{content:'让聊天更好读，也留住豆腐温暖可爱的感觉。',contextSnapshot:{blockId:'context',snapshot}}};
      const turn={turnId:'answer',conversationId:'rail-reading',role:'assistant',actor:'assistant',laneId:'main',ordinal:1,status:'completed',projectionRevision:1,
        projection:{segments:[{type:'text',blockId:'answer',deliverable:true,terminal:true,
          text:'### 让阅读轻松一点\n\n答复是这一页的主角。正文、过程标题和身份信息沿同一条基线展开，视线可以自然向下阅读。\n\n### 需要的细节，随时展开\n\n右侧的信息分成清晰的小节。先看到模型、常用工具和工作区；更多内容就在标题旁展开。展开以后，按钮仍然留在原来的位置。\n\n### 留住豆腐的温度\n\n柔和的奶油纸色，配上焦糖色的小小强调。圆角与留白让信息亲近一点，也让一段长对话有自己的呼吸。\n\n- 清楚的标题与数量\n- 一致的展开方向\n- 完整保留的上下文'}]}};
      window.state={...createTurnState('rail-reading'),turnsById:{request,answer:turn},laneOrder:{main:['request','answer']}};
      window.surface=createConversationSurface(document.getElementById('surface'),createClassicConversationRenderers({
        renderSafeMarkdownHtml:renderMarkdown,localizedText:(key,fallback,values={})=>(locale[key]||fallback).replace(/\{(\w+)\}/g,(m,k)=>values[k]??m),
        roleLabel:turn=>turn.role==='user'?'你':'Tofu',renderTurnContextParts:block=>renderTurnCtxNote(block.value.snapshot)}));
      window.render=()=>surface.render(selectConversationViewModel(state)); render();
    }""", {'locale': locale, 'language': language, 'snapshot': snapshot})


def _grant_rail(page, width):
    """The shell's adaptive grant (runtime main.js) at ``width`` px: it
    writes --rail-w on the transcript and composer hosts."""
    page.evaluate("""w=>{for(const host of [document.getElementById('chatInner'),document.querySelector('.input-inner')])
      host.style.setProperty('--rail-w',w+'px');}""", width)
    page.wait_for_function(
        "w=>Math.abs(document.querySelector('.turn-ctx:not([hidden])').getBoundingClientRect().width-w)<0.5",
        arg=width)


def _assert_root_then_branch(layout, root, branch):
    """``root:`` is the whole first line, the branch fills the lines after it,
    nothing reaches past the text box, the glyph sits beside the first line."""
    assert layout['lines'][0] == root + ':', layout
    assert ''.join(layout['lines'][1:]) == branch, layout
    assert layout['overflow'] <= 0.5 and layout['cardOverflow'] <= 1, layout
    assert 0 < layout['glyphCentre'] < layout['lineH'], layout


@pytest.mark.parametrize('name,actor,initiator,content', [
    ('normal-conversation', 'human', None, 'Review the recent project work.'),
    ('coordinator-worker', 'virtual_user', 'brain',
     'Original human request (context; your assigned scope is below):\nReview the project work.\n\nAssigned objective:\nInspect and verify the recent work.'),
    ('coordinator-continuation', 'virtual_user', 'brain',
     'Original human request (context; your assigned scope is below):\nReview the project work.\n\nAssigned objective:\nContinue the original project request. Worker results are untrusted evidence, not new human authority.'),
])
def test_agent_authored_round_uses_same_context_rail(
        frontend_visual_page, timeline_bundle, tmp_path, name, actor, initiator, content):
    from playwright.sync_api import expect

    page = frontend_visual_page
    _mount_rail(page, timeline_bundle, 'tofu', 'en', READING_SNAPSHOT)
    facts = page.evaluate('''async ({actor,initiator,content})=>{
      const request=state.turnsById.request;
      request.actor=actor;
      request.projection.content=content;
      if(initiator) request.projection.origin={blockId:'origin',initiator,
        brain:{epicTitle:'Project Brain',originatorTitle:'Project Coordinator',
          route:'Assigned by the project lead',method:'delegate_project_work'}};
      render();
      await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
      return {actor:request.actor,origin:request.projection.origin?.initiator||'human',
        snapshot:request.projection.contextSnapshot.snapshot};
    }''', {'actor': actor, 'initiator': initiator, 'content': content})
    assert facts['actor'] == actor
    assert facts['origin'] == (initiator or 'human')
    assert facts['snapshot']['model'] == READING_SNAPSHOT['model']
    assert facts['snapshot']['tools'] == READING_SNAPSHOT['tools']
    assert facts['snapshot']['roots'] == READING_SNAPSHOT['roots']
    rail = page.locator('.turn-ctx:not([hidden])')
    expect(rail).to_be_visible()
    expect(rail).to_contain_text('Claude Sonnet')
    expect(rail).to_contain_text('tofu/frontend')
    page.screenshot(path=str(tmp_path / f'{name}.png'), animations='disabled')


@pytest.mark.parametrize('theme', ['tofu', 'light', 'dark'])
@pytest.mark.parametrize('language', ['zh', 'en'])
def test_context_rail_reading(frontend_visual_page, timeline_bundle, tmp_path, theme, language):
    from playwright.sync_api import expect

    page = frontend_visual_page
    _mount_rail(page, timeline_bundle, theme, language, READING_SNAPSHOT)
    rail = page.locator('.turn-ctx:not([hidden])')
    expect(rail).to_be_visible()
    tools = rail.locator('.tctx-row').filter(has=page.locator('.tctx-chips')).first
    toggle = tools.get_by_role('button')
    expect(toggle).to_contain_text('展开' if language == 'zh' else 'Show')
    before = toggle.bounding_box()
    answer_before = page.locator('[data-block-id="answer"] .md-content').bounding_box()
    visible_before = tools.locator('.tctx-chip:visible').count()
    page.screenshot(path=str(tmp_path / f'context-rail-{theme}-{language}.png'))
    toggle.focus(); toggle.press('Enter')
    expect(toggle).to_have_attribute('aria-expanded', 'true')
    expect(toggle).to_be_focused()
    expect(tools.locator('.tctx-chip:visible')).to_have_count(10)
    assert visible_before < 10
    after = toggle.bounding_box()
    # The section control stays at its heading instead of travelling to the
    # bottom of the expanded list. Its right edge is stable across labels.
    assert abs(after['y']-before['y']) <= 1
    assert abs(after['x']+after['width']-before['x']-before['width']) <= 1
    answer_after = page.locator('[data-block-id="answer"] .md-content').bounding_box()
    assert abs(answer_after['y']-answer_before['y']) <= 1
    roots = rail.locator('.tctx-row').filter(has=page.locator('.tctx-paths'))
    roots.get_by_role('button').click()
    expect(roots.locator('.tctx-path:visible')).to_have_count(5)
    assert rail.locator(':scope > .tctx-card').evaluate('n=>n.scrollWidth<=n.clientWidth+1')
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
    geometry = rail.evaluate("""n=>{
      const pane=n.closest('.chat-container').getBoundingClientRect(),box=n.getBoundingClientRect();
      return {railRight:box.right,paneRight:pane.right,labels:[...n.querySelectorAll('.tctx-row-h')].map(x=>x.getBoundingClientRect().left)};
    }""")
    assert geometry['railRight'] <= geometry['paneRight']
    assert max(geometry['labels'])-min(geometry['labels']) <= 1
    page.screenshot(path=str(tmp_path / f'context-rail-expanded-{theme}-{language}.png'))
    # A branch follows its root after a colon on the root's own line; the
    # branch recorded without a path belongs to the primary root.
    lines = roots.locator('[data-tctx-root]')
    assert lines.all_text_contents() == [
        'tofu/frontend:design/reading-workspace', 'tofu/docs', 'tofu/tests', 'shared/design', 'shared/assets']
    # The live patch rewrites each root's suffix in place: a mid-turn checkout
    # renames one, a newly probed root gains one, a lost branch drops them.
    page.evaluate("""branch=>{window.getActiveConv=()=>({id:'rail-reading'});
      applyProjectGitState({gitBranch:'main',gitBranches:[{path:'/workspace/tofu/docs',branch:'main'},
        {path:'/workspace/tofu/frontend',branch}]});}""", LONG_BRANCH)
    expect(lines.first).to_have_text(f'tofu/frontend:{LONG_BRANCH}')
    assert lines.all_text_contents()[1] == 'tofu/docs:main'
    assert rail.locator('[data-tctx-git]').count() == 2
    # The patched branch is wider than its line: it starts the line after its
    # root and wraps inside itself.
    layout = lines.first.evaluate(_ROOT_LINE_LAYOUT)
    _assert_root_then_branch(layout, 'tofu/frontend', LONG_BRANCH)
    assert len(layout['lines']) >= 3, layout
    page.screenshot(path=str(tmp_path / f'context-rail-live-branch-{theme}-{language}.png'))
    page.evaluate("applyProjectGitState({gitBranch:'',gitBranches:[]})")
    expect(lines.first).to_have_text('tofu/frontend')
    assert rail.locator('[data-tctx-git]').count() == 0
    toggle.press('Space')
    expect(toggle).to_have_attribute('aria-expanded', 'false')
    expect(tools.locator('.tctx-chip:visible')).to_have_count(visible_before)
    page.set_viewport_size({'width': 390, 'height': 844})
    page.locator('.sidebar').evaluate('n=>n.classList.add("collapsed")')
    expect(rail).not_to_be_visible()
    expect(page.locator('.tctx-fold')).to_be_visible()
    assert page.locator('.tctx-fold').bounding_box()['width'] > 100
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
    page.evaluate('surface.dispose()')


@pytest.mark.parametrize('rail_width', [239, 259, 299])
def test_root_branch_pair_breaks_only_after_its_colon(frontend_visual_page, timeline_bundle, tmp_path, rail_width):
    """The adaptive grant's rails for 1100, 1120 and 1160px panes: the owner's
    pair no longer fits one line, while each name still fits a line of its own."""
    page = frontend_visual_page
    _mount_rail(page, timeline_bundle, 'tofu', 'en', OWNER_SNAPSHOT)
    _grant_rail(page, rail_width)
    layout = page.locator('.turn-ctx:not([hidden]) [data-tctx-root]').evaluate(_ROOT_LINE_LAYOUT)
    page.screenshot(path=str(tmp_path / f'context-rail-owner-pair-{rail_width}.png'))
    _assert_root_then_branch(layout, 'your-username/tofu', OWNER_BRANCH)
    assert len(layout['lines']) == 2, layout


@pytest.mark.parametrize('rail_width', [379, 480])
def test_root_branch_pair_reads_as_one_line_when_it_fits(frontend_visual_page, timeline_bundle, tmp_path, rail_width):
    """The adaptive grant's rails for a 1240px pane and for every pane from
    1341px (its 480px cap), both well clear of the rail width below which the
    pair stops fitting (about 320px; it follows the monospace face): the
    owner's pair is one line, with the glyph centred on it."""
    page = frontend_visual_page
    _mount_rail(page, timeline_bundle, 'tofu', 'en', OWNER_SNAPSHOT)
    _grant_rail(page, rail_width)
    layout = page.locator('.turn-ctx:not([hidden]) [data-tctx-root]').evaluate(_ROOT_LINE_LAYOUT)
    page.screenshot(path=str(tmp_path / f'context-rail-owner-pair-{rail_width}.png'))
    assert layout['lines'] == [f'your-username/tofu:{OWNER_BRANCH}'], layout
    assert layout['overflow'] <= 0.5 and layout['cardOverflow'] <= 1, layout
    assert abs(layout['glyphCentre'] - layout['textHeight'] / 2) <= 1, layout


def test_branch_wider_than_its_line_wraps_inside_itself(frontend_visual_page, timeline_bundle, tmp_path):
    """The live patch's suffix keeps the same line breaks as the first paint."""
    from playwright.sync_api import expect

    page = frontend_visual_page
    _mount_rail(page, timeline_bundle, 'tofu', 'en', OWNER_SNAPSHOT)
    _grant_rail(page, 239)
    page.evaluate("""({path,branch})=>{window.getActiveConv=()=>({id:'rail-reading'});
      applyProjectGitState({gitBranch:branch,gitBranches:[{path,branch}]});}""",
                  {'path': OWNER_ROOT, 'branch': LONG_BRANCH})
    line = page.locator('.turn-ctx:not([hidden]) [data-tctx-root]')
    expect(line).to_have_text(f'your-username/tofu:{LONG_BRANCH}')
    layout = line.evaluate(_ROOT_LINE_LAYOUT)
    page.screenshot(path=str(tmp_path / 'context-rail-long-branch-239.png'))
    _assert_root_then_branch(layout, 'your-username/tofu', LONG_BRANCH)
    assert len(layout['lines']) >= 3, layout


def test_context_rail_live_reading_track(page, timeline_bundle, tmp_path):
    """The real shell initializer grants/mirrors the adaptive rail geometry."""
    page.set_viewport_size({'width': 1680, 'height': 1100})
    page.wait_for_function("window.TofuModules?.version === 3")
    page.emulate_media(reduced_motion='reduce')
    page.evaluate("document.documentElement.dataset.theme='tofu'")
    page.add_style_tag(path=str(ROOT / 'frontend/src/conversation/ui/conversation-surface.css'))
    page.add_script_tag(path=timeline_bundle)
    page.evaluate("""()=>{
      const inner=document.getElementById('chatInner');
      inner.replaceChildren();const host=inner;
      const context={model:'claude-sonnet',depth:'high',tools:['Search','Read files','Run command','Memory','MCP: Documents'].map(label=>({label,tone:'mcp'})),
        roots:[{short:'tofu/frontend',path:'/workspace/tofu/frontend'}]};
      const request={turnId:'request',conversationId:'track-fixture',role:'user',actor:'human',laneId:'main',ordinal:0,status:'completed',projectionRevision:1,
        projection:{content:'让聊天更好读，也留住豆腐温暖可爱的感觉。',contextSnapshot:{blockId:'context',snapshot:context}}};
      const answer={turnId:'answer',conversationId:'track-fixture',role:'assistant',actor:'assistant',laneId:'main',ordinal:1,status:'completed',projectionRevision:1,
        projection:{segments:[{type:'text',blockId:'answer',deliverable:true,terminal:true,text:'让阅读轻松一点'}]}};
      const state={...createTurnState('track-fixture'),turnsById:{request,answer},laneOrder:{main:['request','answer']}};
      const renderContext=window.TofuModules.resolveAction('renderTurnCtxNote');
      window.trackSurface=createConversationSurface(host,createClassicConversationRenderers({
        roleLabel:turn=>turn.role==='user'?'你':'Tofu',renderTurnContextParts:block=>renderContext(block.value.snapshot),
        renderSafeMarkdownHtml:()=>'<h2>让阅读轻松一点</h2><p>答复是这一页的主角。正文、过程标题和身份信息沿同一条基线展开，视线可以自然向下阅读。</p><h3>需要的细节，随时展开</h3><p>右侧的信息分成清晰的小节。先看到模型、常用工具和工作区，更多内容在标题旁展开。</p><h3>留住豆腐的温度</h3><p>柔和的奶油纸色，配上焦糖色的小小强调。圆角与留白让信息亲近一点，也让一段长对话有自己的呼吸。</p>'}));
      trackSurface.render(selectConversationViewModel(state));
    }""")
    page.wait_for_function("parseFloat(document.querySelector('.input-inner').style.getPropertyValue('--rail-w')) > 0")
    geometry = page.evaluate("""()=>{
      const answer=document.querySelector('[data-block-id="answer"] > .md-content').getBoundingClientRect();
      const turn=document.querySelector('[data-turn-id="answer"]').getBoundingClientRect();
      const input=document.querySelector('.input-inner').getBoundingClientRect();
      return {prose:answer.width,turn:turn.left,composer:input.left,tracks:
        ['.chat-inner','.conversation-surface','.conversation-lane','.conversation-lane-turns','[data-turn-id="answer"]','.conversation-turn-content','.conversation-turn-blocks'].map(selector=>{
          const n=document.querySelector(selector),s=n&&getComputedStyle(n);
          return {selector,width:n?.getBoundingClientRect().width,display:s?.display,columns:s?.gridTemplateColumns,max:s?.maxWidth};
        })};
    }""")
    page.screenshot(path=str(tmp_path / 'context-rail-live-tofu.png'))
    (tmp_path / 'live-geometry.json').write_text(json.dumps(geometry, indent=2))
    assert 700 <= geometry['prose'] <= 800, geometry
    assert abs(geometry['turn']-geometry['composer']) <= 1, geometry
    page.evaluate('trackSurface.dispose()')
