"""Typed Project receipts and shipped CSS in real Chromium; no backend state writes."""
import json

import pytest

from tests._runtime_sections import ROOT, native_module_path
from tests.support.frontend_visual import frontend_visual_origin, frontend_visual_page as frontend_visual_page  # noqa: F401

pytestmark = pytest.mark.visual


@pytest.mark.parametrize('theme,width', [(theme, width) for theme in ['light', 'dark', 'tofu'] for width in [390, 1180]])
def test_project_receipts_preserve_readability_focus_and_disclosure(frontend_visual_page, theme, width, tmp_path):
    from playwright.sync_api import expect
    page = frontend_visual_page
    page.set_viewport_size({'width': width, 'height': 1400})
    styles = page.locator('link[rel="stylesheet"]').evaluate_all('(nodes)=>nodes.map(n=>n.href)')
    page.evaluate("""() => {
      const frame=document.createElement('iframe');frame.id='project-fixture';
      frame.style.cssText='position:fixed;inset:0;width:100%;height:100%;border:0';document.body.append(frame);
    }""")
    frame = page.frames[-1]
    frame.set_content(f'<html data-theme="{theme}" lang="en"><body><main id="receipts"></main></body></html>')
    for url in styles:
        frame.add_style_tag(url=url)
    frame.add_style_tag(content='body{display:block;height:auto;overflow:auto;margin:0;padding:var(--space-4);background:var(--bg-primary)}'
                         '#receipts{max-width:var(--conversation-reading-measure);margin-inline:auto;display:grid;gap:var(--space-3)}')
    frame.add_script_tag(path=native_module_path('.native/visual-project-receipt.js', ROOT / 'frontend/src/conversation/ui/project-receipt/index.ts'))
    locale = json.loads((ROOT / 'frontend/src/i18n/locales/en.json').read_text())
    frame.evaluate("""locale => {
      const ports={text:(key,fallback)=>locale[key]||fallback,markdown:()=>'',now:()=>1700001000000,
        absoluteTime:value=>new Date(value).toISOString(),conversationTitle:()=> 'Review implementation',
        icon:()=>'<svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true"></svg>'};
      const rounds=[
        {toolName:'project_board_read',results:[{source:'Board',boardSnapshot:{open:2,lanes:{
          open:[{title:'Plan implementation and browser verification',owner:'reviewer'},
            {title:'frontend/conversation/presentation/'+ 'long-path-'.repeat(12),owner:'builder'}],
          claimed:[{title:'Refactor receipt ownership'}],done:[{title:'Verify exact outputs'}]
        }}}]},
        {toolName:'project_board_claim',results:[{source:'Board',boardTransition:{verb:'claim',ok:false,title:'Review delivery',taskId:'epic-review',error:'Already claimed by a sibling conversation',status:'claimed'}}]},
        {toolName:'project_commit',results:[{source:'Board',commitResult:{ok:true,verified:false,committed:['frontend/src/conversation/ui/project-receipt/index.ts'],excluded:[{path:'shared.ts',reason:'Sibling edits retained',numstat:'2 1'}]}}]},
        {toolName:'project_intervene',results:[{source:'Peer',peerDelivery:{tool:'project_intervene',toConv:'review',outcome:'denied',text:'Please check overlap before editing shared files'}}]},
        {toolName:'project_charter_propose',results:[{source:'Charter',charterProposal:{title:'Architecture decision',proposal:'Use explicit ownership and verify browser rendering before integration.'}}]},
      ];
      document.querySelector('#receipts').innerHTML=rounds.map((round,i)=>renderProjectReceipt({...round,roundNum:i},
        '<svg viewBox="0 0 16 16" aria-hidden="true"></svg>','',ports)).join('');
    }""", locale)
    cards = frame.locator('details.ptool-convmeta-block')
    expect(cards).to_have_count(5)
    expect(cards.nth(0)).not_to_have_attribute('open', '')
    expect(cards.nth(0).locator('.ptool-convmeta-count')).to_contain_text('2')
    for i in range(1, 5):
        expect(cards.nth(i)).to_have_attribute('open', '')
    disclosure = cards.nth(0).locator('summary.ptool-convmeta-header')
    disclosure.focus()
    disclosure.press('Enter')
    expect(cards.nth(0).locator('.ptool-convmeta-body')).to_be_visible()
    expect(disclosure).to_be_focused()
    assert disclosure.evaluate('(el)=>getComputedStyle(el).outlineStyle') != 'none'
    expect(frame.locator('#receipts script')).to_have_count(0)
    expect(cards.nth(1).locator('.ptool-board-tr-error')).to_contain_text('Already claimed')
    expect(cards.nth(2)).to_contain_text('Sibling edits retained')
    frame.evaluate("""() => {
      const full=document.querySelector('.ptool-board-tr-error');const range=document.createRange();range.selectNodeContents(full);
      const selection=getSelection();selection.removeAllRanges();selection.addRange(range);
    }""")
    assert 'Already claimed' in frame.evaluate('getSelection().toString()')
    assert frame.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
    assert frame.locator('.ptool-board-mini').evaluate('(el)=>el.scrollWidth<=el.clientWidth+1')
    assert float(cards.nth(0).locator('.ptool-text').evaluate('(el)=>parseFloat(getComputedStyle(el).fontSize)')) >= 13
    assert float(cards.nth(0).locator('.ptool-board-mini-title').first.evaluate('(el)=>parseFloat(getComputedStyle(el).fontSize)')) >= 13
    contrast = frame.evaluate("""() => {
      const rgba=value=>value.match(/[0-9.]+/g).map(Number);
      const lum=rgb=>rgb.slice(0,3).map(v=>v/255).map(v=>v<=.04045?v/12.92:((v+.055)/1.055)**2.4).reduce((s,v,i)=>s+v*[.2126,.7152,.0722][i],0);
      function background(el) {
        const layers=[];
        for(let node=el;node;node=node.parentElement){const rgb=rgba(getComputedStyle(node).backgroundColor);layers.unshift(rgb);}
        let rgb=[255,255,255];
        for(const layer of layers){const alpha=layer.length>3?layer[3]:1;rgb=rgb.map((v,i)=>layer[i]*alpha+v*(1-alpha));}
        return rgb;
      }
      return [...document.querySelectorAll('.ptool-convmeta-header .ptool-text,.ptool-board-mini-title,.ptool-convmeta-count,.ptool-convmeta-why,.ptool-board-tr-error,.ptool-board-tr-failed,.ptool-commit-outcome-warn')].map(el=>{
        const fg=lum(rgba(getComputedStyle(el).color)),bg=lum(background(el));
        return {className:el.className,ratio:(Math.max(fg,bg)+.05)/(Math.min(fg,bg)+.05)};
      });
    }""")
    assert not [value for value in contrast if value['ratio'] < 4.5], (theme, [value for value in contrast if value['ratio'] < 4.5])
    page.locator('#project-fixture').screenshot(path=str(tmp_path / f'project-{theme}-{width}.png'))
    disclosure.press('Enter')
    expect(cards.nth(0).locator('.ptool-convmeta-body')).not_to_be_visible()
    expect(disclosure).to_be_focused()
