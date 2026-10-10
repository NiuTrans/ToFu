"""Real Chromium: browser JS shares command chrome, titles, and disclosure state."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests._runtime_sections import native_module_path, runtime_section_path
from tests.support.frontend_visual import ROOT, frontend_visual_origin as frontend_visual_origin, frontend_visual_page as frontend_visual_page  # noqa: F401

pytestmark = pytest.mark.visual


_SETUP = r"""
window._featureFlags = {debug_mode:false};
window.projectState = {extraRoots:[]};
window.escapeHtml = value => String(value ?? '').replace(/[&<>"']/g,
  ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
window.Icon = () => '<svg viewBox="0 0 24 24" width="18" height="18"><path d="m5 6 6 6-6 6m8 0h6" stroke="currentColor" fill="none"/></svg>';
window.getActiveConv = () => null;
window.renderMarkdown = value => escapeHtml(value);
window.t = (key, params) => {
  let text = window.messages[key] || key;
  return text.replace(/\{(\w+)\}/g, (_, k) => params?.[k] ?? '');
};
"""


def _load(page, frontend_ui_url, width, language, dark):
    page.set_viewport_size({'width': width, 'height': 880})
    page.goto(frontend_ui_url)
    page.evaluate("""(dark) => {
      document.documentElement.setAttribute('data-theme', dark ? 'dark' : 'light');
      document.documentElement.classList.toggle('dark', dark);
      document.body.innerHTML='<main id="preview"><h2>Run Command · JavaScript</h2><div id="shell"></div><div id="js"></div><div id="other"></div></main>';
    }""", dark)
    page.add_style_tag(content="""
      body{display:block;overflow:auto;height:auto}#preview{max-width:1000px;margin:24px auto;padding:0 16px;font-family:var(--font-sans)}
      #preview h2{font-size:18px;font-weight:500;margin-bottom:20px}
      #preview .ptool-cmd-block{margin-bottom:16px}
    """)
    messages = json.loads((ROOT / 'frontend/src/i18n/locales' / f'{language}.json').read_text())
    page.evaluate('(messages)=>window.messages=messages', messages)
    page.add_script_tag(content=_SETUP)
    page.add_script_tag(path=runtime_section_path('ui/tool_rounds.js'))
    page.add_script_tag(path=native_module_path(
        '.native/browser-js-visual-actions.js', ROOT / 'frontend/src/action-registry.ts'))
    page.evaluate(r"""() => {
      installActionRegistry(name=>({ _cmdBodyToggle, _cmdOutputOpenToggle })[name]);
      window.jsRound={toolName:'browser_execute_js',toolCallId:'js-occurrence',
        status:'done',query:'Execute JS in tab 998821',roundNum:3,tStart:1000,tEnd:2000,
        toolArgs:{code:'(async () => {\n  const title = document.title;\n  return { title, links: document.links.length };\n})();',
          description: t('toolBrowserExecution.executeJs')==='JavaScript' && messages['toolCmd.foldOutput']==='输出'
            ? '读取当前页面标题和链接数量' : 'Read the page title and count its links'},
        toolContent:'{\n  "title": "Friday MCP Hub",\n  "links": 27\n}',
        results:[{tabTitle:'Friday MCP Hub · 浏览器工具与执行诊断'}]};
      window.shellRound={toolName:'run_command',toolCallId:'shell-occurrence',status:'done',
        roundNum:2,tStart:1000,tEnd:2000,toolArgs:{command:'npm run test:frontend',description:'Check the frontend rendering contracts'},
        results:[{exitCode:0,output:'All frontend rendering checks passed.'}]};
      window.repaint=()=>{
        document.getElementById('shell').innerHTML=_renderUnifiedToolLine(shellRound,false);
        document.getElementById('js').innerHTML=_renderUnifiedToolLine(jsRound,jsRound.status==='searching');
        document.getElementById('other').innerHTML=_renderUnifiedToolLine({...jsRound,toolCallId:'other-occurrence'},false);
      };
      repaint();
    }""")


@pytest.mark.parametrize('width,language,dark', [
    (1280,'zh',False), (390,'zh',False), (1280,'en',False), (390,'en',False),
    (1280,'zh',True), (390,'zh',True), (1280,'en',True), (390,'en',True),
])
def test_browser_js_command_card_layout_and_independent_folds(
    frontend_visual_page, frontend_visual_origin, tmp_path, width, language, dark,
):
    page = frontend_visual_page
    errors=[]
    page.on('pageerror',lambda error:errors.append(str(error)))
    _load(page, frontend_visual_origin, width, language, dark)
    card=page.locator('#js .ptool-cmd-js')
    assert card.count()==1
    assert card.locator('.ptool-cmd-tab').get_attribute('title')=='Friday MCP Hub · 浏览器工具与执行诊断'
    assert '998821' not in card.inner_text()
    assert card.locator('.ptool-cmd-body').is_hidden()
    assert card.locator('.ptool-cmd-output-wrap').is_hidden()
    assert card.locator('.ptool-cmd-desc').count()==0
    assert card.locator('.ptool-cmd-desc-inline').is_visible()
    assert not page.evaluate('document.documentElement.scrollWidth > innerWidth')
    page.screenshot(path=str(tmp_path/'browser-js-compact.png'))

    card.locator('.ptool-cmd-fold-cmd').click()
    assert card.locator('.ptool-cmd-body').is_visible()
    assert card.locator('.ptool-cmd-fold-cmd').get_attribute('aria-expanded')=='true'
    assert card.locator('.ptool-cmd-output-wrap').is_hidden()
    assert page.locator('#other .ptool-cmd-body').is_hidden()
    assert page.locator('#shell .ptool-cmd-body').is_hidden()
    card.locator('.ptool-cmd-fold-output').click()
    assert card.locator('.ptool-cmd-output-wrap').is_visible()
    assert card.locator('.ptool-cmd-body').is_visible()
    assert not page.evaluate('document.documentElement.scrollWidth > innerWidth')
    page.screenshot(path=str(tmp_path/'browser-js-expanded.png'))
    page.evaluate('repaint()')
    assert card.locator('.ptool-cmd-body').is_visible()
    assert card.locator('.ptool-cmd-output-wrap').is_visible()

    # The same real occurrence keeps disclosure across live -> settled repaint.
    page.evaluate("jsRound.status='searching';repaint()")
    assert card.locator('.ptool-cmd-body').is_visible()
    assert card.locator('.ptool-cmd-fold-output').count()==0
    assert card.locator('.ptool-cmd-tab').get_attribute('title')=='Friday MCP Hub · 浏览器工具与执行诊断'
    page.evaluate("jsRound.status='done';repaint()")
    assert card.locator('.ptool-cmd-body').is_visible()
    assert card.locator('.ptool-cmd-output-wrap').is_visible()
    card.locator('.ptool-cmd-fold-cmd').click()
    assert card.locator('.ptool-cmd-body').is_hidden()
    assert card.locator('.ptool-cmd-output-wrap').is_visible()

    page.evaluate("""() => {
      jsRound.status='error';jsRound.results=[{tabTitle:'<img src=x onerror=boom> 正确页面',badge:'error'}];
      jsRound.toolContent='ReferenceError: missing is not defined';repaint();
    }""")
    assert card.locator('.ptool-cmd-err').count()==0  # class is on the card itself
    assert 'ptool-cmd-err' in card.get_attribute('class')
    assert card.locator('img').count()==0
    assert card.locator('.ptool-cmd-tab').get_attribute('title')=='<img src=x onerror=boom> 正确页面'
    assert 'ReferenceError' in card.locator('.ptool-cmd-output-wrap').inner_text()
    assert not errors, errors


def test_browser_js_unknown_tab_and_long_titles_do_not_leak_ids_or_break_layout(
    frontend_visual_page,frontend_visual_origin,
):
    page = frontend_visual_page
    _load(page,frontend_visual_origin,390,'zh',False)
    page.evaluate("jsRound.results=[{}];jsRound.toolArgs.tab_id=998821;repaint()")
    card=page.locator('#js .ptool-cmd-js')
    assert card.locator('.ptool-cmd-tab').get_attribute('title')=='浏览器标签页'
    assert '998821' not in card.inner_text()
    page.evaluate("jsRound.results=[{tabTitle:'很长的标签页名称'.repeat(100)}];repaint()")
    assert len(card.locator('.ptool-cmd-tab').get_attribute('title'))==240
    box=card.locator('.ptool-cmd-tab').bounding_box()
    assert box and box['width']<=390-32
    assert not page.evaluate('document.documentElement.scrollWidth > innerWidth')
