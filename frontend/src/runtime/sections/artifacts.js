/* ===== migrated source: artifacts.js ===== */
/* artifacts.js — Right-side panel for renderable chat artifacts (md/html/svg).
 *
 * Backend: lib/artifacts/* persists each artifact to the chat_artifacts
 * table.  Producers (write_file post-hook + inline scanner) call
 * create_artifact + emit a metadata-only "artifact" SSE event.  This
 * module:
 *
 *   1. Normalizes artifact metadata into a presentation-only read model,
 *      joined to authoritative Turns through stable task/turn identity.
 *   2. ConversationSurface renders typed artifact blocks; clicking a chip
 *      emits an open-artifact intent back to this panel.
 *   3. Fetches /api/artifacts/<id>/raw lazily on open and renders it:
 *        markdown → renderMarkdown() (DOMPurify-cleaned, see core.js)
 *        html     → <iframe sandbox srcdoc>  (NO scripts by default)
 *        svg      → <iframe sandbox srcdoc>  (forced when <script> present)
 *
 * Security invariants — see lib/artifacts/__init__.py and routes/artifacts.py.
 *   • NEVER innerHTML model HTML into the main document.
 *   • Iframe must NOT have allow-scripts unless the user explicitly opts in
 *     (and we then drop allow-same-origin to keep the spec-safe combo).
 *   • The /raw endpoint already ships a strict CSP — this is defense in depth.
 */

(function () {
  "use strict";

  // ── Module state ────────────────────────────────────────────────────
  // Conv-scoped cache of metadata + content.  Keyed by artifact id.
  // Content fetched lazily on first open; subsequent opens are instant.
  const _metaCache = new Map();    // id → meta dict
  const _contentCache = new Map(); // id → raw string
  // A newer snapshot hint must win over an older metadata request that settles
  // late. Weak keys keep the fence tied to the catalog shell lifecycle.
  const _hydrateEpoch = new WeakMap();

  // The currently open artifact (if any), so SSE updates can refresh.
  let _activeId = null;

  // Per-session script-allow consent (NOT persisted).
  // Set<id>  — present means user clicked "Run scripts".
  const _scriptsAllowed = new Set();

  // What the panel is currently showing: 'doc' | 'library'.
  let _panelMode = "doc";

  // ── Inline SVG icons (24×24 viewBox, currentColor stroke/fill) ─────
  // No emojis anywhere in the panel chrome per design guideline.
  const ICON = {
    close:    '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18M6 6l12 12"/></svg>',
    pinOff:   '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m12 17 .01 5"/><path d="M9 10v3.6a1 1 0 0 1-.4.8L6 16h12l-2.6-1.6a1 1 0 0 1-.4-.8V10"/><path d="M5 8h14l-1.7-3.4a1 1 0 0 0-.9-.6H7.6a1 1 0 0 0-.9.6Z"/></svg>',
    pinOn:    '<svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"><path d="M12 2.4 9.05 5.36a4 4 0 0 0-1.17 2.83v1.7L4 13.77l1.41 1.42 4.6-4.6V22h2v-11.4l4.59 4.59L18 13.78l-3.88-3.89v-1.7a4 4 0 0 0-1.17-2.83L10 2.4Z" fill-opacity=".18"/><path d="M12 22V12"/><path d="m9 6 3-3 3 3v3.2a3 3 0 0 0 .88 2.12L19 14H5l3.12-2.68A3 3 0 0 0 9 9.2V6Z"/></svg>',
    source:   '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/></svg>',
    copy:     '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>',
    download: '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>',
    print:    '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 6 2 18 2 18 9"/><path d="M6 18H4a2 2 0 0 1-2-2v-5a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v5a2 2 0 0 1-2 2h-2"/><rect x="6" y="14" width="12" height="8" rx="1"/></svg>',
    library:  '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2Z"/></svg>',
    docOpen:  '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="9" y1="13" x2="15" y2="13"/><line x1="9" y1="17" x2="15" y2="17"/></svg>',
    chevronDown: '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg>',
    arrowLeft: '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="19" y1="12" x2="5" y2="12"/><polyline points="12 19 5 12 12 5"/></svg>',
    warn:     '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0Z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>',
    search:   '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="7"/><line x1="20" y1="20" x2="16.65" y2="16.65"/></svg>',
    // Format-specific icons used by chips and library rows.
    fmtMarkdown: '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8Z"/><polyline points="14 2 14 8 20 8"/><path d="M8 13h.01"/><path d="M8 17h.01"/><path d="M12 17v-4l1.6 2 1.4-2v4"/></svg>',
    fmtHtml:     '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/></svg>',
    fmtSvg:      '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21 15 16 10 5 21"/></svg>',
  };

  function _formatIcon(fmt) {
    if (fmt === "html") return ICON.fmtHtml;
    if (fmt === "svg")  return ICON.fmtSvg;
    return ICON.fmtMarkdown;  // markdown + unknown share doc icon
  }

  function _formatLabel(fmt) {
    if (fmt === "markdown") return "MD";
    if (fmt === "html")     return "HTML";
    if (fmt === "svg")      return "SVG";
    return (fmt || "DOC").toUpperCase();
  }

  // ── DOM helpers ─────────────────────────────────────────────────────

  function _ensurePanel() {
    let panel = document.getElementById("artifact-panel");
    if (panel) return panel;

    panel = document.createElement("aside");
    panel.id = "artifact-panel";
    panel.className = "artifact-panel artifact-panel-hidden";
    panel.setAttribute("aria-hidden", "true");
    panel.innerHTML =
      '<div class="artifact-panel-resize" data-art-resize title="Drag to resize"></div>' +
      '<header class="artifact-panel-header">' +
        '<div class="artifact-panel-titleblock">' +
          '<button class="artifact-iconbtn artifact-iconbtn-back" data-art-action="library" title="Open library" aria-label="Open library" data-art-mode-icon="doc">' +
            ICON.library +
          '</button>' +
          '<button class="artifact-iconbtn artifact-iconbtn-back" data-art-action="back-to-doc" title="Back to current artifact" aria-label="Back to current artifact" data-art-mode-icon="library" hidden>' +
            ICON.arrowLeft +
          '</button>' +
          '<div class="artifact-panel-titleinfo">' +
            '<div class="artifact-panel-titlerow">' +
              '<span class="artifact-format-pill" data-art-format></span>' +
              '<span class="artifact-panel-title" data-art-title></span>' +
            '</div>' +
            '<div class="artifact-panel-subtitle" data-art-subtitle hidden></div>' +
          '</div>' +
          '<select class="artifact-version-select" data-art-version hidden aria-label="Version"></select>' +
        '</div>' +
        '<div class="artifact-panel-actions" role="toolbar" aria-label="Artifact actions">' +
          '<button class="artifact-iconbtn" data-art-action="toggle-source" title="Toggle source view" aria-label="Toggle source view">' +
            ICON.source +
          '</button>' +
          '<button class="artifact-iconbtn" data-art-action="copy" title="Copy source" aria-label="Copy source">' +
            ICON.copy +
          '</button>' +
          '<button class="artifact-iconbtn" data-art-action="download" title="Download original" aria-label="Download">' +
            ICON.download +
          '</button>' +
          '<button class="artifact-iconbtn artifact-iconbtn-text" data-art-action="export-pdf" title="Export as PDF" aria-label="Export as PDF">PDF</button>' +
          '<button class="artifact-iconbtn" data-art-action="print" title="Print (Ctrl/Cmd+P)" aria-label="Print">' +
            ICON.print +
          '</button>' +
          '<span class="artifact-actions-sep" aria-hidden="true"></span>' +
          '<button class="artifact-iconbtn" data-art-action="pin" title="Pin" aria-label="Pin">' +
            ICON.pinOff +
          '</button>' +
          '<button class="artifact-iconbtn artifact-iconbtn-close" data-art-action="close" title="Close (Esc)" aria-label="Close">' +
            ICON.close +
          '</button>' +
        '</div>' +
      '</header>' +
      '<div class="artifact-panel-banner" data-art-banner hidden></div>' +
      '<div class="artifact-panel-body" data-art-body></div>';
    document.body.appendChild(panel);

    _restorePanelWidth(panel);
    panel.addEventListener("click", _onPanelClick);
    panel.addEventListener("change", _onPanelChange);
    panel.addEventListener("input", _onPanelInput);
    _wireResize(panel);
    return panel;
  }

  // ── Resizable panel (persisted to localStorage) ────────────────────
  const _PANEL_WIDTH_KEY = "tofu.artifactPanel.width";

  function _restorePanelWidth(panel) {
    try {
      const stored = parseInt(localStorage.getItem(_PANEL_WIDTH_KEY) || "", 10);
      if (Number.isFinite(stored) && stored >= 320 && stored <= window.innerWidth - 200) {
        panel.style.width = stored + "px";
      }
    } catch (e) { console.debug("[Artifacts] restore width failed:", e); }
  }

  function _wireResize(panel) {
    const handle = panel.querySelector("[data-art-resize]");
    if (!handle) return;
    let startX = 0, startW = 0;
    const onMove = (e) => {
      const dx = startX - e.clientX;
      const w = Math.max(320, Math.min(window.innerWidth - 200, startW + dx));
      panel.style.width = w + "px";
    };
    const onUp = () => {
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
      document.body.style.userSelect = "";
      try { localStorage.setItem(_PANEL_WIDTH_KEY, String(panel.offsetWidth)); }
      catch (e) { console.debug("[Artifacts] persist width failed:", e); }
    };
    handle.addEventListener("mousedown", (e) => {
      startX = e.clientX;
      startW = panel.offsetWidth;
      document.body.style.userSelect = "none";
      document.addEventListener("mousemove", onMove);
      document.addEventListener("mouseup", onUp);
      e.preventDefault();
    });
  }

  function _onPanelClick(e) {
    const libBtn = e.target.closest("[data-art-lib-id]");
    if (libBtn) {
      const id = libBtn.getAttribute("data-art-lib-id");
      if (id) open(id);
      return;
    }
    const pinChip = e.target.closest("[data-art-pin-id]");
    if (pinChip) {
      const id = pinChip.getAttribute("data-art-pin-id");
      e.stopPropagation();
      _togglePinById(id);
      return;
    }
    const btn = e.target.closest("[data-art-action]");
    if (!btn) return;
    const action = btn.getAttribute("data-art-action");
    if (action === "close") {
      closePanel();
    } else if (action === "library") {
      _showLibrary();
    } else if (action === "back-to-doc") {
      if (_activeId) open(_activeId);
    } else if (action === "copy") {
      _copySource();
    } else if (action === "download") {
      _download();
    } else if (action === "export-pdf") {
      _exportPdf();
    } else if (action === "print") {
      _print();
    } else if (action === "pin") {
      _togglePin();
    } else if (action === "toggle-source") {
      _toggleSourceView();
    } else if (action === "allow-scripts") {
      _allowScripts();
    }
  }

  function _onPanelChange(e) {
    const sel = e.target.closest("[data-art-version]");
    if (sel && sel.value && sel.value !== _activeId) {
      open(sel.value);
    }
  }

  function _onPanelInput(e) {
    const search = e.target.closest("[data-art-lib-search]");
    if (search) {
      _filterLibrary(search.value || "");
    }
  }

  // ── Open / close ────────────────────────────────────────────────────

  /**
   * Open an artifact in the right-side panel by id.  Fetches metadata +
   * raw content on demand; subsequent opens hit cache.
   */
  async function open(id) {
    if (!id) return;
    const panel = _ensurePanel();
    _activeId = id;
    _panelMode = "doc";
    panel.dataset.mode = "doc";

    // Reveal the panel + adjust layout immediately so the user gets feedback.
    panel.classList.remove("artifact-panel-hidden");
    panel.setAttribute("aria-hidden", "false");
    document.body.classList.add("artifact-panel-open");

    const titleEl = panel.querySelector("[data-art-title]");
    const fmtEl   = panel.querySelector("[data-art-format]");
    const subEl   = panel.querySelector("[data-art-subtitle]");
    const bodyEl  = panel.querySelector("[data-art-body]");
    if (titleEl) titleEl.textContent = "Loading…";
    if (fmtEl) { fmtEl.textContent = ""; fmtEl.dataset.format = ""; }
    if (subEl) { subEl.hidden = true; subEl.textContent = ""; }
    if (bodyEl) bodyEl.innerHTML = '<div class="artifact-loading">' + ICON.docOpen + '<span>Loading artifact…</span></div>';

    try {
      const meta = await _ensureMeta(id);
      if (_activeId !== id) return;  // user opened something else
      _renderHeader(panel, meta);

      const content = await _ensureContent(id);
      if (_activeId !== id) return;
      _renderBody(panel, meta, content, /*sourceView=*/ false);
    } catch (err) {
      console.warn("[Artifacts] open failed:", err);
      if (bodyEl) {
        bodyEl.innerHTML =
          '<div class="artifact-error">' + ICON.warn +
          '<div><strong>Could not load artifact</strong>' +
          '<div class="artifact-error-detail">' +
          (typeof escapeHtml === "function" ? escapeHtml(String(err)) : String(err)) +
          '</div></div></div>';
      }
    }
  }

  function closePanel() {
    const panel = document.getElementById("artifact-panel");
    if (!panel) return;
    panel.classList.add("artifact-panel-hidden");
    panel.setAttribute("aria-hidden", "true");
    document.body.classList.remove("artifact-panel-open");
    _activeId = null;
    _panelMode = "doc";
  }

  // ── Fetch helpers ───────────────────────────────────────────────────

  async function _ensureMeta(id) {
    if (_metaCache.has(id)) return _metaCache.get(id);
    const meta = await Api.artifacts.meta(id);
    if (!meta) throw new Error("artifact meta not found");
    _metaCache.set(id, meta);
    return meta;
  }

  async function _ensureContent(id) {
    if (_contentCache.has(id)) return _contentCache.get(id);
    const text = await Api.artifacts.contentText(id);
    _contentCache.set(id, text);
    return text;
  }

  // ── Rendering ───────────────────────────────────────────────────────

  function _setMode(panel, mode) {
    panel.dataset.mode = mode;
    _panelMode = mode;
    // Toggle the leading icon (library button vs back-to-doc).
    panel.querySelectorAll('[data-art-mode-icon]').forEach(function (el) {
      const want = el.getAttribute("data-art-mode-icon");
      // Show "back-to-doc" only in library mode AND only if we have an
      // active doc to go back to.
      if (want === "library") {
        el.hidden = !(mode === "library" && _activeId);
      } else {
        el.hidden = (mode === "library");
      }
    });
    // Hide doc-specific actions in library mode (no doc to act on).
    const docOnly = ["toggle-source", "copy", "download", "export-pdf", "print", "pin"];
    panel.querySelectorAll(".artifact-panel-actions [data-art-action]").forEach(function (el) {
      const a = el.getAttribute("data-art-action");
      if (a === "close") return;
      el.hidden = (mode === "library") && docOnly.indexOf(a) !== -1;
    });
    panel.querySelectorAll(".artifact-actions-sep").forEach(function (el) {
      el.hidden = (mode === "library");
    });
  }

  function _renderHeader(panel, meta) {
    _setMode(panel, "doc");
    const titleEl = panel.querySelector("[data-art-title]");
    const fmtEl   = panel.querySelector("[data-art-format]");
    const subEl   = panel.querySelector("[data-art-subtitle]");
    const titleStr = meta.title || ("artifact-" + (meta.id || "").slice(0, 8));
    if (titleEl) titleEl.textContent = titleStr;
    if (fmtEl) {
      fmtEl.textContent = _formatLabel(meta.format);
      fmtEl.dataset.format = meta.format || "";
    }
    if (subEl) {
      const parts = [];
      if (meta.size_bytes) parts.push(_formatBytes(meta.size_bytes));
      if (meta.created_at) parts.push(_relativeTime(meta.created_at));
      if ((meta.version || 1) > 1) parts.push("v" + meta.version);
      if (parts.length) {
        subEl.hidden = false;
        subEl.textContent = parts.join(" · ");
      } else {
        subEl.hidden = true;
        subEl.textContent = "";
      }
    }
    const pinBtn = panel.querySelector('[data-art-action="pin"]');
    if (pinBtn) {
      pinBtn.innerHTML = meta.pinned ? ICON.pinOn : ICON.pinOff;
      pinBtn.classList.toggle("artifact-iconbtn-active", !!meta.pinned);
      pinBtn.setAttribute("aria-pressed", meta.pinned ? "true" : "false");
    }
    _populateVersionSelect(panel, meta).catch(function (e) {
      console.debug("[Artifacts] version select populate failed:", e);
    });
  }

  async function _populateVersionSelect(panel, meta) {
    const sel = panel.querySelector("[data-art-version]");
    if (!sel) return;
    if (!meta.parent_id && (meta.version || 1) <= 1) {
      sel.hidden = true;
      sel.innerHTML = "";
      return;
    }
    try {
      const payload = await Api.artifacts.versions(meta.id);
      if (!payload) throw new Error("versions fetch failed");
      const chain = (payload && payload.versions) || [];
      if (chain.length <= 1) {
        sel.hidden = true;
        sel.innerHTML = "";
        return;
      }
      sel.innerHTML = chain.map(function (v) {
        const sel_attr = v.id === meta.id ? " selected" : "";
        return '<option value="' + v.id + '"' + sel_attr + '>' +
          'v' + (v.version || 1) +
          '</option>';
      }).join("");
      sel.hidden = false;
    } catch (e) {
      console.debug("[Artifacts] versions fetch failed:", e);
      sel.hidden = true;
      sel.innerHTML = "";
    }
  }

  function _renderBody(panel, meta, content, sourceView) {
    const bodyEl = panel.querySelector("[data-art-body]");
    const banner = panel.querySelector("[data-art-banner]");
    if (!bodyEl) return;

    // Reset state from previous artifact
    if (banner) {
      banner.hidden = true;
      banner.innerHTML = "";
    }
    bodyEl.innerHTML = "";
    bodyEl.dataset.sourceView = sourceView ? "1" : "0";

    if (sourceView) {
      const pre = document.createElement("pre");
      pre.className = "artifact-source-view";
      pre.textContent = content;
      bodyEl.appendChild(pre);
      return;
    }

    const fmt = meta.format;
    if (fmt === "markdown") {
      _renderMarkdown(bodyEl, content);
    } else if (fmt === "html") {
      _renderHtml(panel, banner, bodyEl, meta, content);
    } else if (fmt === "svg") {
      _renderSvg(panel, banner, bodyEl, meta, content);
    } else {
      const pre = document.createElement("pre");
      pre.className = "artifact-source-view";
      pre.textContent = content;
      bodyEl.appendChild(pre);
    }
  }

  function _renderMarkdown(bodyEl, content) {
    // renderMarkdown (core.js:2906) already runs DOMPurify.sanitize on the
    // marked.parse output, so it's safe to inject as HTML.
    let html;
    try {
      html = (typeof renderMarkdown === "function")
        ? renderMarkdown(content)
        : (typeof escapeHtml === "function" ? "<pre>" + escapeHtml(content) + "</pre>"
                                            : "<pre>" + content + "</pre>");
    } catch (e) {
      console.warn("[Artifacts] renderMarkdown failed, falling back to escapeHtml:", e);
      html = (typeof escapeHtml === "function" ? "<pre>" + escapeHtml(content) + "</pre>"
                                                : "<pre>" + content + "</pre>");
    }
    const wrap = document.createElement("article");
    wrap.className = "artifact-md md-content";
    wrap.innerHTML = html;
    bodyEl.appendChild(wrap);
  }

  function _renderHtml(panel, banner, bodyEl, meta, content) {
    const allowScripts = _scriptsAllowed.has(meta.id);
    const hasScripts = /<script[\s>]/i.test(content);

    if (hasScripts && !allowScripts && banner) {
      banner.hidden = false;
      banner.innerHTML =
        '<div class="artifact-banner-icon">' + ICON.warn + '</div>' +
        '<div class="artifact-banner-text">' +
          '<strong>Document scripts blocked.</strong> ' +
          'Math (KaTeX) and table-of-contents links work without enabling scripts. ' +
          'Click the button only if the document needs interactive JavaScript.' +
        '</div>' +
        '<button class="artifact-banner-action" data-art-action="allow-scripts">Run document scripts</button>';
    }

    const iframe = document.createElement("iframe");
    iframe.className = "artifact-iframe";
    iframe.setAttribute("referrerpolicy", "no-referrer");
    iframe.setAttribute("loading", "lazy");
    // ── /view (default) vs /raw + srcdoc (opt-in) ──────────────────
    // Default path uses ``/api/artifacts/<id>/view`` which:
    //   • Wraps the artifact in a minimal HTML shell that injects our
    //     trusted KaTeX bundle so $..$ / $$..$$ / \(..\) / \[..\] in
    //     the model HTML render as proper math even when the model's
    //     own <script> tags are blocked.
    //   • Ships ``Content-Security-Policy: sandbox allow-scripts;
    //     default-src 'self'; script-src 'self'; …`` so ONLY our
    //     same-origin KaTeX scripts run.  Inline + remote model
    //     <script> blocks are forbidden by ``script-src 'self'``;
    //     inline ``on*=`` handlers and ``javascript:`` href are too.
    //   • The iframe sandbox ``allow-scripts`` (NO allow-same-origin)
    //     is the second layer — even if a script slipped through, the
    //     iframe is an opaque origin and can't reach parent
    //     localStorage / cookies / DOM.
    //   • Has a real document URL, so <a href="#anchor"> resolves to
    //     a same-document fragment scroll (TOC works), and relative
    //     URLs stay scoped to /api/artifacts/* — see memory
    //     ``artifacts-iframe-srcdoc-anchor-trap``.
    //
    // The user-opt-in allow-scripts path keeps using srcdoc + /raw
    // because the more permissive /view CSP still blocks model
    // scripts; opt-in needs the broader sandbox model.
    if (allowScripts) {
      iframe.setAttribute("sandbox", "allow-scripts");
      iframe.srcdoc = content;
    } else {
      iframe.setAttribute("sandbox", "allow-scripts");
      iframe.src = Api.artifacts.viewUrl(meta.id);
    }
    bodyEl.appendChild(iframe);
  }

  function _renderSvg(panel, banner, bodyEl, meta, content) {
    const hasScripts = /<script[\s>]/i.test(content);
    if (hasScripts) {
      // Reuse the HTML iframe path so the script-block consent flow
      // (and the trusted-KaTeX wrapper) handle the dynamic case.
      _renderHtml(panel, banner, bodyEl, meta, content);
      return;
    }
    // Static SVG — render as an <img> via data: URL (sandboxed by browser).
    const wrap = document.createElement("div");
    wrap.className = "artifact-svg";
    const img = document.createElement("img");
    img.alt = meta.title || "SVG artifact";
    img.src = "data:image/svg+xml;utf8," + encodeURIComponent(content);
    wrap.appendChild(img);
    bodyEl.appendChild(wrap);
  }

  // ── Header actions ──────────────────────────────────────────────────

  async function _copySource() {
    if (!_activeId) return;
    try {
      const text = await _ensureContent(_activeId);
      await _safeClipboardWrite(text);
      if (typeof showToast === "function") {
        showToast("", "Copied", "Source copied to clipboard.", 1800);
      }
    } catch (e) {
      console.warn("[Artifacts] copy failed:", e);
    }
  }

  async function _download() {
    if (!_activeId) return;
    try {
      const meta = await _ensureMeta(_activeId);
      const text = await _ensureContent(_activeId);
      const ext = meta.format === "html" ? "html"
                : meta.format === "svg" ? "svg"
                : "md";
      const fname = (meta.title && /\.\w+$/.test(meta.title))
        ? meta.title
        : (meta.title || "artifact-" + meta.id.slice(0, 8)) + "." + ext;
      const mime = meta.format === "html" ? "text/html"
                 : meta.format === "svg" ? "image/svg+xml"
                 : "text/markdown";
      const blob = new Blob([text], { type: mime + ";charset=utf-8" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = fname;
      a.style.display = "none";
      document.body.appendChild(a);
      a.click();
      a.remove();
      // Revoke after a short delay so the download has time to start.
      setTimeout(function () { URL.revokeObjectURL(url); }, 5000);
    } catch (e) {
      console.warn("[Artifacts] download failed:", e);
    }
  }

  async function _togglePin() {
    if (!_activeId) return;
    try {
      const meta = await _ensureMeta(_activeId);
      await _setPinned(_activeId, !meta.pinned);
    } catch (e) {
      console.warn("[Artifacts] pin failed:", e);
    }
  }

  async function _togglePinById(id) {
    if (!id) return;
    try {
      const meta = await _ensureMeta(id);
      const updated = await _setPinned(id, !meta.pinned);
      // Re-render the library row in place if visible.
      const panel = document.getElementById("artifact-panel");
      if (panel && _panelMode === "library") {
        const row = panel.querySelector('[data-art-lib-id="' + CSS.escape(id) + '"]');
        if (row) {
          const pinBtn = row.querySelector('[data-art-pin-id]');
          if (pinBtn) {
            pinBtn.innerHTML = updated.pinned ? ICON.pinOn : ICON.pinOff;
            pinBtn.classList.toggle("is-pinned", !!updated.pinned);
            pinBtn.setAttribute("aria-pressed", updated.pinned ? "true" : "false");
          }
        }
      }
    } catch (e) {
      console.warn("[Artifacts] pin failed:", e);
    }
  }

  async function _setPinned(id, pinned) {
    const updated = await Api.artifacts.pin(id, pinned);
    if (!updated) throw new Error("pin failed");
    _metaCache.set(id, updated);
    if (id === _activeId) {
      const panel = document.getElementById("artifact-panel");
      if (panel) _renderHeader(panel, updated);
    }
    return updated;
  }

  async function _toggleSourceView() {
    if (!_activeId) return;
    const panel = document.getElementById("artifact-panel");
    if (!panel) return;
    const bodyEl = panel.querySelector("[data-art-body]");
    const cur = (bodyEl && bodyEl.dataset.sourceView === "1");
    const meta = await _ensureMeta(_activeId);
    const content = await _ensureContent(_activeId);
    _renderBody(panel, meta, content, /*sourceView=*/ !cur);
    const srcBtn = panel.querySelector('[data-art-action="toggle-source"]');
    if (srcBtn) srcBtn.classList.toggle("artifact-iconbtn-active", !cur);
  }

  async function _exportPdf() {
    if (!_activeId) return;
    try {
      // Trigger a download via an anchor — preserves the server-supplied
      // Content-Disposition filename and avoids buffering the PDF in JS.
      const a = document.createElement("a");
      a.href = Api.artifacts.exportPdfUrl(_activeId);
      a.target = "_blank";
      a.rel = "noopener";
      a.style.display = "none";
      document.body.appendChild(a);
      a.click();
      a.remove();
      if (typeof showToast === "function") {
        showToast("", "Export started", "Generating PDF…", 2200);
      }
    } catch (e) {
      console.warn("[Artifacts] export PDF failed:", e);
    }
  }

  function _print() {
    const panel = document.getElementById("artifact-panel");
    if (!panel) return;
    const iframe = panel.querySelector("iframe.artifact-iframe");
    if (iframe && iframe.contentWindow) {
      try {
        iframe.contentWindow.focus();
        iframe.contentWindow.print();
        return;
      } catch (e) {
        console.debug("[Artifacts] iframe print failed, falling back:", e);
      }
    }
    // Markdown / SVG: print via PDF download (server renders it).
    _exportPdf();
  }

  // ── Library ─────────────────────────────────────────────────────────

  async function _showLibrary() {
    const panel = _ensurePanel();
    panel.classList.remove("artifact-panel-hidden");
    panel.setAttribute("aria-hidden", "false");
    document.body.classList.add("artifact-panel-open");
    _setMode(panel, "library");

    const titleEl   = panel.querySelector("[data-art-title]");
    const fmtEl     = panel.querySelector("[data-art-format]");
    const subEl     = panel.querySelector("[data-art-subtitle]");
    const bodyEl    = panel.querySelector("[data-art-body]");
    const banner    = panel.querySelector("[data-art-banner]");
    const versionSel= panel.querySelector("[data-art-version]");
    if (titleEl) titleEl.textContent = "Library";
    if (fmtEl) { fmtEl.textContent = ""; fmtEl.dataset.format = ""; }
    if (subEl) { subEl.hidden = true; subEl.textContent = ""; }
    if (banner) { banner.hidden = true; banner.innerHTML = ""; }
    if (versionSel) { versionSel.hidden = true; versionSel.innerHTML = ""; }

    if (bodyEl) bodyEl.innerHTML = '<div class="artifact-loading">' + ICON.library + '<span>Loading library…</span></div>';
    try {
      const payload = await Api.artifacts.library(120);
      if (!payload) throw new Error("library load failed");
      _renderLibrary(bodyEl, (payload && payload.artifacts) || []);
    } catch (e) {
      console.warn("[Artifacts] library load failed:", e);
      if (bodyEl) bodyEl.innerHTML =
        '<div class="artifact-error">' + ICON.warn +
        '<div><strong>Failed to load library.</strong>' +
        '<div class="artifact-error-detail">' +
        (typeof escapeHtml === "function" ? escapeHtml(String(e)) : String(e)) +
        '</div></div></div>';
    }
  }

  function _renderLibrary(bodyEl, items) {
    if (!bodyEl) return;
    if (!items.length) {
      bodyEl.innerHTML =
        '<div class="artifact-empty">' +
          '<div class="artifact-empty-icon">' + ICON.library + '</div>' +
          '<div class="artifact-empty-title">No artifacts yet</div>' +
          '<div class="artifact-empty-text">Saved reports, generated HTML, and pinned outputs show up here.</div>' +
        '</div>';
      return;
    }
    const _esc = (typeof escapeHtml === "function") ? escapeHtml : function (s) { return String(s); };
    const rows = items.map(function (a) {
      const id = _esc(a.id || "");
      const title = _esc(a.title || ("artifact-" + (a.id || "").slice(0, 8)));
      const fmtLabel = _formatLabel(a.format);
      const fmtClass = "artifact-format-pill artifact-format-" + (a.format || "doc");
      const ts = a.created_at ? _relativeTime(a.created_at) : "";
      const sz = a.size_bytes ? _formatBytes(a.size_bytes) : "";
      const meta = [fmtLabel, sz, ts].filter(Boolean).map(_esc).join(" · ");
      const pinClass = a.pinned ? "is-pinned" : "";
      const pinIcon = a.pinned ? ICON.pinOn : ICON.pinOff;
      return (
        '<button class="artifact-lib-row" data-art-lib-id="' + id + '" data-art-search="' + (title + " " + meta).toLowerCase().replace(/"/g, "&quot;") + '">' +
          '<span class="artifact-lib-icon">' + _formatIcon(a.format) + '</span>' +
          '<span class="artifact-lib-main">' +
            '<span class="artifact-lib-title">' + title + '</span>' +
            '<span class="artifact-lib-meta">' +
              '<span class="' + fmtClass + '">' + fmtLabel + '</span>' +
              (sz ? '<span class="artifact-lib-dim">' + _esc(sz) + '</span>' : '') +
              (ts ? '<span class="artifact-lib-dim">' + _esc(ts) + '</span>' : '') +
            '</span>' +
          '</span>' +
          '<span class="artifact-lib-pin ' + pinClass + '" data-art-pin-id="' + id + '" role="button" tabindex="-1" title="' + (a.pinned ? "Unpin" : "Pin") + '" aria-pressed="' + (a.pinned ? "true" : "false") + '">' + pinIcon + '</span>' +
        '</button>'
      );
    }).join("");
    bodyEl.innerHTML =
      '<div class="artifact-lib">' +
        '<div class="artifact-lib-search">' +
          '<span class="artifact-lib-search-icon">' + ICON.search + '</span>' +
          '<input type="search" placeholder="Filter artifacts…" data-art-lib-search aria-label="Filter artifacts">' +
        '</div>' +
        '<div class="artifact-lib-list">' + rows + '</div>' +
      '</div>';
  }

  function _filterLibrary(query) {
    const panel = document.getElementById("artifact-panel");
    if (!panel) return;
    const q = (query || "").trim().toLowerCase();
    const rows = panel.querySelectorAll(".artifact-lib-row");
    let visible = 0;
    rows.forEach(function (r) {
      const hay = r.getAttribute("data-art-search") || "";
      const match = !q || hay.indexOf(q) !== -1;
      r.style.display = match ? "" : "none";
      if (match) visible++;
    });
    /* When a filter matches nothing, every row is display:none — show an
     * explicit "no matches" line so the list doesn't look blank/broken
     * (sibling of the conv-list collapse bug: never leave a control with
     * zero visible items and no fallback). */
    const listEl = panel.querySelector(".artifact-lib-list");
    if (!listEl) return;
    let noResult = listEl.querySelector(".artifact-lib-noresult");
    if (visible === 0 && rows.length > 0) {
      if (!noResult) {
        noResult = document.createElement("div");
        noResult.className = "artifact-lib-noresult";
        listEl.appendChild(noResult);
      }
      const _esc = (typeof escapeHtml === "function") ? escapeHtml : function (s) { return String(s); };
      noResult.textContent = "No artifacts match \u201c" + _esc(q) + "\u201d";
      noResult.style.display = "";
    } else if (noResult) {
      noResult.style.display = "none";
    }
  }

  function _allowScripts() {
    if (!_activeId) return;
    _scriptsAllowed.add(_activeId);
    // Audit: best-effort, fire and forget.
    try {
      Api.clientError.report({
        message: "[debugLog][info] artifact_scripts_allowed id=" + _activeId,
        url: location.href,
        extra: { artifactId: _activeId },
      });
    } catch (e) {
      console.debug("[Artifacts] consent log build failed:", e);
    }
    // Re-render the body, this time with allow-scripts.
    open(_activeId);
  }

  // ── Formatting helpers ─────────────────────────────────────────────

  function _formatBytes(n) {
    if (!Number.isFinite(n) || n <= 0) return "";
    if (n < 1024) return n + " B";
    if (n < 1024 * 1024) return Math.round(n / 1024) + " KB";
    return (n / (1024 * 1024)).toFixed(1) + " MB";
  }

  function _relativeTime(epochMs) {
    if (!Number.isFinite(epochMs) || epochMs <= 0) return "";
    const diff = Date.now() - epochMs;
    if (diff < 0) return "just now";
    const sec = Math.round(diff / 1000);
    if (sec < 30)   return "just now";
    if (sec < 60)   return sec + "s ago";
    const min = Math.round(sec / 60);
    if (min < 60)   return min + "m ago";
    const hr  = Math.round(min / 60);
    if (hr  < 24)   return hr + "h ago";
    const day = Math.round(hr / 24);
    if (day < 7)    return day + "d ago";
    const wk  = Math.round(day / 7);
    if (wk  < 5)    return wk + "w ago";
    try {
      return new Date(epochMs).toLocaleDateString();
    } catch (_) {
      return "";
    }
  }

  // ── Hydration: join artifact metadata to stable Turn identity ──────

  function _artifactTurnId(conv, meta) {
    const state = runtimeScope.ConversationTurnRead?.state?.(conv);
    if (!state) return '';
    const directId = String(meta?.turn_id || meta?.msg_id || '');
    if (directId && state.turnsById?.[directId]) return directId;
    const taskId = String(meta?.task_id || '');
    if (taskId) {
      const attempt = Object.values(state.attemptsById || {}).find(
        (candidate) => candidate && candidate.taskId === taskId,
      );
      if (attempt?.turnId && state.turnsById[attempt.turnId]) return attempt.turnId;
    }
    return runtimeScope.ConversationTurnRead?.latest?.(
      conv,
      (turn) => turn.actor === 'assistant' || turn.actor === 'planner',
    )?.turnId || '';
  }

  function _artifactPresentation(meta) {
    const id = String(meta?.id || '');
    if (!id) return null;
    const sourcePath = typeof meta?.source_ref?.path === 'string'
      ? meta.source_ref.path : '';
    const sizeBytes = Number(meta?.size_bytes);
    const version = Number(meta?.version);
    const createdAt = Number(meta?.created_at);
    return Object.freeze({
      id,
      format: String(meta?.format || 'doc'),
      title: String(meta?.title || `artifact-${id.slice(0, 8)}`),
      ...(Number.isFinite(sizeBytes) && sizeBytes > 0 ? { sizeBytes } : {}),
      ...(sourcePath ? { sourcePath } : {}),
      ...(Number.isFinite(version) ? { version } : {}),
      ...(Number.isFinite(createdAt) ? { createdAt } : {}),
    });
  }

  /** Replace the active conversation's bounded artifact read model. */
  async function hydrateConversation(conv, hasArtifacts) {
    if (!conv || !conv.id) return;
    const epoch = (_hydrateEpoch.get(conv) || 0) + 1;
    _hydrateEpoch.set(conv, epoch);
    if (hasArtifacts === false) {
      runtimeScope.ConversationSurfacePresentation?.setArtifacts?.(
        conv.id, new Map(),
      );
      return;
    }
    try {
      const payload = await Api.artifacts.forConv(conv.id);
      if (_hydrateEpoch.get(conv) !== epoch) return;
      const rows = Array.isArray(payload?.artifacts) ? payload.artifacts : [];
      const byTurn = new Map();
      for (const meta of rows) {
        if (!meta?.id) continue;
        _metaCache.set(meta.id, meta);
        const turnId = _artifactTurnId(conv, meta);
        const artifact = _artifactPresentation(meta);
        if (!turnId || !artifact) continue;
        const existing = byTurn.get(turnId) || [];
        const duplicateIndex = existing.findIndex((item) => item.id === artifact.id);
        if (duplicateIndex >= 0) existing[duplicateIndex] = artifact;
        else existing.push(artifact);
        byTurn.set(turnId, existing);
      }
      for (const [turnId, artifacts] of byTurn) {
        byTurn.set(turnId, Object.freeze([...artifacts]));
      }
      runtimeScope.ConversationSurfacePresentation?.setArtifacts?.(conv.id, byTurn);
    } catch (e) {
      console.debug("[Artifacts] hydrateConversation failed:", e);
    }
  }

  // ── Keyboard: Esc closes the panel; Ctrl/Cmd+P prints when panel open
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && _activeId) {
      closePanel();
      return;
    }
    // Print only when the panel is open AND focus isn't in a text input
    if ((e.ctrlKey || e.metaKey) && (e.key === "p" || e.key === "P")) {
      const panel = document.getElementById("artifact-panel");
      if (panel && !panel.classList.contains("artifact-panel-hidden")) {
        const tag = (e.target && e.target.tagName) || "";
        if (tag !== "INPUT" && tag !== "TEXTAREA") {
          e.preventDefault();
          _print();
        }
      }
    }
  });

  // ── Public façade ──────────────────────────────────────────────────
  runtimeScope.Artifacts = {
    open: open,
    closePanel: closePanel,
    showLibrary: _showLibrary,
    hydrateConversation: hydrateConversation,
    // Exposed for tests / debug
    _meta: _metaCache,
    _content: _contentCache,
    _filterLibrary: _filterLibrary,
  };
})();
