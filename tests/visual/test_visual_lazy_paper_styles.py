"""Actual Paper module/CSS transactions, shared activation order and failed preload."""
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
def paper_style_origin(tmp_path_factory):
    directory = tmp_path_factory.mktemp('lazy-paper-css')
    output = directory / 'site'
    entry = directory / 'entry.ts'
    source = ROOT / 'frontend/src'
    entry.write_text('import { createFeatureModuleCache } from ' + json.dumps(str(source / 'core/feature-module-cache.ts')) + ';\n' +
        'const readerLoader=()=>import(' + json.dumps(str(source / 'features/paper/pdf-reader.ts')) + ');\n' +
        'const controlsLoader=()=>import(' + json.dumps(str(source / 'features/paper/reader-controls.ts')) + ');\n' +
        'const previewLoader=()=>import(' + json.dumps(str(source / 'features/paper/preview-panel.ts')) + ');\n' +
        'const researchLoader=()=>import(' + json.dumps(str(source / 'features/evolution/research/components/manuscript-preview.ts')) + ');\n' + """
        const load=createFeatureModuleCache();
        window.activation='idle';window.apiCalls=0;window.failures=[];
        const tr=key=>key;
        const api=new Proxy({}, {get(){return ()=>{window.apiCalls++;throw Error('Unexpected API call');};}});
        let reader=null,panel=null,manuscript=null,controlsSignal=null;
        window.destroyPaper=()=>{
          panel?.destroy(true);panel=null;reader?.destroy();reader=null;manuscript?.destroy();manuscript=null;
          controlsSignal?.abort();controlsSignal=null;
          document.querySelector('#shared').replaceChildren();
        };
        window.openPaper=async(kind)=>{
          window.activation='pending';
          try {
            if(kind==='reader') {
              const [core,chrome]=await Promise.all([load(readerLoader),load(controlsLoader)]);
              const host=document.querySelector('#shared');
              const viewport=document.createElement('div');viewport.className='paper-preview-viewport';
              viewport.style.height='240px';reader=core.createPdfReader(viewport);
              controlsSignal=new AbortController();
              const navigation=new chrome.ReaderNavigation(()=>reader,tr,controlsSignal.signal);
              const toolbar=document.createElement('div');toolbar.className='paper-preview-toolbar';
              toolbar.append(navigation.element);host.append(toolbar,viewport);
            } else if(kind==='research') {
              const {ManuscriptPreview}=await load(researchLoader);
              manuscript=new ManuscriptPreview(document.querySelector('#research'),{api,translate:tr});
            } else {
              const {PaperPreviewPanel}=await load(previewLoader);
              panel=new PaperPreviewPanel({host:document.querySelector('#chat'),api,translate:tr,
                close:()=>{panel?.destroy(true);panel=null;},refresh:()=>{},
                focusBefore:document.querySelector('#before')});
            }
            window.activation='ready';
          } catch(error) {window.failures.push(String(error));window.activation='failed';}
        };
    """)
    build = directory / 'build.mjs'
    build.write_text('import { build } from ' + json.dumps(str(ROOT / 'node_modules/vite/dist/node/index.js')) + ';\n' +
        'import { existsSync } from "node:fs";\nawait build(' +
        json.dumps({'configFile': False, 'root': str(directory), 'logLevel': 'warn',
                    'build': {'outDir': str(output), 'cssCodeSplit': True, 'manifest': True,
                              'rollupOptions': {'input': str(entry)}}})[:-1] +
        ',plugins:[{name:"fixture-absolute-imports",resolveId(id){if(id.startsWith("/")&&existsSync(id))return id;}}]});\n')
    proc = subprocess.run(['node', str(build)], cwd=ROOT, capture_output=True, text=True, timeout=90)
    assert proc.returncode == 0, proc.stderr
    manifest = json.loads((output / '.vite/manifest.json').read_text())
    main = next(row for row in manifest.values() if row.get('isEntry'))
    assert not main.get('css'), 'Optional reader/preview CSS must stay outside the entry graph.'
    # Only static dependencies of these components; editors/math remain lazy.
    graph = set()
    def visit(name):
        if name in graph:
            return
        graph.add(name)
        for dependency in manifest[name].get('imports', []):
            visit(dependency)
    for name in manifest:
        if name.endswith(('features/paper/preview-panel.ts', 'features/paper/reader-controls.ts',
                          'features/evolution/research/components/manuscript-preview.ts')):
            visit(name)
    css_paths = ['/' + name for name in sorted({css for name in graph for css in manifest[name].get('css', [])})]
    assert len(css_paths) >= 3, 'PDF geometry, shared controls and preview details need distinct owners.'
    preview_css = ['/' + css for name, row in manifest.items()
                   if name.endswith('features/paper/preview-panel.ts') for css in row.get('css', [])]
    geometry = next(path for path in css_paths if '.paper-reader-surface' in (output / path[1:]).read_text())
    shutil.copyfile(ROOT / 'static/styles.css', output / 'base.css')
    assert '.paper-reader-surface' not in (output / 'base.css').read_text()
    (output / 'index.html').write_text('<!doctype html><html data-theme="light"><head><meta charset="utf-8">'
        '<link rel="stylesheet" href="/base.css"></head><body style="display:block;height:auto;overflow:auto"><button id="before">Before reader</button>'
        '<section id="shared" class="paper-preview-panel"></section><section id="research" style="height:300px;width:100%"></section>'
        '<div id="chat" class="chat-wrapper" style="height:600px;width:100%;position:relative">'
        '<div class="chat-container">Conversation</div><div class="input-area"><textarea></textarea></div></div>'
        '<script type="module" src="/' + main['file'] + '"></script></body></html>')

    class QuietHandler(SimpleHTTPRequestHandler):
        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(QuietHandler, directory=str(output)))
    server.daemon_threads = True
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield {'url': f'http://127.0.0.1:{server.server_port}', 'css': css_paths, 'geometry': geometry, 'preview_css': preview_css}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize('first', ['reader', 'preview', 'research'])
@pytest.mark.parametrize('theme,width', [('light', 1180), ('dark', 390), ('tofu', 1180)])
def test_paper_css_is_awaited_once_for_each_shared_activation_order(browser, paper_style_origin, first, theme, width):
    from playwright.sync_api import expect
    page = browser.new_page(viewport={'width': width, 'height': 900})
    requests, errors = [], []
    page.on('request', lambda request: requests.append(request.url))
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        page.goto(paper_style_origin['url'], wait_until='load')
        page.wait_for_function('typeof openPaper === "function"')
        page.evaluate('theme=>document.documentElement.dataset.theme=theme', theme)
        assert not [url for url in requests if any(url.endswith(path) for path in paper_style_origin['css'])]
        preview_loaded = False
        for kind in [first, 'preview' if first != 'preview' else 'reader', 'research', 'preview']:
            page.locator('#before').focus()
            page.evaluate('kind=>openPaper(kind)', kind)
            assert page.evaluate('activation') == 'ready'
            preview_loaded = preview_loaded or kind == 'preview'
            if not preview_loaded:
                assert not [url for url in requests if any(url.endswith(path) for path in paper_style_origin['preview_css'])]
            owner = {'preview': '#paperPreviewPanel', 'reader': '#shared', 'research': '#researchManuscriptReader'}[kind]
            viewport = page.locator(owner + ' .paper-reader')
            assert viewport.evaluate('(el)=>getComputedStyle(el).overflowY') == 'auto'
            toolbar = page.locator(owner + ' .paper-preview-toolbar')
            assert toolbar.evaluate('(el)=>getComputedStyle(el).display') == 'flex'
            if kind == 'preview':
                expect(page.locator('#paperPreviewPanel')).to_be_visible()
                page.locator('#paperPreviewPanel').evaluate('(el)=>{el.tabIndex=-1;el.focus();}')
                page.locator('#paperPreviewPanel').press('Escape')
                expect(page.locator('#paperPreviewPanel')).to_have_count(0)
                expect(page.locator('#before')).to_be_focused()
                assert not page.locator('#chat').evaluate('(el)=>el.classList.contains("paper-preview-open")')
            page.evaluate('destroyPaper()')
        assert all(len([url for url in requests if url.endswith(path)]) == 1 for path in paper_style_origin['css'])
        assert page.evaluate('apiCalls') == 0
        assert page.evaluate('failures') == []
        assert not errors
    finally:
        page.close()


@pytest.mark.parametrize('kind', ['reader', 'preview', 'research'])
def test_failed_shared_pdf_css_stays_failed_before_component_construction(browser, paper_style_origin, kind):
    from playwright.sync_api import expect
    page = browser.new_page()
    attempts, errors = [], []
    page.on('pageerror', lambda error: errors.append(str(error)))

    def fail_style(route):
        attempts.append(route.request.url)
        route.fulfill(status=503, content_type='text/css', body='Unavailable shared reader styles')

    page.route('**' + paper_style_origin['geometry'], fail_style)
    try:
        page.goto(paper_style_origin['url'], wait_until='load')
        page.wait_for_function('typeof openPaper === "function"')
        for _ in range(2):
            page.evaluate('kind=>openPaper(kind)', kind)
            assert page.evaluate('activation') == 'failed'
            expect(page.locator('.paper-reader')).to_have_count(0)
            expect(page.locator('#paperPreviewPanel')).to_have_count(0)
            expect(page.locator('#researchManuscriptReader')).to_have_count(0)
        assert len(attempts) == 1
        assert page.evaluate('failures.length') == 2
        assert page.evaluate('apiCalls') == 0
        assert not errors
    finally:
        page.close()
