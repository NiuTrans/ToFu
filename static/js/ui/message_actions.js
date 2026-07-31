/* ═══════════════════════════════════════════════════════════════════
   message actions — extracted from ui.js (split 2026-05-28)

   Per-message actions: copy, delete, translate.

   This file is concatenated by lib/js_bundler.py — symbols share
   the same window scope as every other static/js/*.js file. No
   exports / imports needed.
   ═══════════════════════════════════════════════════════════════════ */

function _toggleThinking(el, msgIdx) {
  el.classList.toggle("expanded");
  const txt = el.querySelector(".thinking-text");
  if (!txt || txt.textContent) return; // already populated
  const conv = getActiveConv();
  if (!conv) return;
  const msg = conv.messages[msgIdx];
  if (msg?.thinking) {
    txt.textContent = msg.thinking;
  }
}

/* Lazy expand for the display-only "Earlier Thinking" block (Continue
   rollback). Same lazy-load idiom as _toggleThinking — sources from
   msg.priorThinking instead of msg.thinking. */
function _togglePriorThinking(el, msgIdx) {
  el.classList.toggle("expanded");
  const txt = el.querySelector(".thinking-text");
  if (!txt || txt.textContent) return;
  const conv = getActiveConv();
  if (!conv) return;
  const msg = conv.messages[msgIdx];
  if (msg?.priorThinking) {
    txt.textContent = msg.priorThinking;
  }
}

/* Lazy expand for the display-only "Earlier Response" block (Continue
   rollback). Twin of _togglePriorThinking — sources from msg.priorContent,
   the prose tail that was discarded when resuming from the tool-result
   checkpoint. Rendered as plain text (not markdown) so it reads as a raw
   record of what was rolled back. */
function _togglePriorContent(el, msgIdx) {
  el.classList.toggle("expanded");
  const txt = el.querySelector(".thinking-text");
  if (!txt || txt.textContent) return;
  const conv = getActiveConv();
  if (!conv) return;
  const msg = conv.messages[msgIdx];
  if (msg?.priorContent) {
    txt.textContent = msg.priorContent;
  }
}

/* Lazy-load branch thinking via <details> toggle event */
document.addEventListener("toggle", function (e) {
  const det = e.target;
  if (!det.classList?.contains("branch-thinking") || !det.open) return;
  const lazy = det.querySelector(".branch-think-lazy");
  if (!lazy || lazy.textContent) return; // already loaded
  const mIdx = +det.dataset.branchThinkMsgidx;
  const bIdx = +det.dataset.branchThinkBidx;
  const bMsgIdx = +det.dataset.branchThinkMidx;
  const conv = getActiveConv();
  if (!conv) return;
  const msg = conv.messages[mIdx];
  const branch = msg?.branches?.[bIdx];
  const bMsg = branch?.messages?.[bMsgIdx];
  if (bMsg?.thinking) {
    lazy.textContent = bMsg.thinking;
  }
}, true);

function copyMessage(idx) {
  const conv = getActiveConv();
  if (!conv) return;
  const msg = conv.messages[idx];
  if (!msg) return;
  // Copy what the user currently sees on screen:
  // - Assistant with active translation → translatedContent (Chinese)
  // - User with originalContent (auto-translated input) → originalContent (Chinese)
  // - Otherwise → content
  const isUser = msg.role === "user";
  const showTrans = !isUser && msg.translatedContent && msg._showingTranslation !== false;
  let textToCopy;
  if (showTrans) {
    textToCopy = msg.translatedContent;
  } else if (isUser && msg.originalContent) {
    textToCopy = msg.originalContent;
  } else {
    textToCopy = msg.content || "";
  }
  _safeClipboardWrite(textToCopy)
    .then(() => {
      const btn = document.querySelector(`#msg-${idx} .copy-msg-btn`);
      if (btn) {
        const orig = btn.innerHTML;
        btn.innerHTML = `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="#34d399" stroke-width="2.5"><polyline points="20 6 9 17 4 12"/></svg> Copied!`;
        btn.classList.add("copied");
        setTimeout(() => {
          btn.innerHTML = orig;
          btn.classList.remove("copied");
        }, 1500);
      }
    })
    .catch(() => {});
}

// ── Copy bilingual original text ──
function copyBilingualOriginal(btn, role, idx) {
  const conv = getActiveConv();
  if (!conv) return;
  const msg = conv.messages[idx];
  if (!msg) return;
  const text = msg.content || '';
  _safeClipboardWrite(text).then(() => {
    const origHTML = btn.innerHTML;
    btn.innerHTML = `<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="#34d399" stroke-width="2.5"><polyline points="20 6 9 17 4 12"/></svg>`;
    btn.classList.add('copied');
    setTimeout(() => { btn.innerHTML = origHTML; btn.classList.remove('copied'); }, 1500);
  }).catch(() => {});
}

// ── Delete turn / message ──
// Keeps a small inline confirmation popup so the user doesn't accidentally delete.
let _deletePopup = null;
let _deletePopupTimeout = null;

function _hideDeletePopup() {
  if (_deletePopup) { _deletePopup.remove(); _deletePopup = null; }
  if (_deletePopupTimeout) { clearTimeout(_deletePopupTimeout); _deletePopupTimeout = null; }
}

function deleteTurn(idx) {
  const conv = getActiveConv();
  if (!conv) return;
  if (activeStreams.has(conv.id) || conv.activeTaskId) return;
  const msg = conv.messages[idx];
  if (!msg) return;

  // If there's already a popup, remove it first
  _hideDeletePopup();

  const isUser = msg.role === 'user';
  // Check if a turn delete is possible (user msg followed by assistant msg)
  const hasAssistantAfter = isUser && idx + 1 < conv.messages.length
    && conv.messages[idx + 1].role === 'assistant';

  // Build confirmation popup
  _deletePopup = document.createElement('div');
  _deletePopup.className = 'delete-turn-popup';

  if (isUser && hasAssistantAfter) {
    // User message with a following assistant response — offer both options
    _deletePopup.innerHTML = `
      <div class="delete-popup-title">Delete message?</div>
      <button class="delete-popup-btn delete-popup-turn" onclick="_execDeleteTurn(${idx},'turn')">
        <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>
        Delete turn
      </button>
      <button class="delete-popup-btn delete-popup-single" onclick="_execDeleteTurn(${idx},'single')">
        <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="5" y1="12" x2="19" y2="12"/></svg>
        This only
      </button>
      <button class="delete-popup-btn delete-popup-cancel" onclick="_hideDeletePopup()">Cancel</button>`;
  } else {
    // Assistant message or last user message — single delete only
    _deletePopup.innerHTML = `
      <div class="delete-popup-title">Delete this message?</div>
      <button class="delete-popup-btn delete-popup-single" onclick="_execDeleteTurn(${idx},'single')">
        <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>
        Delete
      </button>
      <button class="delete-popup-btn delete-popup-cancel" onclick="_hideDeletePopup()">Cancel</button>`;
  }

  // Position the popup near the delete button.
  // ★ Attach to <body> with position:fixed so ancestor overflow:hidden / contain:layout
  //   on .message-content (styles.css §143) cannot clip it away — that was the root
  //   cause of "delete button does nothing": the popup WAS created but rendered above
  //   the message-content top edge and immediately clipped.
  const msgEl = document.getElementById(`msg-${idx}`);
  const btnEl = msgEl ? msgEl.querySelector('.msg-delete-btn') : null;
  // ★ The base CSS rule .delete-turn-popup sets bottom:calc(100% + 4px) and right:0 for
  //   absolute-positioned use inside a relative parent. When we switch to fixed and
  //   set top/left, we MUST clear bottom/right or the element stretches to fill the
  //   viewport (caused the full-width horizontal bar bug).
  _deletePopup.style.position = 'fixed';
  _deletePopup.style.zIndex = '9999';
  _deletePopup.style.bottom = 'auto';
  _deletePopup.style.right = 'auto';
  _deletePopup.style.margin = '0';
  _deletePopup.style.maxWidth = 'calc(100vw - 16px)';

  if (btnEl) {
    const r = btnEl.getBoundingClientRect();
    // Temporarily place off-screen to measure real size, then reposition
    _deletePopup.style.top = '-9999px';
    _deletePopup.style.left = '-9999px';
    document.body.appendChild(_deletePopup);
    const pw = _deletePopup.offsetWidth || 160;
    const ph = _deletePopup.offsetHeight || 120;
    // Horizontal: anchor right edge of popup to right edge of button
    let left = Math.round(r.right - pw);
    if (left < 8) left = 8;
    const maxLeft = window.innerWidth - pw - 8;
    if (left > maxLeft) left = maxLeft;
    // Vertical: below button by default; flip above if it would overflow
    let top = Math.round(r.bottom + 6);
    if (top + ph > window.innerHeight - 8) {
      top = Math.max(8, Math.round(r.top - ph - 6));
    }
    _deletePopup.style.left = `${left}px`;
    _deletePopup.style.top = `${top}px`;
  } else {
    // Fallback: no button found — center in viewport
    _deletePopup.style.top = '50%';
    _deletePopup.style.left = '50%';
    _deletePopup.style.transform = 'translate(-50%, -50%)';
    document.body.appendChild(_deletePopup);
  }

  // Auto-dismiss after 5 seconds
  _deletePopupTimeout = setTimeout(_hideDeletePopup, 5000);

  // Dismiss on click outside
  setTimeout(() => {
    document.addEventListener('click', _deletePopupOutsideClick, { once: true, capture: true });
  }, 0);
}

function _deletePopupOutsideClick(e) {
  if (_deletePopup && !_deletePopup.contains(e.target) && !e.target.closest('.msg-delete-btn')) {
    _hideDeletePopup();
  }
}

async function _execDeleteTurn(idx, mode) {
  _hideDeletePopup();
  const conv = getActiveConv();
  if (!conv) return;
  if (activeStreams.has(conv.id) || conv.activeTaskId) return;

  const convId = conv.id;
  const msg = conv.messages[idx];
  if (!msg) return;
  // ★ Capture the exact target objects (and, for a turn, the following
  //   assistant) BEFORE the request so we can remove them by IDENTITY after —
  //   the server may resolve a DIFFERENT index (list drift from a server-side
  //   reconcile), so its returned deletedIndices are SERVER indices that need
  //   not match this local array. Removing by object reference is drift-proof.
  const _targets = [conv.messages[idx]];
  if (mode === 'turn' && msg.role === 'user'
      && conv.messages[idx + 1] && conv.messages[idx + 1].role === 'assistant') {
    _targets.push(conv.messages[idx + 1]);
  }
  try {
    // ★ Send the stable _msgId so the server corrects any index drift.
    const resp = await Api.conversations.deleteMessage(convId, idx, mode, { msgId: msg._msgId });
    if (!resp || !resp.ok) {
      const err = resp ? await resp.json().catch(() => ({ error: `HTTP ${resp.status}` })) : { error: 'no response' };
      console.error('[deleteTurn] Server error:', err);
      if (typeof showToast === 'function') showToast('Delete failed', 'error');
      return;
    }
    const result = await resp.json();
    const deletedIndices = result.deletedIndices || [idx];

    // Update local state: remove the captured target objects by identity.
    // Fall back to the server's deletedIndices only for any target that
    // isn't found by reference (defensive — shouldn't happen).
    let _removed = 0;
    for (const tgt of _targets) {
      const li = conv.messages.indexOf(tgt);
      if (li >= 0) { conv.messages.splice(li, 1); _removed++; }
    }
    if (_removed === 0) {
      for (const i of [...deletedIndices].sort((a, b) => b - a)) {
        if (i >= 0 && i < conv.messages.length) conv.messages.splice(i, 1);
      }
    }
    conv._serverMsgCount = conv.messages.length;
    conv._needsLoad = false;

    // Update IndexedDB cache
    if (typeof ConvCache !== 'undefined') ConvCache.put(conv);

    // Re-render
    if (activeConvId === convId) {
      window.ConvView.replaceAll(convId, { forceScroll: false });
      buildTurnNav(conv);
    }
    renderConversationList();

    if (typeof showToast === 'function') {
      showToast(deletedIndices.length > 1 ? 'Turn deleted' : 'Message deleted', 'success');
    }
  } catch (e) {
    console.error('[deleteTurn] Failed:', e);
    if (typeof showToast === 'function') showToast('Delete failed', 'error');
  }
}

// ── Translate message ──
async function translateMessage(idx) {
  const conv = getActiveConv();
  if (!conv) return;
  const msg = conv.messages[idx];
  if (!msg) return;
  // Allow on: assistant messages, endpoint critic reviews, AND autopilot VU
  // messages (all role=user + a marker).  Reject only plain user messages.
  if (msg.role === "user" && !msg._isEndpointReview && !msg._isVirtualUser) return;
  // If we already have a cached translation (manual or auto-translate), just toggle
  if (msg._translatedCache || msg.translatedContent) {
    if (msg._showingTranslation !== false) {
      // Currently showing translation → revert to original
      msg._showingTranslation = false;
    } else {
      // Currently showing original → switch to translation
      msg._showingTranslation = true;
    }
    saveConversations(conv.id);
    // ★ Targeted PATCH — replaces full-conv PUT so only the toggled flag
    //   hits the server. Fire-and-forget; on error we just log.
    _patchMessageOnServer(conv.id, idx, { _showingTranslation: !!msg._showingTranslation });
    /* ★ FIX: Use surgical single-element replacement instead of full renderChat()
     *   to avoid destroying the #streaming-msg when a stream is active.
     *   renderChat(conv) without forceScroll=false does a full innerHTML wipe. */
    const el = document.getElementById(`msg-${idx}`);
    if (el) {
      const _ct = document.getElementById('chatContainer');
      const _sv = _ct ? _ct.scrollTop : -1;
      window.ConvView.apply(conv.id, idx, msg);
      if (_sv >= 0 && _ct) _ct.scrollTop = _sv;
    } else {
      window.ConvView.replaceAll(conv.id);
    }
    return;
  }
  // First time: kick off the unified translation pipeline.
  // Clear any previous error state so the message-body indicator shows
  // "翻译中…" instead of the stale failed state.
  delete msg._translateError;
  delete msg._translateTaskId;
  msg._translateDone = false;
  const text = msg.content || "";
  if (!text.trim()) return;
  // Detect target language server-side: if the source is already
  // predominantly Chinese → translate to English, otherwise → Chinese.
  // The CJK-ratio threshold is backend policy (lib/text_lang.py
  // CHINESE_RATIO_THRESHOLD); _isAlreadyChinese delegates to
  // /api/v1/text/detect-language so the rule lives in one place.
  const targetLang = (await _isAlreadyChinese(text)) ? "English" : "Chinese";
  msg._originalContent = text;
  // Delegate to unified pipeline. mode='manual' surfaces the error on the
  // message body (renderMessage shows the click-to-retry hint) instead of
  // auto-retrying behind the scenes.
  _runTranslationPipeline(conv, idx, msg, {
    sourceLang: '',
    targetLang,
    field: 'translatedContent',
    mode: 'manual',
  });
}

