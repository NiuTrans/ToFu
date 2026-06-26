/* ═══════════════════════════════════════════════════════════════════
   chat render — extracted from ui.js (split 2026-05-28)

   renderChat() + renderMessage() + per-message fingerprint diffing.

   This file is concatenated by lib/js_bundler.py — symbols share
   the same window scope as every other static/js/*.js file. No
   exports / imports needed.
   ═══════════════════════════════════════════════════════════════════ */

/** Per-message fingerprint for surgical DOM diffing.
 *  Must change whenever the rendered HTML for this message would differ. */
function _msgFingerprint(msg) {
  const sr = msg.toolRounds || msg.searchResults;
  /* Count compacted rounds so a tool_compacted SSE landing on an
   * earlier message triggers a re-render of just that message.
   * Iterating toolRounds is cheap (avg 2-10 entries). */
  let compactedCount = 0, compactedToSum = 0;
  if (Array.isArray(msg.toolRounds)) {
    for (const r of msg.toolRounds) {
      if (r.compactionLayer) compactedCount++;
      compactedToSum += (r.compactedToChars | 0);
    }
  }
  /* Error envelope: include kind + message length so a re-classification
   * (e.g. quota → ratelimit) bumps the fingerprint and the row re-renders. */
  let _errFp = '';
  if (msg.error) {
    if (typeof msg.error === 'object') {
      _errFp = (msg.error.kind || '') + ':' + ((msg.error.message || '').length);
    } else {
      _errFp = String(String(msg.error).length);
    }
  }
  return (msg.role || "") + ":" +
    (msg.content || "").length + ":" +
    (msg.thinking || "").length + ":" +
    _errFp + ":" +
    (msg.finishReason || "") + ":" +
    (msg.translatedContent || "").length + ":" +
    (msg._showingTranslation ? "T" : "F") + ":" +
    (msg._translateDone === false ? "P" : "") + ":" +
    (sr ? sr.length : 0) + ":" +
    (msg._igResult ? "IG" : "") + ":" +
    (msg._igResults ? msg._igResults.length : 0) + ":" +
    (msg._igError ? "IGE" : "") + ":" +
    (msg.modifiedFiles || 0) + ":" +
    (msg.images ? msg.images.length : 0) + ":" +
    (msg.pdfTexts ? msg.pdfTexts.length : 0) + ":" +
    "c" + compactedCount + "ts" + compactedToSum;
}

/* ── Tool-round freshness gradient ──
 *
 * The server compaction cliff (lib/tasks_pkg/compaction.py:MICRO_HOT_TAIL=40)
 * is correct for performance (most runs <40 tool calls) but invisible to
 * the user.  A binary hot/cold cutoff also reads as "broken UI" in the
 * common case where everything is hot.
 *
 * Better signal: a continuous fade of every tool row by its distance
 * from the newest round.  Newest = 100 % opacity, gradually receding
 * toward an older floor.  The user sees AT A GLANCE that recent rounds
 * are bright and older ones recede — the same intuition the model has,
 * surfaced visually.  No magic numbers exposed in the UI; the cliff
 * still exists server-side, but the user sees a smooth gradient.
 *
 * Position-based (NOT length-normalized) so the same round in the same
 * place always renders the same way regardless of how long the conv
 * grows: position 0 from end = 1.00, pos 30 = 0.70, pos 80+ = 0.55 floor.
 *
 * Compacted rows (`compactionLayer` set) skip the fade — they have
 * their own purple/pink stripe and shouldn't compete with the gradient. */
/* Age-based fading was REMOVED per UX directive: transparency is too
 * quiet, and the thing the user actually needs to see — compaction
 * state — is now expressed with a SOLID, opaque label per row (see
 * compactionLabel logic in _renderUnifiedToolLine).
 *
 * Kept callable from existing render entry points so callers don't NPE. */
function _stampFreshness(_conv) { /* no-op */ }

function renderChat(conv, forceScroll) {
  /* ── Guard 1: skip if user is editing a message in this conversation ── */
  if (_editingMsgIdx !== null && conv.id === activeConvId) return;
  /* Stamp freshness on every render so the panel chrome always reflects
   * the current cross-message position of each round.  Cheap pass —
   * one assignment per round, no DOM work. */
  _stampFreshness(conv);
  /* Prefetch all per-message costs in ONE batch round-trip so the
   * synchronous calcCostCny() calls inside renderFinishInfo() hit the
   * cache.  Fire-and-forget; if fresh entries landed, force a re-render
   * (forceScroll=true bypasses the fingerprint guard).  When everything
   * was already cached, the prefetch returns false and we don't paint. */
  if (typeof _prefetchConvCosts === 'function') {
    _prefetchConvCosts(conv).then((didFetch) => {
      if (didFetch && conv.id === activeConvId && typeof renderChat === 'function') {
        // Bypass the fingerprint guard — cache changed even though
        // _convRenderFingerprint didn't.
        renderChat(conv, true);
      }
    });
  }
  /* Same idea for file-changes-bar derivation. Without this, every
   * message lacking a server-derived modifiedFileList fired its own
   * single POST /api/v1/messages/extract-file-changes from inside
   * renderFileChangesBar. With ~10 such messages this was the second-
   * biggest source of post-migration render lag (after cost). */
  if (typeof _prefetchConvFileChanges === 'function') {
    _prefetchConvFileChanges(conv).then((didFetch) => {
      if (didFetch && conv.id === activeConvId && typeof renderChat === 'function') {
        renderChat(conv, true);
      }
    });
  }

  /* ── Guard 1b: skip background re-renders while branch panel is open ── */
  if (forceScroll === false && _activeBranch && conv.id === activeConvId) return;

  /* ── Guard 1c: protect active streaming bubble from destruction ──
   * A full renderChat() destroys the #streaming-msg element and replaces all
   * messages with static renderMessage() output.  The streaming assistant message
   * (which has msg.model set from the state/preset SSE event) gets a finish-bar
   * with only the model tag — appearing as if the message is done while the
   * sidebar still pulses and the stop button is active.
   * Fix: delegate to showStreamingUIForConv() which properly renders prev messages
   * statically and creates a fresh streaming bubble for the in-progress message. */
  if (conv.id === activeConvId && activeStreams.has(conv.id) && document.getElementById('streaming-msg')) {
    if (typeof showStreamingUIForConv === 'function') showStreamingUIForConv(conv.id);
    return;
  }

  /* ── Guard 2: fingerprint-based skip for background syncs (forceScroll===false) ── */
  const fp = _convRenderFingerprint(conv);
  if (
    forceScroll === false &&
    conv.id === activeConvId &&
    fp === _lastRenderedFingerprint
  ) {
    /* Data hasn't actually changed — skip the destructive re-render entirely */
    return;
  }

  const inner = document.getElementById("chatInner");
  const container = document.getElementById("chatContainer");

  /* ═══ Surgical update path (forceScroll === false) ═══
   * Instead of wiping inner.innerHTML (which destroys all DOM nodes,
   * resets content-visibility:auto size caches, and causes scroll flicker),
   * do per-message diffing: only touch messages that actually changed.
   * This preserves scroll position perfectly with ZERO visual flicker.
   *
   * Only use surgical mode when the DOM already has rendered messages
   * (i.e. not showing a welcome screen or loading skeleton). */
  const _hasMsgDom = inner && inner.querySelector('[id^="msg-"]');
  /* ★ FIX: During initial conversation load (_initialSwitchLoad), Phase 2 server
   * response triggers renderChat(conv, false).  The surgical path would do
   * outerHTML replacement which destroys content-visibility:auto size caches,
   * causing scrollHeight to collapse → visible scroll-jump-to-top before the
   * .then() callback scrolls back down.  Skip surgical mode for initial loads
   * so the full-render path runs with _forceScrollToBottom — no flash. */
  if (forceScroll === false && conv.id === activeConvId && conv.messages.length > 0 && _hasMsgDom && !conv._initialSwitchLoad) {
    const total = conv.messages.length;
    /* ★ FIX: Respect _lazyRenderedFrom so force-loaded messages (from scrollToTurn
     * or manual scroll-up) survive surgical updates.  Previously this always used
     * total - _INITIAL_RENDER, which removed force-loaded messages and left
     * _lazyRenderedFrom stale — making turn-nav dots unclickable a second time. */
    const defaultStart = Math.max(0, total - _INITIAL_RENDER);
    const startIdx = (_lazyConvId === conv.id && _lazyRenderedFrom < defaultStart)
      ? _lazyRenderedFrom
      : defaultStart;
    let anyChange = false;

    /* 1) Update or add messages
     * ★ Perf: collect all outerHTML replacements first, then apply in one pass.
     * Each outerHTML assignment invalidates layout; batching avoids interleaving
     * layout reads (getElementById) with writes (outerHTML) — prevents forced reflows. */
    const _pendingUpdates = [];
    /* ★ Skip the streaming message — it's rendered as #streaming-msg,
     *   not as msg-N.  Without this skip, the else branch below would
     *   append a static renderMessage() (with finish-bar) for the streaming
     *   message, then step 3 would remove the live streaming bubble. */
    const _streamingActive = activeStreams.has(conv.id) && document.getElementById('streaming-msg');
    const _skipIdx = _streamingActive ? (total - 1) : -1;
    for (let i = startIdx; i < total; i++) {
      if (i === _skipIdx) continue;  // streaming message — leave #streaming-msg alone
      const msg = conv.messages[i];
      const el = document.getElementById("msg-" + i);
      if (el) {
        /* Element exists — check if content changed */
        const oldFp = el.getAttribute("data-mfp") || "";
        const newFp = _msgFingerprint(msg);
        if (oldFp !== newFp) {
          _pendingUpdates.push({ el, html: renderMessage(msg, i) });
          anyChange = true;
        }
      } else {
        /* New message — append */
        const wrapper = document.createElement("div");
        wrapper.innerHTML = renderMessage(msg, i);
        const newEl = wrapper.firstElementChild;
        if (newEl) inner.appendChild(newEl);
        anyChange = true;
      }
    }
    /* Apply all outerHTML replacements in a single write batch */
    for (const upd of _pendingUpdates) {
      upd.el.outerHTML = upd.html;
    }

    /* 2) Remove stale messages beyond the current count
     * ★ FIX: Use startIdx (which respects _lazyRenderedFrom) instead of
     * recalculating total - _INITIAL_RENDER — keeps force-loaded messages alive. */
    const staleEls = inner.querySelectorAll('[id^="msg-"]');
    for (const el of staleEls) {
      const m = el.id.match(/^msg-(\d+)$/);
      if (m) {
        const idx = parseInt(m[1], 10);
        if (idx >= total || idx < startIdx) {
          el.remove();
          anyChange = true;
        }
      }
    }

    /* 3) Remove any leftover streaming bubble (task finished)
     * ★ Only remove if the stream has actually finished — don't destroy a live
     *   streaming bubble.  Guard 1c should have caught this, but belt-and-suspenders. */
    const leftoverStreaming = document.getElementById("streaming-msg");
    if (leftoverStreaming && !activeStreams.has(conv.id)) {
      leftoverStreaming.remove();
      anyChange = true;
    }

    if (anyChange) {
      buildTurnNav(conv);
    }
    _lastRenderedFingerprint = fp;
    _lazyConvId = conv.id;
    return;
  }

  /* ═══ Full re-render path (forceScroll !== false) ═══ */
  _destroyLazyObserver();
  _lazyConvId = conv.id;

  if (conv.messages.length === 0) {
    if (conv._needsLoad) {
      /* ── Loading skeleton: conv has server messages but they haven't arrived yet ── */
      inner.innerHTML = String(safeHtml`<div class="welcome" id="welcome" style="opacity:0.5"><div class="welcome-icon" style="animation:pulse 1.5s infinite"></div><h2>Loading conversation…</h2><p>Fetching ${conv._serverMsgCount || ''} messages from server</p></div>`);
    } else {
      /* BASE_PATH is a trusted app constant (raw); the i18n subtitle is
       * escaped by default. */
      inner.innerHTML = String(safeHtml`<div class="welcome" id="welcome"><div class="welcome-icon"><img src="${raw(BASE_PATH)}/static/icons/tofu-welcome.svg" alt="Tofu" width="64" height="64"></div><h2 class="tofu-brand"><span class="tofu-brand-t">T</span><span class="tofu-brand-o1">o</span><span class="tofu-brand-f">f</span><span class="tofu-brand-u">u</span><small>豆腐</small></h2><p>${t('welcome.subtitle')}</p><div class="feature-pills"><span class="feature-pill">Extended Thinking</span><span class="feature-pill">Search</span><span class="feature-pill">URL Fetch</span><span class="feature-pill">Image Input</span><span class="feature-pill">Co-Pilot</span><span class="feature-pill">Browser</span></div></div>`);
    }
    _lastRenderedFingerprint = fp;
    buildTurnNav(conv);
    return;
  }

  const total = conv.messages.length;
  const startIdx = Math.max(0, total - _INITIAL_RENDER);
  _lazyRenderedFrom = startIdx;

  let html = "";

  /* Lazy-load sentinel for older messages */
  if (startIdx > 0) {
    _ensureLazyObserver();
    html += `<div id="_lazyLoadSentinel" class="lazy-sentinel"><span class="lazy-sentinel-text">⬆ <span class="_lazy-count">${startIdx}</span> older messages</span></div>`;
  }

  /* Render only the tail portion */
  for (let i = startIdx; i < total; i++) {
    html += renderMessage(conv.messages[i], i);
  }

  inner.innerHTML = html;
  _lastRenderedFingerprint = fp;

  /* Observe the sentinel to trigger loading when scrolled up */
  if (startIdx > 0) {
    const sentinel = document.getElementById("_lazyLoadSentinel");
    if (sentinel) _lazyObserver.observe(sentinel);
  }

  /* ★ PERF: Defer buildTurnNav to after paint — it scans ALL messages and
   * JSON.parse-s every tool round's args, which can take 50-200ms for large
   * conversations.  The turn nav is not critical for the initial render. */
  requestAnimationFrame(() => buildTurnNav(conv));

  /* Always scroll to the very bottom of the conversation.
   * hideUntilSettled=true: content-visibility:auto heights are estimated on
   * first paint, so hide until the 150ms timer has corrected scrollTop. */
  _forceScrollToBottom(container, true);
}

/* ★ Format precise date + time for finished messages */
function _fmtAbsoluteDateTime(ts) {
  const d = typeof ts === 'number' ? new Date(ts) : new Date(ts);
  if (isNaN(d.getTime())) return '';
  return d.toLocaleString([], {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  });
}
function renderMessage(msg, idx) {
  const isUser = msg.role === "user" || msg.role === "optimizer";  // optimizer = endpoint review, render as user
  /* ★ Precise date + time for all messages */
  const messageTime = msg.timestamp ? _fmtAbsoluteDateTime(msg.timestamp) : "";
  let body = "";
  if (msg.images?.length > 0) {
    const srcMap = { clip_render: "CLIP", vector_clip: "VEC", page_render: "SCAN", embedded: "RAW", pixmap_fallback: "PIX", pymupdf4llm: "FIG", figure_page_render: "FIG" };
    body += '<div class="msg-image-grid">';
    body += msg.images.map((img) => {
      const src = img.preview || "";
      const isPdf = !!img.pdfPage;
      const srcLabel = srcMap[img.pdfImageSource] || (isPdf ? "PDF" : "");
      const label = isPdf
        ? `P${img.pdfPage}/${img.pdfTotal} · ${img.sizeKB}KB`
        : `${img.sizeKB || "?"}KB`;
      const tip = img.caption
        ? escapeHtml(String(img.caption))
        : isPdf ? `PDF page ${img.pdfPage}` : "";
      if (src && !src.endsWith("..."))
        return `<div class="msg-img-thumb${isPdf ? " pdf-page" : ""}" ${tip ? `title="${tip}"` : ""} onclick="openImagePreview('${src.replace(/'/g, "\\'")}')"><img src="${src}" alt="uploaded">${srcLabel ? `<div class="msg-img-badge">${srcLabel}</div>` : ""}<div class="msg-img-size">${label}</div></div>`;
      return `<div class="msg-img-thumb placeholder"><span class="msg-img-placeholder-icon"></span><div class="msg-img-size">${img.sizeKB || "?"}KB</div></div>`;
    }).join("");
    body += '</div>';
  }
  if (isUser && msg.pdfTexts?.length > 0) {
    body += '<div class="pdf-attachments-indicator">';
    msg.pdfTexts.forEach((pdf, pdfI) => {
      const sizeStr =
        pdf.textLength >= 1024
          ? `${(pdf.textLength / 1024).toFixed(1)}KB`
          : `${pdf.textLength} chars`;
      const scanBadge = pdf.isScanned ? " · scanned" : "";
      const imgCount = (msg.images || []).filter(
        (img) => img.pdfName === pdf.name,
      ).length;
      const imgStr =
        imgCount > 0 ? ` · ${imgCount} img${imgCount > 1 ? "s" : ""}` : "";
      const methodBadge = pdf.method === "vlm" ? ' · <b>VLM</b>' : '';
      const _ext = pdf.name ? pdf.name.slice(pdf.name.lastIndexOf('.')).toLowerCase() : '';
      const _dsvg = (inner) => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${inner}</svg>`;
      const _DOC_FILE = '<path d="M6 22a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h8a2.4 2.4 0 0 1 1.704.706l3.588 3.588A2.4 2.4 0 0 1 20 8v12a2 2 0 0 1-2 2z"/><path d="M14 2v5a1 1 0 0 0 1 1h5"/><path d="M10 9H8"/><path d="M16 13H8"/><path d="M16 17H8"/>';
      const _DOC_SHEET = '<path d="M6 22a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h8a2.4 2.4 0 0 1 1.704.706l3.588 3.588A2.4 2.4 0 0 1 20 8v12a2 2 0 0 1-2 2z"/><path d="M14 2v5a1 1 0 0 0 1 1h5"/><path d="M8 13h2"/><path d="M14 13h2"/><path d="M8 17h2"/><path d="M14 17h2"/>';
      const _DOC_CODE = '<path d="M6 22a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h8a2.4 2.4 0 0 1 1.704.706l3.588 3.588A2.4 2.4 0 0 1 20 8v12a2 2 0 0 1-2 2z"/><path d="M14 2v5a1 1 0 0 0 1 1h5"/><path d="M10 12.5 8 15l2 2.5"/><path d="m14 12.5 2 2.5-2 2.5"/>';
      const _DOC_SLIDE = '<path d="M2 3h20"/><path d="M21 3v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V3"/><path d="m7 21 5-5 5 5"/>';
      const _docIconMap = {'.pdf':_DOC_FILE, '.docx':_DOC_FILE, '.pptx':_DOC_SLIDE, '.xlsx':_DOC_SHEET, '.txt':_DOC_FILE, '.md':_DOC_FILE,
                           '.csv':_DOC_SHEET, '.json':_DOC_CODE, '.xml':_DOC_CODE, '.py':_DOC_CODE, '.js':_DOC_CODE,
                           '.html':_DOC_CODE, '.yaml':_DOC_CODE, '.yml':_DOC_CODE};
      const docIcon = _dsvg(_docIconMap[_ext] || _DOC_FILE);
      body += `<div class="pdf-attach-badge" title="${escapeHtml(pdf.name)}" onclick="previewMsgPdfText(${idx},${pdfI})" style="cursor:pointer"><span class="pdf-attach-icon">${docIcon}</span><span class="pdf-attach-info"><span class="pdf-attach-name">${escapeHtml(pdf.name.length > 25 ? pdf.name.slice(0, 23) + "…" : pdf.name)}</span><span class="pdf-attach-meta">${pdf.pages} pages · ${sizeStr}${imgStr}${scanBadge}${methodBadge}</span></span></div>`;
    });
    body += "</div>";
  }
  // ── Reply quotes (user messages) — file badge style, supports array ──
  if (isUser) {
    const quotes = msg.replyQuotes || (msg.replyQuote ? [msg.replyQuote] : []);
    for (const rq of quotes) {
      const rqPreview = rq.replace(/\s+/g, " ").slice(0, 80);
      const rqChars = rq.length;
      const rqLines = rq.split("\n").length;
      body += `<div class="reply-quote-badge" title="${escapeHtml(rq.slice(0, 300))}">
        
        <span class="reply-quote-badge-info">
          <span class="reply-quote-badge-name">${escapeHtml(rqPreview)}${rqChars > 80 ? "…" : ""}</span>
          <span class="reply-quote-badge-meta">${rqChars} chars · ${rqLines} line${rqLines > 1 ? "s" : ""}</span>
        </span></div>`;
    }
    // ── Conversation reference badges ──
    if (msg.convRefs && msg.convRefs.length > 0) {
      for (const cr of msg.convRefs) {
        const crTitle = escapeHtml(cr.title || cr.id);
        body += `<div class="reply-quote-badge conv-ref-badge" title="引用对话: ${crTitle}">
          <span class="reply-quote-badge-icon">@</span>
          <span class="reply-quote-badge-info">
            <span class="reply-quote-badge-name">${crTitle}</span>
            <span class="reply-quote-badge-meta">引用对话</span>
          </span></div>`;
      }
    }
  }
  // ── Proactive agent banner ──
  if (msg._proactive) {
    const taskName = msg._proactiveTaskId ? `Task ${(msg._proactiveTaskId || "").slice(0, 8)}` : "Proactive Agent";
    body += `<div class="proactive-banner"><span class="pb-text"><span class="pb-name">${escapeHtml(taskName)}</span> — scheduled execution</span></div>`;
  }
  // ── Swarm auto-continue banner ──
  // This assistant turn was started by the backend (not the user) to deliver
  // sub-agent results that finished after the spawning turn ended. See
  // lib/swarm/integration.py::_start_autocontinue_turn.
  if (msg._swarmAutoContinue) {
    const _acLabel = (typeof t === "function" && t("swarm.autoContinue") !== "swarm.autoContinue")
      ? t("swarm.autoContinue")
      : "Continued automatically after sub-agents finished";
    body += `<div class="proactive-banner"><span class="pb-text">↻ <span class="pb-name">${escapeHtml(_acLabel)}</span></span></div>`;
  }
  // ── MCP login-hint + Memory Prefetch indicator (finished message) ──
  if (!isUser && msg._mcpLoginHint) {
    body += renderMcpLoginHintHtml(msg._mcpLoginHint);
  }
  if (!isUser && msg._memoryPrefetch) {
    body += renderMemoryPrefetchHtml(msg._memoryPrefetch);
  }
  if (!isUser && msg._preferencesApplied) {
    body += renderPreferencesAppliedHtml(msg._preferencesApplied);
  }
  if (!isUser && msg._preferencesLearned) {
    body += renderPreferenceLearnedHtml(msg._preferencesLearned);
  }
  /* ★ Autopilot VU bubble, still streaming and not yet showing content —
   * render a live "composing…" pulse so the eagerly-created bubble isn't
   * blank while the simulated user warms up / investigates. */
  if (msg._isVirtualUser && msg._streamingVu && !msg.content
      && !(msg.toolRounds && msg.toolRounds.length) && !msg.thinking) {
    body += `<div class="stream-status vu-composing"><div class="pulse"></div> `
          + `${escapeHtml(typeof t === "function" ? t("autopilot.composing") : "Autopilot is composing the next reply…")}</div>`;
  }
  const rounds = getToolRoundsFromMsg(msg);
  /* ★ Autopilot VU provenance: the VU's tool investigation + private
   * reasoning are DISPLAY-ONLY — only the reply text reaches the agent
   * (see conv_message_builder._build_user_message, which reads ONLY
   * `content` for a user row). Collect both here and emit them inside one
   * default-collapsed "Private · not sent" container below, so the human
   * can tell at a glance which part is excluded from the agent's context
   * vs. the reply that is actually sent. */
  const _vuPrivate = !!msg._isVirtualUser;
  let _vuPrivateHtml = '';
  if (rounds.length > 0) {
    let _roundsHtml = '';
    /* ★ Autopilot virtual-user messages carry the VU sub-task's tool
     * investigation as `toolRounds`. Surface them under a labelled
     * header so the user can tell "Autopilot probed these things
     * before replying" from a normal assistant tool panel. */
    if (msg._isVirtualUser) {
      _roundsHtml += `<div class="vu-investigation-header" title="Tools the autopilot used to investigate before composing this reply">`
            + `<span class="vu-investigation-icon"></span>`
            + `<span class="vu-investigation-label">Autopilot investigation · ${rounds.length} tool ${rounds.length === 1 ? 'call' : 'calls'}</span>`
            + `</div>`;
    }
    /* Render any persisted async-swarm inbox chips above the tool panel
       so historical messages still tell the user "this turn received
       async sub-agent updates before the model's reply".               */
    if (msg._inboxInjects && msg._inboxInjects.length) {
      _roundsHtml += _buildSwarmInboxChipsHTML(msg._inboxInjects);
    }
    _roundsHtml += renderToolRoundsHTML(rounds, false);
    if (_vuPrivate) _vuPrivateHtml += _roundsHtml; else body += _roundsHtml;
  }
  if (msg.thinking) {
    const thinkLen = msg.thinking.length;
    const thinkMeta = thinkLen >= 1024 ? ` (${Math.round(thinkLen / 1024)}k chars)` : ` (${thinkLen} chars)`;
    const _thinkHtml = `<div class="thinking-block" onclick="_toggleThinking(this,${idx})"><div class="thinking-header"><span class="thinking-label">Thinking Process${thinkMeta}</span><span class="thinking-toggle">▼</span></div><div class="thinking-content"><div class="thinking-text"></div></div></div>`;
    if (_vuPrivate) _vuPrivateHtml += _thinkHtml; else body += _thinkHtml;
  }
  /* ★ Flush the VU private zone — a single default-collapsed <details>
   * container that makes the "not sent to the agent" boundary unmistakable
   * (the reply below stands out). The inner thinking-block's lazy-load via
   * _toggleThinking still works: the element exists in the DOM even while
   * the <details> is closed. */
  if (_vuPrivate && _vuPrivateHtml) {
    const _privLabel = (typeof t === "function" && t("autopilot.privateNotSent") !== "autopilot.privateNotSent")
      ? t("autopilot.privateNotSent")
      : "Private · not sent to the agent";
    body += `<details class="vu-private-zone">`
          + `<summary class="vu-private-summary" title="${escapeHtml(_privLabel)}">`
          + `<svg class="vu-private-icon" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9.88 9.88a3 3 0 1 0 4.24 4.24"/><path d="M10.73 5.08A10.43 10.43 0 0 1 12 5c7 0 10 7 10 7a13.16 13.16 0 0 1-1.67 2.68"/><path d="M6.61 6.61A13.526 13.526 0 0 0 2 12s3 7 10 7a9.74 9.74 0 0 0 5.39-1.61"/><line x1="2" x2="22" y1="2" y2="22"/></svg>`
          + `<span class="vu-private-label">${escapeHtml(_privLabel)}</span>`
          + `<span class="vu-private-toggle">▼</span>`
          + `</summary>`
          + `<div class="vu-private-body">${_vuPrivateHtml}</div>`
          + `</details>`;
  }
  // ── Prior thinking (display-only) ──
  // Trailing reasoning that was emitted after the last completed tool batch
  // and discarded on Continue rollback.  Cannot be replayed on the wire
  // (Anthropic rejects orphan thinking blocks; OpenAI-compat strips it),
  // so the field is stripped by lib.llm_sanitize._strip_non_api_fields
  // before any LLM call.  We surface it here so the user can still see what
  // the model was reasoning about right before the resume.
  if (!isUser && msg.priorThinking) {
    const priorLen = msg.priorThinking.length;
    const priorMeta = priorLen >= 1024 ? ` (${Math.round(priorLen / 1024)}k chars)` : ` (${priorLen} chars)`;
    body += `<div class="thinking-block thinking-prior" onclick="_togglePriorThinking(this,${idx})"><div class="thinking-header"><span class="thinking-label">Earlier Thinking${priorMeta}</span><span class="thinking-toggle">▼</span></div><div class="thinking-content"><div class="thinking-text"></div></div></div>`;
  }
  // ── Prior response (display-only) ──
  // The free-form prose the model wrote after the last completed tool batch,
  // discarded on Continue rollback (the LLM regenerates from the tool-result
  // checkpoint, so this tail can't be replayed on the wire — see priorThinking
  // above for the identical wire-safety contract).  Surfacing it keeps the
  // rollback visible instead of silently dropping the content area while the
  // tool panel stays put.
  if (!isUser && msg.priorContent) {
    const priorCLen = msg.priorContent.length;
    const priorCMeta = priorCLen >= 1024 ? ` (${Math.round(priorCLen / 1024)}k chars)` : ` (${priorCLen} chars)`;
    body += `<div class="thinking-block thinking-prior content-prior" onclick="_togglePriorContent(this,${idx})"><div class="thinking-header"><span class="thinking-label">Earlier Response${priorCMeta} · rolled back</span><span class="thinking-toggle">▼</span></div><div class="thinking-content"><div class="thinking-text"></div></div></div>`;
  }
  // Track which branches have been inlined (rendered right after their anchor text)
  let _inlinedBranches = new Set();
  // ── Image Generation error card (from _igError metadata) ──
  // Renders a styled, color-coded error card based on error type.
  if (!isUser && msg._igError) {
    const ige = msg._igError;
    // Determine error-type CSS class and icon
    let errTypeClass = 'ig-error-generic';
    let errIcon = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"/><path d="M12 9v4"/><path d="M12 17h.01"/></svg>';
    if (ige.isRateLimit || ige.errorType === 'rate_limited') {
      errTypeClass = 'ig-error-ratelimit';
      errIcon = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 22h14"/><path d="M5 2h14"/><path d="M17 22v-4.172a2 2 0 0 0-.586-1.414L12 12l-4.414 4.414A2 2 0 0 0 7 17.828V22"/><path d="M7 2v4.172a2 2 0 0 0 .586 1.414L12 12l4.414-4.414A2 2 0 0 0 17 6.172V2"/></svg>';
    } else if (ige.isContentBlocked || ige.errorType === 'content_blocked') {
      errTypeClass = 'ig-error-blocked';
      errIcon = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M4.929 4.929 19.07 19.071"/></svg>';
    } else if (ige.isTimeout || ige.errorType === 'timeout') {
      errTypeClass = 'ig-error-timeout';
      errIcon = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="10" x2="14" y1="2" y2="2"/><line x1="12" x2="15" y1="14" y2="11"/><circle cx="12" cy="14" r="8"/></svg>';
    } else if (ige.errorType === 'no_slot') {
      errIcon = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m19 5 3-3"/><path d="m2 22 3-3"/><path d="M6.3 20.3a2.4 2.4 0 0 0 3.4 0L12 18l-6-6-2.3 2.3a2.4 2.4 0 0 0 0 3.4Z"/><path d="M7.5 13.5 10 11"/><path d="M10.5 16.5 13 14"/><path d="m12 6 6 6 2.3-2.3a2.4 2.4 0 0 0 0-3.4l-2.6-2.6a2.4 2.4 0 0 0-3.4 0Z"/></svg>';
    }
    body += `<div class="ig-result-wrapper">
      <div class="ig-error-card ${errTypeClass}">
        <div class="ig-error-icon">${errIcon}</div>
        <div class="ig-error-title">${escapeHtml(ige.title || 'Image generation failed')}</div>
        <div class="ig-error-text">${escapeHtml(ige.text || '')}</div>
        ${ige.detail ? `<div class="ig-error-detail">${escapeHtml(ige.detail)}</div>` : ''}
        ${ige.blockReason ? `<div class="ig-error-detail">Block reason: ${escapeHtml(ige.blockReason)}</div>` : ''}
        <button class="ig-retry-btn" onclick="_igRetryLastPrompt()">Retry</button>
      </div>
    </div>`;
  // ── Image Generation result card (from _igResult metadata) ──
  // When an assistant message has _igResult, render a styled card instead of raw markdown
  // so the card survives renderChat re-renders (e.g. when user sends a second image prompt).
  } else if (!isUser && msg._igResult && msg._igResult.image_url) {
    const ig = msg._igResult;
    const imgSrc = ig.image_url.startsWith('/') ? (typeof apiUrl === 'function' ? apiUrl(ig.image_url) : ig.image_url) : ig.image_url;
    const promptText = ig.prompt || '';
    const promptShort = promptText.length > 80 ? promptText.slice(0, 80) + '…' : promptText;
    const sizeStr = ig.file_size ? (typeof _formatFileSize === 'function' ? _formatFileSize(ig.file_size) : Math.round(ig.file_size / 1024) + ' KB') : '';
    const elapsedStr = ig.elapsed ? ig.elapsed + 's' : '';
    const arStr = ig.aspect_ratio || '';
    body += `<div class="ig-result-wrapper">
      <div class="ig-result-card">
        <img src="${imgSrc}" alt="${escapeHtml(promptText.slice(0,100))}"
             onclick="_openImageFullscreen(this.src)" />
        <div class="ig-result-footer">
          <span class="ig-result-prompt" title="${escapeHtml(promptText)}">${escapeHtml(promptShort)}</span>
          <div class="ig-result-meta">
            ${ig.model ? `<span class="ig-meta-pill">${escapeHtml(ig.model)}</span>` : ''}
            ${ig.provider_id ? `<span class="ig-meta-pill">@${escapeHtml(ig.provider_id)}</span>` : ''}
            ${arStr ? `<span class="ig-meta-pill">${escapeHtml(arStr)}</span>` : ''}
            ${sizeStr ? `<span class="ig-meta-pill">${sizeStr}</span>` : ''}
            ${elapsedStr ? `<span class="ig-meta-pill">${elapsedStr}</span>` : ''}
            ${ig.history_turns ? `<span class="ig-meta-pill ig-history-pill" title="${ig.history_turns} prior editing turn${ig.history_turns > 1 ? 's' : ''}">${Icon('refresh', 11)} ${ig.history_turns}</span>` : ''}
          </div>
          <div class="ig-result-actions">
            <button onclick="event.stopPropagation();_downloadGenImage(this)" title="Download">⬇</button>
            <button onclick="event.stopPropagation();_openImageFullscreen(this.closest('.ig-result-card').querySelector('img').src)" title="Fullscreen">⛶</button>
          </div>
        </div>
      </div>
    </div>`;
    // Also render any text content from the model (e.g. revised prompt)
    const textContent = (msg.content || '').replace(/!\[Generated Image\]\([^)]*\)\s*/g, '').trim();
    if (textContent) {
      body += `<div class="md-content">${renderMarkdown(textContent)}</div>`;
    }
  // ── Batch Image Generation results grid (from _igResults array) ──
  } else if (!isUser && msg._igResults && msg._igResults.length > 0) {
    const results = msg._igResults;
    // Skip rendering "pending" placeholders during active batch (DOM has live slots)
    const isPending = msg._igBatchPending && results.every(r => r.error === 'pending');
    if (isPending) {
      body += `<div class="ig-batch-wrapper"><div class="ig-batch-banner">Generating…</div></div>`;
    } else {
      const okResults = results.filter(r => r.ok && r.image_url);
      const cols = Math.min(results.length, 2);
      const _fmtSize = typeof _formatFileSize === 'function' ? _formatFileSize : (b => b > 0 ? Math.round(b / 1024) + ' KB' : '');
      const _shortModel = typeof _IG_MODEL_SHORT !== 'undefined' ? _IG_MODEL_SHORT : {};
      const distinctModels = new Set(results.map(r => r.model)).size;
      const bannerLabel = distinctModels > 1 ? `全模型 ${results.length}连抽` : `${results.length}连抽`;
      body += `<div class="ig-batch-wrapper"><div class="ig-batch-banner">${bannerLabel} · ${okResults.length}/${results.length} 成功</div><div class="ig-batch-grid ig-cols-${cols}">`;
      for (let ri = 0; ri < results.length; ri++) {
        const r = results[ri];
        if (r.ok && r.image_url) {
          const imgSrc = r.image_url.startsWith('/') ? (typeof apiUrl === 'function' ? apiUrl(r.image_url) : r.image_url) : r.image_url;
          const sizeStr = r.file_size ? _fmtSize(r.file_size) : '';
          const modelLabel = _shortModel[r.model] || r.model || '';
          body += `<div class="ig-batch-slot" data-slot-idx="${ri}" data-msg-idx="${idx}">
            <div class="ig-result-card">
              <img src="${imgSrc}" alt="${escapeHtml((r.prompt || '').slice(0,60))}"
                   onclick="_openImageFullscreen(this.src)" />
              <div class="ig-result-footer">
                <span class="ig-result-prompt">${escapeHtml(modelLabel)}</span>
                <div class="ig-result-meta">
                  ${sizeStr ? `<span class="ig-meta-pill">${sizeStr}</span>` : ''}
                  ${r.elapsed ? `<span class="ig-meta-pill">${r.elapsed}s</span>` : ''}
                </div>
                <div class="ig-result-actions">
                  <button onclick="event.stopPropagation();_downloadGenImage(this)" title="Download">⬇</button>
                  <button onclick="event.stopPropagation();_openImageFullscreen(this.closest('.ig-result-card').querySelector('img').src)" title="Fullscreen">⛶</button>
                </div>
              </div>
            </div>
          </div>`;
        } else if (r.error === 'pending') {
          // Still pending — show mini spinner
          const modelLabel = _shortModel[r.model] || r.model || '';
          body += `<div class="ig-batch-slot" data-slot-idx="${ri}" data-msg-idx="${idx}">
            <div class="ig-generating ig-batch-loading">
              <div class="ig-gen-spinner"></div>
              <div class="ig-gen-title">${escapeHtml(modelLabel)}</div>
              <div class="ig-gen-subtitle">Pending…</div>
            </div>
          </div>`;
        } else {
          // Error slot — with error-type differentiation and retry button
          const errModel = _shortModel[r.model] || r.model || '?';
          let errTypeClass = 'ig-error-generic';
          let errIcon = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"/><path d="M12 9v4"/><path d="M12 17h.01"/></svg>';
          const et = r.errorType || '';
          if (et === 'rate_limited') { errTypeClass = 'ig-error-ratelimit'; errIcon = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 22h14"/><path d="M5 2h14"/><path d="M17 22v-4.172a2 2 0 0 0-.586-1.414L12 12l-4.414 4.414A2 2 0 0 0 7 17.828V22"/><path d="M7 2v4.172a2 2 0 0 0 .586 1.414L12 12l4.414-4.414A2 2 0 0 0 17 6.172V2"/></svg>'; }
          else if (et === 'content_blocked') { errTypeClass = 'ig-error-blocked'; errIcon = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M4.929 4.929 19.07 19.071"/></svg>'; }
          else if (et === 'timeout') { errTypeClass = 'ig-error-timeout'; errIcon = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="10" x2="14" y1="2" y2="2"/><line x1="12" x2="15" y1="14" y2="11"/><circle cx="12" cy="14" r="8"/></svg>'; }
          const promptEsc = JSON.stringify(r.prompt || '').replace(/"/g, '&quot;');
          const modelEsc = JSON.stringify(r.model || '').replace(/"/g, '&quot;');
          body += `<div class="ig-batch-slot" data-slot-idx="${ri}" data-msg-idx="${idx}"><div class="ig-batch-error ${errTypeClass}">
            <div class="ig-error-icon">${errIcon}</div>
            <div class="ig-error-title">${escapeHtml(errModel)}</div>
            <div class="ig-error-text">${escapeHtml((r.error || 'Failed').slice(0,200))}</div>
            <button class="ig-slot-retry-btn" onclick="_igRetryBatchSlot(${idx},${ri},${promptEsc},${modelEsc})" title="Retry this slot">↻ Retry</button>
          </div></div>`;
        }
      }
      body += `</div></div>`;
    }
  } else if (msg.content) {
    try {
      let mdHtml;
      // Show translated content only when translation is active (not toggled off).
      // ★ Endpoint-mode critic messages are role=user but produced by the
      //   Critic LLM — they DO receive server-side auto-translate.  Treat
      //   them like assistants for the purposes of translation display.
      // ★ Virtual-user (Autopilot) messages are role=user but produced by the
      //   VU LLM and auto-translated to the UI language for DISPLAY — treat
      //   them like critic/assistant for translation display (the VU composes
      //   in the assistant's language; the user sees it in their UI language).
      const _isCritic = isUser && (msg._isEndpointReview || msg._isVirtualUser);
      const showTrans = (!isUser || _isCritic)
                        && msg.translatedContent
                        && msg._showingTranslation !== false;
      if (showTrans) {
        mdHtml = renderMarkdown(stripNoTranslateTags(msg.translatedContent));
      } else if (_isCritic) {
        // Critic / VU messages are user-role but contain rich markdown
        mdHtml = renderMarkdown(msg.content);
      } else if (isUser) {
        mdHtml = escapeHtml(stripNoTranslateTags(msg.originalContent || msg.content));
      } else {
        mdHtml = renderMarkdown(msg.content);
      }
      // ── Inject anchored branch pills inline (assistant only) ──
      if (!isUser && msg.branches?.length) {
        const r = _injectAnchoredBranches(mdHtml, msg, idx);
        mdHtml = r.html;
        _inlinedBranches = r.inlinedSet;
      }
      /* ★ Autopilot VU provenance boundary: the VU's tool investigation
       * and private reasoning above/below are display-only — only THIS
       * reply text is fed to the assistant as its next user message. Mark
       * it so the human can tell at a glance which part becomes the
       * agent's context vs. which part was private process. */
      const _vuSent = msg._isVirtualUser && !msg._streamingVu && msg.content;
      if (_vuSent) {
        const _vuSentLabel = (typeof t === "function" && t("autopilot.sentToAgent") !== "autopilot.sentToAgent")
          ? t("autopilot.sentToAgent")
          : "Sent to the agent as the next message";
        body += `<div class="vu-sent-zone"><div class="vu-handoff-header" title="${escapeHtml(_vuSentLabel)}">`
              + `<svg class="vu-handoff-icon" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="5" y1="12" x2="19" y2="12"/><polyline points="12 5 19 12 12 19"/></svg>`
              + `<span class="vu-handoff-label">${escapeHtml(_vuSentLabel)}</span></div>`;
      }
      body += `<div class="md-content${isUser ? " user-content" : ""}">${mdHtml}</div>`;
      if (_vuSent) body += `</div>`;
    } catch (e) {
  // ── Compaction markers — render inline chips for each archived snapshot ──
  // Each marker becomes a clickable chip that opens the Compaction Viewer
  // (window.openCompactionViewer in static/js/compaction-viewer.js). We
  // intentionally render in render-time order; clicking is delegated through
  // a global handler so the chips survive innerHTML replacement.
  if (!isUser && Array.isArray(msg._compactions) && msg._compactions.length) {
    const _ccDom = msg._compactions.map((c) => {
      const trig = (c.trigger || 'force');
      const _zapSvg = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 14a1 1 0 0 1-.78-1.63l9.9-10.2a.5.5 0 0 1 .86.46l-1.92 6.02A1 1 0 0 0 13 10h7a1 1 0 0 1 .78 1.63l-9.9 10.2a.5.5 0 0 1-.86-.46l1.92-6.02A1 1 0 0 0 11 14z"/></svg>';
      const _wrenchSvg = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.106-3.105c.32-.322.863-.22.983.218a6 6 0 0 1-8.259 7.057l-7.91 7.91a1 1 0 0 1-2.999-3l7.91-7.91a6 6 0 0 1 7.057-8.259c.438.12.54.662.219.984z"/></svg>';
      const _archiveSvg = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect width="20" height="5" x="2" y="3" rx="1"/><path d="M4 8v11a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8"/><path d="M10 12h4"/></svg>';
      const trigLabel = trig === 'reactive'
        ? _zapSvg + ' 紧急压缩'
        : (trig === 'manual' ? _wrenchSvg + ' 手动压缩' : _archiveSvg + ' 自动压缩');
      const before = c.tokensBefore || 0;
      const after  = c.tokensAfter  || 0;
      const reductionPct = (c.reductionPct != null) ? c.reductionPct
                          : (before > 0 ? Math.round((1 - after / before) * 100) : 0);
      const sizeFrag = before > 0 && after > 0
        ? `${(before/1000).toFixed(0)}k → ${(after/1000).toFixed(0)}k tokens · -${reductionPct}%`
        : (before > 0 ? `${(before/1000).toFixed(0)}k tokens 已归档` : '已归档');
      const reasonFrag = c.reason ? `<span class="compaction-marker-reason">${escapeHtml(c.reason)}</span>` : '';
      const statusCls = (c.status === 'done') ? 'is-done' : 'is-progress';
      const archiveAttr = (c.archiveId == null) ? '' : `data-archive-id="${c.archiveId}"`;
      const convAttr = `data-conv-id="${escapeHtml(c.convId || '')}"`;
      return `<button type="button" class="compaction-marker ${statusCls}"
        ${archiveAttr} ${convAttr}
        onclick="if(window.openCompactionViewer){window.openCompactionViewer(this.dataset.convId, parseInt(this.dataset.archiveId,10))}else{console.warn('compaction-viewer not loaded')}"
        title="点击查看压缩前的完整上下文">
        <span class="compaction-marker-icon"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 8v13H3V8"/><path d="M1 3h22v5H1z"/><path d="M10 12h4"/></svg></span>
        <span class="compaction-marker-trigger">${trigLabel}</span>
        <span class="compaction-marker-stat">${escapeHtml(sizeFrag)}</span>
        ${reasonFrag}
        <span class="compaction-marker-cta">查看历史</span>
      </button>`;
    }).join('');
    body += `<div class="compaction-marker-row">${_ccDom}</div>`;
  }
      body += `<div class="md-content${isUser ? " user-content" : ""}">${escapeHtml(msg.content)}</div>`;
    }
  }
  // ── Bilingual display ──
  if (isUser && msg.originalContent && msg.originalContent !== msg.content) {
    const _tmUser = msg._translateModel ? `<span class="bilingual-model" title="${escapeHtml(msg._translateModel)}">${escapeHtml(msg._translateModel)}</span>` : '';
    // Strip any leaked <notranslate>/<nt> tags from the translation display.
    const _userTrans = stripNoTranslateTags(msg.content || '');
    body += `<div class="bilingual-block bilingual-translated"><div class="bilingual-header" onclick="if(event.target.closest('.bilingual-copy-btn'))return;this.parentElement.classList.toggle('expanded')"><span class="bilingual-label"><span class="bilingual-type">原文</span><span class="bilingual-sep">/</span><span class="bilingual-type active">译文</span>${_tmUser}</span><button class="bilingual-copy-btn" onclick="event.stopPropagation();copyBilingualOriginal(this,'user',${idx})" title="Copy translation"><svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg></button><span class="bilingual-toggle">▼</span></div><div class="bilingual-body"><div class="md-content user-content">${escapeHtml(_userTrans)}</div></div></div>`;
  }
  // ── Auto-translate failed notice (user messages) ──
  // The send-path auto-translate was attempted but failed / timed out, so the
  // ORIGINAL (untranslated) text was sent to the model. We surface a quiet,
  // non-blocking notice instead of silently dropping the translation — click
  // to retranslate just this message.
  if (isUser && !msg._isEndpointReview && !msg.originalContent && msg._translateFailed) {
    const _failKind = msg._translateFailed === 'timed_out' ? 'timed_out' : 'failed';
    const _failKey = `translate.sendFailed.${_failKind}`;
    const _failMsg = (typeof t === 'function' && t(_failKey) !== _failKey)
      ? t(_failKey)
      : (_failKind === 'timed_out'
          ? 'Auto-translate timed out — original text was sent'
          : 'Auto-translate failed — original text was sent');
    const _retryTip = (typeof t === 'function' && t('translate.sendFailed.retry') !== 'translate.sendFailed.retry')
      ? t('translate.sendFailed.retry') : 'Retry';
    // Retry = re-run the turn: regenerateFromUser re-translates the original
    // text AND regenerates the response (translateMessage rejects plain user
    // messages and wouldn't change what the model already received).
    body += `<div class="translate-failed-notice" onclick="event.stopPropagation();regenerateFromUser(${idx})" title="${escapeHtml(_failMsg)}">`
      + `<svg class="tfn-icon" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>`
      + `<span class="tfn-text">${escapeHtml(_failMsg)}</span>`
      + `<span class="tfn-retry">${escapeHtml(_retryTip)}</span>`
      + `</div>`;
  }
  if (!isUser && msg.translatedContent && msg._showingTranslation !== false) {
    const _tmAsst = msg._translateModel ? `<span class="bilingual-model" title="${escapeHtml(msg._translateModel)}">${escapeHtml(msg._translateModel)}</span>` : '';
    // Defense in depth — strip any leaked <notranslate>/<nt> tags.
    const _asstOrig = stripNoTranslateTags(msg.content || '');
    body += `<div class="bilingual-block bilingual-original"><div class="bilingual-header" onclick="if(event.target.closest('.bilingual-copy-btn'))return;this.parentElement.classList.toggle('expanded')"><span class="bilingual-label"><span class="bilingual-type active">原文</span><span class="bilingual-sep">/</span><span class="bilingual-type">译文</span>${_tmAsst}</span><button class="bilingual-copy-btn" onclick="event.stopPropagation();copyBilingualOriginal(this,'assistant',${idx})" title="Copy original text"><svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg></button><span class="bilingual-toggle">▼</span></div><div class="bilingual-body"><div class="md-content">${renderMarkdown(_asstOrig)}</div></div></div>`;
  }
  // ── Critic (endpoint review) / Autopilot VU bilingual block — symmetric with assistant ──
  if (isUser && (msg._isEndpointReview || msg._isVirtualUser) && msg.translatedContent && msg._showingTranslation !== false) {
    const _tmCritic = msg._translateModel ? `<span class="bilingual-model" title="${escapeHtml(msg._translateModel)}">${escapeHtml(msg._translateModel)}</span>` : '';
    const _critOrig = stripNoTranslateTags(msg.content || '');
    body += `<div class="bilingual-block bilingual-original"><div class="bilingual-header" onclick="if(event.target.closest('.bilingual-copy-btn'))return;this.parentElement.classList.toggle('expanded')"><span class="bilingual-label"><span class="bilingual-type active">原文</span><span class="bilingual-sep">/</span><span class="bilingual-type">译文</span>${_tmCritic}</span><button class="bilingual-copy-btn" onclick="event.stopPropagation();copyBilingualOriginal(this,'critic',${idx})" title="Copy original text"><svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg></button><span class="bilingual-toggle">▼</span></div><div class="bilingual-body"><div class="md-content">${renderMarkdown(_critOrig)}</div></div></div>`;
  }
  // ── Persistent "translating..." indicator (survives re-render / tab switch) ──
  // Fires for both assistant messages AND endpoint-critic (role=user,
  // _isEndpointReview) messages — both are routed through the auto-translate
  // pipeline.
  if ((!isUser || (isUser && (msg._isEndpointReview || msg._isVirtualUser)))
      && !msg.translatedContent && msg._translateDone === false) {
    const errText = msg._translateError;
    if (errText) {
      body += `<div class="translate-loading" id="translate-loading-${idx}" style="color:#f59e0b;cursor:pointer" onclick="translateMessage(${idx})">${t('translate.failed')}</div>`;
    } else {
      // ── Show a retry-status sub-line when the backend is retrying
      //    (e.g. 429 / rate-limit / empty-output). Without this the user
      //    sees only "Translating…" and has no idea there's a problem. ──
      let statusSub = '';
      const _benignKinds = (typeof _TRANSLATE_BENIGN_STATUS_KINDS !== 'undefined')
        ? _TRANSLATE_BENIGN_STATUS_KINDS : new Set(['started', 'in_progress']);
      if (msg._translateStatus && !_benignKinds.has(msg._translateStatusKind || '')) {
        const kind = msg._translateStatusKind || '';
        // Prefer a localized label keyed by kind, fall back to the raw server message.
        const i18nKey = kind ? `translate.retry.${kind}` : '';
        const localized = i18nKey && typeof t === 'function' ? t(i18nKey) : '';
        const display = (localized && localized !== i18nKey) ? localized : msg._translateStatus;
        statusSub = `<div class="translate-status-sub" title="${escapeHtml(msg._translateStatus)}">⚠ ${escapeHtml(display)}</div>`;
      }
      // ── Streaming preview: render the partial translation as markdown as it
      //    arrives, in a styled block that morphs smoothly into the final
      //    bilingual译文 once the stream completes. ──
      let previewSub = '';
      if (msg._translatePartial) {
        let _pv;
        try { _pv = renderMarkdown(stripNoTranslateTags(msg._translatePartial)); }
        catch (e) { _pv = escapeHtml(msg._translatePartial); }
        previewSub = `<div class="translate-preview"><div class="md-content">${_pv}</div><span class="translate-caret"></span></div>`;
      }
      const _hasPreview = previewSub ? ' has-preview' : '';
      body += `<div class="translate-loading${_hasPreview}" id="translate-loading-${idx}">`
        + `<div class="translate-loading-head"><span class="translate-spinner"></span>`
        + `<span class="translate-loading-label">${t('translate.translatingToCN')}</span></div>`
        + `${statusSub}${previewSub}</div>`;
    }
  }
  if (msg.error)
    body += renderErrorEnvelope(msg.error);
  if (!isUser) body += renderFileChangesBar(msg, idx);
  /* ── Renderable artifact chips (md/html/svg) ─────────────────────────
   * Hooked from artifacts.js.  Persisted as a first-class row in
   * chat_artifacts so the chip survives compaction.  See lib/artifacts/. */
  if (!isUser && typeof window.Artifacts !== "undefined"
      && Array.isArray(msg._artifacts) && msg._artifacts.length > 0) {
    try { body += window.Artifacts.renderChips(msg._artifacts); }
    catch (e) { console.debug("[Artifacts] renderChips failed:", e); }
  }
  if (!isUser) body += renderFinishInfo(msg);
  const idAttr = typeof idx === "number" ? ` id="msg-${idx}"` : "";
  /* Stable per-message handle for the unified chatInner controller.
   * Server backfills `_msgId` (UUID) on persist; client-only messages get
   * a `tmp_<...>` id from _newClientMsgId().  Either way, when present we
   * mirror it onto the DOM so future surgical updates can address by id
   * instead of mutable index. */
  const msgIdAttr = (msg && msg._msgId) ? ` data-msg-id="${escapeHtml(msg._msgId)}"` : "";
  let actionBtns = "";
  if (typeof idx === "number") {
    const copyH = `<button class="msg-action-btn copy-msg-btn" onclick="event.stopPropagation();copyMessage(${idx})" title="Copy"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg> Copy</button>`;
    const editH = isUser
      ? `<button class="msg-action-btn" onclick="event.stopPropagation();startEditMessage(${idx})" title="Edit"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg> Edit</button>`
      : "";
    const regenH = isUser
      ? `<button class="msg-action-btn msg-regen-btn" onclick="event.stopPropagation();regenerateFromUser(${idx})" title="Regenerate response from this message"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg> Regen</button>`
      : "";
    const conv_ = getActiveConv();
    const isLastAssistant =
      !isUser &&
      conv_ &&
      idx === conv_.messages.length - 1 &&
      !activeStreams.has(conv_.id);
    const continueH = isLastAssistant
      ? `<button class="msg-action-btn msg-continue-btn" onclick="event.stopPropagation();continueAssistant()" title="Continue generating from where it left off"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="5 3 19 12 5 21 5 3"/></svg> Continue</button>`
      : "";
    const isShowingTrans = msg._showingTranslation;
    // Show the Translate button on: (a) assistant messages, (b) endpoint
    // critic review messages (role=user + _isEndpointReview) — they
    // receive auto-translate too.
    const _translateBtnAllowed = !isUser || (isUser && (msg._isEndpointReview || msg._isVirtualUser));
    const translateH = _translateBtnAllowed
      ? `<button class="msg-action-btn msg-translate-btn${isShowingTrans ? " translated" : ""}" onclick="event.stopPropagation();translateMessage(${idx})" title="${isShowingTrans ? "Show Original" : "Translate"}"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12.87 15.07l-2.54-2.51.03-.03A17.52 17.52 0 0014.07 6H17V4h-7V2H8v2H1v2h11.17C11.5 7.92 10.44 9.75 9 11.35 8.07 10.32 7.3 9.19 6.69 8h-2c.73 1.63 1.73 3.17 2.98 4.56l-5.09 5.02L4 19l5-5 3.11 3.11.76-2.04zM18.5 10h-2L12 22h2l1.12-3h4.75L21 22h2l-4.5-12zm-2.62 7l1.62-4.33L19.12 17h-3.24z"/></svg> ${isShowingTrans ? "Original" : "Translate"}</button>`
      : "";
    const exportImgH = !isUser
      ? `<button class="msg-action-btn msg-export-img-btn" onclick="event.stopPropagation();ExportImages.exportMessageWithPreview(${idx})" title="Export as phone-screen images"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><path d="M21 15l-5-5L5 21"/></svg> Export</button>`
      : "";
    const canDelete = conv_ && !activeStreams.has(conv_.id) && !conv_.activeTaskId;
    const deleteH = canDelete
      ? `<button class="msg-action-btn msg-delete-btn" onclick="event.stopPropagation();deleteTurn(${idx})" title="${isUser ? 'Delete this turn' : 'Delete this message'}"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg></button>`
      : "";
    actionBtns = `<div class="message-actions">${copyH}${editH}${regenH}${continueH}${translateH}${exportImgH}${deleteH}</div>`;
  }
  // ★ Tofu mascot avatars: Worker gets worker tofu, Planner gets planner tofu
  let avatarContent = (typeof _TOFU_WORKER_SVG !== 'undefined') ? _TOFU_WORKER_SVG : "✦",
    roleName = "Agent";
  if (msg._isEndpointPlanner) {
    avatarContent = (typeof _TOFU_PLANNER_SVG !== 'undefined') ? _TOFU_PLANNER_SVG
      : '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z"/><path d="M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z"/></svg>';
    roleName = "Planner";
  }

  // ── Branch zone for assistant messages (only un-inlined branches + add button) ──
  let branchHtml = "";
  if (!isUser && typeof renderBranchZone === "function") {
    branchHtml = renderBranchZone(msg, idx, _inlinedBranches);
  }

  // ── Planner badge for planner messages ──
  let plannerBadge = "";
  if (msg._isEndpointPlanner) {
    plannerBadge = `<span class="ep-verdict-badge ep-verdict-planner">Plan</span>`;
  }

  // ── Critic verdict badge for endpoint review messages ──
  let criticBadge = "";
  if (msg._isEndpointReview) {
    if (msg._epApproved) {
      criticBadge = `<span class="ep-verdict-badge ep-verdict-stop">Approved</span>`;
    } else if (msg._isStuck) {
      criticBadge = `<span class="ep-verdict-badge ep-verdict-stuck">Stuck</span>`;
    } else if (msg._epNextPhase === 'planner') {
      // Critic requested a full re-plan — distinct amber badge
      criticBadge = `<span class="ep-verdict-badge ep-verdict-replan">Replan</span>`;
    } else {
      criticBadge = `<span class="ep-verdict-badge ep-verdict-continue">Iteration ${msg._epIteration || ""}</span>`;
    }
  }

  // ── Avatar: tofu critic for reviews & autopilot, onigiri mascot for user ──
  const _criticAvatar = (typeof _TOFU_CRITIC_SVG !== 'undefined') ? _TOFU_CRITIC_SVG
    : '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>';
  const userAvatar = (msg._isEndpointReview || msg._isVirtualUser)
    ? _criticAvatar
    : (typeof _USER_AVATAR_SVG !== 'undefined') ? _USER_AVATAR_SVG
    : '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>';
  const userLabel = msg._isEndpointReview ? "Critic"
    : msg._isVirtualUser ? "Autopilot"
    : "You";

  /* messageTime is a formatter output (digits + localized separators) —
   * escape it by default via safeHtml (it carries no markup). */
  const messageTimeHtml = messageTime ? safeHtml`<span class="message-time">${messageTime}</span>` : '';
  const mfpAttr = typeof idx === "number" ? raw(` data-mfp="${_msgFingerprint(msg)}"`) : "";
  const epWorkerCls = (!isUser && !msg._isEndpointPlanner && !msg._isEndpointReview) ? ' ep-worker-msg' : '';
  const epPlannerCls = msg._isEndpointPlanner ? ' ep-planner-msg' : '';
  const vuCls = msg._isVirtualUser ? ' vu-user-msg' : '';
  // ── Per-turn context capsule (floats in the RIGHT GUTTER beside the turn) ──
  // Frozen snapshot of the workspace/tools/model active when this turn was
  // sent (msg._ctx, captured in the send pipeline). Rendered as a direct
  // child of `.message` — NOT inside `.message-content` (which clips via
  // overflow:hidden) — so CSS can place it at left:100% out in the gutter,
  // clear of both the bubble and the hover action-bar. See info-rail.js.
  let turnCtxHtml = "";
  if (msg._ctx && typeof renderTurnCtxNote === "function") {
    try { turnCtxHtml = renderTurnCtxNote(msg._ctx); }
    catch (e) { console.debug("[turnCtx] renderTurnCtxNote failed:", e); }
  }
  const badgeHtml = plannerBadge || criticBadge;
  /* Final assembly via safeHtml. roleName / userLabel / time are
   * escaped by default. Everything pre-built above (avatars = trusted
   * SVG, badgeHtml, body, branchHtml, actionBtns, the class/attr
   * fragments) is already-trusted HTML, marked raw(). */
  const _classAttr = `${isUser ? ' user-msg' : ''}${msg._isEndpointReview ? ' ep-critic-msg' : ''}${epPlannerCls}${epWorkerCls}${vuCls}`;
  return String(safeHtml`<div class="message${raw(_classAttr)}"${raw(idAttr)}${raw(msgIdAttr)}${raw(mfpAttr)}><div class="message-avatar">${raw(isUser ? userAvatar : avatarContent)}</div><div class="message-content"><div class="message-header"><span class="message-role">${isUser ? userLabel : roleName}</span>${raw(badgeHtml)}${raw(messageTimeHtml)}</div><div class="message-body">${raw(body)}</div>${raw(branchHtml)}${raw(actionBtns)}</div>${raw(turnCtxHtml)}</div>`);
}
