/* ═══════════════════════════════════════════════════════════════════
   main translating bubble — extracted from main.js (split 2026-05-28)

   Translating-indicator bubble (shown while server pre-translates user message).

   This file is concatenated by lib/js_bundler.py BEFORE main.js so
   the boot IIFE can reference these symbols. Symbols share `window`
   scope — no imports / exports needed.
   ═══════════════════════════════════════════════════════════════════ */


/**
 * Render a translating indicator bubble in the chat DOM.
 * Shown while the server translates the user's message before starting the agent.
 * Removed when the real streaming bubble appears.
 */
function _renderTranslatingBubble() {
  const inner = document.getElementById("chatInner");
  if (!inner) return;
  // Remove any previous translating bubble
  _removeTranslatingBubble();
  const el = document.createElement("div");
  el.className = "message message-new";
  el.addEventListener('animationend', () => el.classList.remove('message-new'), { once: true });
  el.id = "translating-msg";
  const avatar = (typeof _TOFU_WORKER_SVG !== 'undefined') ? _TOFU_WORKER_SVG : '✦';
  el.innerHTML = `<div class="message-avatar">${avatar}</div>
    <div class="message-content">
      <div class="message-header">
        <span class="message-role">Agent</span>
        <span class="message-time">${formatClockTime()}</span>
      </div>
      <div class="message-body">
        <div class="stream-status"><div class="pulse"></div> ${t('sidebar.translating')}</div>
      </div>
    </div>`;
  inner.appendChild(el);
  scrollToBottom();
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
  inner.insertAdjacentHTML('beforeend', _streamingBubbleHTML(role, null, null, msgId || null));
  const el = document.getElementById('streaming-msg');
  if (el) {
    el.classList.add('message-new');
    el.addEventListener('animationend', () => el.classList.remove('message-new'), { once: true });
  }
  scrollToBottom();
}

