/* ═══════════════════════════════════════════
   paper-reader.js — Paper Reading Mode
   ═══════════════════════════════════════════ */

// ── State ──
var paperMode = false;
var _paperPdfUrl = '';        // URL to the loaded PDF
var _paperFileName = '';      // Original filename
var _paperParsedText = '';    // Full extracted text
var _paperArxivId = '';       // arXiv ID if from arXiv
var _paperPdfDoc = null;      // PDF.js document instance
var _paperCurrentPage = 1;
var _paperTotalPages = 0;
var _paperScale = 1.5;
var _paperActiveTab = 'qa';   // 'qa' | 'report' | 'translate'
var _paperReportCache = '';   // Cached report text
var _paperReportTaskId = '';  // Active report task ID
var _paperQAHistory = [];     // [{role, content, timestamp}]
var _paperLoading = false;
var _paperQAStreaming = false;
var _paperQAAbort = null;

// ══════════════════════════════════════════════════════
//  ★ Enter / Exit Paper Mode
// ══════════════════════════════════════════════════════

function enterPaperMode(pdfUrl, fileName, parsedText, arxivId) {
  // Exit other modes first
  if (typeof imageGenMode !== 'undefined' && imageGenMode) {
    exitImageGenMode();
  }

  paperMode = true;
  _paperPdfUrl = pdfUrl || '';
  _paperFileName = fileName || 'Paper';
  _paperParsedText = parsedText || '';
  _paperArxivId = arxivId || '';
  _paperActiveTab = 'qa';
  _paperQAHistory = [];
  _paperReportCache = '';
  _paperReportTaskId = '';
  _paperCurrentPage = 1;

  // Show paper mode container, hide normal chat
  const container = document.getElementById('paperModeContainer');
  const chatWrapper = document.querySelector('.chat-wrapper');
  const inputArea = document.querySelector('.input-area');
  if (container) container.style.display = 'flex';
  if (chatWrapper) chatWrapper.style.display = 'none';
  if (inputArea) inputArea.style.display = 'none';

  // Update button state
  document.getElementById('paperModeBtn')?.classList.add('active');

  // Update title bar
  const titleEl = document.getElementById('paperTitle');
  if (titleEl) titleEl.textContent = _paperFileName;
  if (titleEl) titleEl.title = _paperFileName;

  // Save state to conversation
  _savePaperState();

  // If we have a PDF URL, load it
  if (_paperPdfUrl) {
    _loadPaperPdf(_paperPdfUrl);
  } else {
    // Show the landing / upload screen
    _showPaperLanding();
  }

  // Set initial tab
  _switchPaperTab('qa');

  debugLog('Paper Reading Mode: ENTER', 'success');
}

function exitPaperMode() {
  paperMode = false;

  const container = document.getElementById('paperModeContainer');
  const chatWrapper = document.querySelector('.chat-wrapper');
  const inputArea = document.querySelector('.input-area');
  if (container) container.style.display = 'none';
  if (chatWrapper) chatWrapper.style.display = '';
  if (inputArea) inputArea.style.display = '';

  document.getElementById('paperModeBtn')?.classList.remove('active');

  // Cleanup PDF.js
  if (_paperPdfDoc) {
    _paperPdfDoc.destroy();
    _paperPdfDoc = null;
  }

  // Abort any streaming QA
  if (_paperQAAbort) {
    _paperQAAbort.abort();
    _paperQAAbort = null;
  }

  _savePaperState();
  debugLog('Paper Reading Mode: EXIT', 'info');
}

function togglePaperMode() {
  if (paperMode) {
    exitPaperMode();
  } else {
    enterPaperMode();
  }
}

// ══════════════════════════════════════════════════════
//  ★ State persistence (per-conversation)
// ══════════════════════════════════════════════════════

function _savePaperState() {
  if (typeof getActiveConv !== 'function') return;
  const conv = getActiveConv();
  if (!conv) return;
  conv.paperMode = !!paperMode;
  conv.paperPdfUrl = _paperPdfUrl;
  conv.paperFileName = _paperFileName;
  conv.paperParsedText = _paperParsedText;
  conv.paperArxivId = _paperArxivId;
  conv.paperReport = _paperReportCache;
  conv.paperQAHistory = _paperQAHistory;
  if (typeof saveConversations === 'function') saveConversations(null);
}

function _restorePaperState(conv) {
  if (!conv) return;
  if (conv.paperMode) {
    enterPaperMode(conv.paperPdfUrl, conv.paperFileName, conv.paperParsedText, conv.paperArxivId);
    if (conv.paperReport) {
      _paperReportCache = conv.paperReport;
    }
    if (conv.paperQAHistory) {
      _paperQAHistory = conv.paperQAHistory;
      _renderPaperQA();
    }
  } else if (paperMode) {
    exitPaperMode();
  }
}

// ══════════════════════════════════════════════════════
//  ★ PDF Loading & Rendering
// ══════════════════════════════════════════════════════

async function _loadPaperPdf(url) {
  const viewer = document.getElementById('paperPdfViewer');
  if (!viewer) return;
  viewer.innerHTML = '<div class="paper-loading"><div class="paper-loading-spinner"></div><div>Loading PDF…</div></div>';

  try {
    // Ensure PDF.js is available
    if (typeof pdfjsLib === 'undefined') {
      viewer.innerHTML = '<div class="paper-error">PDF.js not loaded. Please refresh the page.</div>';
      return;
    }

    const loadingTask = pdfjsLib.getDocument(url);
    _paperPdfDoc = await loadingTask.promise;
    _paperTotalPages = _paperPdfDoc.numPages;
    _paperCurrentPage = 1;

    _updatePaperPageNav();
    await _renderPaperPage(_paperCurrentPage);
  } catch (e) {
    console.error('[Paper] Failed to load PDF:', e);
    viewer.innerHTML = `<div class="paper-error">Failed to load PDF: ${escapeHtml(e.message)}</div>`;
  }
}

async function _renderPaperPage(pageNum) {
  if (!_paperPdfDoc || pageNum < 1 || pageNum > _paperTotalPages) return;

  const viewer = document.getElementById('paperPdfViewer');
  if (!viewer) return;

  try {
    const page = await _paperPdfDoc.getPage(pageNum);
    const viewport = page.getViewport({ scale: _paperScale });

    // Clear previous
    viewer.innerHTML = '';

    const wrapper = document.createElement('div');
    wrapper.className = 'paper-page-wrapper';

    const canvas = document.createElement('canvas');
    canvas.className = 'paper-pdf-canvas';
    const ctx = canvas.getContext('2d');
    canvas.width = viewport.width;
    canvas.height = viewport.height;

    wrapper.appendChild(canvas);

    // Text layer for selection
    const textDiv = document.createElement('div');
    textDiv.className = 'paper-text-layer';
    textDiv.style.width = viewport.width + 'px';
    textDiv.style.height = viewport.height + 'px';
    wrapper.appendChild(textDiv);

    viewer.appendChild(wrapper);

    // Render canvas
    await page.render({ canvasContext: ctx, viewport }).promise;

    // Render text layer for selection
    const textContent = await page.getTextContent();
    if (typeof pdfjsLib.renderTextLayer === 'function') {
      pdfjsLib.renderTextLayer({
        textContentSource: textContent,
        container: textDiv,
        viewport: viewport,
        textDivs: [],
      });
    }

    _paperCurrentPage = pageNum;
    _updatePaperPageNav();
  } catch (e) {
    console.error('[Paper] Failed to render page:', e);
  }
}

function _renderAllPages() {
  /* Render all pages sequentially for scroll view */
  if (!_paperPdfDoc) return;
  const viewer = document.getElementById('paperPdfViewer');
  if (!viewer) return;
  viewer.innerHTML = '<div class="paper-loading"><div class="paper-loading-spinner"></div><div>Rendering all pages…</div></div>';

  (async function() {
    viewer.innerHTML = '';
    for (let i = 1; i <= _paperTotalPages; i++) {
      try {
        const page = await _paperPdfDoc.getPage(i);
        const viewport = page.getViewport({ scale: _paperScale });

        const wrapper = document.createElement('div');
        wrapper.className = 'paper-page-wrapper';
        wrapper.dataset.page = i;

        const canvas = document.createElement('canvas');
        canvas.className = 'paper-pdf-canvas';
        const ctx = canvas.getContext('2d');
        canvas.width = viewport.width;
        canvas.height = viewport.height;
        wrapper.appendChild(canvas);

        // Text layer
        const textDiv = document.createElement('div');
        textDiv.className = 'paper-text-layer';
        textDiv.style.width = viewport.width + 'px';
        textDiv.style.height = viewport.height + 'px';
        wrapper.appendChild(textDiv);

        viewer.appendChild(wrapper);

        await page.render({ canvasContext: ctx, viewport }).promise;

        const textContent = await page.getTextContent();
        if (typeof pdfjsLib.renderTextLayer === 'function') {
          pdfjsLib.renderTextLayer({
            textContentSource: textContent,
            container: textDiv,
            viewport: viewport,
            textDivs: [],
          });
        }
      } catch (e) {
        console.warn('[Paper] Failed to render page %d:', i, e);
      }
    }
  })();
}

function paperPrevPage() {
  if (_paperCurrentPage > 1) _renderPaperPage(_paperCurrentPage - 1);
}

function paperNextPage() {
  if (_paperCurrentPage < _paperTotalPages) _renderPaperPage(_paperCurrentPage + 1);
}

function paperGoToPage(num) {
  const n = parseInt(num, 10);
  if (n >= 1 && n <= _paperTotalPages) _renderPaperPage(n);
}

function paperZoomIn() {
  _paperScale = Math.min(_paperScale + 0.25, 3.0);
  _renderAllPages();
}

function paperZoomOut() {
  _paperScale = Math.max(_paperScale - 0.25, 0.5);
  _renderAllPages();
}

function _updatePaperPageNav() {
  const nav = document.getElementById('paperPageNav');
  if (!nav) return;
  nav.innerHTML = `
    <button class="paper-nav-btn" onclick="paperPrevPage()" ${_paperCurrentPage <= 1 ? 'disabled' : ''}>◀</button>
    <span class="paper-nav-info">
      <input type="number" class="paper-page-input" value="${_paperCurrentPage}" min="1" max="${_paperTotalPages}"
             onchange="paperGoToPage(this.value)" onclick="this.select()">
      / ${_paperTotalPages}
    </span>
    <button class="paper-nav-btn" onclick="paperNextPage()" ${_paperCurrentPage >= _paperTotalPages ? 'disabled' : ''}>▶</button>
    <span class="paper-nav-sep"></span>
    <button class="paper-nav-btn" onclick="paperZoomOut()" title="Zoom out">−</button>
    <button class="paper-nav-btn" onclick="paperZoomIn()" title="Zoom in">+</button>
    <button class="paper-nav-btn" onclick="_renderAllPages()" title="View all pages">⊞</button>
  `;
}

// ══════════════════════════════════════════════════════
//  ★ Landing / Upload Screen
// ══════════════════════════════════════════════════════

function _showPaperLanding() {
  const viewer = document.getElementById('paperPdfViewer');
  if (!viewer) return;
  viewer.innerHTML = `
    <div class="paper-landing">
      <div class="paper-landing-icon">📄</div>
      <h3>Paper Reading Mode</h3>
      <p>Upload a PDF or paste an arXiv URL to get started</p>
      <div class="paper-landing-actions">
        <label class="paper-upload-btn">
          <input type="file" accept=".pdf,application/pdf" onchange="_handlePaperFileUpload(event)" style="display:none">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>
          Upload PDF
        </label>
        <div class="paper-arxiv-input">
          <input type="text" id="paperArxivUrl" placeholder="arXiv URL or ID (e.g. 2301.12345)"
                 onkeydown="if(event.key==='Enter')_fetchArxivPaper()">
          <button onclick="_fetchArxivPaper()" class="paper-arxiv-btn">Fetch</button>
        </div>
      </div>
    </div>`;
}

async function _handlePaperFileUpload(event) {
  const file = event.target.files[0];
  if (!file) return;
  if (!file.name.toLowerCase().endsWith('.pdf')) {
    debugLog('Only PDF files are supported', 'warning');
    return;
  }

  _paperLoading = true;
  const viewer = document.getElementById('paperPdfViewer');
  if (viewer) viewer.innerHTML = '<div class="paper-loading"><div class="paper-loading-spinner"></div><div>Uploading PDF…</div></div>';

  try {
    // Upload PDF to server
    const formData = new FormData();
    formData.append('file', file);
    const uploadResp = await fetch(apiUrl('/api/paper/upload'), { method: 'POST', body: formData });
    const uploadData = await uploadResp.json();

    if (!uploadData.ok) throw new Error(uploadData.error || 'Upload failed');

    _paperPdfUrl = apiUrl(uploadData.pdf_url);
    _paperFileName = file.name;

    // Update title
    const titleEl = document.getElementById('paperTitle');
    if (titleEl) { titleEl.textContent = _paperFileName; titleEl.title = _paperFileName; }

    // Also parse text for QA/report
    const parseForm = new FormData();
    parseForm.append('file', file);
    parseForm.append('maxTextChars', '0');
    parseForm.append('maxImages', '0');
    const parseResp = await fetch(apiUrl('/api/pdf/parse'), { method: 'POST', body: parseForm });
    const parseData = await parseResp.json();
    if (parseData.success) {
      _paperParsedText = parseData.text || '';
      debugLog(`Paper parsed: ${parseData.totalPages} pages, ${parseData.textLength} chars`, 'success');
    }

    // Load PDF viewer
    await _loadPaperPdf(_paperPdfUrl);
    _savePaperState();

  } catch (e) {
    console.error('[Paper] Upload failed:', e);
    if (viewer) viewer.innerHTML = `<div class="paper-error">Upload failed: ${escapeHtml(e.message)}</div>`;
  } finally {
    _paperLoading = false;
  }
}

async function _fetchArxivPaper() {
  const input = document.getElementById('paperArxivUrl');
  const url = input?.value?.trim();
  if (!url) { debugLog('Please enter an arXiv URL or ID', 'warning'); return; }

  _paperLoading = true;
  const viewer = document.getElementById('paperPdfViewer');
  if (viewer) viewer.innerHTML = '<div class="paper-loading"><div class="paper-loading-spinner"></div><div>Fetching from arXiv…</div></div>';

  try {
    const resp = await fetch(apiUrl('/api/paper/fetch-arxiv'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url }),
    });
    const data = await resp.json();

    if (!data.ok) throw new Error(data.error || 'Fetch failed');

    _paperPdfUrl = apiUrl(data.pdf_url);
    _paperArxivId = data.arxiv_id || '';
    _paperFileName = `arXiv:${_paperArxivId}`;

    const titleEl = document.getElementById('paperTitle');
    if (titleEl) { titleEl.textContent = _paperFileName; titleEl.title = _paperFileName; }

    // Parse text from the downloaded PDF
    try {
      const parseResp = await fetch(apiUrl(data.pdf_url));
      const pdfBlob = await parseResp.blob();
      const parseForm = new FormData();
      parseForm.append('file', new File([pdfBlob], `${_paperArxivId}.pdf`, { type: 'application/pdf' }));
      parseForm.append('maxTextChars', '0');
      parseForm.append('maxImages', '0');
      const textResp = await fetch(apiUrl('/api/pdf/parse'), { method: 'POST', body: parseForm });
      const textData = await textResp.json();
      if (textData.success) {
        _paperParsedText = textData.text || '';
        debugLog(`arXiv paper parsed: ${textData.totalPages} pages, ${textData.textLength} chars`, 'success');
      }
    } catch (pe) {
      console.warn('[Paper] Text extraction failed:', pe);
    }

    await _loadPaperPdf(_paperPdfUrl);
    _savePaperState();
    debugLog(`Fetched arXiv:${_paperArxivId}`, 'success');

  } catch (e) {
    console.error('[Paper] arXiv fetch failed:', e);
    if (viewer) viewer.innerHTML = `<div class="paper-error">Failed to fetch: ${escapeHtml(e.message)}<br><button onclick="_showPaperLanding()" class="paper-retry-btn">Try Again</button></div>`;
  } finally {
    _paperLoading = false;
  }
}

// ══════════════════════════════════════════════════════
//  ★ Right Panel — Tab Switching
// ══════════════════════════════════════════════════════

function _switchPaperTab(tab) {
  _paperActiveTab = tab;
  // Update tab buttons
  document.querySelectorAll('.paper-tab-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.tab === tab);
  });
  // Show/hide panels
  document.querySelectorAll('.paper-tab-panel').forEach(panel => {
    panel.style.display = panel.dataset.tab === tab ? '' : 'none';
  });
  // Auto-generate report when switching to report tab
  if (tab === 'report' && !_paperReportCache && _paperParsedText) {
    _generatePaperReport();
  }
  // Render QA history when switching to QA tab
  if (tab === 'qa') {
    _renderPaperQA();
  }
}

// ══════════════════════════════════════════════════════
//  ★ Tab 1: Q&A
// ══════════════════════════════════════════════════════

function _renderPaperQA() {
  const container = document.getElementById('paperQAMessages');
  if (!container) return;

  if (_paperQAHistory.length === 0) {
    container.innerHTML = `
      <div class="paper-qa-empty">
        <div class="paper-qa-empty-icon">💬</div>
        <p>Ask questions about this paper</p>
        <p class="paper-qa-hint">Select text in the PDF to quote it, or type a question below</p>
      </div>`;
    return;
  }

  let html = '';
  for (const msg of _paperQAHistory) {
    const isUser = msg.role === 'user';
    const contentHtml = isUser
      ? escapeHtml(msg.content)
      : (typeof renderMarkdown === 'function' ? renderMarkdown(msg.content) : escapeHtml(msg.content));
    html += `<div class="paper-qa-msg ${isUser ? 'paper-qa-user' : 'paper-qa-assistant'}">
      <div class="paper-qa-msg-content">${contentHtml}</div>
    </div>`;
  }
  container.innerHTML = html;
  container.scrollTop = container.scrollHeight;
}

async function _sendPaperQuestion() {
  const input = document.getElementById('paperQAInput');
  const question = input?.value?.trim();
  if (!question || _paperQAStreaming) return;

  // Add user message
  _paperQAHistory.push({ role: 'user', content: question, timestamp: Date.now() });
  input.value = '';
  _renderPaperQA();

  // Build messages for LLM
  const systemMsg = `You are a helpful research assistant. The user is reading the following academic paper. Answer their questions based on the paper content. Be specific and cite relevant sections when possible.

Paper text:
${_paperParsedText.slice(0, 100000)}`;

  const messages = [{ role: 'system', content: systemMsg }];
  // Include recent QA history for context (last 10 messages)
  const recentHistory = _paperQAHistory.slice(-10);
  for (const m of recentHistory) {
    messages.push({ role: m.role, content: m.content });
  }

  // Add streaming assistant message
  _paperQAHistory.push({ role: 'assistant', content: '', timestamp: Date.now() });
  _paperQAStreaming = true;

  try {
    _paperQAAbort = new AbortController();
    const resp = await fetch(apiUrl('/api/paper/chat'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      signal: _paperQAAbort.signal,
      body: JSON.stringify({ messages, stream: true }),
    });

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      const lines = buffer.split('\n');
      buffer = lines.pop();

      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        const data = line.slice(6).trim();
        if (data === '[DONE]') continue;
        try {
          const parsed = JSON.parse(data);
          const delta = parsed.choices?.[0]?.delta?.content || '';
          if (delta) {
            _paperQAHistory[_paperQAHistory.length - 1].content += delta;
            _renderPaperQA();
          }
        } catch (e) {
          // skip parse errors
        }
      }
    }
  } catch (e) {
    if (e.name !== 'AbortError') {
      console.error('[Paper] QA streaming error:', e);
      _paperQAHistory[_paperQAHistory.length - 1].content += '\n\n⚠️ Error: ' + e.message;
      _renderPaperQA();
    }
  } finally {
    _paperQAStreaming = false;
    _paperQAAbort = null;
    _savePaperState();
  }
}

function _quotePaperSelection() {
  const selection = window.getSelection();
  const text = selection?.toString()?.trim();
  if (!text) return;

  const input = document.getElementById('paperQAInput');
  if (!input) return;

  // Switch to QA tab if not already there
  if (_paperActiveTab !== 'qa') _switchPaperTab('qa');

  // Insert quoted text
  const quoteBlock = `> ${text.replace(/\n/g, '\n> ')}\n\n`;
  input.value = quoteBlock + input.value;
  input.focus();

  // Clear selection
  selection.removeAllRanges();

  // Hide quote button
  const quoteBtn = document.getElementById('paperQuoteBtn');
  if (quoteBtn) quoteBtn.style.display = 'none';
}

// ── Text selection handling: show floating "Quote" button ──
function _handlePaperTextSelection() {
  const selection = window.getSelection();
  const text = selection?.toString()?.trim();
  const quoteBtn = document.getElementById('paperQuoteBtn');
  if (!quoteBtn) return;

  if (!text || text.length < 3) {
    quoteBtn.style.display = 'none';
    return;
  }

  // Check if selection is within the PDF viewer
  const viewer = document.getElementById('paperPdfViewer');
  if (!viewer) return;

  const anchorNode = selection.anchorNode;
  if (!viewer.contains(anchorNode)) {
    quoteBtn.style.display = 'none';
    return;
  }

  // Position the button near the selection
  const range = selection.getRangeAt(0);
  const rect = range.getBoundingClientRect();
  const viewerRect = viewer.getBoundingClientRect();

  quoteBtn.style.display = 'flex';
  quoteBtn.style.top = (rect.top - viewerRect.top - 36) + 'px';
  quoteBtn.style.left = (rect.left - viewerRect.left + rect.width / 2 - 40) + 'px';
}

// ══════════════════════════════════════════════════════
//  ★ Tab 2: Analysis Report
// ══════════════════════════════════════════════════════

async function _generatePaperReport() {
  const container = document.getElementById('paperReportContent');
  if (!container) return;

  if (_paperReportCache) {
    container.innerHTML = typeof renderMarkdown === 'function'
      ? renderMarkdown(_paperReportCache)
      : `<pre>${escapeHtml(_paperReportCache)}</pre>`;
    return;
  }

  if (!_paperParsedText) {
    container.innerHTML = '<div class="paper-report-empty"><p>No paper text available. Please load a PDF first.</p></div>';
    return;
  }

  container.innerHTML = '<div class="paper-loading"><div class="paper-loading-spinner"></div><div>Generating analysis report…</div></div>';

  try {
    // Start report generation
    const startResp = await fetch(apiUrl('/api/paper/report'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ paper_text: _paperParsedText }),
    });
    const startData = await startResp.json();

    if (!startData.ok) throw new Error(startData.error || 'Failed to start report');

    _paperReportTaskId = startData.task_id;

    // Stream the report via SSE
    const evtSource = new EventSource(apiUrl(`/api/paper/report/${_paperReportTaskId}/stream`));
    let fullText = '';

    evtSource.onmessage = function(e) {
      try {
        const data = JSON.parse(e.data);
        if (data.done) {
          evtSource.close();
          _paperReportCache = fullText;
          _savePaperState();
          return;
        }
        if (data.error) {
          evtSource.close();
          container.innerHTML = `<div class="paper-error">Report failed: ${escapeHtml(data.error)}</div>`;
          return;
        }
        if (data.text) {
          fullText += data.text;
          container.innerHTML = typeof renderMarkdown === 'function'
            ? renderMarkdown(fullText)
            : `<pre>${escapeHtml(fullText)}</pre>`;
          container.scrollTop = container.scrollHeight;
        }
      } catch (err) {
        // skip
      }
    };

    evtSource.onerror = function() {
      evtSource.close();
      if (fullText) {
        _paperReportCache = fullText;
        _savePaperState();
      }
    };

  } catch (e) {
    console.error('[Paper] Report generation failed:', e);
    container.innerHTML = `<div class="paper-error">Failed: ${escapeHtml(e.message)}<br><button onclick="_generatePaperReport()" class="paper-retry-btn">Retry</button></div>`;
  }
}

function _regeneratePaperReport() {
  _paperReportCache = '';
  _paperReportTaskId = '';
  _generatePaperReport();
}

function _copyPaperReport() {
  if (!_paperReportCache) return;
  navigator.clipboard.writeText(_paperReportCache).then(() => {
    debugLog('Report copied to clipboard', 'success');
  }).catch(e => {
    console.warn('[Paper] Copy failed:', e);
  });
}

function _exportPaperReport() {
  if (!_paperReportCache) return;
  const blob = new Blob([_paperReportCache], { type: 'text/markdown' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `paper_report_${_paperFileName.replace(/[^\w]/g, '_')}.md`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
  debugLog('Report exported as Markdown', 'success');
}

// ══════════════════════════════════════════════════════
//  ★ Tab 3: Translation (UI placeholder)
// ══════════════════════════════════════════════════════

function _initPaperTranslateTab() {
  const container = document.getElementById('paperTranslateContent');
  if (!container) return;

  container.innerHTML = `
    <div class="paper-translate-placeholder">
      <div class="paper-translate-header">
        <div class="paper-translate-toggle">
          <button class="paper-translate-lang active" data-lang="original" onclick="_switchPaperLang('original', this)">Original</button>
          <button class="paper-translate-lang" data-lang="zh" onclick="_switchPaperLang('zh', this)">中文</button>
          <button class="paper-translate-lang" data-lang="en" onclick="_switchPaperLang('en', this)">English</button>
          <button class="paper-translate-lang" data-lang="ja" onclick="_switchPaperLang('ja', this)">日本語</button>
        </div>
      </div>
      <div class="paper-translate-body" id="paperTranslateBody">
        <div class="paper-translate-empty">
          <div class="paper-translate-empty-icon">🌐</div>
          <p>Select a target language to translate the paper</p>
          <p class="paper-translate-hint">Translation will be processed page by page</p>
        </div>
      </div>
      <div class="paper-translate-status" id="paperTranslateStatus"></div>
    </div>`;
}

function _switchPaperLang(lang, btn) {
  // Update button states
  document.querySelectorAll('.paper-translate-lang').forEach(b => b.classList.remove('active'));
  if (btn) btn.classList.add('active');

  const body = document.getElementById('paperTranslateBody');
  const status = document.getElementById('paperTranslateStatus');
  if (!body) return;

  if (lang === 'original') {
    body.innerHTML = `<div class="paper-translate-empty">
      <div class="paper-translate-empty-icon">📄</div>
      <p>Viewing original document</p>
    </div>`;
    if (status) status.textContent = '';
    return;
  }

  // Translation placeholder — actual implementation to be added later
  const langNames = { zh: '中文', en: 'English', ja: '日本語' };
  body.innerHTML = `<div class="paper-translate-empty">
    <div class="paper-translate-empty-icon">🔄</div>
    <p>Translating to ${langNames[lang] || lang}…</p>
    <p class="paper-translate-hint">Translation feature coming soon</p>
    <div class="paper-translate-progress">
      <div class="paper-translate-progress-bar" style="width: 0%"></div>
    </div>
  </div>`;
  if (status) status.textContent = `Ready to translate to ${langNames[lang] || lang}`;
}

// ══════════════════════════════════════════════════════
//  ★ Keyboard Shortcuts
// ══════════════════════════════════════════════════════

function _handlePaperKeyDown(e) {
  if (!paperMode) return;

  // Esc → exit paper mode
  if (e.key === 'Escape') {
    e.preventDefault();
    exitPaperMode();
    return;
  }

  // Don't handle shortcuts when typing in input
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') {
    // Enter in QA input → send
    if (e.key === 'Enter' && !e.shiftKey && e.target.id === 'paperQAInput') {
      e.preventDefault();
      _sendPaperQuestion();
    }
    return;
  }

  // Left/Right arrow → prev/next page
  if (e.key === 'ArrowLeft') paperPrevPage();
  if (e.key === 'ArrowRight') paperNextPage();

  // +/- → zoom
  if (e.key === '+' || e.key === '=') paperZoomIn();
  if (e.key === '-') paperZoomOut();
}

// ══════════════════════════════════════════════════════
//  ★ Mobile Responsive Helpers
// ══════════════════════════════════════════════════════

function _paperToggleMobilePanel(panel) {
  /* On mobile, toggle between showing PDF viewer and right panel */
  const container = document.getElementById('paperModeContainer');
  if (!container) return;

  if (panel === 'pdf') {
    container.classList.remove('paper-show-right');
    container.classList.add('paper-show-left');
  } else {
    container.classList.remove('paper-show-left');
    container.classList.add('paper-show-right');
  }
}

// ══════════════════════════════════════════════════════
//  ★ Initialization
// ══════════════════════════════════════════════════════

/* Set up event listeners */
document.addEventListener('keydown', _handlePaperKeyDown);

/* Text selection listener for quote button */
document.addEventListener('mouseup', function() {
  if (paperMode) setTimeout(_handlePaperTextSelection, 10);
});

/* Initialize translate tab content */
document.addEventListener('DOMContentLoaded', function() {
  _initPaperTranslateTab();
});
