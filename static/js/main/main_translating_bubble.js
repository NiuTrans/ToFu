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
 *
 * When ``convId`` is provided, the bubble also starts polling
 * /api/chat/send-translate-status/<convId> to surface transient retry
 * reasons (429 rate-limit, empty output, etc.) underneath the spinner.
 */
function _renderTranslatingBubble(convId) {
  const inner = document.getElementById("chatInner");
  if (!inner) return;
  // Remove any previous translating bubble (also stops any prior poller)
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
        <span class="message-time">${new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</span>
      </div>
      <div class="message-body">
        <div class="stream-status"><div class="pulse"></div> ${t('sidebar.translating')}</div>
        <div class="translating-status-sub" id="translating-status-sub" style="display:none;font-size:11px;color:#f59e0b;margin-top:6px"></div>
      </div>
    </div>`;
  inner.appendChild(el);
  scrollToBottom();

  // Start polling for retry-status updates (429 / empty-output / etc.).
  if (convId) _startSendTranslateStatusPoll(convId);
}

function _removeTranslatingBubble() {
  _stopSendTranslateStatusPoll();
  const el = document.getElementById("translating-msg");
  if (el) el.remove();
}

// ─── Poll loop for send-path translate retry status ───
// The /api/chat/send handler translates synchronously and blocks its HTTP
// response until translation succeeds or times out.  During that window the
// backend may be retrying (429 rate-limit, empty output, etc.) — we poll a
// tiny side-channel endpoint to surface the current reason under the
// "Translating…" bubble so the user knows something is actually happening.
let _sendTranslateStatusTimer = null;

function _startSendTranslateStatusPoll(convId) {
  _stopSendTranslateStatusPoll();
  let lastMsg = '';
  const tick = async () => {
    const sub = document.getElementById("translating-status-sub");
    if (!sub) {  // bubble removed — stop polling
      _stopSendTranslateStatusPoll();
      return;
    }
    try {
      const d = await Api.chat.sendTranslateStatus(convId);
      if (d) {
        if (d.statusMessage) {
          if (d.statusMessage !== lastMsg) {
            lastMsg = d.statusMessage;
            // Prefer a localized label by kind, fall back to the raw server string.
            const kind = d.statusKind || '';
            const i18nKey = kind ? `translate.retry.${kind}` : '';
            const localized = (i18nKey && typeof t === 'function') ? t(i18nKey) : '';
            const display = (localized && localized !== i18nKey) ? localized : d.statusMessage;
            sub.style.display = '';
            sub.title = d.statusMessage;
            sub.textContent = '⚠ ' + display;
          }
        }
      }
    } catch (e) { /* ignore transient fetch errors */ }
  };
  // First poll after a short delay (first retry usually takes a few seconds)
  _sendTranslateStatusTimer = setInterval(tick, 1500);
  setTimeout(tick, 500);
}

function _stopSendTranslateStatusPoll() {
  if (_sendTranslateStatusTimer) {
    clearInterval(_sendTranslateStatusTimer);
    _sendTranslateStatusTimer = null;
  }
}

/**
 * Render the streaming assistant bubble in the chat DOM.
 * Shared by sendMessage and regenerateFromUser flows.
 */
function _renderStreamingBubble(conv, sendConfig) {
  const inner = document.getElementById("chatInner");
  if (!inner) return;
  const role = _streamingBubbleRole(conv, sendConfig);
  inner.insertAdjacentHTML('beforeend', _streamingBubbleHTML(role));
  const el = document.getElementById('streaming-msg');
  if (el) {
    el.classList.add('message-new');
    el.addEventListener('animationend', () => el.classList.remove('message-new'), { once: true });
  }
  scrollToBottom();
}

