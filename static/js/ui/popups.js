/* ═══════════════════════════════════════════════════════════════════
   popups — extracted from ui.js (split 2026-05-28)

   Selection popup, reply quotes, conversation references.

   This file is concatenated by lib/js_bundler.py — symbols share
   the same window scope as every other static/js/*.js file. No
   exports / imports needed.
   ═══════════════════════════════════════════════════════════════════ */

function _initSelectionPopup() {
  _selectionPopup = document.createElement("div");
  _selectionPopup.className = "selection-popup";
  _selectionPopup.style.display = "none";
  _selectionPopup.innerHTML = `
    <button class="selection-popup-btn" data-action="branch">${t('conv.branch')}</button>
    <button class="selection-popup-btn" data-action="reply">${t('conv.reply')}</button>`;
  document.body.appendChild(_selectionPopup);

  _selectionPopup.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-action]");
    if (!btn) return;
    const action = btn.dataset.action;
    const sel = window.getSelection();
    const text = sel.toString().trim();
    if (!text) { _hideSelectionPopup(); return; }

    const msgEl = sel.anchorNode?.parentElement?.closest?.(".message[id]");
    const msgIdx = msgEl ? parseInt(msgEl.id.replace("msg-", ""), 10) : -1;

    if (action === "branch" && msgIdx >= 0) {
      // Capture the live selection Range before it's cleared — we'll use it
      // to insert the branch element directly into the DOM at the exact spot.
      const range = sel.rangeCount > 0 ? sel.getRangeAt(0).cloneRange() : null;
      const title = text.slice(0, 40) + (text.length > 40 ? "…" : "");
      promptNewBranch(msgIdx, title, text, range);
    } else if (action === "reply") {
      _addReplyQuote(text, msgIdx);
    }
    sel.removeAllRanges();
    _hideSelectionPopup();
  });

  // Show popup on selection in chat area
  let _selMouseUpRaf = 0;
  document.addEventListener("mouseup", (e) => {
    if (_selectionPopup.contains(e.target)) return;
    cancelAnimationFrame(_selMouseUpRaf);
    _selMouseUpRaf = requestAnimationFrame(() => {
      const sel = window.getSelection();
      if (!sel || sel.isCollapsed || sel.toString().trim().length < 5) {
        _hideSelectionPopup();
        return;
      }
      const msgEl = sel.anchorNode?.parentElement?.closest?.(".message[id]");
      if (!msgEl) { _hideSelectionPopup(); return; }
      if (msgEl.id === "streaming-msg") { _hideSelectionPopup(); return; }

      const range = sel.getRangeAt(0);
      const rect = range.getBoundingClientRect();
      _selectionPopup.style.left = `${rect.left + rect.width / 2 - 60}px`;
      _selectionPopup.style.top = `${rect.top - 40 + window.scrollY}px`;
      _selectionPopup.style.display = "flex";
    });
  });

  document.addEventListener("mousedown", (e) => {
    if (!_selectionPopup.contains(e.target)) _hideSelectionPopup();
  });
}

function _hideSelectionPopup() {
  if (_selectionPopup) _selectionPopup.style.display = "none";
}

// ── Reply quotes (multi-quote support) ──
function _addReplyQuote(text, msgIdx) {
  _pendingReplyQuotes.push(text);
  _renderReplyQuoteChips();
}

function _renderReplyQuoteChips() {
  let container = document.getElementById("reply-quote-container");
  if (!container) {
    container = document.createElement("div");
    container.id = "reply-quote-container";
    container.className = "reply-quote-container";
    const inputActions = document.querySelector(".input-box .input-actions");
    if (inputActions) inputActions.parentElement.insertBefore(container, inputActions);
  }
  if (!_pendingReplyQuotes.length) {
    container.style.display = "none";
    return;
  }
  container.style.display = "flex";
  container.innerHTML = _pendingReplyQuotes.map((q, i) => {
    const preview = q.replace(/\s+/g, " ").slice(0, 50);
    const chars = q.length;
    const lines = q.split("\n").length;
    return `<div class="reply-quote-chip">
      
      <span class="reply-quote-chip-body">
        <span class="reply-quote-chip-label">${escapeHtml(preview)}${chars > 50 ? "…" : ""}</span>
        <span class="reply-quote-chip-meta">${chars} chars · ${lines} line${lines > 1 ? "s" : ""}</span>
      </span>
      <button class="reply-quote-chip-close" onclick="_removeReplyQuote(${i})" title="Remove">✕</button>
    </div>`;
  }).join("");
}

function _removeReplyQuote(idx) {
  _pendingReplyQuotes.splice(idx, 1);
  _renderReplyQuoteChips();
}

function clearReplyQuote() {
  _pendingReplyQuotes = [];
  _renderReplyQuoteChips();
}

function getPendingReplyQuotes() {
  return _pendingReplyQuotes.length > 0 ? [..._pendingReplyQuotes] : null;
}

// ══════════════════════════════════════════════════════
// ★ Conversation Reference Chips (@-mention)
// ══════════════════════════════════════════════════════
const _pendingConvRefs = [];  // [{id, title}]

function addConvRef(convId, convTitle) {
  // Don't add duplicates or self-references
  const activeConv = getActiveConv();
  if (activeConv && activeConv.id === convId) {
    showToast?.(t('convRef.cannotRef'), "warning");
    return;
  }
  if (_pendingConvRefs.some(r => r.id === convId)) {
    showToast?.(t('convRef.alreadyRef'), "info");
    return;
  }
  _pendingConvRefs.push({ id: convId, title: convTitle || "Untitled" });
  _renderConvRefChips();
  // Focus the input and show confirmation
  document.getElementById("userInput")?.focus();
  const shortTitle = (convTitle || "Untitled").slice(0, 30) + (convTitle && convTitle.length > 30 ? "…" : "");
  showToast?.(t('convRef.referencedToast', { title: shortTitle }), "success");
}

function removeConvRef(index) {
  _pendingConvRefs.splice(index, 1);
  _renderConvRefChips();
}

function clearConvRefs() {
  _pendingConvRefs.length = 0;
  _renderConvRefChips();
}

function getPendingConvRefs() {
  return _pendingConvRefs.length > 0 ? _pendingConvRefs.map(r => ({...r})) : null;
}

function _renderConvRefChips() {
  let container = document.getElementById("conv-ref-container");
  if (!container) {
    container = document.createElement("div");
    container.id = "conv-ref-container";
    container.className = "conv-ref-container";
    // Place inside .input-box, just above .input-actions toolbar
    const inputActions = document.querySelector(".input-box .input-actions");
    if (inputActions) inputActions.parentElement.insertBefore(container, inputActions);
  }
  if (!_pendingConvRefs.length) {
    container.innerHTML = "";
    return;
  }
  container.innerHTML = _pendingConvRefs.map((ref, i) => {
    const title = escapeHtml(ref.title.length > 45 ? ref.title.slice(0, 42) + "…" : ref.title);
    // Show message count instead of raw ID
    const localConv = (typeof conversations !== "undefined" ? conversations : []).find(c => c.id === ref.id);
    const msgCount = localConv?.messages?.length || 0;
    const subtitle = msgCount > 0 ? t('convRef.messagesCount', { n: msgCount }) : t('convRef.convRef');
    return `<div class="conv-ref-chip" data-index="${i}">
      <span class="conv-ref-chip-icon">@</span>
      <span class="conv-ref-chip-info">
        <span class="conv-ref-chip-title">${title}</span>
        <span class="conv-ref-chip-id">${escapeHtml(subtitle)}</span>
      </span>
      <button class="conv-ref-chip-remove" data-index="${i}" title="${escapeHtml(t('convRef.removeRef'))}">×</button>
    </div>`;
  }).join("");
  container.querySelectorAll(".conv-ref-chip-remove").forEach(btn => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      removeConvRef(parseInt(btn.dataset.index));
    });
  });
  // Update toolbar @ button active state
  const refBtn = document.getElementById("convRefBtn");
  if (refBtn) refBtn.classList.toggle("has-refs", _pendingConvRefs.length > 0);
}

