"""Real Chromium MRU contract served over local HTTP without backend storage."""
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import threading

import pytest

pytestmark = pytest.mark.visual
ROOT = Path(__file__).resolve().parents[2]


def test_recent_models_shared_tabs_and_reopen(browser, tmp_path):
    from playwright.sync_api import expect

    bundle = tmp_path / 'picker.js'
    sources = ['application-shell/model-picker/index.ts',
               'core/recent-models.ts', 'core/model-display-fold.ts']
    subprocess.run(['node', str(ROOT / 'scripts/vite_test_bundle.mjs'),
                    '--bundle', '--format=iife', '--global-name=PickerOwners',
                    '--loader=ts', '--sourcefile=owners.ts', f'--outfile={bundle}'],
                   input='\n'.join(f'export * from {str(ROOT / "frontend/src" / source)!r};'
                                   for source in sources),
                   cwd=ROOT, text=True, capture_output=True, check=True)
    (tmp_path / 'index.html').write_text('''<!doctype html><html><body>
<button id="toggle">Models</button><div id="presetWrapper">
<div id="presetDropdownSearch"><input id="presetDropdownSearchInput"></div>
<div id="presetDropdownList"></div></div><script src="picker.js"></script>
<script>
const recents = PickerOwners.createRecentModelsController({resolveStorage:()=>localStorage});
let selection = {modelId:'model-0', preferredProviderId:'', modelRef:null};
const picker = PickerOwners.createModelPicker({window, document,
 list:document.getElementById('presetDropdownList'), selection:()=>selection,
 hiddenModels:()=>new Set(), isChatModel:()=>true, groupKey:()=> 'models',
 groupLabel:()=> 'Models', fold:PickerOwners.modelDisplayUnits,
 recentIds:recents.recentModels, shortName:id=>id, brand:()=> 'generic',
 icon:()=> '', text:key=>key, openSettings:()=>{},
 select:model=>{selection={...selection,modelId:model.model_id};
   recents.pushRecentModel(model.model_id);picker.close();}});
picker.populate(Array.from({length:12},(_,i)=>({model_id:'model-'+i})));
document.getElementById('toggle').onclick=event=>picker.toggle(event);
</script></body></html>''', encoding='utf-8')
    server = ThreadingHTTPServer(('127.0.0.1', 0),
                                partial(SimpleHTTPRequestHandler, directory=str(tmp_path)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    context = browser.new_context(service_workers='block')
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    url = f'http://127.0.0.1:{server.server_port}'
    recent = '#presetDropdownList .preset-dropdown-item[data-section=\'["recent"]\']'
    try:
        page.goto(url)
        page.locator('#toggle').click()
        page.locator('[data-value="model-3"] button').click()
        page.locator('#toggle').click()
        expect(page.locator(recent).first).to_have_attribute('data-value', 'model-3')
        expect(page.locator(recent).first).to_have_attribute('class', 'preset-dropdown-item active')
        # An unchanged reopen keeps the existing row node.
        page.evaluate('window.savedRecent = document.querySelector(".preset-dropdown-item")')
        page.locator('#toggle').click()
        page.locator('#toggle').click()
        assert page.evaluate('savedRecent === document.querySelector(".preset-dropdown-item")')
        page.locator('#toggle').click()
        other = context.new_page()
        other.goto(url)
        other.locator('#toggle').click()
        other.locator('[data-value="model-8"] button').click()
        page.locator('#toggle').click()
        expect(page.locator(recent).first).to_have_attribute('data-value', 'model-8')
        expect(page.locator(recent).nth(1)).to_have_attribute('data-value', 'model-3')
        page.reload()
        expect(page.locator(recent).first).to_have_attribute('data-value', 'model-8')
        assert not errors, errors
    finally:
        context.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
