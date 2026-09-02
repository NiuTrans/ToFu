/* ===== migrated source: knowledge.js ===== */
/* ═══════════════════════════════════════════════════════════════
   knowledge.js — standalone local knowledge workbench

   This surface deliberately does not live in Settings → Tools.  It owns the
   corpus lifecycle (upload, parse feedback, access gate, deletion) and an
   index-preview search so users can validate evidence before exposing the
   read-only search tool to a model.
   ═══════════════════════════════════════════════════════════════ */

var _knowledgeState = {
  data: null,
  loading: false,
  mutating: false,
  searching: false,
  requestSeq: 0,
  uploadReport: null,
  searchResult: null,
  contentView: null,
  refreshTimer: null,
  catalogTimer: null,
  returnFocus: null,
  page: 1,
  pageSize: 30,
  query: '',
  category: 'all',
  sort: 'updated_desc',
  documentSignature: '',
  resetListScroll: false,
};

function _knowledgeEsc(value) {
  if (typeof escapeHtml === 'function') return escapeHtml(String(value == null ? '' : value));
  return String(value == null ? '' : value)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

// Server payloads hand us root-relative asset URLs, but the page itself may
// be served under a path-prefix gateway (VS Code /proxy/<port>/, a reverse
// proxy subpath). Root-relative <img>/<a> targets escape that prefix and die
// at the gateway — every fetch() already goes through the apiUrl() base seam,
// so resolve asset URLs through the same seam.
function _knowledgeAssetUrl(url) {
  var value = String(url || '');
  if (value.charAt(0) !== '/') return value;
  if (typeof apiUrl === 'function') return apiUrl(value);
  var path = (typeof window !== 'undefined' && window.location &&
    window.location.pathname) || '';
  return path.replace(/\/(index\.html)?$/, '') + value;
}

function _knowledgeBytes(value) {
  var size = Number(value || 0);
  if (size < 1024) return Math.round(size) + ' B';
  if (size < 1048576) return (size / 1024).toFixed(size < 10240 ? 1 : 0) + ' KB';
  if (size < 1073741824) return (size / 1048576).toFixed(size < 10485760 ? 1 : 0) + ' MB';
  return (size / 1073741824).toFixed(1) + ' GB';
}

function _knowledgeCount(value) {
  try {
    return Number(value || 0).toLocaleString(
      (typeof _i18nLang !== 'undefined' && _i18nLang === 'en') ? 'en-US' : 'zh-CN');
  } catch (e) {
    return String(Number(value || 0));
  }
}

function _knowledgeDate(timestamp) {
  if (!timestamp) return '';
  try {
    return new Date(Number(timestamp) * 1000).toLocaleDateString(
      (typeof _i18nLang !== 'undefined' && _i18nLang === 'en') ? 'en-US' : 'zh-CN',
      { year: 'numeric', month: 'short', day: 'numeric' });
  } catch (e) {
    return '';
  }
}

function openKnowledgeBase() {
  var modal = document.getElementById('knowledgeModal');
  if (!modal) return;
  _knowledgeState.returnFocus = document.activeElement;
  modal.classList.add('open');
  document.body.classList.add('kb-open');
  // Tell the full-page chat-drop handler to stand down so a file dropped onto
  // the knowledge dropzone is uploaded to the corpus, not attached to chat.
  runtimeScope._tofuKnowledgeModalOpen = true;
  var button = document.getElementById('knowledgeTopbarBtn');
  if (button) button.classList.add('active');
  _knowledgeRefresh();
  setTimeout(function () {
    var target = document.getElementById('knowledgeSearchInput');
    if (target) target.focus({ preventScroll: true });
  }, 80);
}

function closeKnowledgeBase() {
  var modal = document.getElementById('knowledgeModal');
  if (modal) modal.classList.remove('open');
  document.body.classList.remove('kb-open');
  runtimeScope._tofuKnowledgeModalOpen = false;
  if (_knowledgeState.refreshTimer) clearTimeout(_knowledgeState.refreshTimer);
  _knowledgeState.refreshTimer = null;
  if (_knowledgeState.catalogTimer) clearTimeout(_knowledgeState.catalogTimer);
  _knowledgeState.catalogTimer = null;
  var button = document.getElementById('knowledgeTopbarBtn');
  if (button) button.classList.remove('active');
  var previous = _knowledgeState.returnFocus;
  _knowledgeState.returnFocus = null;
  if (previous && typeof previous.focus === 'function') {
    try { previous.focus(); } catch (e) { /* focus recovery is best effort */ }
  }
}

async function _knowledgeRefresh() {
  var seq = ++_knowledgeState.requestSeq;
  _knowledgeState.loading = true;
  _knowledgeRender();
  try {
    var data = await Api.knowledge.status({
      page: _knowledgeState.page,
      page_size: _knowledgeState.pageSize,
      query: _knowledgeState.query,
      category: _knowledgeState.category,
      sort: _knowledgeState.sort,
    });
    if (seq !== _knowledgeState.requestSeq) return;
    _knowledgeState.data = data || null;
    var pagination = data && data.pagination;
    if (pagination) _knowledgeState.page = Number(pagination.page || 1);
  } catch (e) {
    if (seq !== _knowledgeState.requestSeq) return;
    _knowledgeState.data = { _error: (e && e.message) || t('knowledge.failed') };
  } finally {
    if (seq === _knowledgeState.requestSeq) {
      _knowledgeState.loading = false;
      _knowledgeRender();
    }
  }
}

function _knowledgeAccessState(data) {
  var docs = Number(data && data.totals && data.totals.documents || 0);
  if (!docs) return { key: 'knowledge.accessEmpty', tone: 'empty' };
  if (data.enabled) return { key: 'knowledge.accessOn', tone: 'on' };
  return { key: 'knowledge.accessOff', tone: 'off' };
}

function _knowledgeRenderStats(data) {
  var root = document.getElementById('knowledgeStats');
  if (!root) return;
  var totals = (data && data.totals) || {};
  var docs = Number(totals.documents || 0);
  root.dataset.empty = docs ? 'false' : 'true';
  var text = root.querySelector('.kb-stats-text');
  if (!text) return;
  text.textContent = docs ? t('knowledge.statsSummary', {
    docs: _knowledgeCount(docs),
    chunks: _knowledgeCount(totals.chunks),
    assets: _knowledgeCount(totals.assets),
    chars: _knowledgeCount(totals.text_chars),
    size: _knowledgeBytes(totals.size_bytes),
  }) : '';
}

function _knowledgeFileGlyph(kind) {
  var value = String(kind || '').replace('.', '').toUpperCase();
  if (!value) value = 'DOC';
  if (value.length > 5) value = value.slice(0, 5);
  return value;
}

function _knowledgeCaptureDocumentScroll() {
  var library = document.querySelector('.kb-library');
  var preview = document.querySelector('.kb-preview-body');
  return {
    library: library ? library.scrollTop : 0,
    preview: preview ? preview.scrollTop : 0,
    previewId: preview ? preview.getAttribute('data-document-id') : '',
  };
}

function _knowledgeRestoreDocumentScroll(snapshot) {
  var library = document.querySelector('.kb-library');
  if (library) {
    library.scrollTop = _knowledgeState.resetListScroll
      ? 0 : Number(snapshot && snapshot.library || 0);
  }
  _knowledgeState.resetListScroll = false;
  var preview = document.querySelector('.kb-preview-body');
  if (preview && snapshot && snapshot.previewId ===
      preview.getAttribute('data-document-id')) {
    preview.scrollTop = Number(snapshot.preview || 0);
  }
}

function _knowledgeDocumentSignature(data) {
  var docs = (data && data.documents) || [];
  var view = _knowledgeState.contentView;
  return JSON.stringify({
    lang: typeof _i18nLang === 'undefined' ? '' : _i18nLang,
    loading: !!(_knowledgeState.loading && !data),
    error: data && data._error || '',
    docs: docs.map(function (doc) {
      return [doc.id, doc.name, doc.kind, doc.category, doc.method,
        doc.size_bytes, doc.text_chars, doc.chunk_count, doc.asset_count,
        doc.pending_asset_count, doc.asset_issue_count, doc.updated_at,
        (doc.warnings || []).join('\n')];
    }),
    view: view ? [view.id, !!view.loading, !!view.loadingMore, view.error,
      Number(view.version || 0)] : null,
    filtered: Number(data && data.pagination && data.pagination.total_items || 0),
    total: Number(data && data.totals && data.totals.documents || 0),
  });
}

function _knowledgeContentPreview(doc) {
  var view = _knowledgeState.contentView;
  if (!view || view.id !== doc.id) return '';
  var body = '';
  if (view.loading) {
    body = '<div class="kb-preview-status"><span class="kb-spinner"></span><span>' +
      _knowledgeEsc(t('knowledge.contentLoading')) + '</span></div>';
  } else if (view.error) {
    body = '<div class="kb-preview-status is-error"><span>' +
      _knowledgeEsc(view.error) + '</span><button type="button" data-tofu-action="_knowledgeToggleContent(\'' +
      _knowledgeEsc(doc.id) + '\', true)">' + _knowledgeEsc(t('knowledge.retry')) + '</button></div>';
  } else {
    var chunks = (view.payload && view.payload.chunks) || [];
    var contentPagination = (view.payload && view.payload.pagination) || {};
    body = chunks.length ? '<div class="kb-preview-body">' + chunks.map(function (chunk, index) {
      var ordinal = Number.isFinite(Number(chunk.ordinal)) ? Number(chunk.ordinal) + 1 : index + 1;
      var title = chunk.section || t('knowledge.chunkTitle', { n: ordinal });
      return '<section class="kb-preview-chunk"><div class="kb-preview-chunk-head"><span class="kb-preview-ordinal">' +
        _knowledgeEsc(ordinal) + '</span><strong>' +
        _knowledgeEsc(title) + '</strong>' + (chunk.location ? '<span>' +
          _knowledgeEsc(chunk.location) + '</span>' : '') + '</div><pre>' +
        _knowledgeEsc(chunk.content || '') + '</pre></section>';
    }).join('') + (contentPagination.has_more
      ? '<button class="kb-preview-more" type="button" data-tofu-action="_knowledgeLoadMoreContent(\'' +
        _knowledgeEsc(doc.id) + '\')"' + (view.loadingMore ? ' disabled' : '') + '>' +
        _knowledgeEsc(t(view.loadingMore
          ? 'knowledge.loadingMoreContent' : 'knowledge.loadMoreContent')) + '</button>' : '') +
      '</div>' : '<div class="kb-preview-status"><span>' +
      _knowledgeEsc(t('knowledge.contentEmpty')) + '</span></div>';
  }
  return '<section class="kb-doc-preview" aria-live="polite"><div class="kb-preview-head"><div><strong>' +
    _knowledgeEsc(t('knowledge.parsedContent')) + '</strong><span>' +
    _knowledgeEsc(view.payload ? t('knowledge.contentProgress', {
      loaded: ((view.payload && view.payload.chunks) || []).length,
      total: Number(view.payload && view.payload.pagination &&
        view.payload.pagination.total_items || ((view.payload && view.payload.chunks) || []).length),
    }) : t('knowledge.parsedContentHint')) + '</span></div><button type="button" ' +
    'data-tofu-action="_knowledgeToggleContent(\'' + _knowledgeEsc(doc.id) + '\')" aria-label="' +
    _knowledgeEsc(t('knowledge.closeContent')) + '">×</button></div>' + body + '</section>';
}

function _knowledgeRenderDocuments(data) {
  var root = document.getElementById('knowledgeDocs');
  if (!root) return;
  var signature = _knowledgeDocumentSignature(data);
  if (signature === _knowledgeState.documentSignature) return;
  var scroll = _knowledgeCaptureDocumentScroll();
  _knowledgeState.documentSignature = signature;
  if (_knowledgeState.loading && !data) {
    root.innerHTML = '<div class="kb-loading"><span class="kb-spinner"></span><span>' +
      _knowledgeEsc(t('knowledge.loading')) + '</span></div>';
    _knowledgeRestoreDocumentScroll(scroll);
    return;
  }
  if (data && data._error) {
    root.innerHTML = '<div class="kb-error-state"><strong>' +
      _knowledgeEsc(t('knowledge.loadFailed')) + '</strong><span>' +
      _knowledgeEsc(data._error) + '</span><button type="button" data-tofu-action="_knowledgeRefresh()">' +
      _knowledgeEsc(t('knowledge.retry')) + '</button></div>';
    _knowledgeRestoreDocumentScroll(scroll);
    return;
  }
  var docs = (data && data.documents) || [];
  if (!docs.length) {
    var hasCorpus = Number(data && data.totals && data.totals.documents || 0) > 0;
    root.innerHTML = '<div class="kb-empty-state"><span class="kb-empty-mark">' +
      (hasCorpus ? '⌕' : '＋') + '</span><strong>' +
      _knowledgeEsc(t(hasCorpus
        ? 'knowledge.catalogEmptyFilter' : 'knowledge.emptyTitle')) + '</strong><p>' +
      _knowledgeEsc(t(hasCorpus
        ? 'knowledge.catalogEmptyFilterHint' : 'knowledge.emptyHint')) + '</p>' +
      (hasCorpus ? '<button type="button" data-tofu-action="_knowledgeClearCatalog()">' +
        _knowledgeEsc(t('knowledge.clearFilters')) + '</button>' : '') + '</div>';
    _knowledgeRestoreDocumentScroll(scroll);
    return;
  }
  root.innerHTML = docs.map(function (doc) {
    var warnings = (doc.warnings || []).filter(Boolean);
    var details = [
      doc.method || doc.kind || '',
      _knowledgeBytes(doc.size_bytes),
      t('knowledge.chunks', { n: _knowledgeCount(doc.chunk_count) }),
      Number(doc.asset_count || 0)
        ? t('knowledge.assets', { n: _knowledgeCount(doc.asset_count) }) : '',
      Number(doc.pending_asset_count || 0)
        ? t('knowledge.assetsPending', { n: _knowledgeCount(doc.pending_asset_count) }) : '',
      t('knowledge.chars', { n: _knowledgeCount(doc.text_chars) }),
      _knowledgeDate(doc.updated_at || doc.created_at),
    ].filter(Boolean).join(' · ');
    var contentOpen = _knowledgeState.contentView && _knowledgeState.contentView.id === doc.id;
    return '<article class="kb-doc-card" data-document-id="' + _knowledgeEsc(doc.id) + '">' +
      '<span class="kb-doc-kind">' + _knowledgeEsc(_knowledgeFileGlyph(doc.kind)) + '</span>' +
      '<div class="kb-doc-content"><div class="kb-doc-title-row"><strong>' +
      _knowledgeEsc(doc.name) + '</strong>' +
      (warnings.length ? '<span class="kb-warning-count" title="' +
        _knowledgeEsc(warnings.join('; ')) + '">⚠ ' + warnings.length + '</span>' : '') +
      '</div><div class="kb-doc-meta">' + _knowledgeEsc(details) + '</div>' +
      (warnings.length ? '<details class="kb-doc-warnings"><summary>' +
        _knowledgeEsc(t('knowledge.parseNotes')) + '</summary><ul>' + warnings.map(function (warning) {
          return '<li>' + _knowledgeEsc(warning) + '</li>';
        }).join('') + '</ul></details>' : '') + '</div>' +
      '<div class="kb-doc-actions"><button class="kb-doc-view" type="button" data-tofu-action="_knowledgeToggleContent(\'' +
      _knowledgeEsc(doc.id) + '\')" aria-expanded="' + (contentOpen ? 'true' : 'false') +
      '" title="' + _knowledgeEsc(t('knowledge.viewContent')) + '">' +
      '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/></svg><span>' +
      _knowledgeEsc(t('knowledge.viewContent')) + '</span></button>' +
      '<button class="kb-doc-reindex" type="button" data-tofu-action="_knowledgeReindex(\'' +
      _knowledgeEsc(doc.id) + '\')" title="' + _knowledgeEsc(t('knowledge.reindex')) + '">' +
      '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6v5h-5"/><path d="M18.5 15a7 7 0 1 1-.8-8.9L20 11"/></svg></button>' +
      '<button class="kb-doc-delete" type="button" data-tofu-action="_knowledgeRemove(\'' +
      _knowledgeEsc(doc.id) + '\')" title="' + _knowledgeEsc(t('knowledge.remove')) + '">' +
      '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M3 6h18M8 6V4h8v2M19 6l-1 15H6L5 6M10 11v5M14 11v5"/></svg>' +
      '</button></div>' + _knowledgeContentPreview(doc) + '</article>';
  }).join('');
  var preview = root.querySelector('.kb-preview-body');
  if (preview && _knowledgeState.contentView) {
    preview.setAttribute('data-document-id', _knowledgeState.contentView.id);
  }
  _knowledgeRestoreDocumentScroll(scroll);
}

async function _knowledgeToggleContent(id, forceReload) {
  var current = _knowledgeState.contentView;
  if (!forceReload && current && current.id === id) {
    _knowledgeState.contentView = null;
    _knowledgeRenderDocuments(_knowledgeState.data);
    return;
  }
  if (!Api.knowledge || typeof Api.knowledge.content !== 'function') return;
  var view = {
    id: id, loading: true, loadingMore: false, error: '', payload: null,
    version: 0,
  };
  _knowledgeState.contentView = view;
  _knowledgeRenderDocuments(_knowledgeState.data);
  try {
    view.payload = await Api.knowledge.content(id, 0, 80);
  } catch (e) {
    view.error = (e && e.message) || t('knowledge.contentFailed');
  } finally {
    view.loading = false;
    view.version += 1;
    if (_knowledgeState.contentView === view) {
      _knowledgeRenderDocuments(_knowledgeState.data);
    }
  }
}

async function _knowledgeLoadMoreContent(id) {
  var view = _knowledgeState.contentView;
  if (!view || view.id !== id || view.loading || view.loadingMore ||
      !view.payload || !view.payload.pagination ||
      !view.payload.pagination.has_more) return;
  view.loadingMore = true;
  view.version += 1;
  _knowledgeRenderDocuments(_knowledgeState.data);
  try {
    var offset = (view.payload.chunks || []).length;
    var next = await Api.knowledge.content(id, offset, 80);
    if (_knowledgeState.contentView !== view) return;
    view.payload.chunks = (view.payload.chunks || []).concat(
      (next && next.chunks) || []);
    view.payload.pagination = (next && next.pagination) || {
      total_items: view.payload.chunks.length,
      has_more: false,
    };
  } catch (e) {
    view.error = (e && e.message) || t('knowledge.contentFailed');
  } finally {
    view.loadingMore = false;
    view.version += 1;
    if (_knowledgeState.contentView === view) {
      _knowledgeRenderDocuments(_knowledgeState.data);
    }
  }
}

function _knowledgeRenderUploadReport() {
  var root = document.getElementById('knowledgeUploadReport');
  if (!root) return;
  var report = _knowledgeState.uploadReport;
  if (!report) {
    root.innerHTML = '';
    root.className = 'kb-upload-report';
    return;
  }
  if (report.pending) {
    root.className = 'kb-upload-report is-visible is-loading';
    root.innerHTML = '<span class="kb-spinner"></span><div><strong>' +
      _knowledgeEsc(t('knowledge.parsingFiles', { n: report.files.length })) + '</strong><span>' +
      _knowledgeEsc(report.files.map(function (file) { return file.name; }).join(' · ')) +
      '</span></div>';
    return;
  }
  var indexed = report.indexed || [];
  var errors = report.errors || [];
  var duplicates = indexed.filter(function (doc) { return doc.duplicate; }).length;
  root.className = 'kb-upload-report is-visible ' + (errors.length ? 'is-warning' : 'is-success');
  var title = errors.length
    ? (report.reindexed ? t('knowledge.reindexFailed') :
      t('knowledge.partial', { ok: indexed.length, bad: errors.length }))
    : (report.reindexed ? t('knowledge.reindexed') :
      t('knowledge.uploaded', { n: indexed.length }));
  var lines = [];
  if (duplicates) lines.push(t('knowledge.duplicates', { n: duplicates }));
  errors.forEach(function (item) { lines.push((item.name || '?') + ': ' + (item.error || t('knowledge.failed'))); });
  root.innerHTML = '<span class="kb-report-mark">' + (errors.length ? '!' : '✓') + '</span>' +
    '<div><strong>' + _knowledgeEsc(title) + '</strong>' +
    (lines.length ? '<span>' + _knowledgeEsc(lines.join(' · ')) + '</span>' : '') + '</div>' +
    '<button type="button" data-tofu-action="_knowledgeDismissUploadReport()" aria-label="' +
    _knowledgeEsc(t('knowledge.dismiss')) + '">×</button>';
}

function _knowledgeCategoryLabel(category) {
  var keys = {
    all: 'knowledge.categoryAll',
    pdf: 'knowledge.categoryPdf',
    document: 'knowledge.categoryDocument',
    spreadsheet: 'knowledge.categorySpreadsheet',
    presentation: 'knowledge.categoryPresentation',
    image: 'knowledge.categoryImage',
    email: 'knowledge.categoryEmail',
    ebook: 'knowledge.categoryEbook',
    text: 'knowledge.categoryText',
    other: 'knowledge.categoryOther',
  };
  return t(keys[category] || keys.other);
}

function _knowledgeRenderCatalog(data) {
  var facets = {};
  ((data && data.facets) || []).forEach(function (item) {
    facets[item.category] = Number(item.count || 0);
  });
  facets.all = Number(data && data.totals && data.totals.documents || 0);
  var categories = [
    'all', 'pdf', 'document', 'spreadsheet', 'presentation', 'image',
    'email', 'ebook', 'text', 'other',
  ];
  var categoryRoot = document.getElementById('knowledgeCategories');
  if (categoryRoot) {
    categoryRoot.innerHTML = categories.filter(function (category) {
      return category === 'all' || facets[category] ||
        category === _knowledgeState.category;
    }).map(function (category) {
      var active = category === _knowledgeState.category;
      return '<button type="button" data-tofu-action="_knowledgeSetCategory(\'' + category +
        '\')" aria-pressed="' + (active ? 'true' : 'false') + '"><span>' +
        _knowledgeEsc(_knowledgeCategoryLabel(category)) + '</span><strong>' +
        _knowledgeEsc(_knowledgeCount(facets[category] || 0)) + '</strong></button>';
    }).join('');
  }

  var queryInput = document.getElementById('knowledgeCatalogQuery');
  if (queryInput && queryInput.value !== _knowledgeState.query) {
    queryInput.value = _knowledgeState.query;
  }
  var clear = document.getElementById('knowledgeCatalogClear');
  if (clear) clear.hidden = !_knowledgeState.query;
  var sort = document.getElementById('knowledgeCatalogSort');
  if (sort && sort.value !== _knowledgeState.sort) sort.value = _knowledgeState.sort;

  var pagination = (data && data.pagination) || {
    page: 1, page_size: _knowledgeState.pageSize,
    total_items: ((data && data.documents) || []).length,
    total_pages: 1, has_previous: false, has_next: false,
  };
  var total = Number(pagination.total_items || 0);
  var page = Number(pagination.page || 1);
  var pageSize = Number(pagination.page_size || _knowledgeState.pageSize);
  var start = total ? ((page - 1) * pageSize) + 1 : 0;
  var end = Math.min(page * pageSize, total);
  var summary = document.getElementById('knowledgeCatalogSummary');
  if (summary) {
    summary.innerHTML = total ? '<span>' + _knowledgeEsc(t('knowledge.catalogShowing', {
      start: _knowledgeCount(start), end: _knowledgeCount(end),
      total: _knowledgeCount(total),
    })) + '</span>' + (_knowledgeState.loading
      ? '<span class="kb-catalog-refresh"><span class="kb-spinner"></span></span>' : '') : '';
  }
  var paginationRoot = document.getElementById('knowledgePagination');
  if (paginationRoot) {
    paginationRoot.hidden = total === 0 || Number(pagination.total_pages || 1) <= 1;
    paginationRoot.innerHTML = '<button type="button" data-tofu-action="_knowledgeGoPage(' +
      (page - 1) + ')"' + (!pagination.has_previous || _knowledgeState.loading
        ? ' disabled' : '') + '>' + _knowledgeEsc(t('knowledge.previousPage')) +
      '</button><span>' + _knowledgeEsc(t('knowledge.pageStatus', {
        page: _knowledgeCount(page),
        pages: _knowledgeCount(pagination.total_pages || 1),
      })) + '</span><button type="button" data-tofu-action="_knowledgeGoPage(' +
      (page + 1) + ')"' + (!pagination.has_next || _knowledgeState.loading
        ? ' disabled' : '') + '>' + _knowledgeEsc(t('knowledge.nextPage')) + '</button>';
  }
}

function _knowledgeCatalogChanged() {
  _knowledgeState.page = 1;
  _knowledgeState.contentView = null;
  _knowledgeState.resetListScroll = true;
}

function _knowledgeApplyCatalog(event) {
  if (event) event.preventDefault();
  if (_knowledgeState.catalogTimer) clearTimeout(_knowledgeState.catalogTimer);
  _knowledgeState.catalogTimer = null;
  var input = document.getElementById('knowledgeCatalogQuery');
  _knowledgeState.query = input ? input.value.trim() : '';
  _knowledgeCatalogChanged();
  _knowledgeRefresh();
}

function _knowledgeDebounceCatalog() {
  var input = document.getElementById('knowledgeCatalogQuery');
  _knowledgeState.query = input ? input.value.trim() : '';
  if (_knowledgeState.catalogTimer) clearTimeout(_knowledgeState.catalogTimer);
  _knowledgeState.catalogTimer = setTimeout(function () {
    _knowledgeState.catalogTimer = null;
    _knowledgeCatalogChanged();
    _knowledgeRefresh();
  }, 280);
}

function _knowledgeClearCatalog() {
  if (_knowledgeState.catalogTimer) clearTimeout(_knowledgeState.catalogTimer);
  _knowledgeState.catalogTimer = null;
  _knowledgeState.query = '';
  _knowledgeState.category = 'all';
  _knowledgeCatalogChanged();
  _knowledgeRefresh();
}

function _knowledgeSetCategory(category) {
  if (!category || category === _knowledgeState.category) return;
  _knowledgeState.category = category;
  _knowledgeCatalogChanged();
  _knowledgeRefresh();
}

function _knowledgeSetSort(sort) {
  if (!sort || sort === _knowledgeState.sort) return;
  _knowledgeState.sort = sort;
  _knowledgeCatalogChanged();
  _knowledgeRefresh();
}

function _knowledgeGoPage(page) {
  var pagination = _knowledgeState.data && _knowledgeState.data.pagination;
  var target = Number(page || 1);
  if (_knowledgeState.loading || target < 1 ||
      (pagination && target > Number(pagination.total_pages || 1))) return;
  _knowledgeState.page = target;
  _knowledgeState.contentView = null;
  _knowledgeState.resetListScroll = true;
  _knowledgeRefresh();
}

function _knowledgeRenderSearch() {
  var root = document.getElementById('knowledgeSearchResults');
  var button = document.getElementById('knowledgeSearchBtn');
  if (button) button.disabled = _knowledgeState.searching;
  if (!root) return;
  if (_knowledgeState.searching) {
    root.innerHTML = '<div class="kb-search-loading"><span class="kb-spinner"></span><span>' +
      _knowledgeEsc(t('knowledge.searching')) + '</span></div>';
    return;
  }
  var payload = _knowledgeState.searchResult;
  if (!payload) return;
  if (payload.error) {
    root.innerHTML = '<div class="kb-search-message is-error"><strong>' +
      _knowledgeEsc(t('knowledge.searchFailed')) + '</strong><span>' +
      _knowledgeEsc(payload.error) + '</span></div>';
    return;
  }
  var results = payload.results || [];
  if (!results.length) {
    root.innerHTML = '<div class="kb-search-message"><strong>' +
      _knowledgeEsc(t('knowledge.noResults')) + '</strong><span>' +
      _knowledgeEsc(t('knowledge.noResultsHint')) + '</span></div>';
    return;
  }
  root.innerHTML = '<div class="kb-result-summary">' +
    _knowledgeEsc(t('knowledge.resultCount', { n: results.length })) + '</div>' +
    results.map(function (result, index) {
      var location = [result.section, result.location].filter(Boolean).join(' · ');
      var assets = (result.assets || []).filter(function (asset) {
        return asset && asset.thumbnail_url;
      });
      var visual = assets.length ? '<div class="kb-result-assets">' + assets.map(function (asset) {
        var label = asset.caption || t('knowledge.visualEvidence');
        return '<a href="' + _knowledgeEsc(_knowledgeAssetUrl(asset.url)) +
          '" target="_blank" rel="noopener" title="' +
          _knowledgeEsc(label) + '"><img src="' + _knowledgeEsc(_knowledgeAssetUrl(asset.thumbnail_url)) +
          '" loading="lazy" alt="' + _knowledgeEsc(label) + '"><span>' +
          _knowledgeEsc(asset.kind || 'image') +
          (asset.page ? ' · p.' + _knowledgeEsc(asset.page) : '') + '</span></a>';
      }).join('') + '</div>' : '';
      return '<article class="kb-result"><div class="kb-result-head"><span>' +
        (index + 1) + '</span><strong>' + _knowledgeEsc(result.source) + '</strong></div>' +
        (location ? '<div class="kb-result-location">' + _knowledgeEsc(location) + '</div>' : '') +
        visual + '<pre>' + _knowledgeEsc(result.excerpt) + '</pre></article>';
    }).join('');
}

function _knowledgeRender() {
  var data = _knowledgeState.data;
  var workbench = document.querySelector('#knowledgeModal .kb-workbench');
  if (workbench) {
    workbench.dataset.hasDocs = Number(
      data && data.totals && data.totals.documents || 0) ? 'true' : 'false';
  }
  var access = _knowledgeAccessState(data || {});
  var toggle = document.getElementById('knowledgeEnabled');
  var accessHint = document.getElementById('knowledgeAccessHint');
  var upload = document.getElementById('knowledgeUploadBtn');
  var input = /** @type {HTMLInputElement|null} */ (
    document.getElementById('knowledgeFileInput'));
  var visualToggle = document.getElementById('knowledgeVisualEnrichment');
  if (toggle) {
    toggle.setAttribute('aria-checked', data && data.enabled ? 'true' : 'false');
    toggle.disabled = _knowledgeState.mutating ||
      !Number(data && data.totals && data.totals.documents || 0);
  }
  if (accessHint) {
    accessHint.textContent = t(access.key);
    accessHint.dataset.tone = access.tone;
  }
  if (upload) upload.disabled = _knowledgeState.mutating;
  if (visualToggle) {
    visualToggle.setAttribute(
      'aria-checked', data && data.visual_enrichment ? 'true' : 'false');
    visualToggle.disabled = _knowledgeState.mutating || !data || !!data._error;
  }
  var visualHint = document.getElementById('knowledgeVisualHint');
  if (visualHint) {
    var pending = Number(data && data.totals && data.totals.pending_assets || 0);
    var issues = Number(data && data.totals && data.totals.asset_issues || 0);
    visualHint.textContent = pending
      ? t('knowledge.visualPending', { n: _knowledgeCount(pending) })
      : (data && data.visual_enrichment && issues
        ? t('knowledge.visualIssues', { n: _knowledgeCount(issues) })
      : t(data && data.visual_enrichment
        ? 'knowledge.visualOnHint' : 'knowledge.visualOffHint'));
  }
  if (input && data && Array.isArray(data.supported_extensions)) {
    input.accept = data.supported_extensions.join(',');
  }
  var formatHint = document.getElementById('knowledgeFormatHint');
  if (formatHint && data && data.limits) {
    formatHint.textContent = t('knowledge.formatHintDynamic', {
      n: (data.supported_extensions || []).length,
      mb: Math.round(Number(data.limits.max_file_bytes || 0) / 1048576),
    });
  }
  _knowledgeRenderStats(data || {});
  _knowledgeRenderCatalog(data || {});
  _knowledgeRenderDocuments(data);
  _knowledgeRenderUploadReport();
  _knowledgeRenderSearch();
  _knowledgeScheduleRefresh(data);
}


function _knowledgeScheduleRefresh(data) {
  if (_knowledgeState.refreshTimer) clearTimeout(_knowledgeState.refreshTimer);
  _knowledgeState.refreshTimer = null;
  var modal = document.getElementById('knowledgeModal');
  var pending = Number(data && data.totals && data.totals.pending_assets || 0);
  if (_knowledgeState.loading || !pending || !modal ||
      !modal.classList.contains('open')) return;
  _knowledgeState.refreshTimer = setTimeout(function () {
    _knowledgeState.refreshTimer = null;
    _knowledgePollActivity();
  }, 2500);
}

async function _knowledgePollActivity() {
  var modal = document.getElementById('knowledgeModal');
  if (!modal || !modal.classList.contains('open')) return;
  if (!Api.knowledge || typeof Api.knowledge.activity !== 'function') {
    _knowledgeRefresh();
    return;
  }
  try {
    var activity = await Api.knowledge.activity();
    var data = _knowledgeState.data;
    if (!data || !activity) return;
    var previous = Number(data.totals && data.totals.pending_assets || 0);
    data.totals = data.totals || {};
    data.totals.pending_assets = Number(activity.pending_assets || 0);
    data.totals.asset_issues = Number(activity.asset_issues || 0);
    data.visual_enrichment = !!activity.visual_enrichment;
    _knowledgeRender();
    if (previous && !data.totals.pending_assets) {
      await _knowledgeRefresh();
    }
  } catch (e) {
    _knowledgeScheduleRefresh(_knowledgeState.data);
  }
}

function _knowledgeMergeStatus(result) {
  if (!result) return;
  if (!_knowledgeState.data) {
    _knowledgeState.data = result;
    return;
  }
  ['enabled', 'visual_enrichment', 'available', 'totals', 'facets',
    'limits', 'supported_extensions', 'privacy',
    'visual_enrichment_sends_images_to_configured_provider'].forEach(function (key) {
    if (Object.prototype.hasOwnProperty.call(result, key)) {
      _knowledgeState.data[key] = result[key];
    }
  });
}

async function _knowledgeToggle() {
  var data = _knowledgeState.data;
  if (_knowledgeState.mutating || !data ||
      !Number(data.totals && data.totals.documents || 0)) return;
  var enabled = !data.enabled;
  _knowledgeState.mutating = true;
  _knowledgeRender();
  try {
    var result = await Api.knowledge.setEnabled(enabled);
    _knowledgeMergeStatus(result);
    if (typeof showToast === 'function') {
      showToast(t(enabled ? 'knowledge.enabledToast' : 'knowledge.disabledToast'), 'success');
    }
  } catch (e) {
    if (typeof showToast === 'function') {
      showToast((e && e.message) || t('knowledge.failed'), 'error');
    }
  } finally {
    _knowledgeState.mutating = false;
    _knowledgeRender();
  }
}


async function _knowledgeToggleVisual() {
  var data = _knowledgeState.data;
  if (_knowledgeState.mutating || !data || !Api.knowledge) return;
  var enabled = !data.visual_enrichment;
  if (enabled) {
    var accepted = typeof showConfirm === 'function'
      ? await showConfirm(t('knowledge.visualConfirm'), {
        title: t('knowledge.visualTitle'),
        okText: t('knowledge.visualEnable'),
        danger: false,
      })
      : window.confirm(t('knowledge.visualConfirm'));
    if (!accepted) return;
  }
  _knowledgeState.mutating = true;
  _knowledgeRender();
  try {
    var result = await Api.knowledge.setVisualEnrichment(enabled);
    _knowledgeMergeStatus(result);
    if (typeof showToast === 'function') {
      showToast(t(enabled
        ? 'knowledge.visualEnabledToast' : 'knowledge.visualDisabledToast'), 'success');
    }
  } catch (e) {
    if (typeof showToast === 'function') {
      showToast((e && e.message) || t('knowledge.failed'), 'error');
    }
  } finally {
    _knowledgeState.mutating = false;
    _knowledgeRender();
  }
}

function _knowledgeValidateFiles(files) {
  var data = _knowledgeState.data || {};
  var limits = data.limits || {};
  var maxFiles = Number(limits.max_batch_files || 20);
  var maxFileBytes = Number(limits.max_file_bytes || 50 * 1048576);
  var maxBatchBytes = Number(limits.max_batch_bytes || 200 * 1048576);
  var errors = [];
  if (files.length > maxFiles) {
    errors.push({ name: t('knowledge.batch'), error: t('knowledge.tooMany', { n: maxFiles }) });
  }
  var total = 0;
  files.forEach(function (file) {
    total += Number(file.size || 0);
    if (file.size > maxFileBytes) {
      errors.push({ name: file.name, error: t('knowledge.tooLarge', { size: _knowledgeBytes(maxFileBytes) }) });
    }
  });
  if (total > maxBatchBytes) {
    errors.push({ name: t('knowledge.batch'), error: t('knowledge.batchTooLarge', { size: _knowledgeBytes(maxBatchBytes) }) });
  }
  return errors;
}

async function _knowledgeUpload(fileList) {
  var files = Array.prototype.slice.call(fileList || []);
  if (!files.length || _knowledgeState.mutating || !Api.knowledge) return;
  var validationErrors = _knowledgeValidateFiles(files);
  if (validationErrors.length) {
    _knowledgeState.uploadReport = { indexed: [], errors: validationErrors };
    _knowledgeRenderUploadReport();
    return;
  }
  _knowledgeState.mutating = true;
  _knowledgeState.uploadReport = { pending: true, files: files };
  _knowledgeRender();
  try {
    var form = new FormData();
    files.forEach(function (file) { form.append('files', file, file.name); });
    var result = await Api.knowledge.upload(form);
    _knowledgeState.query = '';
    _knowledgeState.category = 'all';
    _knowledgeState.sort = 'updated_desc';
    _knowledgeState.page = 1;
    _knowledgeState.contentView = null;
    _knowledgeState.resetListScroll = true;
    _knowledgeState.data = result;
    _knowledgeState.uploadReport = {
      indexed: (result && result.indexed) || [],
      errors: (result && result.errors) || [],
    };
  } catch (e) {
    _knowledgeState.uploadReport = {
      indexed: [],
      errors: [{ name: t('knowledge.batch'), error: (e && e.message) || t('knowledge.failed') }],
    };
  } finally {
    _knowledgeState.mutating = false;
    _knowledgeRender();
  }
}

function _knowledgeDismissUploadReport() {
  _knowledgeState.uploadReport = null;
  _knowledgeRenderUploadReport();
}

function _knowledgeDrag(event, active) {
  if (event) {
    event.preventDefault();
    event.stopPropagation();
  }
  var zone = document.getElementById('knowledgeDropzone');
  if (zone) zone.classList.toggle('is-dragging', !!active);
}

function _knowledgeDrop(event) {
  _knowledgeDrag(event, false);
  var files = event && event.dataTransfer && event.dataTransfer.files;
  if (files && files.length) _knowledgeUpload(files);
}

async function _knowledgeRemove(id) {
  if (_knowledgeState.mutating || !Api.knowledge) return;
  var docs = ((_knowledgeState.data && _knowledgeState.data.documents) || []);
  var doc = docs.find(function (item) { return item && item.id === id; });
  var message = t('knowledge.removeConfirm', { name: (doc && doc.name) || '' });
  var accepted;
  if (typeof showConfirm === 'function') {
    accepted = await showConfirm(message, {
      title: t('knowledge.removeTitle'),
      okText: t('knowledge.remove'),
      danger: true,
    });
  } else {
    accepted = window.confirm(message);
  }
  if (!accepted) return;
  _knowledgeState.mutating = true;
  _knowledgeRender();
  try {
    await Api.knowledge.remove(id);
    _knowledgeState.searchResult = null;
    if (_knowledgeState.contentView && _knowledgeState.contentView.id === id) {
      _knowledgeState.contentView = null;
    }
    if (docs.length <= 1 && _knowledgeState.page > 1) {
      _knowledgeState.page -= 1;
    }
    await _knowledgeRefresh();
  } catch (e) {
    if (typeof showToast === 'function') {
      showToast((e && e.message) || t('knowledge.failed'), 'error');
    }
  } finally {
    _knowledgeState.mutating = false;
    _knowledgeRender();
  }
}

async function _knowledgeReindex(id) {
  if (_knowledgeState.mutating || !Api.knowledge) return;
  var docs = ((_knowledgeState.data && _knowledgeState.data.documents) || []);
  var doc = docs.find(function (item) { return item && item.id === id; });
  _knowledgeState.mutating = true;
  _knowledgeState.uploadReport = {
    pending: true,
    files: [{ name: (doc && doc.name) || t('knowledge.document') }],
  };
  _knowledgeRender();
  try {
    var result = await Api.knowledge.reindex(id);
    _knowledgeState.uploadReport = {
      indexed: result && result.reindexed ? [result.reindexed] : [],
      errors: [],
      reindexed: true,
    };
    _knowledgeState.searchResult = null;
    if (_knowledgeState.contentView && _knowledgeState.contentView.id === id) {
      _knowledgeState.contentView = null;
    }
    await _knowledgeRefresh();
  } catch (e) {
    _knowledgeState.uploadReport = {
      indexed: [],
      errors: [{
        name: (doc && doc.name) || t('knowledge.document'),
        error: (e && e.message) || t('knowledge.reindexFailed'),
      }],
      reindexed: true,
    };
  } finally {
    _knowledgeState.mutating = false;
    _knowledgeRender();
  }
}

async function _knowledgeSearch(event) {
  if (event) event.preventDefault();
  if (_knowledgeState.searching) return;
  var input = document.getElementById('knowledgeSearchInput');
  var query = input ? input.value.trim() : '';
  if (!query) {
    if (input) input.focus();
    return;
  }
  if (!Number(_knowledgeState.data && _knowledgeState.data.totals &&
      _knowledgeState.data.totals.documents || 0)) {
    _knowledgeState.searchResult = { results: [] };
    _knowledgeRenderSearch();
    return;
  }
  _knowledgeState.searching = true;
  _knowledgeRenderSearch();
  try {
    _knowledgeState.searchResult = await Api.knowledge.search(query, 6);
  } catch (e) {
    _knowledgeState.searchResult = { error: (e && e.message) || t('knowledge.searchFailed') };
  } finally {
    _knowledgeState.searching = false;
    _knowledgeRenderSearch();
  }
}

document.addEventListener('keydown', function (event) {
  if (event.key !== 'Escape') return;
  var modal = document.getElementById('knowledgeModal');
  if (modal && modal.classList.contains('open')) closeKnowledgeBase();
});

