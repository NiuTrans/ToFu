"""Real report article navigation, glossary/figure keyboard paths and teardown."""
import functools
import http.server
import json
import shutil
import subprocess
import threading

import pytest
from tests._runtime_sections import ROOT

pytestmark = pytest.mark.visual


@pytest.fixture(scope='module')
def article_origin(tmp_path_factory):
    directory = tmp_path_factory.mktemp('report-article-browser')
    entry = directory / 'entry.ts'
    entry.write_text('import * as owner from ' + json.dumps(str(ROOT / 'frontend/src/features/paper/report-article/index.ts')) + ';\n' + r"""
      window.zoomCalls=[];
      const ports={openImage:source=>window.zoomCalls.push(source),reducedMotion:()=>true};
      window.mountArticle=(id)=>{
        const host=document.getElementById(id);owner.releaseReportArticle(host);
        const article=document.createElement('article');article.className='paper-report-article';
        article.innerHTML=`<table><thead><tr><th>Term</th><th>Definition</th></tr></thead><tbody>
          <tr><td>Evidence</td><td><strong>Measured facts</strong> with preserved context.</td></tr></tbody></table>
          <h2 id="same">Introduction</h2><p>Evidence supports this result.</p><blockquote>Key takeaway: retain the reading context.</blockquote>
          <p><img alt="Local fixture diagram" src="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='240' height='100'%3E%3Crect width='240' height='100' fill='%2394a3b8'/%3E%3C/svg%3E"></p>
          <p>${'Long reading paragraph. '.repeat(100)}</p><h2>Method</h2><p>${'Measured evidence. '.repeat(60)}</p><h2>Result</h2><p>Evidence is retained.</p>`;
        const entries=owner.enhanceReportArticle(article,()=> 'Enlarge image');
        const doc=document.createElement('div');doc.className='paper-report-doc';
        doc.innerHTML=owner.buildReportTableOfContents(entries,'Contents');doc.append(article);
        host.replaceChildren(doc);owner.mountReportArticle(host,article,ports);
      };
      window.disposeArticles=()=>owner.destroyReportArticles();
      window.remountMany=()=>{for(let i=0;i<40;i++)window.mountArticle('one');};
      window.mountArticle('one');window.mountArticle('two');window.ready=true;
    """)
    result = subprocess.run(['node', 'scripts/vite_test_bundle.mjs', str(entry), '--bundle', '--format=iife',
                             '--platform=browser', '--outfile=' + str(directory / 'article.js')],
                            cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    shutil.copyfile(ROOT / 'static/styles.css', directory / 'styles.css')
    (directory / 'index.html').write_text('''<!doctype html><html data-theme="light"><meta charset="utf-8">
      <link rel="stylesheet" href="/styles.css"><body style="display:block;height:auto;overflow:auto">
      <main><section id="one" class="paper-report-content" style="height:420px;overflow:auto"></section>
      <section id="two" class="paper-report-content" style="height:420px;overflow:auto"></section></main>
      <script>
      window.liveObservers=0;window.documentListeners=[];
      const Native=window.IntersectionObserver;
      window.IntersectionObserver=class extends Native {
        constructor(...args){super(...args);this.closed=false;window.liveObservers++;}
        disconnect(){super.disconnect();if(!this.closed){this.closed=true;window.liveObservers--;}}
      };
      const listen=document.addEventListener.bind(document);
      document.addEventListener=(type,...args)=>{window.documentListeners.push(type);return listen(type,...args);};
      </script><script src="/article.js"></script></body></html>''')

    class QuietHandler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *_args):
            pass

    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(QuietHandler, directory=str(directory)))
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}'
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize('theme', ['light', 'dark', 'tofu'])
@pytest.mark.parametrize('width', [390, 1180])
def test_article_reading_actions_are_scoped_accessible_and_released(browser, article_origin, theme, width, tmp_path):
    from playwright.sync_api import expect
    page = browser.new_page(viewport={'width': width, 'height': 1000}, reduced_motion='reduce')
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        page.goto(article_origin, wait_until='load')
        page.wait_for_function('window.ready === true')
        page.evaluate('theme=>document.documentElement.dataset.theme=theme', theme)
        term = page.locator('#one .paper-term').first
        term.focus()
        expect(term).to_be_focused()
        expect(term.locator('.paper-term-card')).to_be_visible()
        assert term.get_attribute('aria-label') == 'Evidence: Measured facts with preserved context.'
        bounds = term.evaluate('''el=>{const card=el.querySelector('.paper-term-card').getBoundingClientRect(),
          host=el.closest('.paper-report-content').getBoundingClientRect();return {left:card.left,right:card.right,min:host.left,max:host.right}}''')
        assert bounds['left'] >= bounds['min'] and bounds['right'] <= bounds['max']
        image = page.locator('#one figure img')
        expect(image).to_have_attribute('role', 'button')
        image.focus(); image.press('Enter'); image.press('Space')
        assert page.evaluate('zoomCalls.length') == 2
        assert page.locator('#one .paper-callout-takeaway').count() == 1
        if width == 1180:
            page.evaluate("document.getElementById('one').scrollTop=0;document.getElementById('two').scrollTop=0")
            link = page.locator('#two .paper-report-toc').get_by_role('link', name='Method', exact=True)
            link.focus(); link.press('Enter')
            expect(page.locator('#two h2').filter(has_text='Method')).to_be_focused()
            assert page.evaluate("document.getElementById('two').scrollTop>0 && document.getElementById('one').scrollTop===0")
        assert page.evaluate('liveObservers') == 2
        page.evaluate('remountMany()')
        assert page.evaluate('liveObservers') == 2
        assert not page.evaluate("documentListeners.filter(type=>['mouseover','focusin','click','keydown'].includes(type))")
        page.screenshot(path=str(tmp_path / f'article-{theme}-{width}.png'))
        page.evaluate('disposeArticles()')
        assert page.evaluate('liveObservers') == 0
        page.locator('#one figure img').focus(); page.locator('#one figure img').press('Enter')
        assert page.evaluate('zoomCalls.length') == 2
        assert not errors
    finally:
        page.close()
