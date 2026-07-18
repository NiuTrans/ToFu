/* ═══════════════════════════════════════════════════════════════════
   main translating bubble — extracted from main.js (split 2026-05-28)

   Translating-indicator bubble (shown while server pre-translates user message).

   This file is concatenated by lib/js_bundler.py BEFORE main.js so
   the boot IIFE can reference these symbols. Symbols share `window`
   scope — no imports / exports needed.
   ═══════════════════════════════════════════════════════════════════ */


/**
 * Render a pre-stream assistant placeholder bubble in the chat DOM.
 *
 * Serves TWO phases on the SAME DOM node (#translating-msg) so every send /
 * regenerate / edit exit path's existing `_removeTranslatingBubble()` tears it
 * down uniformly:
 *   • auto-translate on  → default label '翻译中…' (server pre-translates the
 *     user message before starting the agent);
 *   • auto-translate off → caller passes '连接中…' so the assistant side is NOT
 *     blank during the synchronous /api/chat/send POST (load → task-start).
 * Upgraded in place to the real streaming bubble once the POST returns a taskId.
 *
 * @param {string} [label] Status text. Defaults to the translating label.
 */
function _renderTranslatingBubble(label) {
  const inner = document.getElementById("chatInner");
  if (!inner) return;
  // Remove any previous translating bubble
  _removeTranslatingBubble();
  const el = document.createElement("div");
  el.className = "message message-new";
  el.addEventListener('animationend', () => el.classList.remove('message-new'), { once: true });
  el.id = "translating-msg";
  const avatar = (typeof _TOFU_WORKER_SVG !== 'undefined') ? _TOFU_WORKER_SVG : '✦';
  const _label = label || t('sidebar.translating');
  el.innerHTML = `<div class="message-avatar">${avatar}</div>
    <div class="message-content">
      <div class="message-header">
        <span class="message-role">Agent</span>
        <span class="message-time">${formatClockTime()}</span>
      </div>
      <div class="message-body">
        <div class="stream-status"><div class="pulse"></div> ${_label}</div>
      </div>
    </div>`;
  inner.appendChild(el);
  /* ★ Real-height scroll (see sendMessage): plain scrollToBottom under-measures
   *   against content-visibility:auto estimates and lands mid-history. */
  _forceScrollToBottom(null, true);
}

function _removeTranslatingBubble() {
  const el = document.getElementById("translating-msg");
  if (el) el.remove();
}

/**
 * Render the streaming assistant bubble in the chat DOM.
 * Shared by sendMessage and regenerateFromUser flows.
 */
function _renderStreamingBubble(conv, sendConfig, msgId) {
  const inner = document.getElementById("chatInner");
  if (!inner) return;
  const role = _streamingBubbleRole(conv, sendConfig);
  // Stamp the assistant message id (data-msg-id) on the bubble so live
  // per-round translation partials can be routed to it while it streams.
  /* ★ Dedup at the insert boundary via ConvView.startStreaming (_evictByMsgId)
   *   so a residual #streaming-msg / drifted static twin can't leave a second
   *   empty bubble. Fallback keeps dev-mode parity. */
  if (window.ConvView && typeof window.ConvView.startStreaming === 'function') {
    window.ConvView.startStreaming(conv.id, { role, msgId: msgId || null });
  } else {
    inner.insertAdjacentHTML('beforeend', _streamingBubbleHTML(role, null, null, msgId || null));
  }
  const el = document.getElementById('streaming-msg');
  if (el) {
    el.classList.add('message-new');
    el.addEventListener('animationend', () => el.classList.remove('message-new'), { once: true });
  }
  /* ★ Real-height scroll (see sendMessage): plain scrollToBottom under-measures
   *   against content-visibility:auto estimates and lands mid-history. */
  _forceScrollToBottom(null, true);
}

