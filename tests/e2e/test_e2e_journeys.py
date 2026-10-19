"""Critical Vite/ESM user journeys driven through the real DOM."""

from __future__ import annotations

import base64
import re
import time

import pytest

from tests.support import desktop_paper

from tests.e2e.test_e2e_smoke import (  # noqa: F401 -- imported fixtures are load-bearing
    _SENTINEL,
    _disable_open_mode_rate_limit,
    _install_llm_stubs,
    _wait_app_ready,
    _select_stub_model,
)


pytestmark = [pytest.mark.visual]
desktop_paper_device = desktop_paper.desktop_paper_device
_TINY_PNG = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=='
)


def _fresh_chat(page):
    _wait_app_ready(page)
    page.locator('.new-chat-btn').click()
    page.wait_for_function(
        "!document.querySelector('#chatInner').innerText.includes('stubbed model')")
    _select_stub_model(page)


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
    """Wait for the authoritative conversation snapshot before a hard reload."""
    endpoint = (
        f"{page.url.rstrip('/')}/api/v4/conversations/"
        f"{conversation_id}/sync"
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


def _wait_conversation_catalog_ready(page, conversation_id, timeout=30000):
    """Wait for post-reload catalog hydration, not only the application shell."""
    page.wait_for_function(
        """(conversationId) => Array.isArray(window.conversations) &&
          window.conversations.some((conversation) =>
            conversation.id === conversationId &&
            Number(conversation._serverTurnCount || 0) > 0)""",
        arg=conversation_id,
        timeout=timeout,
    )


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
    _wait_conversation_catalog_ready(page, conversation_id)
    item = page.locator(f'.conv-item[data-conv-id="{conversation_id}"]')
    item.wait_for(state='attached', timeout=5000)
    item.click(position={'x': 12, 'y': 12})
    page.wait_for_function(
        "document.querySelector('#chatInner').innerText.includes('Hello reload E2E')",
        timeout=15000,
    )
    page.wait_for_function(
        "() => !document.querySelector('#backend-offline-banner')",
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
    # See test_visual_surfaces.py: flags piggyback on /api/v1/server-config;
    # the /api/v1/features fallback never fires for a valid boot payload.
    def _force_debug_flags(route):
        response = route.fetch()
        body = response.json()
        body.get('data', body)['feature_flags'] = {'debug_mode': True, 'optimizer_enabled': True}
        route.fulfill(response=response, json=body)

    page.route('**/api/v*/server-config**', _force_debug_flags)
    page.reload()
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
        '#paperPdfViewer .paper-reader-page canvas', state='attached', timeout=30000)
    result = page.evaluate("""() => ({
      pages: document.querySelectorAll('#paperPdfViewer .paper-reader-page').length,
      canvases: document.querySelectorAll('#paperPdfViewer canvas').length,
      error: document.querySelector('.paper-error')?.textContent || '',
    })""")

    assert result == {'pages': 1, 'canvases': 1, 'error': ''}
    assert any('pdf.worker.min' in url for url in requested_assets), (
        'the real PDF.js worker asset was never requested')


def test_paper_landing_pastes_direct_pdf_link_to_fetch_stream(page):
    """The landing column renders wide enough AND a pasted PDF URL routes to
    the arXiv fetch stream, never to a title search.

    Two regressions this journey locks in against a REAL browser + bundle
    (jsdom unit tests cannot see either — no layout, no served ESM):

    1. ``.paper-landing`` used flex ``align-items:center`` with no
       ``align-self``, so it SHRANK to its content width (~386px) inside its
       ~496px ``.paper-pdf-container`` parent; the action column was doubly
       squeezed no matter how large its own ``max-width`` cap was. The fix
       (``align-self:stretch``) lets the landing — and therefore the column —
       fill the PDF pane. This is a LAYOUT regression a stylesheet drift-guard
       cannot see: only a real browser measures it.

    2. ``submitArxivQuery`` previously classified every non-arXiv input as a
       title *search*; a Hugging Face ``resolve/main/....pdf`` link would hit
       the arXiv search API and return zero results instead of streaming the
       PDF through ``/api/paper/fetch-arxiv-stream``.

    The stream POST is intercepted (``route.fulfill``) so the test needs no
    external network and never downloads the real 100-page PDF: what matters
    is WHICH endpoint the click targets, not the bytes behind it. A title
    query is also submitted to prove the search branch still routes to the
    search API (the fetch-stream count must stay at exactly one).

    Uses ``page`` WITHOUT ``assert_no_js_errors``: the stubbed stream returns a
    bare ``done`` with no completion payload, so the reader's fetch handler
    logs its app-authored "Fetch ended without completion" notice and shows a
    retry card. That is the exercised degradation path, not a defect — the
    conftest benign-error list is intentionally short, and this test's job is
    endpoint routing + layout, not a clean-console sweep.
    """
    fetch_stream_posts = []
    search_calls = []

    def _capture_fetch_stream(route):
        fetch_stream_posts.append(route.request.post_data or '')
        # An immediately-closing SSE stream; the frontend's transport falls
        # back without surfacing a JS error (premature-close is benign here).
        route.fulfill(status=200, content_type='text/event-stream',
                      body='data: {"type":"done"}\n\n')

    def _capture_search_api(route):
        search_calls.append(route.request.url)
        route.fulfill(status=200, content_type='application/json',
                      json={'ok': True, 'results': []})

    page.route('**/api/paper/fetch-arxiv-stream**', _capture_fetch_stream)
    page.route('**/api/v*/paper/search-arxiv**', _capture_search_api)

    from tests.conftest import _dismiss_onboarding_modals
    _wait_app_ready(page)
    _dismiss_onboarding_modals(page)
    page.locator('#paperModeBtn').click()
    page.wait_for_selector('#paperModeContainer', state='visible', timeout=15000)

    # 1. The landing action column must FILL the PDF pane's inner content box,
    #    not shrink-wrap to its own content. The pane is a ~50% split of the
    #    ~1000px reader (draggable divider) and both the pane and the landing
    #    carry horizontal padding, so the absolute width varies with the
    #    viewport — assert the column tracks the landing's actual inner width
    #    (clientWidth minus padding), not a fixed pixel cap the split can't
    #    reach. That is the live signal that align-self:stretch still works.
    page.wait_for_selector('.paper-landing-actions', state='visible', timeout=15000)
    widths = page.evaluate("""() => {
      const col = document.querySelector('.paper-landing-actions');
      const landing = document.querySelector('.paper-landing');
      const cs = getComputedStyle(landing);
      const inner = (landing?.clientWidth || 0)
        - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight);
      return { col: col?.getBoundingClientRect().width || 0, inner };
    }""")
    expected = min(widths['inner'], 640)  # fill the inner box, capped at max-width
    assert widths['col'] >= expected - 1, (
        f"action column {widths['col']:.0f}px does not fill the landing's "
        f"{widths['inner']:.0f}px inner box (expected >= {expected:.0f}px) — the "
        'landing is shrink-wrapping to its content again (align-self:stretch '
        'lost), re-clipping the PDF-link placeholder.')

    # 2. A direct PDF link must hit the fetch stream, NOT the search API.
    page.locator('#paperArxivUrl').fill(
        'https://huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash/'
        'resolve/main/DeepSeek_V41_Tech_Report.pdf')
    page.locator('.paper-arxiv-btn').click()
    page.wait_for_timeout(300)
    assert len(fetch_stream_posts) == 1, (
        f'expected exactly 1 fetch-arxiv-stream POST, got {fetch_stream_posts}; '
        'a direct PDF link must stream, not title-search')
    assert 'DeepSeek_V41_Tech_Report.pdf' in fetch_stream_posts[0]
    assert search_calls == [], (
        f'a direct PDF link must never hit the arXiv search API: {search_calls}')

    # 3. The title-search branch still routes to the search API (regression:
    #    adding the PDF branch must not swallow plain title queries). The
    #    stubbed stream left a retry card (no completion payload), so restore
    #    the landing before re-submitting.
    page.evaluate("window.TofuModules?.resolveAction?.('_showPaperLanding')?.()")
    page.wait_for_selector('#paperArxivUrl', state='visible', timeout=10000)
    page.locator('#paperArxivUrl').fill('attention is all you need')
    page.locator('.paper-arxiv-btn').click()
    page.wait_for_timeout(300)
    assert len(search_calls) == 1, (
        f'a plain title must hit the search API exactly once: {search_calls}')
    assert len(fetch_stream_posts) == 1, (
        'a plain title must NOT add a second fetch-arxiv-stream POST')


def test_journaled_tex_edit_builds_through_the_real_chat_preview(page, tmp_path, monkeypatch, assert_no_js_errors):
    """Journal → canonical turn state → owned build API → real TeX/PDF.js."""
    import shutil
    from pathlib import Path
    from lib.conversations.settings_store import set_conversation_settings
    from lib.paper.builds.cache import LocalBuildCache
    from lib.paper.builds import runtime as build_runtime
    from lib.project_mod import modifications
    from lib.tasks_pkg.commit_round._derive import note_live_file_change
    from tests.conftest import _dismiss_onboarding_modals
    import lib.tasks_pkg.llm_fallback._call as llm_call

    if not shutil.which('latexmk') and not (Path(__file__).resolve().parents[2] / '.texlive/bin').is_dir():
        pytest.skip('TeX toolchain unavailable')
    project = tmp_path / 'paper-project'
    project.mkdir()
    source = project / 'main.tex'
    original = ('\\documentclass{article}\n\\begin{document}\nOriginal paper.\n'
                + ''.join(f'\\newpage Reading page {number}.\n' for number in range(2, 21))
                + '\\end{document}\n')
    source.write_text(original)
    scheduler = build_runtime.PreviewBuildScheduler(LocalBuildCache(tmp_path / 'build-cache'))
    monkeypatch.setattr(build_runtime, '_scheduler', scheduler)
    monkeypatch.setattr(modifications, '_live_change_listeners', [note_live_file_change])
    _fresh_chat(page)
    _send_and_wait_done(page, 'Create the isolated paper preview conversation')
    conversation_id = page.locator('.conv-item.active').get_attribute('data-conv-id')
    assert conversation_id
    assert set_conversation_settings(conversation_id,
                                     {'projectPath': str(project), 'projectPaths': [str(project)]}, user_id=1)
    page.reload()
    _wait_app_ready(page)
    _dismiss_onboarding_modals(page)
    page.wait_for_function("document.querySelector('#chatInner')?.innerText.includes('stubbed model')")
    # Explicit no-edit activation crosses the actual lazy project-menu bridge,
    # owned HTTP discovery/selection and real TeX/PDF.js (no preview API stubs).
    build_posts = []
    viewer_responses = []
    page.on('response', lambda response: viewer_responses.append(response)
            if response.url.endswith('/viewers') else None)
    page.on('request', lambda request: build_posts.append(request.url)
            if request.method == 'POST' and request.url.endswith('/api/v4/paper/builds') else None)
    page.locator('#projectBarFolders').click()
    page.get_by_role('button', name=re.compile('打开论文预览|Open paper preview')).click()
    page.get_by_role('button', name=re.compile('选择主文档|Choose main document')).click()
    picker = page.locator('.paper-preview-documents select')
    picker.select_option('0')
    page.wait_for_function("document.querySelector('.paper-preview-status')?.textContent.match(/可编译|Ready to compile/)")
    assert build_posts == []
    page.get_by_role('button', name=re.compile('编译 PDF|Compile PDF')).click()
    try:
        page.wait_for_function("document.querySelector('#paperPreviewPanel .paper-reader-text')?.textContent.includes('Original paper')", timeout=30000)
    except Exception as error:
        raise AssertionError({'preview': page.locator('#paperPreviewPanel').inner_text(),
            'viewers': [(response.status, response.json()) for response in viewer_responses]}) from error
    assert len(build_posts) == 1
    page.wait_for_function("document.querySelector('#paperPreviewPanel')?.dataset.buildId")
    initial_panel = page.locator('#paperPreviewPanel')
    initial_document = initial_panel.get_attribute('data-document-id')
    initial_build = initial_panel.get_attribute('data-build-id')
    initial_viewer = next(response for response in viewer_responses
                          if response.request.post_data_json['operation'] == 'retain')
    assert initial_viewer.status == 200 and initial_viewer.json()['data']['retained']
    initial_panel.get_by_role('button', name=re.compile('^阅读模式$|^Reading mode$')).click()
    initial_panel.locator('.paper-preview-page-number').fill('15')
    initial_panel.locator('.paper-preview-page-number').press('Tab')
    page.wait_for_function("document.querySelector('#paperPreviewPanel [data-page=\"15\"] canvas')")
    initial_panel.locator('.paper-preview-zoom').fill('125%')
    initial_panel.locator('.paper-preview-zoom').press('Tab')
    with page.expect_response(lambda response: response.url.endswith('/viewers')
                              and response.request.post_data_json.get('operation') == 'release') as released:
        initial_panel.get_by_role('button', name=re.compile('关闭预览|Close preview')).click()
    assert released.value.status == 200 and not released.value.json()['data']['retained']
    from lib.paper.builds.document_repository import PaperDocumentRepository
    papers = PaperDocumentRepository(1)
    deadline = time.monotonic() + 10
    while True:
        saved = papers.get(initial_document)['reading']
        if saved and saved['page'] == 15 and abs(saved['scale'] - 1.25) < .001 and saved['reading_mode']:
            break
        assert time.monotonic() < deadline, saved
        time.sleep(.1)
    # A readable saved manuscript is independent of the current source and cache.
    missing_source = source.with_suffix('.unavailable')
    source.rename(missing_source)
    scheduler.cache = LocalBuildCache(tmp_path / 'empty-on-reopen')
    page.reload()
    _wait_app_ready(page)
    _dismiss_onboarding_modals(page)
    page.locator('.paper-preview-reopen').click()
    try:
        page.wait_for_function("document.querySelector('#paperPreviewPanel [data-page=\"15\"] canvas')")
        assert initial_panel.get_attribute('data-build-id') == initial_build
        assert initial_panel.locator('.paper-preview-page-number').input_value() == '15'
        assert initial_panel.locator('.paper-preview-zoom').input_value() == '125%'
        assert page.locator('.chat-wrapper').get_attribute('data-paper-preview-reading') == 'true'
        artifact = Path(__file__).resolve().parents[2] / 'output/playwright/paper-preview'
        artifact.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(artifact / 'saved-reopened-desktop.png'))
        page.set_viewport_size({'width': 390, 'height': 844})
        page.wait_for_function("document.querySelector('#sidebar').getBoundingClientRect().right <= 0")
        page.wait_for_function("document.querySelector('#paperPreviewPanel')?.clientWidth<=390")
        assert initial_panel.locator('.paper-preview-page-number').input_value() == '15'
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.screenshot(path=str(artifact / 'saved-reopened-mobile.png'))
        page.set_viewport_size({'width': 1280, 'height': 720})
        initial_panel.locator('.paper-preview-toolbar').get_by_role('button', name=re.compile('更多操作|More controls')).click()
        assert page.get_by_role('button', name=re.compile('重新编译|Recompile')).is_disabled()
        initial_panel.locator('.paper-preview-toolbar').get_by_role('button', name=re.compile('选择主文档|Choose main document')).click()
        assert len(build_posts) == 1, 'saved PDF opens without source access or recompilation'
    finally:
        missing_source.rename(source)
    page.get_by_role('button', name=re.compile('查找 TeX 源码|Find TeX sources')).click()
    page.wait_for_function("()=>{const button=document.querySelector('.paper-preview-compile');return button&&!button.disabled}")
    initial_panel.locator('.paper-preview-page-number').fill('1')
    initial_panel.locator('.paper-preview-page-number').press('Tab')
    initial_panel.get_by_role('button', name=re.compile('退出阅读模式|Exit reading mode')).click()
    initial_panel.locator('.paper-preview-toolbar').get_by_role('button', name=re.compile('更多操作|More controls')).click()
    initial_panel.locator('.paper-preview-drawer').get_by_role('button', name=re.compile('适应宽度|Fit to width')).click()
    page.locator('.paper-preview-viewport').focus()
    page.locator('.paper-preview-viewport').press('Escape')
    page.get_by_role('button', name=re.compile('选择主文档|Choose main document')).click()
    page.wait_for_function("document.querySelector('.paper-preview-documents select')?.value === '0'")
    picker.press('Escape')
    initial_panel.locator('.paper-preview-page-number').fill('1')
    initial_panel.locator('.paper-preview-page-number').press('Tab')
    page.wait_for_function("""() => {
      const panel = document.querySelector('#paperPreviewPanel');
      return panel.querySelector('.paper-preview-page-number').value === '1'
        && panel.querySelector('[data-page="1"] .paper-reader-text')?.textContent.includes('Original paper');
    }""")
    assert len(build_posts) == 1, 'reload restores selection without recompiling history'
    prior = llm_call.stream_llm_response
    receipts = []

    def write_during_turn(task, body, tag='', on_tool_call_ready=None):
        if task.get('convId') == conversation_id and not task.get('_paperPreviewTestWrote'):
            task['_paperPreviewTestWrote'] = True
            source.write_text(original.replace('Original paper.', 'A committed preview revision.'))
            receipts.append(modifications._record_modification(
                str(project), 'write_file', 'main.tex', original_content=original,
                conv_id=conversation_id, task_id=task['id']))
        return prior(task, body, tag, on_tool_call_ready)

    monkeypatch.setattr(llm_call, 'stream_llm_response', write_during_turn)
    requests = []
    page.on('request', lambda request: requests.append(request.url))
    try:
        _send_and_wait_done(page, 'Apply the isolated TeX revision', expected_replies=2)
        page.wait_for_function("document.querySelector('#paperPreviewPanel')?.dataset.buildId", timeout=30000)
        try:
            page.wait_for_function("document.querySelector('#paperPreviewPanel [data-page=\"1\"] .paper-reader-text')?.textContent.includes('committed preview revision')", timeout=30000)
        except Exception as error:
            visible = initial_panel.evaluate("""panel => ({build: panel.dataset.buildId,
              page: panel.querySelector('.paper-preview-page-number').value,
              text: panel.innerText.slice(-3000)})""")
            raise AssertionError({'visible': visible, 'build_requests': len(build_posts),
                                  'write_receipts': receipts}) from error
        assert receipts == [True]
        assert any('/api/v4/paper/builds' in url for url in requests)
        assert not any('/api/v4/latex/preview' in url or '/api/v4/overleaf/preview' in url for url in requests)
        panel = page.locator('#paperPreviewPanel')
        assert len(panel.get_attribute('data-build-id')) == 32
        assert len(panel.get_attribute('data-document-id')) == 64
        _dismiss_onboarding_modals(page)
        artifact = Path(__file__).resolve().parents[2] / 'output/playwright/paper-preview'
        artifact.mkdir(parents=True, exist_ok=True)
        for width in [1080, 1280]:
            page.set_viewport_size({'width': width, 'height': 720})
            page.wait_for_function("""() => {
              const input = document.querySelector('.input-box').getBoundingClientRect();
              const divider = document.querySelector('.paper-preview-divider').getBoundingClientRect();
              const send = document.querySelector('#sendBtn');
              const button = send.getBoundingClientRect();
              const controls = [...document.querySelectorAll('.input-actions > *')]
                .map(element => element.getBoundingClientRect()).filter(rect => rect.width && rect.height);
              return input.right <= divider.left && controls.every(rect => rect.left >= input.left
                && rect.right <= input.right) && button.right <= input.right
                && send.contains(document.elementFromPoint(button.x + button.width / 2, button.y + button.height / 2));
            }""")
        posted_builds = len(build_posts)
        page.locator('#mobileMoreBtn').press('Enter')
        page.wait_for_function("document.querySelector('#mobileSheet')?.classList.contains('open')")
        assert page.locator('#mobileModeStudio').is_visible()
        assert page.locator('#mobileAgentModeStandard').is_visible()
        assert page.locator('#mobileSheet').bounding_box()['width'] <= 480
        page.keyboard.press('Shift+Tab')
        assert page.locator('#mobileSheet').evaluate('element => element.contains(document.activeElement)')
        page.keyboard.press('Tab')
        assert page.locator('#mobileSheet').evaluate('element => element.contains(document.activeElement)')
        page.locator('#mobileAgentModeStandard').click()
        page.screenshot(path=str(artifact / 'composer-more.png'))
        page.keyboard.press('Escape')
        page.wait_for_function("!document.querySelector('#mobileSheet')?.classList.contains('open')")
        assert not page.locator('#mobileSheet').is_visible()
        assert page.locator('#mobileMoreBtn').evaluate('element => document.activeElement === element')
        monkeypatch.setattr(llm_call, 'stream_llm_response', prior)
        next_reply_count = page.locator('#chatInner').inner_text().count('stubbed model') + 1
        _send_and_wait_done(page, 'Continue reviewing the displayed paper', expected_replies=next_reply_count)
        assert len(build_posts) == posted_builds, 'ordinary chat must not recompile unchanged paper sources'
        page.screenshot(path=str(artifact / 'app-desktop.png'))
        page.set_viewport_size({'width': 390, 'height': 844})
        page.wait_for_function("document.querySelector('#sidebar').getBoundingClientRect().right <= 0")
        page.wait_for_function("""() => {
          const panel = document.querySelector('#paperPreviewPanel');
          const canvas = panel.querySelector('canvas');
          return panel.clientWidth > 300 && canvas?.getBoundingClientRect().width <= panel.clientWidth;
        }""")
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        assert panel.locator('.paper-preview-toolbar').bounding_box()['height'] < 65
        assert not page.locator('#userInput').is_visible()
        page.screenshot(path=str(artifact / 'app-mobile.png'))
        page.locator('.paper-preview-tabs [data-mode=chat]').click()
        assert page.locator('#userInput').is_visible()
        page.locator('#mobileMoreBtn').click()
        page.wait_for_function("document.querySelector('#mobileSheet')?.classList.contains('open')")
        assert page.locator('#mobileSheet').bounding_box()['width'] <= 390
        page.keyboard.press('Escape')
        assert page.locator('#mobileMoreBtn').evaluate('element => document.activeElement === element')
        page.locator('.paper-preview-tabs [data-mode=paper]').click()
        panel.locator('.paper-preview-viewport').press('Escape')
        assert page.locator('#paperPreviewPanel').count() == 0
    finally:
        scheduler.shutdown()


@pytest.mark.parametrize('desktop_paper_device', [{'owner_user_id': 1, 'allow_write': True}], indirect=True)
def test_remote_tex_write_automatically_builds_in_the_real_reader(
        page, tmp_path, monkeypatch, desktop_paper_device, assert_no_js_errors):
    """Real project tools → device commits → turn state → device build → PDF.js."""
    import json
    import shutil
    from pathlib import Path
    from lib.conversations.settings_store import set_conversation_settings
    from lib.paper.builds.cache import LocalBuildCache
    from lib.paper.builds import runtime as build_runtime
    from tests.conftest import _dismiss_onboarding_modals
    import lib.tasks_pkg.llm_fallback._call as llm_call

    if not shutil.which('latexmk') and not (Path(__file__).resolve().parents[2] / '.texlive/bin').is_dir():
        pytest.skip('TeX toolchain unavailable')
    device = desktop_paper_device
    scheduler = build_runtime.PreviewBuildScheduler(LocalBuildCache(tmp_path / 'remote-build-cache'))
    monkeypatch.setattr(build_runtime, '_scheduler', scheduler)
    _dismiss_onboarding_modals(page)
    _fresh_chat(page)
    _send_and_wait_done(page, 'Create the remote paper conversation')
    conversation_id = page.locator('.conv-item.active').get_attribute('data-conv-id')
    assert conversation_id
    _wait_conversation_persisted(page, conversation_id, 'stubbed model')
    assert set_conversation_settings(conversation_id, {
        'projectPath': 'remote:device:project', 'projectPaths': ['remote:device:project'],
    }, user_id=1)
    page.reload()
    _wait_app_ready(page)
    _dismiss_onboarding_modals(page)
    _wait_conversation_catalog_ready(page, conversation_id)
    page.locator(f'.conv-item[data-conv-id="{conversation_id}"]').click(position={'x': 12, 'y': 12})
    page.wait_for_function("""id => document.querySelector('.conv-item.active')?.dataset.convId === id
      && document.querySelector('#chatInner')?.innerText.includes('stubbed model')""", arg=conversation_id)
    prior = llm_call.stream_llm_response
    source = '\\documentclass{article}\n\\begin{document}\nRemote committed preview.\n\\end{document}\n'
    observed = {}

    def tool_during_turn(task, body, tag='', on_tool_call_ready=None):
        observed['task'] = task
        observed['results'] = [str(message.get('content', ''))[:800]
            for message in body.get('messages', []) if message.get('role') == 'tool'][-2:]
        stage = task.get('_remotePaperJourneyStage', 0)
        if task.get('convId') == conversation_id and stage < 2:
            task['_remotePaperJourneyStage'] = stage + 1
            arguments = {'path': 'paper/main.tex'}
            if stage:
                arguments['content'] = source
                arguments['description'] = 'Update the remote paper preview'
            call = {'id': f'call_remote_paper_{stage}', 'index': 0, 'type': 'function',
                'function': {'name': 'write_file' if stage else 'read_files',
                             'arguments': json.dumps(arguments)}}
            if on_tool_call_ready:
                on_tool_call_ready(call)
            return ({'role': 'assistant', 'content': '', 'tool_calls': [call]}, 'tool_calls',
                    {'prompt_tokens': 10, 'completion_tokens': 2, 'total_tokens': 12})
        return prior(task, body, tag, on_tool_call_ready)

    monkeypatch.setattr(llm_call, 'stream_llm_response', tool_during_turn)
    submitted = []
    page.on('request', lambda request: submitted.append(request.url)
            if request.method == 'POST' and '/api/v4/paper/builds' in request.url else None)
    try:
        _send_and_wait_done(page, 'Update the remote TeX document', expected_replies=2)
        assert (device.paper / 'main.tex').read_text() == source, json.dumps({
            'commands': [command['type'] for command in device.commands],
            'results': observed.get('results'),
            'expected_conversation': conversation_id,
            'actual_conversation': observed.get('task', {}).get('convId'),
            'tool_stage': observed.get('task', {}).get('_remotePaperJourneyStage'),
        })
        page.wait_for_function("document.querySelector('#paperPreviewPanel .paper-reader-text')?.textContent.includes('Remote committed preview')", timeout=30000)
        assert (device.paper / 'main.tex').read_text() == source
        types = {command['type'] for command in device.commands}
        assert {'project_read_files', 'project_write_file', 'paper_resolve', 'paper_build'} <= types
        panel = page.locator('#paperPreviewPanel')
        document_id, build_id = panel.get_attribute('data-document-id'), panel.get_attribute('data-build-id')
        with scheduler.cache.lease(1, document_id, build_id) as build:
            assert build.receipt['target'] == 'desktop' and build.receipt['version_verified']
        assert page.locator('[data-conversation-action="undo-turn-files"]').count() == 0
        assert submitted
        panel.locator('.paper-preview-viewport').focus()
        panel.locator('.paper-preview-viewport').press('Escape')
        page.reload()
        _wait_app_ready(page)
        _dismiss_onboarding_modals(page)
        page.wait_for_function("document.querySelector('#chatInner')?.innerText.includes('main.tex')")
        assert page.locator('#paperPreviewPanel').count() == 0
        assert len([url for url in submitted if url.endswith('/paper/builds')]) == 1
    finally:
        scheduler.shutdown()


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



def test_translation_overlay_keeps_completed_checkpoint_and_updates_browser(page, assert_no_js_errors):
    """Real app/storage/translation/invalidation/DOM; provider transport is stubbed."""
    from lib.storage.model_continuation import ModelContinuationStore
    from lib.translate.commit import commit_translation_to_turn
    from lib.turn_lifecycle import get_turn
    _fresh_chat(page)
    _send_and_wait_done(page, 'translation continuity E2E')
    active = page.locator('.conv-item.active')
    active.wait_for(state='attached', timeout=15000)
    conversation_id = active.get_attribute('data-conv-id')
    _wait_conversation_persisted(page, conversation_id, 'stubbed model')
    snapshot = page.request.get(
        f"{page.url.rstrip('/')}/api/v4/conversations/{conversation_id}/sync").json()
    terminal = next(turn for turn in reversed(snapshot['turns'])
                    if turn['actor'] == 'assistant' and turn['status'] == 'completed')
    before = get_turn(conversation_id, terminal['turnId'], user_id=1)
    cache = ModelContinuationStore()
    checkpoint = cache.get(user_id=1, conversation_id=conversation_id, lane_id='main')
    assert checkpoint is not None
    result = commit_translation_to_turn(
        conversation_id, terminal['turnId'], 'translatedContent', '后台展示译文验收',
        user_id=1, model='synthetic translator')
    after = result['turn']
    assert after['projectionRevision'] > before['projectionRevision']
    assert after['modelContextRevision'] == before['modelContextRevision']
    assert cache.get(user_id=1, conversation_id=conversation_id, lane_id='main') == checkpoint
    page.wait_for_function("document.querySelector('#chatInner').innerText.includes('后台展示译文验收')",
                           timeout=15000)
    _send_and_wait_done(page, 'next turn after translation E2E', expected_replies=1)
    next_checkpoint = cache.get(user_id=1, conversation_id=conversation_id, lane_id='main')
    assert next_checkpoint['historyProof']['version'] == 2
    assert next_checkpoint['historyProof']['count'] > checkpoint['historyProof']['count']
