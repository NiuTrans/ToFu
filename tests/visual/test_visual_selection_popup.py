"""Selected-text browser interactions on the local shipped-style fixture.

Real selection, pointer/keyboard, popup and retained quote composition; only
branch transport and conversation identity are controlled ports.
"""
import json
import pytest
from tests._runtime_sections import ROOT, native_module_path, runtime_section
from tests.support.frontend_visual import frontend_visual_origin as frontend_visual_origin, frontend_visual_page as frontend_visual_page  # noqa: F401

pytestmark = pytest.mark.visual


def mount(page, language='zh', theme='tofu', width=1100, ready=True):
    page.set_viewport_size({'width': width, 'height': 800})
    locales = {lang: json.loads((ROOT / f'frontend/src/i18n/locales/{lang}.json').read_text()) for lang in ('zh', 'en')}
    page.evaluate('''({locales,language,theme,ready})=>{
      document.documentElement.dataset.theme=theme;
      document.body.innerHTML=`<main data-conversation-id="conversation-a" style="padding:100px 16px;max-width:700px;margin:auto">
        <article class="message" data-turn-id="turn-a" data-turn-status="completed">
          <div class="message-content"><p id="passage">你好，选中这段文字继续讨论。A selected passage to explore.</p>
          <p id="short">词</p><p id="long"></p><input id="edit" value="Do not quote an editor"></div></article>
        <article class="message" data-turn-id="turn-b"><div class="message-content"><p id="second">Another turn.</p></div></article>
        <div class="input-box"><textarea id="userInput">Keep my draft</textarea><div class="input-actions"></div></div>
      </main>`;
      document.getElementById('long').textContent='Long passage with line breaks. '.repeat(30);
      document.getElementById('long').hidden=true;
      window.locales=locales; window.language=language; window.localeReady=ready;
      window.t=(key,args)=>(localeReady ? locales[window.language][key]||key : key).replace(/\\{(\\w+)\\}/g,(_,name)=>args?.[name]??'');
      window.activeId='conversation-a';window.getActiveConv=()=>({id:activeId});
      window.calls=[];window.promptNewBranch=(turnId,title,text)=>{calls.push({turnId,title,text});return new Promise(resolve=>window.resolveBranch=resolve);};
    }''', {'locales': locales, 'language': language, 'theme': theme, 'ready': ready})
    page.add_script_tag(path=native_module_path('.native/selection-popup.js', ROOT / 'frontend/src/selection-popup.ts'))
    page.add_script_tag(content=runtime_section('ui/popups.js'))
    page.evaluate('_initSelectionPopup()')
    return locales[language]


def select(page, selector='#passage'):
    page.evaluate('''selector=>{
      document.activeElement?.blur();
      const node=document.querySelector(selector);
      node.dispatchEvent(new PointerEvent('pointerdown',{bubbles:true}));
      const range=document.createRange();range.selectNodeContents(node);
      const sel=getSelection();sel.removeAllRanges();sel.addRange(range);
      node.dispatchEvent(new PointerEvent('pointerup',{bubbles:true}));
    }''', selector)
    page.locator('.selection-popup').wait_for(state='visible')


@pytest.mark.parametrize('theme', ['tofu', 'dark', 'light'])
@pytest.mark.parametrize('width,language', [(1100, 'zh'), (390, 'en')])
def test_quote_preview_draft_and_viewport(frontend_visual_page, theme, width, language):
    page = frontend_visual_page
    locale = mount(page, language, theme, width)
    select(page)
    popup = page.locator('.selection-popup')
    assert popup.get_by_role('button').all_text_contents() == [locale['selection.quote'], locale['selection.branch']]
    bounds = popup.bounding_box()
    assert bounds['x'] >= 0 and bounds['x'] + bounds['width'] <= width
    assert bounds['y'] >= 0 and bounds['y'] + bounds['height'] <= 800
    out = ROOT / 'output/playwright/selection-popup'
    out.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(out / f'{theme}-{width}-{language}.png'))
    original = page.locator('#passage').inner_text()
    # Mouse down actually clears a native selection unless the control preserves it.
    popup.get_by_role('button', name=locale['selection.quote'], exact=True).click()
    assert page.locator('#userInput').evaluate('el=>el===document.activeElement')
    assert page.locator('#userInput').input_value() == 'Keep my draft'
    assert page.evaluate('getPendingReplyQuotes()') == [original]
    assert not popup.is_visible()
    preview = page.locator('.reply-quote-preview')
    preview.locator('summary').click()
    assert preview.locator('blockquote').inner_text() == original
    select(page)
    popup.locator('[data-action=reply]').click()
    assert preview.count() == 1
    # Acknowledging one captured send must preserve a newly added quote; rollback
    # requeues only the missing original, using the real retained composer owner.
    page.evaluate("_addReplyQuote('newer quote');consumePendingReplyQuotes([getPendingReplyQuotes()[0]])")
    assert page.evaluate('getPendingReplyQuotes()') == ['newer quote']
    page.evaluate('text=>restorePendingReplyQuotes([text])', original)
    assert page.evaluate('getPendingReplyQuotes()') == [original, 'newer quote']
    page.evaluate('text=>restorePendingReplyQuotes([text])', original)
    assert preview.count() == 2
    preview.first.get_by_role('button', name=locale['selection.removeQuote']).click()
    assert page.evaluate('getPendingReplyQuotes()') == ['newer quote']


def test_locale_readiness_keyboard_and_branch_retry(frontend_visual_page):
    page = frontend_visual_page
    mount(page, ready=False)
    page.evaluate('''()=>{const r=document.createRange();r.selectNodeContents(document.querySelector('#short'));getSelection().addRange(r);}''')
    # Wait two render frames so the initial selection processing runs.
    page.evaluate('()=>new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))')
    assert not page.locator('.selection-popup').is_visible()
    page.evaluate("localeReady=true;dispatchEvent(new Event('tofu:language-change'))")
    page.locator('.selection-popup').wait_for(state='visible')
    assert 'selection.' not in page.locator('.selection-popup').inner_text()
    page.evaluate("language='en';dispatchEvent(new Event('tofu:language-change'))")
    assert page.locator('[data-action=reply]').inner_text() == 'Quote & ask'
    page.keyboard.press('Tab')
    assert page.locator('[data-action=reply]').evaluate('el=>el===document.activeElement')
    page.keyboard.press('ArrowRight')
    page.keyboard.press('Enter')
    assert page.evaluate('calls.length') == 1
    assert page.evaluate('calls[0].text') == '词'
    assert page.locator('[data-action=branch]').is_disabled()
    page.evaluate('resolveBranch(false)')
    page.get_by_text('Could not create branch. Try again.').wait_for()
    page.locator('[data-action=branch]').click()
    assert page.evaluate('calls.length') == 2
    page.evaluate('resolveBranch(true)')
    page.locator('.selection-popup').wait_for(state='hidden')


def test_selection_scope_dismissal_and_stale_context(frontend_visual_page):
    page = frontend_visual_page
    mount(page)
    select(page)
    page.keyboard.press('Escape')
    assert not page.locator('.selection-popup').is_visible()
    select(page)
    page.evaluate("activeId='conversation-b'")
    page.locator('[data-action=reply]').click()
    assert page.evaluate('getPendingReplyQuotes()') is None
    page.evaluate("activeId='conversation-a'")
    select(page)
    page.evaluate("document.querySelector('#passage').textContent='Changed during streaming'")
    page.locator('[data-action=branch]').dispatch_event('click')
    assert page.evaluate('calls.length') == 0
    page.evaluate('''()=>{
      const range=document.createRange();range.setStart(document.querySelector('#short').firstChild,0);
      range.setEnd(document.querySelector('#second').firstChild,5);
      getSelection().removeAllRanges();getSelection().addRange(range);
    }''')
    page.locator('.selection-popup').wait_for(state='hidden')
    page.locator('#edit').select_text()
    assert not page.locator('.selection-popup').is_visible()
    select(page)
    page.evaluate("document.dispatchEvent(new Event('scroll'))")
    assert not page.locator('.selection-popup').is_visible()
    page.evaluate("document.querySelector('.message').dataset.turnStatus='running'")
    select(page)
    assert page.locator('[data-action=branch]').is_disabled()
    assert page.locator('[data-action=reply]').is_enabled()
    page.evaluate('_selectionPopup.destroy()')
    assert page.locator('.selection-popup').count() == 0


@pytest.mark.parametrize('edge', ['top', 'bottom'])
def test_touch_and_viewport_edges(frontend_visual_page, edge):
    page=frontend_visual_page
    mount(page, width=320)
    page.evaluate('''edge=>{
      const p=document.querySelector('#passage');
      p.style.cssText=`position:fixed;left:0;${edge}:0;margin:0;width:100%`;
      const range=document.createRange();range.selectNodeContents(p);
      getSelection().removeAllRanges();getSelection().addRange(range);
      // Touch selection handles emit selectionchange without mouseup.
      document.dispatchEvent(new Event('selectionchange'));
    }''',edge)
    popup=page.locator('.selection-popup')
    popup.wait_for(state='visible')
    box=popup.bounding_box()
    assert 0 <= box['x'] and box['x']+box['width'] <= 320
    assert 0 <= box['y'] and box['y']+box['height'] <= 800
    popup.locator('[data-action=reply]').dispatch_event('click')
    assert page.evaluate('getPendingReplyQuotes().length') == 1


def test_long_quote_keeps_exact_text_and_safe_preview(frontend_visual_page):
    page=frontend_visual_page
    mount(page)
    exact='第一行 <script>unsafe()</script>\n' + '完整保留引用内容。' * 100
    page.evaluate('text=>{const p=document.querySelector("#passage");p.textContent=text;p.style.whiteSpace="pre-wrap";}', exact)
    select(page)
    page.locator('[data-action=reply]').click()
    assert page.evaluate('getPendingReplyQuotes()') == [exact]
    preview=page.locator('.reply-quote-preview')
    assert preview.locator('script').count() == 0
    preview.locator('summary').click()
    assert preview.locator('blockquote').text_content() == exact
    assert preview.locator('blockquote').evaluate('el=>el.clientHeight<=innerHeight*0.3+1')
    page.screenshot(path=str(ROOT/'output/playwright/selection-popup/expanded-quote.png'))


def test_touch_tap_preserves_selected_passage(browser, frontend_visual_origin):
    page=browser.new_page(has_touch=True, viewport={'width':390,'height':800})
    try:
        page.goto(frontend_visual_origin)
        mount(page,width=390)
        select(page)
        expected=page.locator('#passage').inner_text()
        page.locator('[data-action=reply]').tap()
        assert page.evaluate('getPendingReplyQuotes()') == [expected]
        assert page.locator('#userInput').evaluate('el=>el===document.activeElement')
    finally:
        page.close()
