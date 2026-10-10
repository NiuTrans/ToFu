"""Actual receipt owners, action registry, reconciler and shipped CSS in Chromium."""
import json

import pytest

from tests._runtime_sections import ROOT, native_module_path
from tests.support.frontend_visual import frontend_visual_origin, frontend_visual_page as frontend_visual_page  # noqa: F401

pytestmark = pytest.mark.visual


@pytest.mark.parametrize('theme,width', [(theme, width) for theme in ['light', 'dark', 'tofu'] for width in [390, 1180]])
def test_checklist_timer_reading_and_local_disclosures(frontend_visual_page, theme, width, tmp_path):
    from playwright.sync_api import expect
    page = frontend_visual_page
    page.set_viewport_size({'width': width, 'height': 1400})
    styles = page.locator('link[rel="stylesheet"]').evaluate_all('(nodes)=>nodes.map(n=>n.href)')
    page.evaluate("""() => {
      const frame=document.createElement('iframe');frame.id='receipt-fixture';
      frame.style.cssText='position:fixed;inset:0;width:100%;height:100%;border:0';document.body.append(frame);
    }""")
    frame = page.frames[-1]
    frame.set_content(f'<html data-theme="{theme}" lang="en"><body><main id="receipts" class="conversation-surface"></main></body></html>')
    for url in styles:
        frame.add_style_tag(url=url)
    frame.add_style_tag(content='body{display:block;height:auto;overflow:auto;margin:0;padding:var(--space-4);background:var(--bg-primary)}'
                         '#receipts{max-width:var(--conversation-reading-measure);margin-inline:auto;display:grid;gap:var(--space-3)}')
    entry = tmp_path / 'owners.ts'
    entry.write_text('\n'.join('export * from ' + json.dumps(str(ROOT / path)) + ';' for path in [
        'frontend/src/conversation/ui/checklist-card/index.ts',
        'frontend/src/conversation/ui/timer-card/index.ts',
        'frontend/src/conversation/ui/timer-card/interactions.ts',
        'frontend/src/conversation/ui/rich-card-reconciliation.ts',
        'frontend/src/action-registry.ts',
    ]))
    frame.add_script_tag(path=native_module_path('.native/visual-checklist-timer.js', entry))
    locale = json.loads((ROOT / 'frontend/src/i18n/locales/en.json').read_text())
    frame.evaluate("""locale => {
      const text=(key,fallback)=>typeof fallback==='object' ? locale[key].replace('{n}',fallback.n) : locale[key] || fallback;
      let clock=10000; window.demands=0; window.chipClicks=0;
      document.addEventListener('click',event=>{if(event.target.closest('.timer-id-chip'))window.chipClicks++;});
      const ports={text,now:()=>clock,formatTime:()=> '12:34:56',markdown:value=>value,
        demandCountdown:()=>window.demands++,icon:()=>'<svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true"></svg>'};
      window.paintClock=value=>{clock=value;return refreshTimerCountdowns(document,ports);};
      const todo={roundNum:1,results:[{todoExplanation:'Status reports progress, not proof of success. Review every item before accepting the change.',
        todos:['completed','in_progress','blocked','pending'].map(status=>({status,content:'Review '+status+' — '+ 'long-path/'.repeat(12)})),
        todoDisplayUpdateCount:2,todoDisplayHistory:[{operation:'set',done:0,total:4},{operation:'complete',done:1,total:4}]}]};
      window.timerRound={roundNum:2,status:'searching',_timerTimerId:'tmr_fixture_read_only',_timerPollInterval:30,
        _timerConditionKind:'hybrid',_timerCheckCommand:'test -f build/evidence-ready',_timerSkipCount:3,
        _timerCheckInstruction:'Verify deployment evidence and preserve every detail. '.repeat(6),_timerNextPollTs:15000};
      window.timerPolls=[{decision:'ready',pollNum:0,ts:1000,pollId:'tmr_fixture.p0',model:'Verifier',reason:'Earlier evidence met the condition'},
        {decision:'parse_error',pollNum:1,reason:'Check could not be interpreted',rawContent:'unparseable evidence <script>literal</script>',
        cmdOutput:'Command evidence remains selectable',toolTrace:[{name:'run_command',argsBrief:'inspect build',isError:true}]}];
      window.timerHtml=()=>renderTimerCard(timerRound,{polls:timerPolls,triggered:false},'',ports);
      document.querySelector('#receipts').innerHTML=renderChecklistCard(todo,'',()=>'',text)+
        '<section class="first-timer">'+timerHtml()+'</section><section class="second-timer">'+timerHtml()+'</section>';
      const actions={_timerWatcherToggle:(el,event)=>toggleTimerWatcher(el,event,ports.demandCountdown),
        _timerInstructionToggle:toggleTimerInstruction,_timerPollToggle:toggleTimerPoll};
      window.releaseActions=installActionRegistry(name=>actions[name]);
    }""", locale)
    checklist = frame.locator('details.ptool-todo-block')
    summary = checklist.locator(':scope > summary')
    summary.focus()
    summary.press('Enter')
    expect(checklist.locator('.ptool-todo-body')).to_be_visible()
    expect(summary).to_be_focused()
    assert summary.evaluate('(el)=>getComputedStyle(el).outlineStyle') != 'none'
    expect(checklist.locator('.ptool-todo-count')).to_have_text('1/4')
    for selector, minimum in [('.ptool-text', 13), ('.ptool-todo-explanation', 13), ('.ptool-todo-text', 13)]:
        assert float(checklist.locator(selector).first.evaluate('(el)=>parseFloat(getComputedStyle(el).fontSize)')) >= minimum
    # Task state changes emphasis, never the shared content size or face.
    task_type = checklist.locator('.ptool-todo-text').evaluate_all("""nodes => nodes.map(el => {
      const style=getComputedStyle(el);
      return {size:parseFloat(style.fontSize), family:style.fontFamily,
        rowFamily:getComputedStyle(el.closest('.ptool-todo-item')).fontFamily,
        weight:style.fontWeight};
    })""")
    assert all(item['size'] == 13 and item['family'] == item['rowFamily'] for item in task_type)
    assert {item['weight'] for item in task_type} <= {'400', '600'}
    frame.evaluate("document.documentElement.style.setProperty('--conversation-font-size-content', '15px')")
    for selector in ['.ptool-todo-text', '.ptool-text', '.ptool-todo-explanation']:
        expect(checklist.locator(selector).first).to_have_css('font-size', '15px')
    frame.evaluate("document.documentElement.style.removeProperty('--conversation-font-size-content')")

    assert checklist.locator('.ptool-todo-text').first.evaluate('(el)=>getComputedStyle(el).opacity') == '1'
    history = checklist.locator('.ptool-todo-history summary')
    history.focus()
    history.press('Space')
    expect(checklist.locator('.ptool-todo-history')).to_have_attribute('open', '')
    expect(history).to_be_focused()

    radius = frame.locator('.timer-watcher-block').first.evaluate('(el)=>parseFloat(getComputedStyle(el).borderTopLeftRadius)')
    assert radius > 0, 'Timer shell geometry must consume a declared, resolved radius token.'
    first = frame.locator('.first-timer')
    second = frame.locator('.second-timer')
    chip = second.locator('.timer-id-chip')
    chip.focus()
    chip.press('Enter')
    chip.press('Space')
    chip.click()
    expect(second.locator('.timer-watcher-header')).to_have_attribute('aria-expanded', 'true')
    assert frame.evaluate('chipClicks') == 3
    instruction = second.locator('.timer-meta-row-instr')
    instruction.focus()
    instruction.press('Enter')
    expect(instruction).to_have_attribute('aria-expanded', 'true')
    expect(first.locator('.timer-meta-row-instr')).to_have_attribute('aria-expanded', 'false')
    expect(instruction).to_be_focused()
    instruction.press('Escape')
    expect(instruction).to_have_attribute('aria-expanded', 'true')
    poll = second.locator('.timer-poll-has-detail[data-prn="number:1"]')
    poll.focus()
    poll.press('Space')
    expect(poll).to_have_attribute('aria-expanded', 'true')
    expect(second.locator('.timer-poll-raw')).to_be_visible()
    assert second.locator('.timer-poll-caret svg').evaluate('(el)=>el.getAttribute("style")') is None
    expect(second.locator('.timer-poll-caret svg')).to_have_css('transform', 'matrix(1, 0, 0, 1, 0, 0)')
    for selector, minimum in [('.timer-watcher-label', 13), ('.timer-meta-md', 14), ('.timer-poll-reason', 13), ('.timer-poll-raw code', 12)]:
        assert float(second.locator(selector).first.evaluate('(el)=>parseFloat(getComputedStyle(el).fontSize)')) >= minimum
    assert second.locator('.timer-poll-skipped').evaluate('(el)=>getComputedStyle(el).opacity') == '1'
    expect(first.locator('.timer-poll-raw')).not_to_be_visible()
    assert frame.evaluate("""() => {
      window.focusedPoll=document.querySelector('.second-timer .timer-poll-has-detail');
      const raw=document.querySelector('.second-timer .timer-poll-raw code');
      const range=document.createRange();range.selectNodeContents(raw);getSelection().removeAllRanges();getSelection().addRange(range);
      const hint=document.querySelector('.second-timer .timer-next-poll-txt');window.hint=hint;
      const active=paintClock(11000);
      return active && hint===window.hint && hint.textContent.includes('4s') && getSelection().toString().includes('unparseable evidence');
    }""")
    frame.evaluate("""() => {
      timerRound._timerNextPollTs=16000;
      timerPolls.push({decision:'error',pollNum:2,reason:'New check evidence',cmdOutput:'Separate failure evidence'});
      reconcileRichCardHtml(document.querySelector('.second-timer'),timerHtml());
    }""")
    expect(second.locator('.timer-poll-has-detail[data-prn="number:2"]')).to_have_attribute('aria-expanded', 'false')
    expect(poll).to_be_focused()
    expect(poll).to_have_attribute('aria-expanded', 'true')
    expect(instruction).to_have_attribute('aria-expanded', 'true')
    assert frame.evaluate('focusedPoll===document.activeElement && hint===document.querySelector(".second-timer .timer-next-poll-txt")')
    assert 'unparseable evidence' in frame.evaluate('getSelection().toString()')
    assert frame.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
    expect(frame.locator('#receipts script')).to_have_count(0)
    contrast = frame.evaluate("""() => {
      const rgba=value=>value.match(/[0-9.]+/g).map(Number);
      const lum=rgb=>rgb.slice(0,3).map(v=>v/255).map(v=>v<=.04045?v/12.92:((v+.055)/1.055)**2.4).reduce((s,v,i)=>s+v*[.2126,.7152,.0722][i],0);
      function background(el) {
        const layers=[];
        for(let node=el;node;node=node.parentElement)layers.unshift(rgba(getComputedStyle(node).backgroundColor));
        let rgb=[255,255,255];
        for(const layer of layers){const alpha=layer.length>3?layer[3]:1;rgb=rgb.map((v,i)=>layer[i]*alpha+v*(1-alpha));}
        return rgb;
      }
      window.receiptTextContrast=()=> [...document.querySelectorAll('.ptool-todo-text,.ptool-todo-header .ptool-text,.ptool-todo-count,.ptool-todo-explanation,.timer-watcher-label,.timer-id-txt,.timer-kind-badge,.timer-meta-label,.timer-meta-val,.timer-meta-more,.timer-poll-num,.timer-poll-reason,.timer-poll-meta,.timer-poll-model,.timer-poll-id,.timer-poll-toolcount,.timer-next-poll,.timer-poll-detail-label,.timer-poll-detail-output code,.timer-poll-detail-reason,.timer-poll-trace .sw-tl-name,.timer-poll-trace .sw-tl-args')].map(el=>{
        const fg=lum(rgba(getComputedStyle(el).color)),bg=lum(background(el));
        return {className:el.className,ratio:(Math.max(fg,bg)+.05)/(Math.min(fg,bg)+.05)};
      });
      return receiptTextContrast();
    }""")
    assert not [value for value in contrast if value['ratio'] < 4.5], (theme, contrast)
    for row in second.locator('.timer-poll-line').all():
        row.hover()
        values = frame.evaluate('receiptTextContrast()')
        assert not [value for value in values if value['ratio'] < 4.5], (theme, values)
    frame.evaluate('scrollTo(0, 0)')
    page.locator('#receipt-fixture').screenshot(path=str(tmp_path / f'checklist-timer-{theme}-{width}.png'))
    for card in [first, second]:
        header = card.locator('.timer-watcher-header')
        header.focus()
        header.press('Enter')
        expect(header).to_have_attribute('aria-expanded', 'false')
        expect(header).to_be_focused()
    assert frame.evaluate('paintClock(12000)') is False
    demands = frame.evaluate('demands')
    second.locator('.timer-watcher-header').press('Space')
    assert frame.evaluate('paintClock(12000)') is True
    assert frame.evaluate('demands') == demands + 1
    frame.evaluate('releaseActions()')
