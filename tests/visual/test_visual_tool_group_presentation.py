"""Actual typed tool-group graph, reader reconciliation and delegated disclosure over shipped CSS in Chromium."""
import json

import pytest

from tests._runtime_sections import ROOT, native_module_path
from tests.support.frontend_visual import frontend_visual_origin, frontend_visual_page as frontend_visual_page  # noqa: F401

pytestmark = pytest.mark.visual


@pytest.mark.parametrize('theme,width', [(theme, width) for theme in ['light', 'dark', 'tofu'] for width in [390, 1180]])
def test_tool_group_disclosure_narration_and_reader_state(frontend_visual_page, theme, width, tmp_path):
    from playwright.sync_api import expect
    page = frontend_visual_page
    page.set_viewport_size({'width': width, 'height': 1000})
    page.evaluate('theme=>{document.documentElement.dataset.theme=theme;document.body.innerHTML="<main class=conversation-surface id=fixture></main>";}', theme)
    page.add_style_tag(content='body{display:block;height:auto;overflow:auto;margin:0;padding:var(--space-4);background:var(--bg-primary)}'
                                '#fixture{max-width:var(--conversation-reading-measure);margin-inline:auto;}')
    entry = tmp_path / 'owners.ts'
    entry.write_text('\n'.join('export * from ' + json.dumps(str(ROOT / name)) + ';' for name in [
        'frontend/src/conversation/ui/tool-groups/index.ts',
        'frontend/src/conversation/ui/tool-execution-disclosure.ts',
        'frontend/src/conversation/ui/rich-card-reconciliation.ts',
        'frontend/src/html-safety.ts',
    ]))
    page.add_script_tag(path=native_module_path('.native/visual-tool-group-presentation.js', entry))
    locale = json.loads((ROOT / 'frontend/src/i18n/locales/en.json').read_text())
    page.evaluate(r"""locale=>{
      const text=(key,values={})=>(locale[key]||key).replace(/\{(\w+)\}/g,(match,name)=>values[name]??match);
      window.rows=[1,2,3,4].map((n)=>({roundNum:n,toolCallId:'call'+n,toolName:'read_files',
        query:n===4?'Read final resource':'Read resource',toolArgs:{path:'resource-'+n},llmRound:n===4?1:0,status:'done'}));
      window.anchorClicks=0;
      const ports={text,icon:()=>'<svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true"></svg>',
        markdown:value=>'<p>'+escapeHtml(value)+'</p>',cleanTranslation:value=>value.replace(/<\/?nt>/g,''),
        renderLine:r=>'<div class="ptool-line"><span class="ptool-text">'+escapeHtml(r.query)+'</span><span class="ptool-row-ctl">'+ui.renderInspector(r)+'</span></div>',
        renderSwarm:()=>null,renderProgram:()=>'',debugEnabled:()=>true,taskIdForRound:()=> 'fixture-task',debugLog:()=>{}};
      const ui=createToolGroupPresentation(ports);
      window.paintGroups=()=>reconcileRichCardHtml(document.querySelector('#fixture'),ui.renderPanel(rows,
        [{type:'text',llmRound:0,text:'Source narration',translatedText:'<nt>Translated narration</nt>'}]));
      document.addEventListener('click',handleToolExecutionDisclosureClick);
      document.addEventListener('click',event=>{if(event.target.closest('.ri-tool-anchor'))window.anchorClicks++;});
      paintGroups();
    }""", locale)
    panel = page.locator('.ptool-panel')
    panel_header = panel.locator('.ptool-panel-header')
    expect(panel_header).to_have_attribute('aria-expanded', 'false')
    panel_header.focus()
    panel_header.press('Enter')
    expect(panel_header).to_have_attribute('aria-expanded', 'true')
    narration = panel.locator('.seg-narration')
    expect(narration).to_have_text('Translated narration')
    assert narration.evaluate('(el)=>!el.closest(".ptool-turn")')
    batch = panel.locator('.ptool-turn[data-llm-round="L0"]')
    batch_header = batch.locator('.ptool-turn-head')
    expect(batch_header).to_have_attribute('aria-expanded', 'false')
    batch_header.focus()
    batch_header.press(' ')
    expect(batch_header).to_have_attribute('aria-expanded', 'true')
    old_row = batch.locator('[data-prn="2"]')
    expect(old_row.locator('.ptool-text')).to_contain_text('path=resource-2')
    old_row.locator('.ri-tool-anchor').press('Enter')
    assert page.evaluate('anchorClicks') == 1
    expect(batch_header).to_have_attribute('aria-expanded', 'true')
    batch_header.focus()
    page.evaluate("""()=>{
      window.oldHeader=document.querySelector('.ptool-turn[data-llm-round="L0"] .ptool-turn-head');
      window.oldText=document.querySelector('[data-prn="2"] .ptool-text').firstChild;
      const range=document.createRange();range.setStart(oldText,0);range.setEnd(oldText,4);
      getSelection().removeAllRanges();getSelection().addRange(range);
      rows.push({roundNum:5,toolCallId:'call5',toolName:'read_files',query:'New resource',llmRound:2,status:'done'});
      paintGroups();
    }""")
    expect(batch_header).to_be_focused()
    expect(batch_header).to_have_attribute('aria-expanded', 'true')
    expect(panel_header).to_have_attribute('aria-expanded', 'true')
    assert page.evaluate('document.activeElement===oldHeader')
    assert page.evaluate('getSelection().anchorNode===oldText && getSelection().toString()==="Read"')
    expect(panel.locator('[data-prn="5"]')).to_have_count(1)
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
    expect(panel).to_have_css('opacity', '1')  # Measure reading contrast after the existing entrance animation.
    assert old_row.locator('.ptool-text').evaluate('(el)=>Number.parseFloat(getComputedStyle(el).fontSize)>=13')
    contrast = old_row.locator('.ptool-text').evaluate(r"""el=>{
      const rgba=value=>{const values=value.match(/[0-9.]+/g).map(Number);
        return value.startsWith('color(srgb ') ? [...values.slice(0,3).map(x=>x*255),values[3]??1] : values;};
      const layers=[];let opacity=1;
      for(let node=el;node;node=node.parentElement){
        const css=getComputedStyle(node);layers.unshift(rgba(css.backgroundColor));opacity*=Number(css.opacity);
      }
      let bg=[255,255,255];
      for(const paint of layers){const alpha=paint[3]??1;bg=bg.map((value,i)=>paint[i]*alpha+value*(1-alpha));}
      const paint=rgba(getComputedStyle(el).color),alpha=(paint[3]??1)*opacity;
      const fg=bg.map((value,i)=>paint[i]*alpha+value*(1-alpha));
      const luminance=rgb=>rgb.map(value=>{const x=value/255;return x<=.04045?x/12.92:((x+.055)/1.055)**2.4;})
        .reduce((sum,value,i)=>sum+value*[.2126,.7152,.0722][i],0);
      const a=luminance(fg),b=luminance(bg);return {ratio:(Math.max(a,b)+.05)/(Math.min(a,b)+.05),fg,bg,opacity,color:getComputedStyle(el).color};
    }""")
    assert contrast['ratio'] >= 4.5, contrast
    assert narration.evaluate('(el)=>el.getBoundingClientRect().bottom<=el.nextElementSibling.getBoundingClientRect().top+1')
    page.screenshot(path=str(tmp_path / f'tool-groups-{theme}-{width}.png'), full_page=True)
