"""Unified conversation controls in the production shell and composed stylesheet.

Synthetic state, no server/user data. BAR_SCREENSHOTS optionally saves a bounded
set of review images. Uses real typed presentation and native browser defaults.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.visual


@pytest.fixture(scope="module")
def bar_html():
    sources = json.loads(subprocess.check_output([
        "node", "-e", r"""
const fs=require('fs'),ts=require('typescript'),result={};
for(const name of ['disclosure','project-context','context-meter','turn-navigation'])result[name]=ts.transpileModule(
 fs.readFileSync('frontend/src/features/conversation-bar/'+name+'.ts','utf8'),
 {compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
process.stdout.write(JSON.stringify(result));
"""], cwd=ROOT, text=True))
    locales = {lang: json.loads((ROOT / f"frontend/src/i18n/locales/{lang}.json").read_text()) for lang in ("en", "zh")}
    js = "const sources=" + json.dumps(sources) + ";const locales=" + json.dumps(locales) + ";" + r"""
const modules={};function load(name){if(modules[name])return modules[name];const exports={};modules[name]=exports;
new Function('require','exports',sources[name])(id=>load(id.replace('./','')),exports);return exports;}
window.lang='en';const t=(key,args={})=>(locales[lang][key]||key).replace(/\{(\w+)\}/g,(_,k)=>args[k]??'{'+k+'}');
document.documentElement.dataset.theme='tofu';
if(innerWidth<=800)document.querySelector('#sidebar').classList.add('collapsed');
document.querySelector('#topbarTitle').textContent='A conversation, with room to think';
document.querySelector('#presetToggle .ps-label').textContent='GPT-6 Astra';
document.querySelector('#userInput').placeholder='What shall we work on?';
window.actions=[];window.roots=[{path:'/workspace/tofu',readOnly:false}];
const bar=document.querySelector('#projectBar');
window.project=load('project-context').createProjectContext({bar,t,manage:()=>actions.push('manage'),clear:()=>{
 actions.push('clear');roots=[];project.update({roots});},toggleReadOnly:index=>{
 actions.push('readonly:'+index);roots=roots.map((root,i)=>i===index?{...root,readOnly:!root.readOnly}:root);project.update({roots});}});
project.update({roots});
window.summary={convId:'demo',used:299880,limit:833000,known:true,exact:false,pctRaw:.36,pct:36,zone:'ok',compactions:3,busy:false,hasHistory:true};
window.meter=load('context-meter').createContextMeter({host:document.querySelector('#convStatusStrip'),translate:(key,fallback)=>t(key),read:()=>summary,onCompact:()=>actions.push('compact'),onHistory:()=>actions.push('history')});
window.state={laneOrder:{main:[]},turnsById:{},historyByLane:{}};
const inner=document.querySelector('#chatInner');inner.innerHTML='';
for(let i=1;i<=3;i++){
 const id='turn-'+i;state.laneOrder.main.push(id);state.turnsById[id]={actor:'human',projection:{content:['Could we make these controls feel like one system?','Keep context close, without making it the main event.','Yes. Let the conversation be the main event.'][i-1]}};
 const node=document.createElement('article');node.className='message';node.dataset.turnId=id;
 node.innerHTML='<div class="message-avatar">'+i+'</div><div class="message-content"><div class="md-content"><h3></h3><p></p></div></div>';
 node.querySelector('h3').textContent=['Start with the work','Keep the details within reach','Less chrome, more conversation'][i-1];
 node.querySelector('p').textContent=['The project, context meter and turn position now share a single quiet line. There is no landscape or separate project strip above the input.','Open the project for folder permissions. Open the context reading for capacity and compression history. The controls stay in place while you read.','Use the arrows to step between turns, or the count to jump. The working area stays yours.'][i-1];inner.append(node);
}
window.nav=load('turn-navigation').createTurnNavigation({host:document.querySelector('#turnNav'),translate:t,scrollToTurn:id=>{actions.push(id);document.querySelector('[data-turn-id="'+id+'"]').scrollIntoView({block:'start'});},loadEarlier:async()=>{actions.push('load-history')}});
nav.update('demo',state);
window.refresh=()=>{project.update({roots});meter.update(summary);nav.update('demo',state)};
document.querySelector('#autoApplyToggle').onclick=()=>actions.push('auto');
"""
    shell = (ROOT / "index.html").read_text()
    shell = re.sub(r"<script\b[^>]*>[\s\S]*?</script>", "", shell, flags=re.I)
    shell = re.sub(r"<head\b[^>]*>[\s\S]*?</head>", '<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>', shell, flags=re.I)
    css = (ROOT / "static/styles.css").read_text()
    shell = shell.replace('</head>', '<style>' + css + '</style></head>')
    return shell.replace('</body>', '<script>' + js.replace('</script', '<\\/script') + '</script></body>')


def snapshot(page, name):
    directory = os.environ.get('BAR_SCREENSHOTS')
    if directory:
        target = Path(directory)
        target.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(target / f'{name}.png'))


def geometry(page):
    return page.evaluate("""() => {
      const stripEl=document.querySelector('#convStatusStrip');
      const box=document.querySelector('.input-box');
      const strip=stripEl.getBoundingClientRect();
      const input=box.getBoundingClientRect();
      const parts=['#projectBar','#contextHealthBar','#turnNav'].map(s=>document.querySelector(s).getBoundingClientRect());
      return {height:strip.height,inputY:input.y,
        edges:stripEl.parentElement===box&&box.firstElementChild===stripEl&&
          strip.left>=input.left-1&&strip.right<=input.right+1&&
          strip.top>=input.top-1&&strip.bottom<=input.bottom+1,
        overflow:document.documentElement.scrollWidth>innerWidth,
        contained:parts.every(r=>r.left>=strip.left-1&&r.right<=strip.right+1&&r.top>=strip.top-1&&r.bottom<=strip.bottom+1),
        oneRow:Math.max(...parts.map(r=>r.top))-Math.min(...parts.map(r=>r.top))<6};
    }""")


def assert_panel(page, selector, anchor_selector=None):
    box = page.locator(selector).bounding_box()
    assert box is not None
    view = page.viewport_size
    assert box['x'] >= 7 and box['y'] >= 7
    assert box['x'] + box['width'] <= view['width'] - 7
    assert box['y'] + box['height'] <= view['height'] - 7

    if anchor_selector:
        anchor = page.locator(anchor_selector).bounding_box()
        expected_left = max(8, min(anchor['x'] + anchor['width'] - box['width'], view['width'] - box['width'] - 8))
        assert abs(box['x'] - expected_left) <= 1, (box, anchor)

@pytest.mark.parametrize('width,height', [(1440, 900), (800, 800), (390, 844), (320, 740), (480, 360)])
def test_one_line_native_controls_and_disclosure_geometry(browser, bar_html, width, height):
    context = browser.new_context(viewport={'width': width, 'height': height}, has_touch=width < 500, is_mobile=width < 450)
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.set_content(bar_html)
    baseline = geometry(page)
    assert baseline['edges'] and baseline['contained'] and not baseline['overflow']
    assert baseline['oneRow'] and baseline['height'] <= 46
    assert page.locator('#sceneSwitchBtn,.pt-tab,.pt-theatre,.ctx-bar-bubble,.turn-dot').count() == 0
    assert page.locator('#contextHealthBar').is_visible()
    snapshot(page, f'workspace-{width}')
    for control, panel in [('#projectBarFolders', '#projectContextPanel'), ('#contextHealthBar', '#ctxBarPopover'), ('.turn-nav-position', '.turn-jump-panel')]:
        page.locator(control).focus()
        page.keyboard.press('Enter')
        assert_panel(page, panel, control)
        assert page.locator(panel).evaluate('e=>e.contains(document.activeElement)')
        assert geometry(page) == baseline
        page.keyboard.press('Escape')
        assert page.locator(control).evaluate('e=>e===document.activeElement')
        assert page.locator(panel).count() == 0
    page.locator('[data-direction="previous"]').click()
    assert page.evaluate('actions.at(-1)') == 'turn-2'
    page.locator('.turn-nav-position').click()
    page.locator('.turn-jump-entry[data-turn-id="turn-1"]').click()
    assert page.locator('.turn-nav-readout').inner_text() == '1 / 3'
    page.locator('#contextHealthBar').click()
    page.locator('#userInput').click()
    assert page.locator('#ctxBarPopover').count() == 0
    assert page.locator('#userInput').evaluate('e=>e===document.activeElement')
    assert not errors
    context.close()


@pytest.mark.parametrize('width', [1440, 390, 320])
def test_bar_stable_with_large_history_long_project_and_background_work(browser, bar_html, width):
    context = browser.new_context(viewport={'width': width, 'height': 844}, has_touch=width < 500, is_mobile=width < 500)
    page = context.new_page()
    page.set_content(bar_html)
    # The readout slot scales with the total's digit count: no reflow per turn
    # inside one order of magnitude, one reflow at the power-of-ten boundary.
    before = page.locator('#turnNav').bounding_box()
    page.evaluate("for(let i=4;i<=9;i++){const id='turn-'+i;state.laneOrder.main.push(id);state.turnsById[id]={actor:'human',projection:{content:'Message '+i}}}refresh()")
    same_magnitude = page.locator('#turnNav').bounding_box()
    assert abs(same_magnitude['width'] - before['width']) < 1
    page.evaluate("for(let i=10;i<=1203;i++){const id='turn-'+i;state.laneOrder.main.push(id);state.turnsById[id]={actor:'human',projection:{content:'Message '+i}}}refresh()")
    assert page.locator('.turn-nav-readout').inner_text() == '3 / 1203'  # selection survives growth
    page.locator('.turn-nav-position').click()
    for _ in range(30):
        if page.locator('.turn-jump-entry[data-turn-id="turn-1203"]').count():
            break
        page.locator('.turn-jump-page[data-direction="newer"]').click()
    page.locator('.turn-jump-entry[data-turn-id="turn-1203"]').click()
    assert page.locator('.turn-nav-readout').inner_text() == '1203 / 1203'
    slot = page.locator('.turn-nav-readout').evaluate(
        "e=>{const s=e.getBoundingClientRect().width;e.style.width='auto';const c=e.getBoundingClientRect().width;e.style.width='';refresh();return {slot:s,content:c}}")
    assert slot['slot'] - slot['content'] < 12, slot  # compact slot, not a fixed 11ch blank
    page.evaluate("roots=[{path:'/workspace/'+'long-'.repeat(25),readOnly:true}];summary.used=833000;summary.pct=100;summary.pctRaw=1;refresh();const b=document.createElement('button');b.id='bgActivity';b.className='bg-activity-chip';const text=document.createElement('span');text.className='bg-act-label';text.textContent='3 background agents running';b.append(text);b.title=text.textContent;document.querySelector('#turnNav').before(b)")
    metrics = geometry(page)
    assert metrics['oneRow'] and metrics['height'] <= 46 and metrics['contained'] and not metrics['overflow'], metrics
    assert page.locator('#bgActivity').is_visible()
    context.close()



def test_narrow_desktop_pane_uses_compact_controls(browser, bar_html):
    context = browser.new_context(viewport={'width': 1440, 'height': 900})
    page = context.new_page()
    page.set_content(bar_html)
    page.evaluate("document.querySelector('.input-inner').style.width='308px'")
    baseline = geometry(page)
    assert baseline['oneRow'] and baseline['contained'], baseline
    assert page.locator('.ctx-meter-label').is_hidden()
    assert page.locator('.turn-nav-label').is_hidden()
    page.evaluate("const b=document.createElement('button');b.id='bgActivity';b.className='bg-activity-chip';b.textContent='3 agents';document.querySelector('#turnNav').before(b)")
    assert geometry(page)['contained']
    page.locator('#contextHealthBar').click()
    assert_panel(page, '#ctxBarPopover', '#contextHealthBar')
    context.close()


def test_project_bar_uses_leftover_width_for_folder_names(browser, bar_html):
    context = browser.new_context(viewport={'width': 1440, 'height': 900})
    page = context.new_page()
    page.set_content(bar_html)
    page.evaluate("roots=[{path:'/workspace/tofu',readOnly:false},{path:'/workspace/xuecheng-mcp',readOnly:false}];refresh()")
    assert page.locator('.project-location-name').inner_text() == 'tofu · xuecheng-mcp'
    assert page.locator('.project-location-count').is_hidden()
    assert page.locator('#autoApplyToggle').is_visible()
    cluster = page.evaluate("""() => {
      const strip=document.querySelector('#convStatusStrip').getBoundingClientRect();
      const bar=document.querySelector('#projectBar').getBoundingClientRect();
      const loc=document.querySelector('#projectBarFolders').getBoundingClientRect();
      const auto=document.querySelector('#autoApplyToggle').getBoundingClientRect();
      const meter=document.querySelector('#contextHealthBar').getBoundingClientRect();
      return {
        strip:strip.width, bar:bar.width, autoGap:auto.left-loc.right,
        autoOnscreen:auto.width>8 && auto.left>=strip.left-1 && auto.right<=strip.right+1,
        meterAfterAuto:meter.left>=auto.right-1,
      };
    }""")
    assert 0 <= cluster['autoGap'] <= 16, cluster
    assert cluster['autoOnscreen'] and cluster['meterAfterAuto'], cluster
    assert cluster['bar'] < cluster['strip'] * 0.55, cluster
    assert page.locator('.project-location-name').evaluate('e=>e.scrollWidth<=e.clientWidth+1')
    assert geometry(page)['oneRow'] and not geometry(page)['overflow']
    page.evaluate("""() => {
      document.querySelector('#projectBar').style.flex='0 1 auto';
      roots=[{path:'/workspace/tofu',readOnly:false}];
      refresh();
      roots=[{path:'/workspace/tofu',readOnly:false},{path:'/workspace/xuecheng-mcp',readOnly:false}];
      refresh();
    }""")
    assert page.locator('.project-location-name').inner_text() == 'tofu · xuecheng-mcp'
    page.evaluate("roots=['alpha','bravo','charlie','delta','echo','foxtrot'].map(name=>({path:'/workspace/'+name,readOnly:false}));document.querySelector('.input-inner').style.width='280px';refresh()")
    assert page.locator('.project-location-name').inner_text().startswith('alpha')
    assert page.locator('.project-location-count').inner_text().startswith('+')
    assert geometry(page)['oneRow'] and geometry(page)['contained']
    context.close()

def test_project_bar_width_sweep_never_clips_fitted_label(browser, bar_html):
    context = browser.new_context(viewport={'width': 1440, 'height': 900})
    page = context.new_page()
    page.set_content(bar_html)
    page.evaluate("""() => {
      roots=[{path:'/ws/tofu',readOnly:false},{path:'/ws/xuecheng-mcp',readOnly:false}];
      for(let i=4;i<=1203;i++){const id='turn-'+i;state.laneOrder.main.push(id);state.turnsById[id]={actor:'human',projection:{content:'m'}}};
      refresh();
      const b=document.createElement('button');b.id='bgActivity';b.className='bg-activity-chip';
      const s=document.createElement('span');s.className='bg-act-label';s.textContent='3 background agents';b.append(s);
      document.querySelector('#turnNav').before(b);
    }""")
    bad = []
    for width in range(300, 900, 4):
        page.evaluate(f"document.querySelector('.input-inner').style.width='{width}px'")
        page.wait_for_timeout(30)
        state = page.evaluate("""() => {
          const n=document.querySelector('.project-location-name');
          const c=document.querySelector('.project-location-count');
          return {clip:n.scrollWidth>n.clientWidth+1, text:n.textContent,
                  count:c.hidden?'':c.textContent};
        }""")
        # Clipping is only acceptable as a last resort: the label is already
        # the single first name with the +N badge up. Anything longer must fit.
        if state['clip'] and (state['text'] != 'tofu' or state['count'] != '+1'):
            bad.append((width, state['text'], state['count']))
    context.close()
    assert not bad, f'{len(bad)} clipped fitted labels, e.g. {bad[:5]}'

def test_long_roots_localization_themes_background_and_detach(browser, bar_html):
    context = browser.new_context(viewport={'width': 390, 'height': 844}, reduced_motion='reduce')
    page = context.new_page()
    page.set_content(bar_html)
    page.evaluate("roots=[{path:'/work/一个非常长的项目名称-'+'long-'.repeat(25),readOnly:true},{path:'remote:device:/repo',readOnly:false}];lang='zh';refresh()")
    assert not geometry(page)['overflow']
    page.locator('#projectBarFolders').click()
    assert 'remote:device:/repo' in page.locator('#projectContextPanel').inner_text()
    page.locator('.project-context-permission').first.click()
    assert page.evaluate('actions.at(-1)') == 'readonly:0'
    for theme in ('tofu', 'dark', 'light'):
        page.evaluate('(theme)=>document.documentElement.dataset.theme=theme', theme)
        page.locator('#contextHealthBar').click()
        assert '上下文详情' in page.locator('#ctxBarPopover').inner_text()
        assert_panel(page, '#ctxBarPopover')
        snapshot(page, f'mobile-{theme}-context')
        page.keyboard.press('Escape')
    page.evaluate("const b=document.createElement('button');b.id='bgActivity';b.className='bg-activity-chip';b.textContent='后台 3';document.querySelector('#turnNav').before(b)")
    assert geometry(page)['contained'] and not geometry(page)['overflow']
    page.locator('#projectBarFolders').click()
    page.get_by_role('button', name='移除项目', exact=True).click()
    assert page.locator('.project-location-name').inner_text() == '添加项目'
    assert page.locator('#autoApplyToggle').is_hidden()
    page.locator('#projectBarFolders').click()
    assert page.evaluate('actions.at(-1)') == 'manage'
    context.close()


def test_large_jump_list_native_focus_and_busy_context(browser, bar_html):
    context = browser.new_context(viewport={'width': 480, 'height': 360})
    page = context.new_page()
    page.set_content(bar_html)
    page.evaluate("for(let i=4;i<=1203;i++){const id='turn-'+i;state.laneOrder.main.push(id);state.turnsById[id]={actor:'human',projection:{content:'Message '+i}}}summary.busy=true;refresh()")
    page.locator('.turn-nav-position').click()
    assert page.locator('.turn-jump-entry').count() <= 50
    assert_panel(page, '.turn-jump-panel')
    assert page.evaluate("(()=>{const a=document.activeElement.getBoundingClientRect(),p=document.querySelector('.turn-jump-panel').getBoundingClientRect();return a.top>=p.top&&a.bottom<=p.bottom})()")
    page.keyboard.press('Escape')
    page.locator('#contextHealthBar').click()
    assert page.locator('.ctx-pop-compact').is_disabled()
    assert page.locator('.ctx-pop-history').evaluate('e=>e===document.activeElement')
    page.keyboard.press('Tab')
    page.keyboard.press('Tab')
    assert page.locator('#ctxBarPopover').count() == 0
    assert page.locator('.turn-nav-step[data-direction="previous"]').evaluate('e=>e===document.activeElement')
    context.close()
