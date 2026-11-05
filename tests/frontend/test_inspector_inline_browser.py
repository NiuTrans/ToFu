"""Native keyboard controls and detached-node release during an unfinished read."""
import pytest

pytestmark = pytest.mark.visual


@pytest.mark.parametrize('scenario', ['keyboard_enter', 'keyboard_space', 'pending_close', 'detached_panel', 'reading_narrow', 'reading_wide'])
def test_inspector_inline_controls(browser, live_server, tmp_path, scenario):
    from playwright.sync_api import expect

    context = browser.new_context(service_workers='block')
    context.add_init_script("localStorage.setItem('tofu_ui_lang','en');localStorage.setItem('tofu_onboarding_v1_done','1');")
    page = context.new_page()
    if scenario.startswith('reading'):
        page.set_viewport_size({'width': 390 if scenario.endswith('narrow') else 1180, 'height': 1000})
    pending, errors = [], []
    page.on('pageerror', lambda error: errors.append(str(error)))
    context.route('**/api/v4/tasks/inline-task/requests/**', lambda route: pending.append(route))
    try:
        page.goto(live_server, wait_until='domcontentloaded')
        page.wait_for_function('window.TofuModules && window.TofuModules.version === 3')
        page.evaluate("""() => {
          const slot=document.createElement('div');slot.dataset.prn='2';slot.id='inline-slot';
          const anchor=document.createElement('button');anchor.id='inline-origin';anchor.textContent='Inspect tool evidence';
          anchor.dataset.riState='inline-task:2';anchor.dataset.riTurn='worker:exact';slot.append(anchor);
          const host=document.getElementById('chatInner');
          if(!host)throw new Error('Inline fixture requires the actual chatInner host');
          const surface=document.createElement('main');surface.className='conversation-surface';
          surface.innerHTML='<section class="conversation-turn message assistant"><div class="conversation-turn-content message-content"><div class="conversation-turn-blocks"></div></div></section>';
          surface.querySelector('.conversation-turn-blocks').append(slot);
          host.replaceChildren(surface);
          void window.TofuModules.invokeFeature('openToolDebugPanel',['inline-task',2,anchor],()=>{});
        }""")
        panel = page.locator('.ri-state-panel')
        close = panel.locator('.ri-state-panel-close')
        expect(close).to_be_visible()
        expect(page.locator('#chatInner .conversation-turn-blocks .ri-state-panel')).to_have_count(1)
        if scenario.startswith('reading'):
            page.evaluate('(theme) => document.documentElement.dataset.theme=theme', 'dark' if scenario.endswith('narrow') else 'tofu')
            pending.pop().fulfill(json={'data': {'foldPrefix': 1, 'label': 'R2', 'messages': [
                {'role': 'system', 'content': 'previous history'},
                {'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'read-1', 'type': 'function',
                    'function': {'name': 'read_files', 'arguments': '{"paths":["src/handler.ts","tests/handler.test.ts"]}'}}]},
                {'role': 'tool', 'tool_call_id': 'read-1', 'content': 'Exact tool evidence\nThe handler and its failure contract were inspected.'},
            ]}, 'meta': {}})
            expect(panel).to_contain_text('Exact tool evidence')
            expect(panel.locator('.debug-msg-block.open')).to_have_count(2)
            expect(close).to_have_attribute('type', 'button')
            geometry = close.evaluate('node => ({width:node.getBoundingClientRect().width,height:node.getBoundingClientRect().height,right:node.getBoundingClientRect().right,panelRight:node.closest(".ri-state-panel").getBoundingClientRect().right})')
            assert geometry['width'] >= 28 and geometry['height'] >= 28
            assert geometry['right'] <= geometry['panelRight']
            bounds = panel.evaluate('node => ({left:node.getBoundingClientRect().left,right:node.getBoundingClientRect().right,viewport:document.documentElement.clientWidth})')
            assert bounds['left'] >= 0 and bounds['right'] <= bounds['viewport'] + 1
            close.focus()
            page.screenshot(path=str(tmp_path / f'inspector-inline-{scenario}.png'))
            assert not errors, errors
            return
        if scenario.startswith('keyboard'):
            close.focus()
            close.press('Enter' if scenario.endswith('enter') else 'Space')
            expect(panel).to_have_count(0)
            expect(page.locator('#inline-origin')).to_be_focused()
        else:
            page.evaluate("""() => {
              window.inlinePanelWitness=new WeakRef(document.querySelector('.ri-state-panel'));
              const control=document.createElement('div');document.body.append(control);
              window.inlineControlWitness=new WeakRef(control);control.remove();
            }""")
            if scenario == 'pending_close':
                close.click()
            else:
                page.evaluate("document.querySelector('.ri-state-panel').remove()")
            expect(panel).to_have_count(0)
            cdp = context.new_cdp_session(page)
            for _ in range(2):
                page.evaluate('() => new Promise(resolve => requestAnimationFrame(resolve))')
                cdp.send('HeapProfiler.collectGarbage')
            assert page.evaluate('window.inlineControlWitness.deref() === undefined'), 'GC control must be collectible'
            assert page.evaluate('window.inlinePanelWitness.deref() === undefined'), 'unfinished read retains removed inline DOM'
        assert len(pending) == 1
        with page.expect_response(lambda response: '/inline-task/requests/' in response.url):
            pending.pop().fulfill(json={'data': {'messages': [{'role': 'tool', 'content': 'late evidence'}]}, 'meta': {}})
        page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
        expect(panel).to_have_count(0)
        assert not errors, errors
    finally:
        context.close()
