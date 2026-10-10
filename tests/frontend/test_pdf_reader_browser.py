"""Real PDF.js journeys: viewport residency, text, navigation and teardown."""
from __future__ import annotations

import functools
import http.server
import json
import shutil
import subprocess
import threading
from pathlib import Path

import pytest
from tests._runtime_sections import _require_frontend_bundler

pytestmark = pytest.mark.visual
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def reader_page(tmp_path):
    playwright = pytest.importorskip('playwright.sync_api')
    fitz = pytest.importorskip('fitz')
    from chromium_env import chromium_executable, ensure_chromium_env
    executable = chromium_executable()
    if not executable:
        pytest.skip('Chromium is unavailable')
    ensure_chromium_env()
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is unavailable')
    # Shared dep guard: skip on dep-less lanes, fail under
    # TOFU_REQUIRE_FRONTEND=1 (was an unguarded bundler call).
    _require_frontend_bundler()
    subprocess.run([node, 'scripts/vite_test_bundle.mjs', 'frontend/src/features/paper/pdf-reader.ts',
                    '--bundle', '--format=iife', '--platform=browser', '--global-name=Reader',
                    f'--outfile={tmp_path / "reader.js"}'], cwd=ROOT, check=True, capture_output=True)
    for source, target in [('build/pdf.mjs', 'pdf.mjs'), ('build/pdf.worker.mjs', 'pdf.worker.mjs')]:
        shutil.copyfile(ROOT / 'node_modules/pdfjs-dist/legacy' / source, tmp_path / target)
    # Isolated reader delivery shares the application's token vocabulary.
    styles = ROOT / 'frontend/src/styles/application'
    (tmp_path / 'reader.css').write_text((styles / '00-design-tokens.css').read_text() +
                                        (styles / '03-pdf-reader.css').read_text())
    document = fitz.open()
    for number in range(1, 21):
        page = document.new_page(width=612, height=792)
        page.insert_text((56, 64), f'Chapter {number}: Reliable paper reading', fontsize=19)
        for line in range(26):
            page.insert_text((56, 110 + line * 20), f'Page {number}. A selectable paragraph with searchable evidence.', fontsize=11)
        page.insert_link({'kind': fitz.LINK_URI, 'from': fitz.Rect(56, 65, 280, 88), 'uri': 'https://example.com/paper'})
    document.set_toc([[1, 'First chapter', 1], [1, 'Later chapter', 15]])
    document.save(tmp_path / 'paper.pdf')
    document.close()
    document = fitz.open()
    for number in range(500):
        # Delayed discovery of both wider and taller pages must preserve position.
        width, height = (900, 600) if number % 3 == 1 else (612, 1000 if number % 3 == 2 else 792)
        page = document.new_page(width=width, height=height)
        page.insert_text((56, 64), f'Page {number + 1}. Mixed page dimensions.', fontsize=19)
    document.save(tmp_path / 'long.pdf')
    document.close()
    document = fitz.open()
    for number in range(2):
        page = document.new_page(width=612, height=792)
        page.insert_text((48, 46), 'Reliable Evidence in Scientific Workflows', fontname='tibo', fontsize=17)
        page.insert_text((48, 69), 'Synthetic browser fixture for two-column paper reading', fontname='tiro', fontsize=10)
        for side, x in [('Left', 48), ('Right', 328)]:
            page.insert_text((x, 108), f'{number + 1}. {side} column', fontname='tibo', fontsize=12)
            for line in range(37):
                page.insert_text((x, 130 + line * 16),
                    f'{side} evidence {line + 1}: measure the reading position.', fontname='tiro', fontsize=10.5)
        page.insert_text((300, 765), str(number + 1), fontname='tiro', fontsize=10)
    document.save(tmp_path / 'columns.pdf')
    document.close()
    (tmp_path / 'index.html').write_text('''<!doctype html><html lang="en"><meta charset="utf-8">
      <link rel="icon" href="data:,"><link rel="stylesheet" href="reader.css">
      <style>body{margin:0;background:#eee;font-family:Arial}#reader{width:620px;height:880px;max-width:100vw}
      #second{width:390px;height:500px;position:absolute;right:0;top:0}</style>
      <div id="reader" aria-label="Paper"></div><div id="second" aria-label="Second paper"></div>
      <script src="reader.js"></script><script type="module">
      import * as pdfjs from './pdf.mjs'; pdfjs.GlobalWorkerOptions.workerSrc='/pdf.worker.mjs';
      window.failures=[];window.states=[];
      window.reader=Reader.createPdfReader(document.querySelector('#reader'),{
        loadPdfJs:async()=>pdfjs,onChange:s=>{window.latest=s},onError:e=>window.failures.push(String(e))});
      window.makeSecond=()=>Reader.createPdfReader(document.querySelector('#second'),{loadPdfJs:async()=>pdfjs});
      await window.reader.load({id:'build-a',url:'/paper.pdf'});window.ready=true;
      </script></html>''')
    class QuietHandler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *_args):
            pass
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(QuietHandler, directory=str(tmp_path)))
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    with playwright.sync_playwright() as driver:
        browser = driver.chromium.launch(executable_path=executable, headless=True, args=['--no-sandbox'])
        context = browser.new_context(viewport={'width': 1440, 'height': 1000}, device_scale_factor=2)
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        try:
            page.goto(f'http://127.0.0.1:{server.server_port}/index.html')
            page.wait_for_function('window.ready === true')
            yield page, errors
        finally:
            context.close()
            browser.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


def test_initial_reading_view_paints_saved_page_without_a_first_page_flash(reader_page):
    page, errors = reader_page
    page.evaluate("""async()=>{
      reader.destroy();window.publications=[];
      const pdfjs=await import('/pdf.mjs');pdfjs.GlobalWorkerOptions.workerSrc='/pdf.worker.mjs';
      window.reader=Reader.createPdfReader(document.querySelector('#reader'),{
        loadPdfJs:async()=>pdfjs,onChange:state=>{if(state.sourceId)publications.push(state)}});
      await reader.load({id:'saved-build',url:'/paper.pdf',initialView:{
        page:15,fraction:.35,horizontal:.5,scale:1.25,fit:false}},false);
    }""")
    page.wait_for_function("document.querySelector('#reader [data-page=\"15\"] canvas')")
    states = page.evaluate('publications')
    assert states and all(state['page'] == 15 and state['scale'] == 1.25 and not state['fit'] for state in states)
    anchor = page.evaluate('reader.anchor()')
    assert abs(anchor['fraction'] - .35) < .015
    assert not errors


def test_pdf_reader_preserves_position_and_bounds_real_page_rendering(reader_page, tmp_path):
    page, errors = reader_page
    page.wait_for_function("document.querySelector('#reader .paper-reader-text span')")
    first = page.evaluate('''() => {
      const root=document.querySelector('#reader'), canvas=root.querySelector('canvas');
      const range=document.createRange();range.selectNodeContents(root.querySelector('.paper-reader-text'));
      window.getSelection().removeAllRanges();window.getSelection().addRange(range);
      return {pages:root.querySelectorAll('.paper-reader-page').length,canvases:root.querySelectorAll('canvas').length,
        ratio:canvas.width/canvas.getBoundingClientRect().width,text:window.getSelection().toString(),state:reader.state};
    }''')
    assert first['pages'] == 20
    assert 1 <= first['canvases'] <= 4
    assert first['ratio'] >= 1.95
    assert 'selectable paragraph' in first['text']
    assert first['state']['canvasBytes'] <= 64 * 1024 * 1024
    page.evaluate('reader.goToPage(15, 0.35)')
    page.wait_for_function("document.querySelector('#reader [data-page=\"15\"] canvas')")
    before = page.evaluate('reader.anchor()')
    page.evaluate("reader.setScale(1.25)")
    page.wait_for_function("reader.state.scale === 1.25 && document.querySelector('#reader [data-page=\"15\"] canvas')")
    zoomed = page.evaluate('reader.anchor()')
    assert zoomed['page'] == before['page'] == 15
    assert abs(zoomed['fraction'] - before['fraction']) < 0.015
    page.evaluate("reader.setScale('fit')")
    page.set_viewport_size({'width': 390, 'height': 844})
    page.wait_for_function("document.querySelector('#reader [data-page=\"15\"]').clientWidth < 390")
    narrow = page.evaluate('reader.anchor()')
    assert narrow['page'] == 15
    assert abs(narrow['fraction'] - before['fraction']) < 0.015
    assert page.evaluate("document.querySelector('#reader').scrollWidth <= 390")
    page.evaluate("reader.load({id:'build-b',url:'/paper.pdf?revision=b'})")
    assert page.evaluate('reader.state.sourceId') == 'build-b'
    assert page.evaluate('reader.anchor().page') == 15
    outline = page.evaluate('reader.outline()')
    assert [item['title'] for item in outline] == ['First chapter', 'Later chapter']
    found = page.evaluate("reader.search('Page 17.')")
    assert found['matches'] and {row['page'] for row in found['matches']} == {17}
    link = page.locator('#reader .paper-reader-links a').first
    assert link.get_attribute('href') == 'https://example.com/paper'
    assert link.get_attribute('rel') == 'noopener noreferrer'
    # A failed replacement keeps the valid PDF and its reading anchor.
    page.evaluate("reader.load({id:'broken',url:'/missing.pdf'}).catch(()=>{})")
    assert page.evaluate('reader.state.sourceId') == 'build-b'
    assert page.evaluate('reader.anchor().page') == 15
    assert len(page.evaluate('window.failures')) == 1
    artifact = ROOT / 'output/playwright/paper-reader'
    artifact.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(artifact / 'narrow.png'))
    (artifact / 'metrics.json').write_text(json.dumps({'first': first, 'zoomed': zoomed, 'narrow': narrow}, indent=2))
    assert not errors


def test_pdf_reader_instances_and_close_fence_pending_load(reader_page):
    page, errors = reader_page
    page.evaluate("window.second=makeSecond();second.load({id:'second',url:'/paper.pdf'})")
    page.evaluate('second.goToPage(3)')
    page.wait_for_function("document.querySelector('#second [data-page=\"3\"] canvas')")
    assert page.evaluate('reader.state.page') == 1
    assert page.evaluate('second.state.page') == 3
    page.evaluate('''() => {
      window.late=reader.load({id:'late',url:'/paper.pdf?late=1'});
      reader.destroy();
    }''')
    assert page.evaluate('window.late') is False
    assert page.evaluate("document.querySelector('#reader').childElementCount") == 0
    assert page.evaluate('reader.state.canvasBytes') == 0
    assert page.locator('#reader').get_attribute('tabindex') is None
    assert page.evaluate('second.state.pages') == 20
    page.evaluate('second.destroy()')
    assert page.evaluate('second.state.canvasBytes') == 0
    assert not errors


def test_pdf_reader_mixed_long_document_preserves_anchor_and_resource_budget(reader_page):
    page, errors = reader_page
    page.evaluate("reader.load({id:'long',url:'/long.pdf'},false)")
    for number in [250, 400, 8, 498]:
        page.evaluate(f'reader.goToPage({number},0.3)')
        page.wait_for_function(f"document.querySelector('#reader [data-page=\"{number}\"] canvas')")
        result = page.evaluate('''() => ({anchor:reader.anchor(),state:reader.state,
          canvases:document.querySelectorAll('#reader canvas').length,
          overflow:reader.container.scrollWidth-reader.container.clientWidth})''')
        assert result['anchor']['page'] == number
        assert abs(result['anchor']['fraction'] - 0.3) < 0.015
        assert result['state']['canvasBytes'] <= 64 * 1024 * 1024
        assert result['canvases'] <= 8
        assert result['overflow'] == 0
    page.evaluate('reader.destroy()')
    assert page.evaluate('reader.state.canvasBytes') == 0
    assert not errors


def test_pdf_reader_zoom_during_page_fetch_cannot_commit_stale_scale(reader_page):
    page, errors = reader_page
    page.evaluate('''() => {
      const doc=reader.document,original=doc.getPage.bind(doc);
      let held=false;
      doc.getPage=async number=>{
        const result=await original(number);
        if(number===14&&!held){held=true;await new Promise(resolve=>{window.releasePage=resolve})}
        return result;
      };
      reader.goToPage(14,0.3);
    }''')
    page.wait_for_function('typeof window.releasePage === "function"')
    page.evaluate('reader.setScale(1.5);window.releasePage()')
    page.wait_for_function("document.querySelector('#reader [data-page=\"14\"] canvas')?.style.width === '918px'")
    anchor = page.evaluate('reader.anchor()')
    assert anchor['page'] == 14
    assert abs(anchor['fraction'] - 0.3) < 0.015
    assert not errors


def test_pdf_reader_byte_transport_cancellation_does_not_replace_visible_document(reader_page):
    page, errors = reader_page
    page.evaluate('''() => {
      window.replacement=reader.load({id:'held',url:'/unused.pdf',preferBytes:true,
        loadBytes:signal=>new Promise((resolve,reject)=>{
          window.byteLoadStarted=true;
          signal.addEventListener('abort',()=>{window.byteLoadAborted=true;reject(new DOMException('Aborted','AbortError'))});
        })});
    }''')
    page.wait_for_function('window.byteLoadStarted === true')
    page.evaluate('reader.cancelLoad()')
    assert page.evaluate('window.replacement') is False
    assert page.evaluate('window.byteLoadAborted') is True
    assert page.evaluate('reader.state.sourceId') == 'build-a'
    assert page.evaluate('reader.state.loading') is False
    page.evaluate("reader.load({id:'bytes',url:'/unused.pdf',preferBytes:true,loadBytes:signal=>fetch('/paper.pdf',{signal}).then(r=>r.arrayBuffer())})")
    assert page.evaluate('reader.state.sourceId') == 'bytes'
    assert not errors


def _column_box(page, page_number, text):
    # PDF residency can replace a text layer between visibility and a protocol
    # boundingBox call; resolve and measure the current layer atomically.
    return page.wait_for_function("""({pageNumber,text})=>{
      const node=[...document.querySelectorAll('#reader [data-page="'+pageNumber+'"] .paper-reader-text span')]
        .find(node=>node.textContent.includes(text));
      if(!node)return false;
      const {x,y,width,height}=node.getBoundingClientRect();
      return height>0?{x,y,width,height}:false;
    }""", arg={"pageNumber": page_number, "text": text}).json_value()


def test_pdf_reader_columns_are_readable_and_pinch_keeps_the_point_under_fingers(reader_page):
    page, errors = reader_page
    page.set_viewport_size({'width': 390, 'height': 844})
    page.evaluate("document.querySelector('#second').remove()")
    page.evaluate("reader.load({id:'columns',url:'/columns.pdf'},false)")
    page.evaluate("reader.focusColumn('left')")
    page.wait_for_function("reader.state.scale>1 && document.querySelector('#reader [data-page=\"1\"] .paper-reader-text span')")
    page.wait_for_function('''()=>{const c=document.querySelector('#reader [data-page="1"] canvas');
      return c && c.width/c.getBoundingClientRect().width>1.9}''')
    left = page.locator('#reader [data-page="1"] .paper-reader-text span').filter(has_text='Left evidence 1:').first
    left.wait_for(state='visible')
    left_box = _column_box(page, 1, 'Left evidence 1:')
    assert left_box and left_box['height'] >= 14
    assert left_box['x'] >= 0 and left_box['x'] + left_box['width'] <= 390
    artifact = ROOT / 'output/playwright/paper-preview'
    artifact.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(artifact / 'mobile-column-left.png'))
    page.evaluate("reader.goToPage(2);reader.focusColumn('right')")
    page.wait_for_function("reader.state.page===2 && document.querySelector('#reader [data-page=\"2\"] .paper-reader-text span')")
    right = page.locator('#reader [data-page="2"] .paper-reader-text span').filter(has_text='Right evidence 1:').first
    right.wait_for(state='visible')
    right_box = _column_box(page, 2, 'Right evidence 1:')
    assert right_box and right_box['height'] >= 14
    assert right_box['x'] >= 0 and right_box['x'] + right_box['width'] <= 390
    page.screenshot(path=str(artifact / 'mobile-column-right.png'))
    before = page.evaluate('reader.anchor()')
    page.evaluate("reader.load({id:'columns-rebuilt',url:'/columns.pdf'})")
    after = page.evaluate('reader.anchor()')
    assert after['page'] == before['page'] == 2
    assert abs(after['horizontal'] - before['horizontal']) < 0.01
    page.evaluate("reader.setScale('fit');reader.goToPage(1)")
    page.wait_for_function('reader.state.fit && reader.state.page===1')
    start = page.evaluate('({scale:reader.state.scale,point:reader.readingPoint(180,300)})')
    session = page.context.new_cdp_session(page)
    session.send('Emulation.setTouchEmulationEnabled', {'enabled': True, 'maxTouchPoints': 2})
    try:
        session.send('Input.dispatchTouchEvent', {'type': 'touchStart', 'touchPoints': [
            {'x': 120, 'y': 300, 'id': 1}, {'x': 240, 'y': 300, 'id': 2}]})
        session.send('Input.dispatchTouchEvent', {'type': 'touchMove', 'touchPoints': [
            {'x': 80, 'y': 300, 'id': 1}, {'x': 280, 'y': 300, 'id': 2}]})
        page.wait_for_function('scale=>reader.state.scale>scale*1.5', arg=start['scale'])
        point = page.evaluate('reader.readingPoint(180,300)')
        assert point['page'] == start['point']['page']
        assert abs(point['x'] - start['point']['x']) < 0.01
        assert abs(point['y'] - start['point']['y']) < 0.01
        assert page.evaluate('visualViewport.scale') == 1
        session.send('Input.dispatchTouchEvent', {'type': 'touchEnd', 'touchPoints': []})
        page.wait_for_function("document.querySelector('#reader [data-page=\"1\"] canvas') !== null")
        assert page.evaluate('reader.state.canvasBytes') <= 64 * 1024**2
        assert page.locator('#reader canvas').count() <= 8
        page.evaluate('reader.destroy()')
        assert page.locator('#reader canvas').count() == 0
    finally:
        session.detach()
    assert not errors


def test_pdf_reader_hidden_replacement_retains_the_reading_position(reader_page):
    page, errors = reader_page
    page.evaluate('reader.setScale(1.4);reader.goToPage(15,0.3)')
    before = page.evaluate('reader.anchor()')
    page.evaluate("reader.container.style.display='none'")
    page.wait_for_function('reader.container.clientWidth===0')
    assert page.evaluate("reader.load({id:'hidden-rebuild',url:'/paper.pdf'})")
    assert page.evaluate('reader.state.page') == 15
    assert page.evaluate('reader.state.scale') == 1.4
    page.evaluate("reader.container.style.display=''")
    page.wait_for_function('reader.anchor().page===15 && Math.abs(reader.anchor().fraction-0.3)<0.015')
    after = page.evaluate('reader.anchor()')
    assert abs(after['horizontal'] - before['horizontal']) < 0.01
    assert page.evaluate('reader.state.canvasBytes') <= 64 * 1024**2
    assert not errors


def test_pdf_reader_prepared_replacement_waits_for_pinch_and_cancel_retires_it(reader_page):
    page, errors = reader_page
    page.set_viewport_size({'width': 390, 'height': 844})
    page.evaluate('''async()=>{
      document.querySelector('#second').remove();reader.destroy();
      const pdfjs=await import('/pdf.mjs');
      const library={...pdfjs,getDocument:options=>{
        const task=pdfjs.getDocument(options);
        if(options.url.includes('swap='))task.promise.then(doc=>{
          const getPage=doc.getPage.bind(doc);
          doc.getPage=async number=>{
            const page=await getPage(number),annotations=page.getAnnotations.bind(page);
            page.getAnnotations=async options2=>{const value=await annotations(options2);window.prepared=options.url;return value};
            return page;
          };
        }).catch(()=>{});
        return task;
      }};
      window.reader=Reader.createPdfReader(document.querySelector('#reader'),{
        loadPdfJs:async()=>library,onError:error=>failures.push(String(error))});
      await reader.load({id:'before',url:'/columns.pdf'});
    }''')
    session = page.context.new_cdp_session(page)
    session.send('Emulation.setTouchEmulationEnabled', {'enabled': True, 'maxTouchPoints': 2})
    points = [{'x': 120, 'y': 300, 'id': 1}, {'x': 240, 'y': 300, 'id': 2}]
    try:
        session.send('Input.dispatchTouchEvent', {'type': 'touchStart', 'touchPoints': points})
        page.evaluate("()=>{window.swap=reader.load({id:'after',url:'/columns.pdf?swap=1'})}")
        page.wait_for_function("window.prepared==='/columns.pdf?swap=1'")
        assert page.evaluate('reader.state.sourceId') == 'before'
        session.send('Input.dispatchTouchEvent', {'type': 'touchEnd', 'touchPoints': []})
        assert page.evaluate('window.swap') is True
        assert page.evaluate('reader.state.sourceId') == 'after'
        session.send('Input.dispatchTouchEvent', {'type': 'touchStart', 'touchPoints': points})
        page.evaluate("()=>{window.swap=reader.load({id:'cancelled',url:'/columns.pdf?swap=2'})}")
        page.wait_for_function("window.prepared==='/columns.pdf?swap=2'")
        page.evaluate('reader.cancelLoad()')
        assert page.evaluate('window.swap') is False
        assert page.evaluate('reader.state.sourceId') == 'after'
        page.evaluate('reader.destroy()')
        assert page.evaluate('reader.state.canvasBytes') == 0
        session.send('Input.dispatchTouchEvent', {'type': 'touchEnd', 'touchPoints': []})
        assert not page.evaluate('failures')
    finally:
        session.detach()
    assert not errors
