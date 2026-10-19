"""Actual typed receipt projection and shipped CSS; synthetic data, no deck/job writes."""
import json

import pytest

from tests._runtime_sections import ROOT, native_module_path
from tests.support.frontend_visual import frontend_visual_origin, frontend_visual_page as frontend_visual_page  # noqa: F401

pytestmark = pytest.mark.visual


@pytest.mark.parametrize('theme,width', [(theme, width) for theme in ['light', 'dark', 'tofu'] for width in [390, 1180]])
def test_multimedia_receipts_remain_readable_selectable_and_collapsible(frontend_visual_page, theme, width, tmp_path):
    from playwright.sync_api import expect
    page = frontend_visual_page
    page.set_viewport_size({'width': width, 'height': 1100})
    styles = page.locator('link[rel="stylesheet"]').evaluate_all('(nodes)=>nodes.map(n=>n.href)')
    page.evaluate("""() => {
      const frame=document.createElement('iframe');frame.id='receipt-fixture';
      frame.style.cssText='position:fixed;inset:0;width:100%;height:100%;border:0';document.body.append(frame);
    }""")
    frame = page.frames[-1]
    frame.set_content(f'<html data-theme="{theme}" lang="en"><body><main id="receipts"></main></body></html>')
    for url in styles:
        frame.add_style_tag(url=url)
    frame.add_style_tag(content='body{display:block;height:auto;overflow:auto;margin:0;padding:var(--space-4)}'
                         '#receipts{max-width:var(--conversation-reading-measure);margin-inline:auto;display:grid;gap:var(--space-3)}')
    frame.add_script_tag(path=native_module_path('.native/visual-motion-card.js', ROOT / 'frontend/src/conversation/ui/motion-card/index.ts'))
    locale = json.loads((ROOT / 'frontend/src/i18n/locales/en.json').read_text())
    frame.evaluate("""locale => {
      window.workUnitDemands=0;
      window.t=(key,fallback)=>locale[key]||(typeof fallback==='string'?fallback:key);
      window.escapeHtml=value=>String(value??'').replace(/[&<>"']/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
      const ports={text:(key,fallback)=>locale[key]||fallback,scanWorkUnits:()=>workUnitDemands++};
      const rounds=[
        {toolName:'motion_video_check',query:'Check scene-001',payload:{ok:false,category:'layout',elapsed:0,
          gates:{lint:true,layout:false},errors:['Title overlaps chart'],warnings:['Small caption'],fix_hints:['Move title above chart']}},
        {toolName:'produce_report',query:'Produce the evidence review',payload:{ok:true,task_id:'owner-task',topic:'A cited research report'},
          results:[{workUnitRef:{taskId:'owner-task',kind:'report'}}]},
        {toolName:'patch_slides',query:'Patch title on page 2',payload:{ok:false,errors:['Layout regression; deck rolled back','Invalid target <script>literal</script>']}},
        {toolName:'motion_video_probe',query:'Probe scene-001.mp4',payload:{probe:{codec:'h264',width:1920,height:1080,fps:0,duration:0,has_audio:false}}},
      ];
      document.querySelector('#receipts').innerHTML=rounds.map((round,i)=>renderMotionCard(
        {...round,roundNum:i,toolContent:JSON.stringify(round.payload)},'<svg viewBox="0 0 16 16" aria-hidden="true"></svg>','',ports)).join('');
    }""", locale)
    cards = frame.locator('#receipts > details.ptool-motion-block, #receipts > article.ptool-work-card')
    expect(cards).to_have_count(4)
    assert frame.evaluate('workUnitDemands') == 1
    assert cards.nth(1).locator('.ptool-convmeta-src').inner_text() == 'Report'
    for i in range(4):
        expect(cards.nth(i)).not_to_have_attribute('open', '')
        cards.nth(i).locator('summary').first.click()
        expect(cards.nth(i).locator('.ptool-motion-body')).to_be_visible()
    expect(cards.nth(0)).to_contain_text('Title overlaps chart')
    expect(cards.nth(2)).to_contain_text('deck rolled back')
    expect(cards.nth(2)).to_contain_text('<script>literal</script>')
    expect(frame.locator('#receipts script')).to_have_count(0)
    expect(cards.nth(3)).to_contain_text('0.00s')
    frame.evaluate("""() => {
      const text=document.querySelector('.ptool-motion-err');const range=document.createRange();range.selectNodeContents(text);
      const selection=getSelection();selection.removeAllRanges();selection.addRange(range);
    }""")
    assert frame.evaluate('getSelection().toString()') == 'Title overlaps chart'
    geometry = frame.evaluate("""() => ({width:innerWidth,scroll:document.documentElement.scrollWidth,
      cards:[...document.querySelectorAll('details')].map(n=>({width:n.clientWidth,scroll:n.scrollWidth}))})""")
    assert geometry['scroll'] <= geometry['width'] + 1
    assert all(card['scroll'] <= card['width'] + 1 for card in geometry['cards'])
    contrasts = frame.evaluate("""() => {
      const rgb=value=>value.match(/[0-9.]+/g).slice(0,3).map(Number);
      const luminance=values=>values.map(v=>v/255).map(v=>v<=.04045?v/12.92:((v+.055)/1.055)**2.4).reduce((s,v,i)=>s+v*[.2126,.7152,.0722][i],0);
      return [...document.querySelectorAll('.ptool-motion-warn,.ptool-motion-err,.ptool-convmeta-why')].map(el=>{
        const fg=luminance(rgb(getComputedStyle(el).color)),bg=luminance(rgb(getComputedStyle(el.closest('.ptool-motion-body')).backgroundColor));
        return (Math.max(fg,bg)+.05)/(Math.min(fg,bg)+.05);
      });
    }""")
    assert min(contrasts) >= 4.5, (theme, contrasts)
    page.locator('#receipt-fixture').screenshot(path=str(tmp_path / f'receipts-{theme}-{width}.png'))
    cards.nth(2).locator('summary').first.click()
    expect(cards.nth(2).locator('.ptool-motion-body')).not_to_be_visible()
    # The same token migration owns project receipts: exercise the actual board projection.
    frame.add_script_tag(path=native_module_path('.native/visual-receipt-board.js', ROOT / 'frontend/src/conversation/ui/project-receipt/board.ts'))
    frame.evaluate("""() => {
      const board=document.createElement('section');board.id='board';board.className='ptool-convmeta-body';
      board.innerHTML=renderBoardSnapshot({lanes:{open:[{title:'Plan the implementation',owner:'reviewer'}],
        claimed:[{title:'A long file path / frontend / conversation / presentation / projection',owner:'builder'}],done:[{title:'Verification complete'}]}}, {text:window.t});
      document.querySelector('#receipts').append(board);
    }""")
    expect(frame.locator('.ptool-board-mini-card')).to_have_count(3)
    assert frame.locator('#board').evaluate('(el)=>el.scrollWidth<=el.clientWidth+1')
