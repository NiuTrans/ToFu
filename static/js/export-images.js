/* ══════════════════════════════════════════════════════════════
   Export Assistant Message to Phone-Screen Images (9:16, 1080×1920)
   ══════════════════════════════════════════════════════════════ */

const ExportImages = (() => {
  // ── Lazy-load html2canvas (199KB) on first use ─────────────
  let _html2canvasReady = null;
  function _ensureHtml2Canvas() {
    if (window.html2canvas) return Promise.resolve();
    if (_html2canvasReady) return _html2canvasReady;
    _html2canvasReady = new Promise((resolve, reject) => {
      const s = document.createElement('script');
      s.src = 'static/vendor/html2canvas.min.js';
      s.onload = resolve;
      s.onerror = () => reject(new Error('Failed to load html2canvas'));
      document.head.appendChild(s);
    });
    return _html2canvasReady;
  }

  // ── Constants ──────────────────────────────────────────────
  const PAGE_W = 1080;
  const PAGE_H = 1920;
  const PAD_X = 72;
  const PAD_TOP = 100;
  const PAD_BOT = 120; // extra room for page number footer
  const CONTENT_W = PAGE_W - PAD_X * 2;
  const CONTENT_H = PAGE_H - PAD_TOP - PAD_BOT;
  const SCALE = 2; // render at 2x for crisp text

  // ── Theme colors (dark) ────────────────────────────────────
  const THEME = {
    bg:       '#0a0a0c',
    bgCard:   '#111115',
    text:     '#e8e8ed',
    textSec:  '#9898a8',
    textTer:  '#6a6a7a',
    accent:   '#6e56cf',
    border:   '#2a2a35',
    codeBg:   '#111115',
    blockBg:  '#1a1a21',
  };

  /**
   * Main entry: export assistant message at index `idx` to images
   */
  async function exportMessage(idx) {
    const conv = getActiveConv();
    if (!conv) return;
    const msg = conv.messages[idx];
    if (!msg || msg.role !== 'assistant') return;

    // Show progress toast
    const toastId = _showProgress('Preparing export…', 0);

    try {
      // 1. Get the rendered HTML from the DOM
      const msgEl = document.getElementById(`msg-${idx}`);
      if (!msgEl) throw new Error('Message element not found');

      const mdEl = msgEl.querySelector('.md-content');
      if (!mdEl) throw new Error('No content to export');

      // 2. Build the offscreen container
      const { container, contentDiv } = _createOffscreenContainer();
      document.body.appendChild(container);

      // 3. Clone content into the measurement container
      const clonedContent = mdEl.cloneNode(true);
      // Remove any user-content class (it changes white-space handling)
      clonedContent.classList.remove('user-content');
      contentDiv.appendChild(clonedContent);

      // 4. Wait for any images/fonts to settle
      await new Promise(r => setTimeout(r, 200));

      // 5. Measure & split into pages
      const pages = _splitIntoPages(contentDiv);
      _updateProgress(toastId, `Rendering ${pages.length} page(s)…`, 10);

      // 6. Render each page to canvas
      const images = [];
      for (let i = 0; i < pages.length; i++) {
        const pct = 10 + Math.round((i / pages.length) * 80);
        _updateProgress(toastId, `Rendering page ${i + 1}/${pages.length}…`, pct);

        const canvas = await _renderPage(contentDiv, pages[i], i, pages.length);
        images.push(canvas);
      }

      // 7. Download
      _updateProgress(toastId, 'Downloading…', 95);
      await _downloadImages(images, conv, idx);

      // 8. Cleanup
      document.body.removeChild(container);
      _updateProgress(toastId, `✅ Exported ${images.length} image(s)`, 100);
      setTimeout(() => _removeProgress(toastId), 2500);

    } catch (err) {
      console.error('[ExportImages] Error:', err);
      _updateProgress(toastId, `❌ Export failed: ${err.message}`, -1);
      setTimeout(() => _removeProgress(toastId), 4000);
    }
  }

  // ── Create offscreen measuring/rendering container ─────────
  function _createOffscreenContainer() {
    const container = document.createElement('div');
    container.id = 'export-offscreen';
    container.style.cssText = `
      position: fixed; left: -9999px; top: 0; z-index: -1;
      width: ${CONTENT_W}px; overflow: hidden;
      background: ${THEME.bg}; color: ${THEME.text};
      font-family: 'Plus Jakarta Sans', -apple-system, sans-serif;
    `;

    const contentDiv = document.createElement('div');
    contentDiv.className = 'md-content export-md-content';
    contentDiv.style.cssText = `
      width: ${CONTENT_W}px;
      font-size: 17px;
      line-height: 1.8;
      color: ${THEME.text};
      overflow: visible;
    `;

    container.appendChild(contentDiv);
    return { container, contentDiv };
  }

  // ── Split content into page ranges ─────────────────────────
  // Returns array of { startY, endY } for each page slice
  function _splitIntoPages(contentDiv) {
    const totalHeight = contentDiv.scrollHeight;
    if (totalHeight <= CONTENT_H) {
      return [{ startY: 0, endY: totalHeight }];
    }

    const pages = [];
    let currentY = 0;

    while (currentY < totalHeight) {
      let endY = currentY + CONTENT_H;
      if (endY >= totalHeight) {
        pages.push({ startY: currentY, endY: totalHeight });
        break;
      }

      // Try to find a good break point (between block elements)
      const breakY = _findBreakPoint(contentDiv, currentY, endY);
      pages.push({ startY: currentY, endY: breakY });
      currentY = breakY;
    }

    return pages;
  }

  // ── Find a good break point near targetY ───────────────────
  function _findBreakPoint(contentDiv, startY, targetY) {
    // Walk all top-level children and find the last one that ends before targetY
    const children = contentDiv.children;
    let bestBreak = targetY;

    // Check all descendant elements for break points
    const allElements = contentDiv.querySelectorAll('p, h1, h2, h3, h4, li, pre, blockquote, table, hr, .code-header, .katex-display, br');
    let lastGoodBreak = startY + CONTENT_H * 0.3; // minimum 30% fill

    for (const el of allElements) {
      const rect = el.getBoundingClientRect();
      const containerRect = contentDiv.getBoundingClientRect();
      const elBottom = rect.bottom - containerRect.top;
      const elTop = rect.top - containerRect.top;

      // We want elements that end within our page range
      if (elBottom > startY && elBottom <= targetY) {
        // Break after this element
        if (elBottom > lastGoodBreak) {
          lastGoodBreak = elBottom;
        }
      }
      // If element starts before target but extends past, break before it
      if (elTop > startY + CONTENT_H * 0.3 && elTop <= targetY && elBottom > targetY) {
        if (elTop > startY + CONTENT_H * 0.2) {
          bestBreak = elTop;
          return bestBreak;
        }
      }
    }

    return lastGoodBreak > startY ? lastGoodBreak : targetY;
  }

  // ── Render a single page to canvas ─────────────────────────
  async function _renderPage(contentDiv, pageRange, pageIdx, totalPages) {
    const { startY, endY } = pageRange;
    const sliceH = endY - startY;

    // Create a wrapper with exact page dimensions
    const pageDiv = document.createElement('div');
    pageDiv.style.cssText = `
      width: ${PAGE_W}px;
      height: ${PAGE_H}px;
      background: ${THEME.bg};
      position: fixed;
      left: -9999px;
      top: 0;
      z-index: -1;
      overflow: hidden;
    `;

    // ── Decorative header gradient bar ──
    const headerBar = document.createElement('div');
    headerBar.style.cssText = `
      position: absolute; top: 0; left: 0; right: 0; height: 4px;
      background: linear-gradient(90deg, ${THEME.accent}, #a78bfa, #6e56cf);
    `;
    pageDiv.appendChild(headerBar);

    // ── Page number / branding (top-right) ──
    if (totalPages > 1) {
      const pageLabel = document.createElement('div');
      pageLabel.style.cssText = `
        position: absolute; top: 24px; right: ${PAD_X}px;
        font-size: 13px; color: ${THEME.textTer};
        font-family: 'JetBrains Mono', monospace;
        letter-spacing: 0.05em;
      `;
      pageLabel.textContent = `${pageIdx + 1} / ${totalPages}`;
      pageDiv.appendChild(pageLabel);
    }

    // ── Brand label (top-left) ──
    const brand = document.createElement('div');
    brand.style.cssText = `
      position: absolute; top: 22px; left: ${PAD_X}px;
      font-size: 14px; font-weight: 700; letter-spacing: -0.02em;
      background: linear-gradient(135deg, ${THEME.accent}, #a78bfa);
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
      font-family: 'Plus Jakarta Sans', -apple-system, sans-serif;
    `;
    brand.textContent = '✦ Claude';
    pageDiv.appendChild(brand);

    // ── Content viewport ──
    const viewport = document.createElement('div');
    viewport.style.cssText = `
      position: absolute;
      left: ${PAD_X}px;
      top: ${PAD_TOP}px;
      width: ${CONTENT_W}px;
      height: ${CONTENT_H}px;
      overflow: hidden;
    `;

    // Clone the full content and offset it
    const contentClone = contentDiv.cloneNode(true);
    contentClone.style.cssText = `
      position: absolute;
      top: ${-startY}px;
      left: 0;
      width: ${CONTENT_W}px;
      font-size: 17px;
      line-height: 1.8;
      color: ${THEME.text};
    `;
    viewport.appendChild(contentClone);
    pageDiv.appendChild(viewport);

    // ── Footer watermark ──
    const footer = document.createElement('div');
    footer.style.cssText = `
      position: absolute;
      bottom: 32px;
      left: 0; right: 0;
      text-align: center;
      font-size: 11px;
      color: ${THEME.textTer};
      font-family: 'JetBrains Mono', monospace;
      opacity: 0.5;
    `;
    footer.textContent = 'Generated by ChatUI';
    pageDiv.appendChild(footer);

    // ── Bottom gradient bar (mirror of top) ──
    const footerBar = document.createElement('div');
    footerBar.style.cssText = `
      position: absolute; bottom: 0; left: 0; right: 0; height: 3px;
      background: linear-gradient(90deg, #a78bfa, ${THEME.accent});
      opacity: 0.5;
    `;
    pageDiv.appendChild(footerBar);

    document.body.appendChild(pageDiv);

    // Lazy-load & use html2canvas to render
    await _ensureHtml2Canvas();
    const canvas = await html2canvas(pageDiv, {
      width: PAGE_W,
      height: PAGE_H,
      scale: SCALE,
      backgroundColor: THEME.bg,
      useCORS: true,
      logging: false,
      // Ensure fonts are rendered
      onclone: (doc) => {
        // Copy computed styles for code blocks, etc.
        const styles = doc.querySelectorAll('style, link[rel="stylesheet"]');
        // html2canvas handles this automatically
      }
    });

    document.body.removeChild(pageDiv);
    return canvas;
  }

  // ── Download images ────────────────────────────────────────
  async function _downloadImages(canvases, conv, idx) {
    const timestamp = new Date().toISOString().slice(0, 19).replace(/[T:]/g, '-');
    const prefix = `chat-export-${timestamp}`;

    if (canvases.length === 1) {
      // Single image — direct download
      const blob = await _canvasToBlob(canvases[0]);
      _downloadBlob(blob, `${prefix}.png`);
    } else {
      // Multiple images — download individually with slight delay
      for (let i = 0; i < canvases.length; i++) {
        const blob = await _canvasToBlob(canvases[i]);
        _downloadBlob(blob, `${prefix}-${String(i + 1).padStart(2, '0')}.png`);
        // Small delay to avoid browser throttling
        if (i < canvases.length - 1) {
          await new Promise(r => setTimeout(r, 300));
        }
      }
    }
  }

  function _canvasToBlob(canvas) {
    return new Promise(resolve => {
      canvas.toBlob(blob => resolve(blob), 'image/png');
    });
  }

  // Synchronous blob creation for preview (avoids async per-card)
  function _canvasToBlobSync(canvas) {
    try {
      const dataUrl = canvas.toDataURL('image/png');
      const parts = dataUrl.split(',');
      const mime = parts[0].match(/:(.*?);/)[1];
      const bstr = atob(parts[1]);
      const n = bstr.length;
      const u8 = new Uint8Array(n);
      for (let i = 0; i < n; i++) u8[i] = bstr.charCodeAt(i);
      return new Blob([u8], { type: mime });
    } catch {
      return null;
    }
  }

  function _downloadBlob(blob, filename) {
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    a.style.display = 'none';
    document.body.appendChild(a);
    a.click();
    setTimeout(() => {
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    }, 100);
  }

  // ── Progress toast helpers ─────────────────────────────────
  let _toastCounter = 0;

  function _showProgress(text, pct) {
    const id = `export-progress-${++_toastCounter}`;
    const el = document.createElement('div');
    el.id = id;
    el.className = 'export-progress-toast';
    el.innerHTML = `
      <div class="export-progress-icon">📸</div>
      <div class="export-progress-body">
        <div class="export-progress-text">${text}</div>
        <div class="export-progress-bar-wrap">
          <div class="export-progress-bar-fill" style="width:${Math.max(pct, 0)}%"></div>
        </div>
      </div>
    `;
    const container = document.getElementById('toastContainer');
    if (container) container.appendChild(el);
    return id;
  }

  function _updateProgress(id, text, pct) {
    const el = document.getElementById(id);
    if (!el) return;
    const textEl = el.querySelector('.export-progress-text');
    const barEl = el.querySelector('.export-progress-bar-fill');
    if (textEl) textEl.textContent = text;
    if (barEl) barEl.style.width = `${Math.max(pct, 0)}%`;
    if (pct >= 100 || pct < 0) {
      el.classList.add(pct >= 100 ? 'done' : 'error');
    }
  }

  function _removeProgress(id) {
    const el = document.getElementById(id);
    if (el) {
      el.style.opacity = '0';
      el.style.transform = 'translateX(100%)';
      setTimeout(() => el.remove(), 300);
    }
  }

  // ── Preview modal (optional — shows all pages before download) ──
  async function exportMessageWithPreview(idx) {
    const conv = getActiveConv();
    if (!conv) return;
    const msg = conv.messages[idx];
    if (!msg || msg.role !== 'assistant') return;

    const toastId = _showProgress('Preparing preview…', 0);

    try {
      const msgEl = document.getElementById(`msg-${idx}`);
      if (!msgEl) throw new Error('Message element not found');
      const mdEl = msgEl.querySelector('.md-content');
      if (!mdEl) throw new Error('No content to export');

      const { container, contentDiv } = _createOffscreenContainer();
      document.body.appendChild(container);

      const clonedContent = mdEl.cloneNode(true);
      clonedContent.classList.remove('user-content');
      contentDiv.appendChild(clonedContent);
      await new Promise(r => setTimeout(r, 200));

      const pages = _splitIntoPages(contentDiv);
      _updateProgress(toastId, `Rendering ${pages.length} page(s)…`, 10);

      const canvases = [];
      for (let i = 0; i < pages.length; i++) {
        const pct = 10 + Math.round((i / pages.length) * 80);
        _updateProgress(toastId, `Rendering page ${i + 1}/${pages.length}…`, pct);
        const canvas = await _renderPage(contentDiv, pages[i], i, pages.length);
        canvases.push(canvas);
      }

      document.body.removeChild(container);
      _updateProgress(toastId, '✅ Preview ready', 100);
      setTimeout(() => _removeProgress(toastId), 1500);

      // Show preview modal
      _showPreviewModal(canvases, conv, idx);

    } catch (err) {
      console.error('[ExportImages] Error:', err);
      _updateProgress(toastId, `❌ Export failed: ${err.message}`, -1);
      setTimeout(() => _removeProgress(toastId), 4000);
    }
  }

  function _showPreviewModal(canvases, conv, idx) {
    // Remove any existing modal
    const existing = document.getElementById('export-preview-modal');
    if (existing) {
      // Revoke any old blob URLs to free memory
      existing.querySelectorAll('img[src^="blob:"]').forEach(img => URL.revokeObjectURL(img.src));
      existing.remove();
    }

    const overlay = document.createElement('div');
    overlay.id = 'export-preview-modal';
    overlay.className = 'export-preview-overlay';

    // Track blob URLs so we can revoke on close
    const blobUrls = [];
    function _cleanup() {
      blobUrls.forEach(u => URL.revokeObjectURL(u));
      blobUrls.length = 0;
      overlay.remove();
      document.removeEventListener('keydown', escHandler);
    }

    overlay.onclick = (e) => {
      if (e.target === overlay) _cleanup();
    };

    // ── Toolbar (matches CSS .epv-toolbar) ──
    const toolbar = document.createElement('div');
    toolbar.className = 'epv-toolbar';
    toolbar.innerHTML = `
      <span style="font-size:15px;display:flex;align-items:center;gap:6px">
        📸 <strong>Export Preview</strong>
        <span style="opacity:0.6;font-weight:400">${canvases.length} page${canvases.length > 1 ? 's' : ''}</span>
      </span>
      <button class="primary" id="exportDownloadAll">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="vertical-align:-2px">
          <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
          <polyline points="7 10 12 15 17 10"/>
          <line x1="12" y1="15" x2="12" y2="3"/>
        </svg>
        Download All
      </button>
      <button id="exportCloseBtn">✕ Close</button>
    `;
    overlay.appendChild(toolbar);

    // ── Pages grid (matches CSS .epv-grid > .epv-card) ──
    const grid = document.createElement('div');
    grid.className = 'epv-grid';

    canvases.forEach((canvas, i) => {
      const card = document.createElement('div');
      card.className = 'epv-card';

      // Use blob URL instead of data URL — far less memory pressure
      const img = document.createElement('img');
      const blob = _canvasToBlobSync(canvas);
      if (blob) {
        const blobUrl = URL.createObjectURL(blob);
        blobUrls.push(blobUrl);
        img.src = blobUrl;
      } else {
        // Fallback: toDataURL (synchronous but heavier)
        img.src = canvas.toDataURL('image/png');
      }
      img.alt = `Page ${i + 1}`;
      img.loading = 'lazy';

      const label = document.createElement('div');
      label.className = 'epv-label';
      label.innerHTML = `
        <span>Page ${i + 1}</span>
        <button class="epv-card-dl" data-idx="${i}" title="Download this page">
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
            <polyline points="7 10 12 15 17 10"/>
            <line x1="12" y1="15" x2="12" y2="3"/>
          </svg>
        </button>
      `;

      card.appendChild(img);
      card.appendChild(label);
      grid.appendChild(card);
    });

    overlay.appendChild(grid);
    document.body.appendChild(overlay);

    // Event listeners
    document.getElementById('exportCloseBtn').onclick = () => _cleanup();
    document.getElementById('exportDownloadAll').onclick = async () => {
      await _downloadImages(canvases, conv, idx);
      showToast('✅', 'Downloaded', `${canvases.length} image(s) saved`);
    };

    // Individual page download
    grid.querySelectorAll('.epv-card-dl').forEach(btn => {
      btn.onclick = async (e) => {
        e.stopPropagation();
        const i = parseInt(btn.dataset.idx);
        const timestamp = new Date().toISOString().slice(0, 19).replace(/[T:]/g, '-');
        const blob = await _canvasToBlob(canvases[i]);
        _downloadBlob(blob, `chat-export-${timestamp}-${String(i + 1).padStart(2, '0')}.png`);
      };
    });

    // ESC to close
    const escHandler = (e) => {
      if (e.key === 'Escape') _cleanup();
    };
    document.addEventListener('keydown', escHandler);
  }

  // Public API
  return { exportMessage, exportMessageWithPreview };
})();
