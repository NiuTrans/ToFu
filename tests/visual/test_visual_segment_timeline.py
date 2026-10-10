"""Actual retained timeline adapter plus typed projection/grouping/reconciliation in Chromium."""
import json

import pytest
from tests._runtime_sections import ROOT, native_module_path, runtime_section
from tests.support.frontend_visual import frontend_visual_origin as _visual_origin, frontend_visual_page as _visual_page

frontend_visual_origin = _visual_origin
frontend_visual_page = _visual_page

pytestmark = pytest.mark.visual


@pytest.mark.parametrize('theme', ['light', 'dark', 'tofu'])
@pytest.mark.parametrize('width', [390, 1180])
def test_retry_timeline_keeps_one_injection_and_distinct_results_with_reader_state(frontend_visual_page, theme, width, tmp_path):
    from playwright.sync_api import expect
    page = frontend_visual_page
    page.set_viewport_size({'width': width, 'height': 900})
    page.emulate_media(reduced_motion='reduce')
    page.set_content('<!doctype html><html data-theme="' + theme + '"><head><link rel="stylesheet" href="/static/styles.css">'
                     '</head><body><main id="fixture" style="width:100%;max-width:720px;padding:var(--space-4)"></main></body></html>')
    labels = json.loads((ROOT / 'frontend/src/i18n/locales/en.json').read_text())
    page.evaluate(r"""labels=>{
      window.t=(key,values={})=>(labels[key]||key).replace(/\{(\w+)\}/g,(match,name)=>values[name]??match);
      window.Icon=()=>'<svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true"></svg>';
      window.escapeHtml=value=>String(value??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;');
      window.renderMarkdown=value=>'<p>'+escapeHtml(value)+'</p>';
      window.stripNoTranslateTags=value=>value.replace(/<\/?nt>/g,'');
      window.getToolRoundsFromMsg=msg=>msg.toolRounds;window.config={};window.projectState={extraRoots:[]};
    }""", labels)
    page.add_script_tag(content=runtime_section('ui/tool_rounds.js'))
    entry = tmp_path / 'reader-entry.ts'
    entry.write_text(''.join('export * from ' + json.dumps(str(ROOT / path)) + ';' for path in [
        'frontend/src/conversation/ui/rich-card-reconciliation.ts',
        'frontend/src/conversation/ui/tool-execution-disclosure.ts',
    ]))
    page.add_script_tag(path=native_module_path('.native/visual-segment-timeline-reader.js', entry))
    page.evaluate(r"""()=>{
      _renderUnifiedToolLine=round=>'<div class="ptool-line '+(round._peerInject?'timeline-injection':'timeline-result')+'">'
        +'<span class="ptool-text">'+escapeHtml(round.query)+'</span></div>';
      window.timelineRows=[{toolName:'peer',_peerInject:true,peerRound:1,query:'Peer guidance <script>literal</script>'},
        {toolName:'read_files',toolCallId:'repeat',attemptId:'A',llmRound:0,roundNum:1,status:'done',query:'First source result'},
        {toolName:'read_files',toolCallId:'repeat',attemptId:'B',llmRound:0,roundNum:2,status:'done',query:'Retry source result'}];
      window.timelineSegments=[{type:'text',text:'First narration',translatedText:'<nt>Translated first narration</nt>',llmRound:0,attemptId:'A'},
        {type:'tool_use',id:'repeat',llmRound:0,attemptId:'A'},
        {type:'text',text:'Retry narration',llmRound:0,attemptId:'B'},
        {type:'tool_use',id:'repeat',llmRound:0,attemptId:'B'},
        {type:'text',text:'Terminal answer',deliverable:true,terminal:true}];
      window.paintTimeline=()=>reconcileRichCardHtml(document.getElementById('fixture'),
        renderSegmentTimelineHTML(timelineSegments,{toolRounds:timelineRows},0));
      document.addEventListener('click',handleToolExecutionDisclosureClick);
      paintTimeline();
    }""")
    panel = page.locator('.seg-timeline')
    header = panel.locator('.ptool-panel-header')
    if header.get_attribute('aria-expanded') == 'false':
        header.focus(); header.press('Enter')
    expect(header).to_have_attribute('aria-expanded', 'true')
    for batch in panel.locator('.ptool-turn-head').all():
        if batch.get_attribute('aria-expanded') == 'false':
            batch.focus(); batch.press('Space')
    expect(panel.locator('.timeline-injection')).to_have_count(1)
    expect(panel.locator('.timeline-result')).to_have_count(2)
    expect(panel.locator('.timeline-result').nth(0)).to_have_text('First source result')
    expect(panel.locator('.timeline-result').nth(1)).to_have_text('Retry source result')
    expect(panel.locator('.seg-narration').nth(0)).to_have_text('Translated first narration')
    assert 'Terminal answer' not in panel.inner_text()
    assert panel.locator('script').count() == 0
    assert panel.locator('.ptool-panel-body').get_attribute('data-full-count') == '2'
    assert panel.evaluate('''el=>{const nodes=[...el.querySelectorAll('.timeline-injection,.timeline-result,.seg-narration')];
      return nodes.map(node=>node.textContent).join('|')==='Peer guidance <script>literal</script>|Translated first narration|First source result|Retry narration|Retry source result'}''')
    header.focus()
    page.evaluate("""()=>{
      window.oldTimelineHeader=document.querySelector('.ptool-panel-header');
      window.selectedTimelineText=document.querySelector('.timeline-result .ptool-text').firstChild;
      const range=document.createRange();range.setStart(selectedTimelineText,0);range.setEnd(selectedTimelineText,5);
      getSelection().removeAllRanges();getSelection().addRange(range);
      timelineRows.push({toolName:'read_files',toolCallId:'third',attemptId:'C',llmRound:1,roundNum:3,status:'done',query:'Later result'});
      timelineSegments.splice(timelineSegments.length-1,0,{type:'tool_use',id:'third',attemptId:'C',llmRound:1});
      paintTimeline();
    }""")
    expect(header).to_be_focused()
    expect(header).to_have_attribute('aria-expanded', 'true')
    assert header.evaluate('(el)=>el===oldTimelineHeader')
    assert page.evaluate("getSelection().toString()==='First' && selectedTimelineText.isConnected")
    expect(panel.locator('.timeline-injection')).to_have_count(1)
    expect(panel.locator('.timeline-result')).to_have_count(3)
    assert panel.locator('.ptool-panel-body').get_attribute('data-full-count') == '3'
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
    page.screenshot(path=str(tmp_path / f'timeline-{theme}-{width}.png'))

    # Durable replay may retain tool bodies after an earlier segment is trimmed.
    # The resumed occurrence must still display B's result, never borrow A's.
    page.evaluate("""()=>{
      timelineSegments=timelineSegments.filter(segment=>segment.attemptId!=='A');
      paintTimeline();
    }""")
    expect(panel.locator('.timeline-result')).to_have_count(2)
    expect(panel.locator('.timeline-result').nth(0)).to_have_text('Retry source result')
    expect(panel.locator('.timeline-result').nth(1)).to_have_text('Later result')
    expect(header).to_be_focused()
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
