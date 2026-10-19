"""Offline Chromium contract for native depth gestures and responsive geometry.

Uses the shared browser lifecycle; request contexts and screenshot artifacts are
fixture-owned so this suite composes with full-app journeys in one pytest run.
"""
import json
from pathlib import Path
import subprocess
import unittest


import pytest

pytestmark = pytest.mark.visual

ROOT = Path(__file__).resolve().parents[2]



class ThinkingDepthBrowser(unittest.TestCase):
    @pytest.fixture(autouse=True)
    def surface_resources(self, browser, tmp_path):
        self.browser = browser
        self.artifacts = tmp_path
        from lib.application_shell_fragments import inject_fragments
        self.html = inject_fragments((ROOT / 'index.html').read_text())
        self.css = (ROOT / 'static/styles.css').read_text()
        self.script = subprocess.check_output(['node', '-e', "const ts=require('typescript'),fs=require('fs');process.stdout.write(ts.transpileModule(fs.readFileSync('frontend/src/features/thinking-depth-slider.ts','utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2020}}).outputText)"], cwd=ROOT, text=True, timeout=30)

    def open_surface(self, width, theme='tofu', lang='en'):
        context = self.browser.new_context(viewport={'width': width, 'height': 800}, has_touch=width < 600, is_mobile=width < 600, service_workers='block')
        self.addCleanup(context.close)
        context.route('**/*', lambda route: route.abort())
        page = context.new_page()
        page.set_content('<html><head><meta name="viewport" content="width=device-width,initial-scale=1"></head><body></body></html>')
        page.add_style_tag(content=self.css)
        page.evaluate('''({html,theme,mobile}) => {
          document.documentElement.dataset.theme=theme;
          const src=new DOMParser().parseFromString(html,'text/html');
          for(const id of ['presetDropdown','mobileSheet']) {
            const el=src.getElementById(id); document.body.append(el);
            el.classList.add('open');
            if((id==='mobileSheet')!==mobile) el.style.display='none';
          }
          if(!mobile) Object.assign(document.getElementById('presetDropdown').style,{display:'block',position:'absolute',left:'40px',top:'40px',bottom:'auto'});
        }''', {'html': self.html, 'theme': theme, 'mobile': width < 600})
        locale = json.loads((ROOT / f'frontend/src/i18n/locales/{lang}.json').read_text())
        page.evaluate('''dict => { window.depthLocale=dict; document.querySelectorAll('[data-i18n]').forEach(el=>{const t=dict[el.dataset.i18n];if(t)el.textContent=t;}); }''', locale)
        page.add_script_tag(content='var exports={};\n' + self.script + '\nwindow.depthCommits=[];window.depthOwner=exports.createThinkingDepthSlider({document,translate:k=>depthLocale[k]||k,commit:d=>{depthCommits.push(d);depthOwner.sync(d,true);}});depthOwner.sync("medium",true);')
        self.addCleanup(lambda: page.evaluate('depthOwner.destroy()') if not page.is_closed() else None)
        selector = '#mobileThinkingDepthRange' if width < 600 else '#thinkingDepthRange'
        page.locator(selector).scroll_into_view_if_needed()
        return page, selector

    def state(self, page, selector):
        return page.locator(selector).evaluate('el=>({value:+el.value,step:el.step,fill:+el.closest("[data-thinking-depth-slider]").style.getPropertyValue("--depth-fraction"),commits:[...depthCommits]})')

    def gesture(self, page, selector, positions, touch=False, cancel=False, refresh=False):
        box = page.locator(selector).bounding_box()
        thumb = page.locator(selector).evaluate('el=>parseFloat(getComputedStyle(el).getPropertyValue("--depth-thumb-size"))')
        def point(value):
            return {'x': box['x'] + thumb/2 + (box['width'] - thumb) * value / 5, 'y': box['y'] + box['height']/2}
        cdp = page.context.new_cdp_session(page) if touch else None
        start = point(self.state(page, selector)['value'])
        if touch:
            cdp.send('Input.dispatchTouchEvent', {'type':'touchStart','touchPoints':[dict(start, id=1)]})
        else:
            page.mouse.move(**start)
            page.mouse.down()
        try:
            for value in positions:
                if touch:
                    cdp.send('Input.dispatchTouchEvent', {'type':'touchMove','touchPoints':[dict(point(value), id=1)]})
                else:
                    page.mouse.move(**point(value))
                if refresh:
                    page.evaluate('depthOwner.sync("medium",true)')
                state = self.state(page, selector)
                self.assertAlmostEqual(state['value'], value, delta=.035, msg=str(state))
                self.assertEqual(state['step'], 'any')
                self.assertAlmostEqual(state['fill'], state['value']/5, places=6)
                self.assertEqual(state['commits'], [])
            if cancel and not touch:
                page.locator(selector).dispatch_event('pointercancel', {'pointerId':1})
            if touch:
                cdp.send('Input.dispatchTouchEvent', {'type':'touchCancel' if cancel else 'touchEnd','touchPoints':[]})
            else:
                page.mouse.up()
        finally:
            if cdp:
                cdp.detach()

    def test_identical_refresh_does_not_interrupt_a_mouse_drag(self):
        page, selector = self.open_surface(1100)
        self.gesture(page, selector, [2.2, 2.6, 3.4, 4.8], refresh=True)
        self.assertEqual(self.state(page, selector)['commits'], ['max'])
        self.assertEqual(self.state(page, selector)['value'], 5)

    def test_mouse_and_real_touch(self):
        for width in [1100, 320, 390]:
            with self.subTest(width=width):
                page, selector = self.open_surface(width)
                touch = width < 600
                self.gesture(page, selector, [2.1, 2.2, 2.35], touch)
                self.assertEqual(self.state(page, selector), {'value':2,'step':'1','fill':.4,'commits':[]})
                self.gesture(page, selector, [2.1, 2.2, 2.35, 3.2, 4.7], touch)
                self.assertEqual(self.state(page, selector), {'value':5,'step':'1','fill':1,'commits':['max']})
                for key, value in [('ArrowLeft',4),('Home',0),('End',5),('ArrowLeft',4)]:
                    before = len(self.state(page, selector)['commits'])
                    page.locator(selector).focus()
                    page.keyboard.press(key)
                    state = self.state(page, selector)
                    self.assertEqual(state['value'],value)
                    self.assertEqual(len(state['commits']), before+1)
                    self.assertEqual(state['step'],'1')
                page.evaluate('depthOwner.sync("medium",true);depthCommits.length=0')
                self.gesture(page, selector, [2.35,3.2], touch, cancel=True)
                self.assertEqual(self.state(page, selector), {'value':2,'step':'1','fill':.4,'commits':[]})
                # TouchCancel has no later pointerup: keyboard must recover immediately.
                page.locator(selector).focus()
                page.keyboard.press('ArrowRight')
                self.assertEqual(self.state(page, selector), {'value':3,'step':'1','fill':.6,'commits':['high']})
                page.evaluate('depthOwner.sync("medium",false)')

                self.assertTrue(page.locator(selector).is_disabled())
                self.assertFalse(page.locator(selector).is_visible())

    def test_dynamic_authority_and_saved_intent(self):
        for width in [1100, 320]:
            page, selector = self.open_surface(width)
            page.evaluate('depthOwner.sync("lowest", {levels:["default","lowest","high"]})')
            self.assertEqual(page.locator(selector).input_value(), '0')
            self.assertEqual(page.locator(selector).get_attribute('max'), '1')
            page.evaluate('depthOwner.sync("max", {levels:["default"],visible:false})')
            self.assertTrue(page.locator(selector).is_visible())
            self.assertIn('Unavailable', page.locator(selector).get_attribute('aria-valuetext'))
            self.assertEqual(page.evaluate('depthCommits'), [])

            page.locator(selector).locator('xpath=../..').screenshot(path=str(self.artifacts/f'{width}-unavailable.png'))
            page.locator(selector).focus()
            page.keyboard.press('Home')
            self.assertTrue(page.locator(selector).is_disabled())
            self.assertEqual(page.evaluate('depthCommits'), [])
            self.assertTrue(page.locator(selector).is_visible())
            page.context.close()

    def test_server_restoration_table_does_not_rewrite_saved_intent(self):
        from lib.model_routing.controls import reasoning_controls
        from tests.model_routing.test_model_reasoning_controls import _document, _alpha
        row = _alpha(reasoning_controls(_document()))['']
        for width in [1100, 320]:
            page, selector = self.open_surface(width)
            page.evaluate('''row => {
              depthOwner.destroy(); depthCommits.length=0;
              const options=exports.projectReasoningControl([row],row);
              window.depthOwner=exports.createThinkingDepthSlider({document,
                translate:k=>depthLocale[k]||k,
                commit:d=>{depthCommits.push(d);depthOwner.sync(d,options);}});
              depthOwner.sync('ultra',options);
              depthOwner.sync('ultra',options);
            }''', row)
            self.assertEqual(page.evaluate('depthCommits'), [])
            # The server table maps 'ultra' onto 'high' (intersection's
            # richest rung below it, native index 1 in ['low','high']): the
            # ladder is NOT inflated, the thumb parks on 'high' and the label
            # keeps the saved intent, flagged.
            self.assertEqual(page.locator(selector).get_attribute('max'), '1')
            self.assertEqual(page.locator(selector).input_value(), '1')
            self.assertIn('Unavailable', page.locator(selector).get_attribute('aria-valuetext'))
            page.locator(selector).focus()
            page.keyboard.press('ArrowLeft')
            self.assertEqual(page.evaluate('depthCommits'), ['low'])
            page.locator(selector).locator('xpath=../..').screenshot(path=str(self.artifacts/f'{width}-server-restoration.png'))
            page.context.close()

    def test_responsive_themes_locales(self):
        for width in [1100,320,390]:
            for theme in ['tofu','light','dark']:
                for lang in ['en','zh']:
                    with self.subTest(width=width,theme=theme,lang=lang):
                        page, selector = self.open_surface(width,theme,lang)
                        labels = json.loads((ROOT / 'frontend/src/i18n/locales/en.json').read_text())
                        for level in ['off','medium','max']:
                            page.evaluate('level=>depthOwner.sync(level,true)',level)
                            result=page.locator(selector).evaluate('''el=>{
                              const card=el.closest('[data-thinking-depth-slider]'),rail=el.parentElement;
                              const r=rail.getBoundingClientRect(),b=el.getBoundingClientRect(),c=card.getBoundingClientRect();
                              const track=getComputedStyle(rail,'::before');
                              const th=parseFloat(getComputedStyle(el).getPropertyValue('--depth-thumb-size'));
                              const center=b.x+th/2+(b.width-th)*(+el.value/5);
                              const value=card.querySelector('[data-depth-value]');
                              const texts=[card.querySelector('label'),value];
                              return {height:parseFloat(track.height),trackHeight:parseFloat(getComputedStyle(rail).getPropertyValue('--depth-track-height')),
                                inside:center-th/2>=r.left-0.5&&center+th/2<=r.right+0.5,
                                unclipped:texts.every(t=>{const b=t.getBoundingClientRect();return b.left>=c.left&&b.right<=c.right&&b.left>=0&&b.right<=innerWidth&&t.scrollWidth<=t.clientWidth+1}),
                                valueText:value.textContent,
                                children:[...card.children].filter(x=>!x.hidden).map(x=>x.className),outputs:card.querySelectorAll('output,[role="status"]:not([hidden])').length};
                            }''')
                            self.assertEqual(result['height'],result['trackHeight'])
                            self.assertTrue(result['inside'],result)
                            self.assertTrue(result['unclipped'],result)
                            self.assertEqual(result['valueText'],labels['mobile.'+level],result)
                            self.assertEqual(result['outputs'],0)
                            self.assertEqual(result['children'],['depth-slider-heading','depth-slider-rail'])
                        if theme=='tofu' and lang=='en':
                            page.locator(selector).locator('xpath=../..').screenshot(path=str(self.artifacts/f'{width}-{theme}-{lang}.png'))
                        page.evaluate('depthOwner.destroy()')
                        page.context.close()


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-q']))
