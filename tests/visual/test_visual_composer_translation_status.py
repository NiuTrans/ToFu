"""Composer translation waiting receipt: real Chromium geometry and teardown."""
from pathlib import Path
import subprocess

from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
import pytest

pytestmark = pytest.mark.visual
ROOT = Path(__file__).resolve().parents[2]

@pytest.fixture
def translation_receipt_server(tmp_path):
    # A served component surface uses shipped CSS without opening user storage.
    (tmp_path / 'styles.css').write_bytes((ROOT / 'static/styles.css').read_bytes())
    (tmp_path / 'index.html').write_text('''<!doctype html><html lang="zh"><head>
      <meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
      <link rel="stylesheet" href="styles.css"></head><body>
      <main class="input-area" style="position:static;width:100%;max-width:720px;margin:auto">
      <div class="input-box"><div class="input-row"><textarea placeholder="输入消息"></textarea>
      </div></div></main></body></html>''')
    server = ThreadingHTTPServer(('127.0.0.1', 0),
        partial(SimpleHTTPRequestHandler, directory=str(tmp_path)))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/'
    finally:
        server.shutdown(); server.server_close(); thread.join()

@pytest.mark.parametrize('width', [390, 1280])
def test_composer_translation_receipt(translation_receipt_server, browser, tmp_path, width):
    bundle = tmp_path / 'translation-status.js'
    result = subprocess.run([str(ROOT / 'scripts/vite_test_bundle.mjs'),
        str(ROOT / 'frontend/src/conversation/application/composer-translation-status.ts'),
        '--bundle', '--format=iife', '--platform=browser',
        '--global-name=TranslationReceipt', f'--outfile={bundle}'],
        cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    page = browser.new_page(viewport={'width': width, 'height': 900})
    try:
        page.goto(translation_receipt_server, wait_until='domcontentloaded')
        page.wait_for_selector('.input-box')
        page.add_script_tag(content=bundle.read_text())
        page.evaluate('''() => {
          window.receiptClock = 0;
          window.receiptActive = 'visual-conv';
          window.receipt = TranslationReceipt.createComposerTranslationStatus({document,
            now: () => receiptClock, activeConversationId: () => receiptActive,
            label: key => ({'translationWait.send':'翻译 / 提交中…',
              'translationWait.completed':'处理完成', 'translationWait.failed':'翻译失败', 'translationWait.waited':'等待',
              'translationWait.modelPending':'模型以返回结果为准'})[key] || key});
          window.receiptWait = receipt.begin('visual-conv','send');
        }''')
        page.wait_for_selector('.composer-translation-status')
        page.evaluate('receiptClock = 2300; receipt.refresh()')
        assert '2.3s' in page.locator('.composer-translation-status').inner_text()
        pending = page.locator('.composer-translation-status').bounding_box()
        assert pending and pending['width'] > 100 and pending['height'] < 90
        page.evaluate("receipt.finish(receiptWait,'completed','deepseek-v4-flash-yourprovider')")
        # Successful attribution belongs to the durable message. Only pending
        # work and unresolved failure/cancellation stay in the composer.
        assert page.locator('.composer-translation-status').count() == 0
        page.evaluate("receiptWait=receipt.begin('visual-conv','send');receiptClock=4600;"
                      "receipt.finish(receiptWait,'failed','deepseek-v4-flash-yourprovider')")
        text = page.locator('.composer-translation-status').inner_text()
        assert 'deepseek-v4-flash-yourprovider' in text and '2.3s' in text
        box = page.locator('.composer-translation-status').bounding_box()
        assert box and box['x'] >= 0 and box['x'] + box['width'] <= width + 1
        page.screenshot(path=str(tmp_path / f'composer-translation-{width}.png'), full_page=True)
        page.evaluate("receiptActive='other-conv'; receipt.refresh()")
        assert page.locator('.composer-translation-status').count() == 0
        page.evaluate('receipt.destroy()')
    finally:
        page.close()
