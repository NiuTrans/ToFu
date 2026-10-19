/* ===== migrated source: ui/popups.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   popups — extracted from ui.js (split 2026-05-28)

   Selection popup, reply quotes, conversation references.

   This file is concatenated by Vite's module graph — symbols share
   the same window scope as every other frontend/src/runtime/*.js file. No
   exports / imports needed.
   ═══════════════════════════════════════════════════════════════════ */

// Temporary composition bridge; delete when composer orchestration migrates.
let _selectionPopup = null;
let _pendingReplyQuotes = [];

function _initSelectionPopup() {
  _selectionPopup?.destroy();
  _selectionPopup = createSelectionPopup({
    t,
    conversationId: () => getActiveConv()?.id || null,
    quote: ({ text }) => {
      _addReplyQuote(text);
      document.getElementById('userInput')?.focus();
    },
    branch: ({ conversationId, turnId, text }) => {
      if (getActiveConv()?.id !== conversationId) return Promise.resolve(false);
      const title = Array.from(text.replace(/\s+/g, ' ')).slice(0, 40).join('');
      return promptNewBranch(turnId, title, text);
    },
  });
  if (typeof retainedCompositionLifecycle !== 'undefined') {
    retainedCompositionLifecycle.add(_selectionPopup.destroy);
  }
}

// ── Reply quotes: existing send/ACK/rollback owner retains the state. ──
function _addReplyQuote(text) {
  if (!text?.trim() || _pendingReplyQuotes.includes(text)) return;
  _pendingReplyQuotes.push(text);
  _renderReplyQuoteChips();
}

function _renderReplyQuoteChips() {
  let container = document.getElementById('reply-quote-container');
  if (!container) {
    if (!_pendingReplyQuotes.length) return;
    const inputActions = document.querySelector('.input-box .input-actions');
    if (!inputActions) return;
    container = document.createElement('div');
    container.id = 'reply-quote-container';
    container.className = 'reply-quote-container';
    inputActions.parentElement.insertBefore(container, inputActions);
  }
  renderReplyQuotePreviews(container, _pendingReplyQuotes, t, _removeReplyQuote);
}

window.addEventListener('tofu:language-change', _renderReplyQuoteChips);

function _removeReplyQuote(idx) {
  _pendingReplyQuotes.splice(idx, 1);
  _renderReplyQuoteChips();
}

function clearReplyQuote() {
  _pendingReplyQuotes = [];
  _renderReplyQuoteChips();
}

/** Remove only quotes carried by an acknowledged composer submission. */
function consumePendingReplyQuotes(consumedQuotes) {
  for (const quote of consumedQuotes || []) {
    const index = _pendingReplyQuotes.indexOf(quote);
    if (index >= 0) _pendingReplyQuotes.splice(index, 1);
  }
  _renderReplyQuoteChips();
}

function getPendingReplyQuotes() {
  return _pendingReplyQuotes.length > 0 ? [..._pendingReplyQuotes] : null;
}
/** Re-queue quotes from an uncommitted composer submission (send rollback). */
function restorePendingReplyQuotes(quotes) {
  const missing = (quotes || []).filter(
    (quote) => !_pendingReplyQuotes.includes(quote),
  );
  if (missing.length > 0) _pendingReplyQuotes.unshift(...missing);
  _renderReplyQuoteChips();
}

// ══════════════════════════════════════════════════════
// Conversation Reference Chips (@-mention)
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

/** Remove only references carried by an acknowledged composer submission. */
function consumePendingConvRefs(consumedRefs) {
  for (const consumed of consumedRefs || []) {
    const index = _pendingConvRefs.findIndex((candidate) =>
      candidate.id === consumed.id && candidate.title === consumed.title);
    if (index >= 0) _pendingConvRefs.splice(index, 1);
  }
  _renderConvRefChips();
}

function getPendingConvRefs() {
  return _pendingConvRefs.length > 0 ? _pendingConvRefs.map(r => ({...r})) : null;
}
/** Re-queue references from an uncommitted composer submission (rollback). */
function restorePendingConvRefs(refs) {
  const missing = (refs || []).filter((ref) => ref
    && !_pendingConvRefs.some((candidate) =>
      candidate.id === ref.id && candidate.title === ref.title));
  if (missing.length > 0) _pendingConvRefs.unshift(...missing);
  _renderConvRefChips();
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
    const msgCount = localConv
      ? (runtimeScope.ConversationTurnRead?.ordered?.(localConv)?.length
        || localConv._serverTurnCount || 0)
      : 0;
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
