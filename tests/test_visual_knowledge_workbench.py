"""Real-browser QA for the standalone local-knowledge workbench."""

from pathlib import Path
import re

import pytest


pytestmark = [pytest.mark.visual, pytest.mark.slow]


def _surface_fit(page):
    return page.evaluate('''() => {
      const ids = ['knowledgeModal', 'knowledgeStats'];
      const selectors = ['.kb-workbench', '.kb-header', '.kb-body'];
      const nodes = ids.map(id => document.getElementById(id))
        .concat(selectors.map(selector => document.querySelector(selector)));
      return {
        viewport: { width: innerWidth, height: innerHeight },
        documentScrollX: document.documentElement.scrollWidth >
          document.documentElement.clientWidth,
        regions: nodes.filter(Boolean).map(node => {
          const r = node.getBoundingClientRect();
          return { key: node.id || node.className, left: r.left, top: r.top,
                   right: r.right, bottom: r.bottom, width: r.width,
                   height: r.height };
        }),
      };
    }''')


def _knowledge_harness():
    root = Path(__file__).resolve().parents[1]
    index = (root / 'index.html').read_text(encoding='utf-8')
    modal = re.search(
        r'<!-- Local Knowledge.*?(<div class="modal-overlay kb-overlay".*?</div>)\s*'
        r'<!-- Preview Modal -->', index, re.S)
    assert modal, 'could not extract #knowledgeModal from index.html'
    launcher = '''<div class="topbar"><div class="topbar-tools">
      <button class="topbar-tool-btn" id="knowledgeTopbarBtn"
        onclick="openKnowledgeBase()"><span>知识库</span></button>
    </div></div>'''
    html = f'''<!doctype html><html data-theme="tofu"><head>
      <meta charset="utf-8"><meta name="viewport" content="width=device-width">
      <link rel="stylesheet" href="/styles.css">
      </head><body>{launcher}{modal.group(1)}
      <script src="/i18n.js"></script>
      <script src="/escape.js"></script>
      <script src="/dialog.js"></script>
      <script src="/mock.js"></script>
      <script src="/knowledge.js"></script>
      </body></html>'''
    mock = r'''
      window.showToast = function () {};
      window._mockKnowledge = {
        enabled: false, available: false, visual_enrichment: false, documents: [],
        totals: { documents: 0, chunks: 0, assets: 0, pending_assets: 0,
                  asset_issues: 0, text_chars: 0, size_bytes: 0 },
        facets: [],
        pagination: { page: 1, page_size: 30, total_items: 0, total_pages: 1,
                      has_previous: false, has_next: false },
        limits: { max_file_bytes: 52428800, max_batch_bytes: 209715200,
                  max_batch_files: 20 },
        supported_extensions: ['.pdf','.docx','.xlsx','.pptx','.odt','.epub',
                               '.eml','.rtf','.md','.txt'],
        privacy: 'local_only'
      };
      function _mockSnapshot() { return JSON.parse(JSON.stringify(_mockKnowledge)); }
      window.Api.knowledge = {
        status: async function (options) {
          var snapshot = _mockSnapshot(), value = options || {};
          var docs = snapshot.documents.slice();
          if (value.category && value.category !== 'all') {
            docs = docs.filter(function (doc) { return doc.category === value.category; });
          }
          if (value.query) {
            var needle = String(value.query).toLowerCase();
            docs = docs.filter(function (doc) {
              return doc.name.toLowerCase().includes(needle);
            });
          }
          if (value.sort === 'name_asc') {
            docs.sort(function (a, b) { return a.name.localeCompare(b.name); });
          }
          var pageSize = Number(value.page_size || 30), page = Number(value.page || 1);
          var pages = Math.max(1, Math.ceil(docs.length / pageSize));
          page = Math.min(page, pages);
          snapshot.documents = docs.slice((page - 1) * pageSize, page * pageSize);
          snapshot.pagination = { page: page, page_size: pageSize,
            total_items: docs.length, total_pages: pages,
            has_previous: page > 1, has_next: page < pages };
          return snapshot;
        },
        activity: async function () {
          return { pending_assets: _mockKnowledge.totals.pending_assets,
            asset_issues: _mockKnowledge.totals.asset_issues,
            visual_enrichment: _mockKnowledge.visual_enrichment };
        },
        setEnabled: async function (enabled) {
          _mockKnowledge.enabled = !!enabled;
          _mockKnowledge.available = !!enabled && _mockKnowledge.documents.length > 0;
          return _mockSnapshot();
        },
        setVisualEnrichment: async function (enabled) {
          _mockKnowledge.visual_enrichment = !!enabled;
          return _mockSnapshot();
        },
        upload: async function (form) {
          var files = form.getAll('files');
          var indexed = [], errors = [];
          for (var file of files) {
            var bytes = new Uint8Array(await file.arrayBuffer());
            if (file.name.endsWith('.bin')) {
              errors.push({ name: file.name, error: '不支持的二进制文件类型' });
              continue;
            }
            var doc = { id: 'doc-' + (_mockKnowledge.documents.length + 1),
              name: file.name, kind: '.md', method: 'plaintext (.md)',
              category: 'text', size_bytes: bytes.length, text_chars: 9000,
              chunk_count: 90,
              asset_count: 1,
              pending_asset_count: 0, asset_issue_count: 0,
              warnings: [], created_at: Date.now() / 1000,
              updated_at: Date.now() / 1000, duplicate: false };
            _mockKnowledge.documents.unshift(doc); indexed.push(doc);
          }
          _mockKnowledge.enabled = _mockKnowledge.documents.length > 0
            ? (_mockKnowledge.enabled || indexed.length > 0) : false;
          _mockKnowledge.available = _mockKnowledge.enabled &&
            _mockKnowledge.documents.length > 0;
          _mockKnowledge.totals = {
            documents: _mockKnowledge.documents.length,
            chunks: _mockKnowledge.documents.length * 90,
            assets: _mockKnowledge.documents.reduce(function (n, d) {
              return n + Number(d.asset_count || 0);
            }, 0), pending_assets: 0, asset_issues: 0,
            text_chars: _mockKnowledge.documents.length * 9000,
            size_bytes: _mockKnowledge.documents.reduce(function (n, d) {
              return n + d.size_bytes;
            }, 0)
          };
          _mockKnowledge.facets = [{ category: 'text', count: _mockKnowledge.documents.length }];
          _mockKnowledge.pagination = { page: 1, page_size: 30,
            total_items: _mockKnowledge.documents.length, total_pages: 1,
            has_previous: false, has_next: false };
          return Object.assign(_mockSnapshot(), { indexed: indexed, errors: errors });
        },
        search: async function (query) {
          return { query: query, count: 1, results: [{ source: '值班手册.md',
            section: '联系方式', location: 'lines 1-3',
            excerpt: '# 联系方式\n\n夜间值班电话是 12345。', score: 9.5,
            assets: [{ id: 'asset-1', kind: 'figure', page: 2,
              caption: '值班流程图', url: '/visual-thumb.svg',
              thumbnail_url: '/visual-thumb.svg' }] }] };
        },
        content: async function (id, offset, limit) {
          var chunks = Array.from({ length: 90 }, function (_, index) {
            return { ordinal: index,
              section: index ? '值班流程 ' + (index + 1) : '联系方式',
              location: 'lines ' + (index * 3 + 1) + '-' + (index * 3 + 3),
              content: index ? ('这是用于核对解析结果的正文段落。'.repeat(8))
                : '# 联系方式\n\n夜间值班电话是 12345。' };
          });
          var start = Number(offset || 0), size = Number(limit || 80);
          return { document: { id: id, name: '值班手册.md' },
            chunks: chunks.slice(start, start + size),
            pagination: { offset: start, limit: size, total_items: chunks.length,
                          has_more: start + size < chunks.length } };
        },
        remove: async function (id) {
          _mockKnowledge.documents = _mockKnowledge.documents.filter(function (d) {
            return d.id !== id;
          });
          _mockKnowledge.enabled = false; _mockKnowledge.available = false;
          _mockKnowledge.totals = { documents: 0, chunks: 0, assets: 0,
            pending_assets: 0, asset_issues: 0, text_chars: 0, size_bytes: 0 };
          _mockKnowledge.facets = [];
          _mockKnowledge.pagination = { page: 1, page_size: 30,
            total_items: 0, total_pages: 1, has_previous: false, has_next: false };
          return _mockSnapshot();
        }
      };
    '''
    return root, html, mock


def test_knowledge_upload_preview_toggle_and_responsive_fit(page, tmp_path):
    _, _, mock = _knowledge_harness()
    page.route(
        '**/visual-thumb.svg',
        lambda route: route.fulfill(
            status=200,
            content_type='image/svg+xml',
            body='''<svg xmlns="http://www.w3.org/2000/svg" width="160" height="100"><rect width="160" height="100" fill="#48a062"/><circle cx="45" cy="50" r="22" fill="#fff"/><path d="M75 50h60" stroke="#fff" stroke-width="8"/></svg>''',
        ),
    )
    page.set_viewport_size({'width': 1440, 'height': 900})
    page.wait_for_function(
        "window.TofuModules?.version === 3 && window.Api?.knowledge")
    page.evaluate(mock)
    hard_errors = getattr(page, '_tofu_js_errors', [])
    hard_errors.clear()
    page.evaluate("""async () => {
      await window.TofuModules.invokeFeature('openKnowledgeBase', [], () => {});
    }""")
    page.locator('#knowledgeModal.open').wait_for(state='visible')
    page.locator('.kb-empty-state').wait_for(state='visible')

    # Critical flow: choose a real file → parse/index → inspect result card.
    page.locator('#knowledgeFileInput').set_input_files({
        'name': '值班手册.md',
        'mimeType': 'text/markdown',
        'buffer': '# 联系方式\n\n夜间值班电话是 12345。\n'.encode(),
    })
    page.locator('.kb-doc-card').wait_for(state='visible', timeout=20000)
    assert page.locator('.kb-doc-title-row').filter(
        has_text='值班手册.md').count() == 1
    assert page.locator('#knowledgeEnabled').get_attribute('aria-checked') == 'true'

    # Catalog controls use server-side filters while keeping the DOM page bounded.
    page.locator('#knowledgeCategories button').filter(has_text='文本与代码').click()
    assert page.locator(
        '#knowledgeCategories button[aria-pressed="true"]').filter(
            has_text='文本与代码').count() == 1
    page.locator('#knowledgeCategories button').filter(has_text='全部').click()
    page.locator('#knowledgeCatalogQuery').fill('不存在的资料')
    page.locator('.kb-empty-state').wait_for(state='visible')
    assert '没有符合当前筛选' in page.locator('.kb-empty-state').text_content()
    page.locator('#knowledgeCatalogClear').click()
    page.locator('.kb-doc-card').wait_for(state='visible')
    page.locator('#knowledgeCatalogSort').select_option('name_asc')

    # Page controls are exercised with a dense staged catalogue; navigation
    # itself still happens through the visible user controls.
    page.evaluate('''async () => {
      var original = _mockKnowledge.documents[0];
      _mockKnowledge.documents = Array.from({ length: 31 }, function (_, index) {
        return Object.assign({}, original, {
          id: 'page-doc-' + index,
          name: '分页资料-' + String(index).padStart(2, '0') + '.md'
        });
      });
      _mockKnowledge.totals.documents = 31;
      _mockKnowledge.totals.chunks = 31 * 90;
      _mockKnowledge.totals.assets = 31;
      _mockKnowledge.facets = [{ category: 'text', count: 31 }];
      await window.TofuModules.resolveAction('_knowledgeRefresh')();
    }''')
    page.locator('#knowledgePagination button').filter(has_text='下一页').click()
    page.wait_for_function(
        "document.getElementById('knowledgeCatalogSummary').textContent.includes('31') && "
        "document.querySelectorAll('.kb-doc-card').length === 1")
    assert '第 2 / 2 页' in page.locator('#knowledgePagination').text_content()
    page.locator('#knowledgePagination button').filter(has_text='上一页').click()
    page.wait_for_function("document.querySelectorAll('.kb-doc-card').length === 30")
    page.evaluate('''async () => {
      _mockKnowledge.documents = [_mockKnowledge.documents[0]];
      _mockKnowledge.documents[0].id = 'doc-1';
      _mockKnowledge.documents[0].name = '值班手册.md';
      _mockKnowledge.totals.documents = 1;
      _mockKnowledge.totals.chunks = 90;
      _mockKnowledge.totals.assets = 1;
      _mockKnowledge.facets = [{ category: 'text', count: 1 }];
      await window.TofuModules.resolveAction('_knowledgeRefresh')();
    }''')

    # Parsed text is inspectable without guessing from retrieval results.
    page.locator('.kb-doc-view').click()
    page.locator('.kb-doc-preview').wait_for(state='visible')
    assert '夜间值班电话是 12345' in page.locator(
        '.kb-preview-chunk pre').first.text_content()
    content_shot = Path(tmp_path) / 'knowledge-parsed-content.png'
    page.screenshot(path=str(content_shot), full_page=False)

    # Regression: background enrichment polling must not recreate the parsed
    # body and throw a reader back to its first chunk.
    preview_body = page.locator('.kb-preview-body')
    preview_body.hover()
    page.mouse.wheel(0, 720)
    page.wait_for_timeout(120)
    scroll_before = preview_body.evaluate('(node) => node.scrollTop')
    assert scroll_before > 200
    page.evaluate('''async () => {
      _mockKnowledge.totals.pending_assets = 1;
      _mockKnowledge.documents[0].pending_asset_count = 1;
      await window.TofuModules.resolveAction('_knowledgeRefresh')();
    }''')
    page.wait_for_timeout(2800)
    scroll_after = preview_body.evaluate('(node) => node.scrollTop')
    assert abs(scroll_after - scroll_before) <= 2
    page.evaluate('''async () => {
      _mockKnowledge.totals.pending_assets = 0;
      _mockKnowledge.documents[0].pending_asset_count = 0;
      await window.TofuModules.resolveAction('_knowledgeRefresh')();
    }''')
    page.locator('.kb-preview-more').click()
    page.wait_for_function(
        "document.querySelectorAll('.kb-preview-chunk').length === 90")
    assert page.locator('.kb-preview-more').count() == 0
    page.locator('.kb-preview-head button').click()

    # Retrieval preview is a user-facing proof of the index, not just a DB row.
    page.locator('#knowledgeSearchInput').fill('夜间值班电话')
    page.locator('#knowledgeSearchBtn').click()
    page.locator('.kb-result').wait_for(state='visible')
    assert '12345' in page.locator('.kb-result pre').first.text_content()
    page.locator('.kb-result-assets img').wait_for(state='visible')
    assert page.locator('.kb-result-assets img').evaluate(
        '(image) => image.naturalWidth > 0')

    # Explicit privacy consent is required, and the reversible switch completes
    # its full off → confirm/on → off cycle through real controls.
    page.locator('#knowledgeVisualEnrichment').click()
    page.locator('.app-dialog-ok').click()
    page.wait_for_function(
        "document.getElementById('knowledgeVisualEnrichment').getAttribute('aria-checked') === 'true'")
    assert '视觉模型' in page.locator('#knowledgeVisualHint').text_content()
    page.locator('#knowledgeVisualEnrichment').click()
    page.wait_for_function(
        "document.getElementById('knowledgeVisualEnrichment').getAttribute('aria-checked') === 'false'")

    # Full switch cycle: off keeps the index and preview search available.
    page.locator('#knowledgeEnabled').click()
    page.wait_for_function(
        "document.getElementById('knowledgeEnabled').getAttribute('aria-checked') === 'false'")
    page.locator('#knowledgeSearchInput').fill('12345')
    page.locator('#knowledgeSearchBtn').click()
    page.locator('.kb-result').wait_for(state='visible')
    assert '12345' in page.locator('.kb-result pre').first.text_content()

    # Off-happy-path: an unsupported binary is isolated into visible feedback.
    page.locator('#knowledgeFileInput').set_input_files({
        'name': 'opaque.bin',
        'mimeType': 'application/octet-stream',
        'buffer': bytes(range(32)) * 3,
    })
    page.locator('.kb-upload-report.is-warning').wait_for(
        state='visible', timeout=20000)
    assert 'opaque.bin' in page.locator('#knowledgeUploadReport').text_content()
    assert page.locator('.kb-doc-card').count() == 1

    desktop_fit = _surface_fit(page)
    assert not desktop_fit['documentScrollX']
    for region in desktop_fit['regions']:
        assert region['left'] >= -1 and region['right'] <= 1441, region
        assert region['top'] >= -1 and region['bottom'] <= 901, region
    desktop_shot = Path(tmp_path) / 'knowledge-desktop.png'
    page.screenshot(path=str(desktop_shot), full_page=False)

    # Small realistic viewport: fixed shell stays fitted; its body owns scroll.
    page.set_viewport_size({'width': 390, 'height': 844})
    page.wait_for_timeout(250)
    mobile_fit = _surface_fit(page)
    assert not mobile_fit['documentScrollX']
    workbench = next(
        region for region in mobile_fit['regions']
        if region['key'] == 'kb-workbench')
    assert workbench['width'] <= 391
    assert workbench['height'] <= 845
    mobile_shot = Path(tmp_path) / 'knowledge-mobile.png'
    page.screenshot(path=str(mobile_shot), full_page=False)
    page.locator('#knowledgeVisualEnrichment').scroll_into_view_if_needed()
    mobile_settings_shot = Path(tmp_path) / 'knowledge-mobile-settings.png'
    page.screenshot(path=str(mobile_settings_shot), full_page=False)
    print('knowledge screenshots: '
          f'{content_shot} {desktop_shot} {mobile_shot} {mobile_settings_shot}')

    # Cleanup through the real UI and custom confirmation dialog.
    page.locator('.kb-doc-delete').click()
    page.locator('.app-dialog-ok').click()
    page.locator('.kb-empty-state').wait_for(state='visible')
    assert not hard_errors, hard_errors


def test_knowledge_long_results_never_overlap_the_panel_footer(page, tmp_path):
    """Regression (2026-08-14): three long excerpts collapsed the flex column.

    ``.kb-search-results`` used to carry the one-token ``flex: 1`` inside the
    scrollable ``.kb-search-panel`` column, so the box shrank below its content
    height and the overflowing result cards painted THROUGH ``.kb-panel-foot``
    (text-on-text in the owner's screenshot). Thumbnails on the same cards must
    also really load — a broken ``<img>`` is how the deployment-base URL bug
    first showed up.
    """
    _, _, mock = _knowledge_harness()
    page.route(
        '**/visual-thumb.svg',
        lambda route: route.fulfill(
            status=200,
            content_type='image/svg+xml',
            body='''<svg xmlns="http://www.w3.org/2000/svg" width="160" height="100"><rect width="160" height="100" fill="#b8860b"/><circle cx="45" cy="50" r="22" fill="#fff"/><path d="M75 50h60" stroke="#fff" stroke-width="8"/></svg>''',
        ),
    )
    page.set_viewport_size({'width': 1440, 'height': 900})
    page.wait_for_function(
        "window.TofuModules?.version === 3 && window.Api?.knowledge")
    page.evaluate(mock)
    hard_errors = getattr(page, '_tofu_js_errors', [])
    hard_errors.clear()
    page.evaluate('''() => {
      _mockKnowledge.documents = [{ id: 'doc-acl', name: 'ACL26接受邮件.pdf',
        kind: '.pdf', method: 'pymupdf4llm', category: 'pdf',
        size_bytes: 96 * 1024, text_chars: 3619, chunk_count: 5,
        asset_count: 1, pending_asset_count: 0, asset_issue_count: 0,
        warnings: [], created_at: Date.now() / 1000,
        updated_at: Date.now() / 1000, duplicate: false }];
      _mockKnowledge.enabled = true;
      _mockKnowledge.available = true;
      _mockKnowledge.totals = { documents: 1, chunks: 5, assets: 1,
        pending_assets: 0, asset_issues: 0, text_chars: 3619,
        size_bytes: 96 * 1024 };
      _mockKnowledge.facets = [{ category: 'pdf', count: 1 }];
      _mockKnowledge.pagination = { page: 1, page_size: 30, total_items: 1,
        total_pages: 1, has_previous: false, has_next: false };
      var excerpt = ('[ACL 2026] Decision notification for your submission ' +
        '4556: MTR-Suite: A Framework for Evaluating and Synthesizing ' +
        'Conversational Retrieval Benchmarks. Registration and attendance ' +
        'details follow. ').repeat(6);
      window.Api.knowledge.search = async function (query) {
        function result(index) {
          return { source: 'ACL26接受邮件.pdf', section: 'Decision notification',
            location: 'lines ' + (index * 10 + 1) + '-' + (index * 10 + 9),
            excerpt: excerpt, score: 9.5 - index,
            assets: [{ id: 'asset-' + index, kind: 'image', page: 1,
              caption: 'Embedded image on page 1', url: '/visual-thumb.svg',
              thumbnail_url: '/visual-thumb.svg' }] };
        }
        return { query: query, count: 3,
          results: [result(0), result(1), result(2)] };
      };
    }''')
    page.evaluate("""async () => {
      await window.TofuModules.invokeFeature('openKnowledgeBase', [], () => {});
    }""")
    page.locator('#knowledgeModal.open').wait_for(state='visible')
    page.locator('.kb-doc-card').wait_for(state='visible')

    page.locator('#knowledgeSearchInput').fill('ACL26')
    page.locator('#knowledgeSearchBtn').click()
    page.wait_for_function(
        "document.querySelectorAll('.kb-result').length === 3")
    page.locator('.kb-result-assets img').first.wait_for(state='visible')
    assert page.locator('.kb-result-assets img').first.evaluate(
        '(image) => image.naturalWidth > 0'), (
        'the result thumbnail must actually load — a broken image was the '
        'first visible symptom of the deployment-base URL bug')

    geometry = page.evaluate('''() => {
      const panel = document.querySelector('.kb-search-panel');
      const foot = document.querySelector('.kb-panel-foot')
        .getBoundingClientRect();
      const cards = Array.from(document.querySelectorAll('.kb-result'))
        .map((node) => node.getBoundingClientRect());
      const results = document.querySelector('.kb-search-results')
        .getBoundingClientRect();
      return {
        panelScrollable: panel.scrollHeight > panel.clientHeight + 1,
        lastCardBottom: cards[cards.length - 1].bottom,
        resultsBottom: results.bottom,
        footTop: foot.top,
        overlap: cards[cards.length - 1].bottom - foot.top,
      };
    }''')
    assert geometry['panelScrollable'], (
        'the staged results must genuinely overflow the panel, otherwise the '
        f"overlap assertions below prove nothing: {geometry}")
    assert geometry['resultsBottom'] >= geometry['lastCardBottom'] - 1, (
        'the results box must CONTAIN its cards — a box that stops short is '
        f"the shrunken-flex regression: {geometry}")
    assert geometry['overlap'] <= 1, (
        'the last result card paints through the panel footer '
        f"(overlap={geometry['overlap']:.1f}px): {geometry}")

    shot = Path(tmp_path) / 'knowledge-long-results.png'
    page.screenshot(path=str(shot), full_page=False)
    print(f'knowledge long-results screenshot: {shot}')
    assert not hard_errors, hard_errors
