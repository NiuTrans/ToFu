"""Offline authored-catalog Chromium contract; no server, credentials or live feed.
Run: python tests/providers/test_provider_template_picker_browser.py
Installed Playwright/TypeScript only. Two screenshots survive in a unique /tmp dir.
"""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import pytest

pytestmark = pytest.mark.visual
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


class ProviderTemplatePickerBrowser(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from chromium_env import ensure_chromium_env
        from playwright.sync_api import sync_playwright
        ensure_chromium_env()
        cls.pw = sync_playwright().start()
        try:
            cls.browser = cls.pw.chromium.launch(headless=True, args=['--no-sandbox', '--disable-gpu'])
        except Exception:
            cls.pw.stop()
            raise
        cls.addClassCleanup(cls.pw.stop)
        cls.addClassCleanup(cls.browser.close)
        cls.artifacts = Path(tempfile.mkdtemp(prefix='tofu-provider-picker-browser-'))
        # Read authored JSON directly: never import the live feed or contact providers.
        catalog = {}
        for directory in ['lib/model_info/data', 'static/provider_templates']:
            for path in sorted((ROOT / directory).glob('*.json')):
                row = json.loads(path.read_text())
                if isinstance(row, dict) and row.get('key') and 'offering_recipes' in row:
                    catalog[row['key']] = row
        cls.catalog = list(catalog.values())
        cls.css = '\n'.join(path.read_text() for path in sorted(
            (ROOT / 'frontend/src/styles/application').glob('*.css')))
        cls.css += '\n' + (ROOT / 'frontend/src/styles/settings/06-model-routing-v2.css').read_text()
        cls.script = subprocess.check_output(['node', '-e', "const ts=require('typescript'),fs=require('fs');process.stdout.write(ts.transpileModule(fs.readFileSync('frontend/src/features/settings/provider-template-picker.ts','utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText)"], cwd=ROOT, text=True)
        cls.icons_script = subprocess.check_output(['node', '-e', "const ts=require('typescript'),fs=require('fs');process.stdout.write(ts.transpileModule(fs.readFileSync('frontend/src/core/model-brand-icons.ts','utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText)"], cwd=ROOT, text=True)
        print(f'Chromium {cls.browser.version}; authored rows={len(cls.catalog)}; screenshots: {cls.artifacts}', flush=True)

    def open_surface(self, width=1100, theme='tofu', lang='en'):
        context = self.browser.new_context(viewport={'width': width, 'height': 820},
                                          has_touch=width < 600, is_mobile=width < 600,
                                          service_workers='block')
        self.addCleanup(context.close)
        context.route('**/*', lambda route: route.abort())
        page = context.new_page()
        page.set_default_timeout(5000)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        self.addCleanup(lambda: self.assertEqual(errors, []))
        page.set_content('<html><head><meta name="viewport" content="width=device-width,initial-scale=1"></head><body><button id="open">Templates</button></body></html>')
        page.add_style_tag(content=self.css)
        locale = json.loads((ROOT / f'frontend/src/i18n/locales/{lang}.json').read_text())
        page.evaluate('''({theme,lang,catalog,locale}) => {
            document.documentElement.dataset.theme=theme;
            document.documentElement.lang=lang;
            window.catalog=catalog; window.copy=locale; window.selected=[];
        }''', dict(theme=theme, lang=lang, catalog=self.catalog, locale=locale))
        page.add_script_tag(content='(()=>{var exports={};\n' + self.icons_script + '\nwindow.brandIconHtml=exports.brandIconHtml;})();')
        page.add_script_tag(content='var exports={};\n' + self.script + '''
            window.picker=exports.createProviderTemplatePicker({document,
                translate:key=>copy[key]||key, load:async()=>catalog,
                select:key=>selected.push(key), iconHtml:window.brandIconHtml});
            document.getElementById('open').onclick=()=>picker.toggle(document.getElementById('open'));
        ''')
        page.locator('#open').click()
        page.wait_for_selector('[data-tpl-key]')
        page.wait_for_function('document.querySelector("[role=dialog]").getAttribute("aria-busy")==="false"')
        return page, locale

    def test_catalog_filter_search_keyboard_and_selection(self):
        page, copy = self.open_surface()
        search = page.locator('input[type=search]')
        self.assertTrue(search.evaluate('el=>el===document.activeElement'))
        for category in ['official', 'relay']:
            page.locator('button[data-category="all"]').click()
            heading = page.locator('.stg-provider-catalog-group h3').all_text_contents()
            self.assertTrue(any(text.startswith(copy['providerCatalog.' + category]) for text in heading), heading)
            page.locator(f'button[data-category="{category}"]').click()
            self.assertEqual(page.locator('.stg-provider-catalog-group').count(), 1)
            self.assertEqual(page.locator(f'button[data-category="{category}"]').get_attribute('aria-pressed'), 'true')
            keys = page.locator('[data-tpl-key]').evaluate_all('els=>els.map(el=>el.dataset.tplKey)')
            self.assertTrue(keys)
            self.assertTrue(all(next(row for row in self.catalog if row['key'] == key)['category'] == category for key in keys))
        page.locator('button[data-category="all"]').click()
        # A real authored provider name, then a real model identifier, not mock examples.
        row = next(row for row in self.catalog if row.get('category') == 'official' and row.get('offering_recipes'))
        search.fill(row['name'])
        self.assertTrue(page.locator(f'[data-tpl-key="{row["key"]}"]').is_visible())
        search.fill(row['offering_recipes'][0]['model_id'])
        self.assertGreater(page.locator('[data-tpl-key]').count(), 0)
        search.fill('no-such-provider-93842')
        self.assertEqual(page.locator('[data-tpl-key]').count(), 0)
        self.assertEqual(page.locator('[role=status]').inner_text(), copy['providerCatalog.noMatches'])
        search.fill('')
        focusables = page.locator('[role=dialog]').locator('button:not(:disabled), input, a[href]')
        focusables.last.focus()
        page.keyboard.press('Tab')
        self.assertTrue(focusables.first.evaluate('el=>el===document.activeElement'))
        page.keyboard.press('Shift+Tab')
        self.assertTrue(focusables.last.evaluate('el=>el===document.activeElement'))
        page.keyboard.press('Escape')
        self.assertEqual(page.locator('[role=dialog]').count(), 0)
        self.assertTrue(page.locator('#open').evaluate('el=>el===document.activeElement'))
        self.assertEqual(page.locator('#open').get_attribute('aria-expanded'), 'false')
        page.locator('#open').click()
        page.wait_for_selector('[data-tpl-key]')
        page.locator(f'[data-tpl-key="{row["key"]}"]').click()
        self.assertEqual(page.evaluate('selected'), [row['key']])
        self.assertTrue(page.locator('#open').evaluate('el=>el===document.activeElement'))

    def test_responsive_locales_and_dark_geometry(self):
        for width in [1100, 320, 390]:
            for theme in ['tofu', 'dark']:
                for lang in ['en', 'zh']:
                    with self.subTest(width=width, theme=theme, lang=lang):
                        page, copy = self.open_surface(width, theme, lang)
                        # Tiles are buttons: park the pointer off the dialog so a
                        # leftover :hover from opening never skews backgrounds.
                        page.mouse.move(2, 2)
                        self.assertEqual(page.locator('[role=dialog] h2').inner_text(), copy['providerCatalog.title'])
                        geometry = page.locator('[role=dialog]').evaluate('''el=>{
                            const rect=el.getBoundingClientRect();
                            const controls=[...el.querySelectorAll('button,input')];
                            const tiles=[...el.querySelectorAll('.stg-provider-catalog-tile')];
                            return {viewport:innerWidth,documentWidth:document.documentElement.scrollWidth,
                              left:rect.left,right:rect.right,width:el.clientWidth,scrollWidth:el.scrollWidth,
                              controlMinHeight:Math.min(...controls.map(x=>x.getBoundingClientRect().height)),
                              buttonMinHeight:Math.min(...[...el.querySelectorAll('button')].map(x=>x.getBoundingClientRect().height)),
                              controlMinWidth:Math.min(...controls.map(x=>x.getBoundingClientRect().width)),
                              tilesInside:tiles.every(x=>{const r=x.getBoundingClientRect();return r.left>=rect.left&&r.right<=rect.right}),
                              background:getComputedStyle(el).backgroundColor,

                              expectedBackground:getComputedStyle(document.documentElement).getPropertyValue('--bg-secondary').trim(),

                              expectedColor:getComputedStyle(document.documentElement).getPropertyValue('--text-primary').trim(),
                              inputBackground:getComputedStyle(el.querySelector('input')).backgroundColor,
                              tileBackground:getComputedStyle(el.querySelector('.stg-provider-catalog-tile')).backgroundColor,
                              color:getComputedStyle(el).color};
                        }''')
                        self.assertLessEqual(geometry['documentWidth'], width + 1, geometry)
                        self.assertGreaterEqual(geometry['left'], 0, geometry)
                        self.assertLessEqual(geometry['right'], width, geometry)
                        self.assertLessEqual(geometry['scrollWidth'], geometry['width'] + 1, geometry)
                        self.assertTrue(geometry['tilesInside'], geometry)
                        self.assertGreaterEqual(geometry['controlMinHeight'], 44 if width < 600 else 40, geometry)
                        self.assertGreaterEqual(geometry['buttonMinHeight'], 44 if width < 600 else 40, geometry)
                        self.assertGreaterEqual(geometry['controlMinWidth'], 44 if width < 600 else 24, geometry)
                        def rgb(hex_color):
                            return 'rgb(' + ', '.join(str(int(hex_color[i:i + 2], 16)) for i in (1, 3, 5)) + ')'
                        self.assertEqual(geometry['background'], rgb(geometry['expectedBackground']), geometry)
                        self.assertEqual(geometry['color'], rgb(geometry['expectedColor']), geometry)
                        self.assertEqual(geometry['inputBackground'], geometry['background'], geometry)
                        self.assertEqual(geometry['tileBackground'], 'rgba(0, 0, 0, 0)', geometry)
                        self.assertEqual(page.locator('[data-tpl-key] .stg-brand-icon svg').count(),
                                         page.locator('.stg-provider-catalog-tile[data-tpl-key]').count())
                        self.assertNotEqual(geometry['background'], geometry['color'], geometry)
                        if (width, theme, lang) in [(1100, 'tofu', 'en'), (320, 'dark', 'zh')]:
                            path = self.artifacts / f'{width}-{theme}-{lang}.png'
                            page.screenshot(path=str(path))
                            print(f'SCREENSHOT {path}; geometry={json.dumps(geometry)}', flush=True)
                        if width < 600:
                            page.locator('button[data-category="relay"]').tap()
                            self.assertEqual(page.locator('button[data-category="relay"]').get_attribute('aria-pressed'), 'true')
                        page.keyboard.press('Escape')
                        self.assertTrue(page.locator('#open').evaluate('el=>el===document.activeElement'))
                        context = page.context
                        context.close()


if __name__ == '__main__':
    unittest.main(verbosity=2, failfast=True)
