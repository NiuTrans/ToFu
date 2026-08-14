"""Critical Vite/ESM user journeys driven through the real DOM."""

from __future__ import annotations

import base64
import re
import time

import pytest

from tests.test_e2e_smoke import (  # noqa: F401 -- imported fixtures are load-bearing
    _SENTINEL,
    _disable_open_mode_rate_limit,
    _install_llm_stubs,
    _wait_app_ready,
)


pytestmark = [pytest.mark.visual]
_TINY_PNG = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=='
)


def _fresh_chat(page):
    _wait_app_ready(page)
    page.locator('.new-chat-btn').click()
    page.wait_for_function(
        "!document.querySelector('#chatInner').innerText.includes('stubbed model')")


def _send_and_wait_done(page, text, expected_replies=1, timeout=30000):
    calls_before = _SENTINEL['stream_calls']
    page.locator('#userInput').fill(text)
    page.locator('#sendBtn').click()
    page.wait_for_function(
        """(expected) => {
          const body = document.querySelector('#chatInner')?.innerText || '';
          const replies = body.split('stubbed model').length - 1;
          const send = document.querySelector('#sendBtn');
          return replies >= expected && send && !send.classList.contains('stop-btn');
        }""",
        arg=expected_replies,
        timeout=timeout,
    )
    assert _SENTINEL['stream_calls'] > calls_before


def _wait_conversation_persisted(page, conversation_id, text, timeout=10000):
    """Wait for the authoritative V2 turn projection before a hard reload."""
    endpoint = (
        f"{page.url.rstrip('/')}/api/v2/conversations/"
        f"{conversation_id}/turns"
    )
    deadline = time.monotonic() + timeout / 1000
    while time.monotonic() < deadline:
        response = page.request.get(endpoint)
        if response.ok:
            turns = response.json().get('turns', [])
            if any(text in str((turn.get('projection') or {}).get('content', ''))
                   for turn in turns):
                return
        page.wait_for_timeout(100)
    pytest.fail(f'conversation {conversation_id} was not durable before reload')


def test_abort_halts_stream_and_keeps_partial(page, assert_no_js_errors):
    _fresh_chat(page)
    page.locator('#userInput').fill('__e2e_slow__ stream please')
    page.locator('#sendBtn').click()
    page.wait_for_selector('#sendBtn.stop-btn', state='visible', timeout=10000)
    page.wait_for_function(
        "document.querySelector('#chatInner').innerText.includes('slow03')",
        timeout=10000,
    )
    page.locator('#sendBtn').click()
    page.wait_for_function(
        "!document.querySelector('#sendBtn').classList.contains('stop-btn')",
        timeout=10000,
    )
    page.wait_for_function(
        "/slow\\d\\d/.test(document.querySelector('#chatInner')?.innerText || '')",
        timeout=10000,
    )
    first = [int(value) for value in re.findall(
        r'slow(\d\d)', page.inner_text('#chatInner'))]
    assert first and max(first) < 55
    time.sleep(0.2)
    later = [int(value) for value in re.findall(
        r'slow(\d\d)', page.inner_text('#chatInner'))]
    assert max(later or [0]) <= max(first) + 2


def test_reload_restores_active_conversation(page, assert_no_js_errors):
    _fresh_chat(page)
    _send_and_wait_done(page, 'Hello reload E2E')
    active = page.locator('.conv-item.active')
    active.wait_for(state='attached', timeout=15000)
    conversation_id = active.get_attribute('data-conv-id')
    assert conversation_id
    _wait_conversation_persisted(page, conversation_id, 'Hello reload E2E')
    page.reload()
    _wait_app_ready(page)
    item = page.locator(f'.conv-item[data-conv-id="{conversation_id}"]')
    item.wait_for(state='attached', timeout=15000)
    item.click(position={'x': 12, 'y': 12})
    page.wait_for_function(
        "document.querySelector('#chatInner').innerText.includes('Hello reload E2E')",
        timeout=15000,
    )
    page.wait_for_function(
        """() => (!window.BackendOfflineMonitor ||
          window.BackendOfflineMonitor.phase === 'online') &&
          !document.querySelector('#backend-offline-banner')""",
        timeout=15000,
    )


def test_multi_turn_and_enter_key_render(page, assert_no_js_errors):
    _fresh_chat(page)
    _send_and_wait_done(page, 'turn one E2E', expected_replies=1)
    calls_before = _SENTINEL['stream_calls']
    page.locator('#userInput').fill('turn two E2E')
    page.keyboard.press('Enter')
    page.wait_for_function(
        """() => {
          const body = document.querySelector('#chatInner')?.innerText || '';
          return body.includes('turn one E2E') && body.includes('turn two E2E')
            && body.split('stubbed model').length - 1 >= 2;
        }""",
        timeout=30000,
    )
    assert _SENTINEL['stream_calls'] > calls_before


def test_new_chat_control_clears_chat_view(page, assert_no_js_errors):
    _fresh_chat(page)
    _send_and_wait_done(page, 'Hello clear E2E')
    page.locator('.new-chat-btn').click()
    page.wait_for_function(
        "!document.querySelector('#chatInner').innerText.includes('stubbed model')",
        timeout=10000,
    )


def test_settings_actions_and_theme_persist(page, assert_no_js_errors):
    _wait_app_ready(page)
    page.locator('[data-tofu-action="openSettings()"]').first.click()
    page.wait_for_selector('#settingsModal.open', timeout=10000)
    current = page.evaluate(
        "document.documentElement.getAttribute('data-theme')")
    target = 'light' if current != 'light' else 'dark'
    page.locator(f'.theme-option[data-theme="{target}"]').click()
    assert page.evaluate(
        "document.documentElement.getAttribute('data-theme')") == target
    page.locator('.settings-close-btn').first.click()
    page.wait_for_selector('#settingsModal.open', state='detached', timeout=5000)
    page.reload()
    _wait_app_ready(page)
    assert page.evaluate(
        "document.documentElement.getAttribute('data-theme')") == target


def test_paper_and_orchestration_domains_boot(page, assert_no_js_errors):
    _wait_app_ready(page)
    page.locator('#paperModeBtn').click()
    page.wait_for_selector('#paperModeContainer', state='visible', timeout=15000)
    page.locator('#paperModeBtn').click()
    page.wait_for_selector('#paperModeContainer', state='hidden', timeout=10000)

    opened = page.evaluate("""async () => {
      await window.TofuModules.prepareFeature('openOrchestration');
      const action = window.TofuModules.resolveAction('openOrchestration');
      if (typeof action !== 'function') return false;
      await action();
      return true;
    }""")
    assert opened
    page.wait_for_selector('#orchModal', state='visible', timeout=15000)
    page.evaluate("""async () => {
      const action = window.TofuModules.resolveAction('closeOrchestration');
      if (typeof action === 'function') await action(null, true);
    }""")


def test_paper_pdfjs_loads_and_renders_a_real_document(
        page, tmp_path, assert_no_js_errors):
    """The shipped PDF.js main chunk and worker must render an actual PDF.

    Opening the empty Paper shell is insufficient: the classic-to-ESM split
    previously left both ``_ensurePdfJs`` and ``pdfjsLib`` outside the private
    feature service table, so every shell-only browser test stayed green while
    selecting a document displayed "PDF.js failed to load".
    """
    pymupdf = pytest.importorskip('pymupdf')
    document = pymupdf.open()
    pdf_page = document.new_page(width=420, height=300)
    pdf_page.insert_text((42, 72), 'Tofu Paper Reader PDF.js E2E')
    pdf_bytes = document.tobytes()
    document.close()
    pdf_path = tmp_path / 'paper-pdfjs-e2e.pdf'
    pdf_path.write_bytes(pdf_bytes)

    requested_assets = []
    page.on('request', lambda request: requested_assets.append(request.url))

    _wait_app_ready(page)
    page.locator('#paperModeBtn').click()
    page.wait_for_selector('#paperModeContainer', state='visible', timeout=15000)
    page.set_input_files('.paper-upload-btn input[type="file"]', str(pdf_path))
    page.wait_for_selector(
        '.paper-page-wrapper .paper-pdf-canvas', state='attached', timeout=30000)
    result = page.evaluate("""() => ({
      pages: document.querySelectorAll('.paper-page-wrapper').length,
      canvases: document.querySelectorAll('.paper-pdf-canvas').length,
      error: document.querySelector('.paper-error')?.textContent || '',
    })""")

    assert result == {'pages': 1, 'canvases': 1, 'error': ''}
    assert any('pdf.worker.min' in url for url in requested_assets), (
        'the real PDF.js worker asset was never requested')


def test_upload_image_chip_renders(page, tmp_path, assert_no_js_errors):
    _fresh_chat(page)
    png = tmp_path / 'tiny.png'
    png.write_bytes(_TINY_PNG)
    page.set_input_files('#fileInput', str(png))
    preview = page.locator('.image-previews .img-preview[data-img-idx="0"]')
    preview.wait_for(state='visible', timeout=10000)
    image = preview.locator('img[alt="preview"]')
    assert image.count() == 1
    source = image.get_attribute('src') or ''
    assert source.startswith(('blob:', 'data:image/png')), source
    assert preview.locator('.remove-img').is_visible()
