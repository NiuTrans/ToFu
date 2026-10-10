"""Real Vite CSS preload transaction and typed dialog lifecycle; no application writes."""
import json
import shutil
import subprocess
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from tests._runtime_sections import ROOT

pytestmark = pytest.mark.visual


@pytest.fixture(scope='module')
def dialog_origin(tmp_path_factory):
    directory = tmp_path_factory.mktemp('lazy-dialog-css')
    output = directory / 'site'
    entry = directory / 'entry.ts'
    entry.write_text('import { createDialogServices } from ' + json.dumps(str(ROOT / 'frontend/src/lazy-dialog-controller.ts')) + ';\n' + """
      window.dialogWarnings=[];
      const schedule=Object.fromEntries(['setTimeout','clearTimeout','setInterval','clearInterval','requestAnimationFrame','cancelAnimationFrame']
        .map(name=>[name,window[name].bind(window)]));
      const services=createDialogServices({document,schedule,copy:{confirm:()=> 'Confirm',cancel:()=> 'Cancel',ok:()=> 'OK'},
        log:{warn:message=>window.dialogWarnings.push(message)}});
      window.openConfirmation=()=>{window.dialogResult='pending';void services.showConfirm('Read-only browser fixture',
        {title:'Review confirmation'}).then(value=>window.dialogResult=value);};
      window.destroyDialogs=()=>services.destroy();
    """)
    build = directory / 'build.mjs'
    build.write_text('import { build } from ' + json.dumps(str(ROOT / 'node_modules/vite/dist/node/index.js')) + ';\n' +
                     'import { existsSync } from "node:fs";\n' +
                     'await build(' + json.dumps({'configFile': False, 'root': str(directory), 'logLevel': 'warn',
                      'build': {'outDir': str(output), 'cssCodeSplit': True, 'manifest': True,
                                'rollupOptions': {'input': str(entry)}}})[:-1] +
                     ',plugins:[{name:"fixture-absolute-imports",resolveId(id){if(id.startsWith("/")&&existsSync(id))return id;}}]});\n')
    proc = subprocess.run(['node', str(build)], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    manifest = json.loads((output / '.vite/manifest.json').read_text())
    main = next(row for row in manifest.values() if row.get('isEntry'))
    controller = next(row for name, row in manifest.items() if name.endswith('frontend/src/dialog-controller.ts'))
    assert len(controller['css']) == 1
    css_path = '/' + controller['css'][0]
    assert not main.get('css'), 'Dialog CSS must stay outside the entry graph.'
    shutil.copyfile(ROOT / 'static/styles.css', output / 'base.css')
    shutil.copyfile(ROOT / 'static/settings.css', output / 'settings.css')
    assert '.app-dialog-overlay' not in (output / 'base.css').read_text()
    (output / 'index.html').write_text('<!doctype html><html data-theme="light"><head><link rel="stylesheet" href="/base.css">'
      '<link rel="stylesheet" href="/settings.css"></head><body><button id="before">Before dialog</button>'
      '<div class="stg-v2-manager-overlay">Settings layer</div><script type="module" src="/' + main['file'] + '"></script></body></html>')

    class QuietHandler(SimpleHTTPRequestHandler):
        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(QuietHandler, directory=str(output)))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield {'url': f'http://127.0.0.1:{server.server_port}', 'css': css_path}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize('theme', ['light', 'dark', 'tofu'])
def test_dialog_css_loads_once_on_intent_before_styled_focused_overlay(browser, dialog_origin, theme):
    from playwright.sync_api import expect
    page = browser.new_page(viewport={'width': 390, 'height': 900})
    requested = []
    errors = []
    page.on('request', lambda request: requested.append(request.url))
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        page.goto(dialog_origin['url'], wait_until='load')
        page.wait_for_function('typeof openConfirmation === "function"')
        page.evaluate('theme=>document.documentElement.dataset.theme=theme', theme)
        assert not [url for url in requested if url.endswith(dialog_origin['css'])]
        page.evaluate('openConfirmation()')
        overlay = page.locator('.app-dialog-overlay')
        expect(overlay).to_be_visible()
        confirm = overlay.get_by_role('button', name='Confirm', exact=True)
        expect(confirm).to_be_focused()
        assert overlay.evaluate('(el)=>getComputedStyle(el).position') == 'fixed'
        assert overlay.evaluate('(el)=>Number(getComputedStyle(el).zIndex)>Number(getComputedStyle(document.querySelector(".stg-v2-manager-overlay")).zIndex)')
        assert confirm.evaluate('(el)=>document.elementFromPoint(el.getBoundingClientRect().x+el.clientWidth/2,el.getBoundingClientRect().y+el.clientHeight/2).closest(".app-dialog-overlay")===el.closest(".app-dialog-overlay")')
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
        confirm.press('Enter')
        page.wait_for_function('dialogResult === true')
        expect(overlay).to_have_count(0)
        page.evaluate('openConfirmation()')
        expect(overlay).to_be_visible()
        page.evaluate('destroyDialogs()')
        page.wait_for_function('dialogResult === false')
        expect(overlay).to_have_count(0)
        assert len([url for url in requested if url.endswith(dialog_origin['css'])]) == 1
        assert not errors
    finally:
        page.close()


def test_failed_dialog_css_cannot_be_bypassed_by_second_activation(browser, dialog_origin):
    from playwright.sync_api import expect
    page = browser.new_page()
    attempts = []
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))

    def fail_styles(route):
        attempts.append(route.request.url)
        route.fulfill(status=503, content_type='text/css', body='Unavailable dialog stylesheet')

    page.route('**' + dialog_origin['css'], fail_styles)
    try:
        page.goto(dialog_origin['url'], wait_until='load')
        page.wait_for_function('typeof openConfirmation === "function"')
        for _ in range(2):
            page.evaluate('openConfirmation()')
            page.wait_for_function('dialogResult === false')
            expect(page.locator('.app-dialog-overlay')).to_have_count(0)
        assert len(attempts) == 1
        assert page.evaluate('dialogWarnings.length') == 2
        assert not errors
        page.evaluate('destroyDialogs()')
    finally:
        page.close()
