/* ═══════════════════════════════════════════════════════════════════
   core/dialog.js — themed modal dialogs (confirm / alert / prompt)

   Drop-in replacements for the ugly browser-native window.confirm/alert/
   prompt. Each returns a Promise so call sites `await` the user's choice:

       if (!await showConfirm('删除这条记录？')) return;
       await showAlert('保存失败: ' + msg);
       const name = await showPrompt('分支名称：');   // null if cancelled

   Options (all optional):
     showConfirm(message, { title, okText, cancelText, danger })
     showAlert(message,   { title, okText })
     showPrompt(message,  { title, defaultValue, placeholder, okText,
                            cancelText })

   The card is built with DOM APIs (textContent), so the message and title
   are never interpreted as HTML — safe for user/model content by default.

   This file is concatenated by lib/js_bundler.py AFTER the slim core.js
   shell — symbols share `window` scope so no exports needed.
   ═══════════════════════════════════════════════════════════════════ */

/** Resolve a localized label, falling back when i18n isn't loaded yet. */
function _dlgT(key, fallback) {
  try {
    if (typeof t === 'function') {
      const v = t(key);
      if (v && v !== key) return v;
    }
  } catch (e) { /* i18n not ready — use fallback */ }
  return fallback;
}

/** Append a message string to `el`, preserving line breaks as <br>. */
function _dlgSetMessage(el, message) {
  const lines = String(message == null ? '' : message).split('\n');
  lines.forEach((line, i) => {
    if (i > 0) el.appendChild(document.createElement('br'));
    el.appendChild(document.createTextNode(line));
  });
}

/**
 * Core builder shared by confirm/alert/prompt.
 *
 * @param {Object} cfg
 * @param {string} cfg.message   Body text (newlines preserved).
 * @param {string} [cfg.title]   Optional bold header.
 * @param {string} cfg.okText    Primary button label.
 * @param {string} [cfg.cancelText]  Cancel label; omit to hide (alert).
 * @param {boolean} [cfg.danger] Style the primary button as destructive.
 * @param {boolean} [cfg.prompt] Render a text input; resolve its value.
 * @param {string} [cfg.defaultValue] Initial input value (prompt).
 * @param {string} [cfg.placeholder]  Input placeholder (prompt).
 * @returns {Promise<boolean|string|null>}
 */
function _openDialog(cfg) {
  return new Promise((resolve) => {
    const overlay = document.createElement('div');
    overlay.className = 'app-dialog-overlay';

    const card = document.createElement('div');
    card.className = 'app-dialog' + (cfg.danger ? ' is-danger' : '');
    card.setAttribute('role', cfg.prompt ? 'dialog' : 'alertdialog');
    card.setAttribute('aria-modal', 'true');

    if (cfg.title) {
      const h = document.createElement('div');
      h.className = 'app-dialog-title';
      h.textContent = cfg.title;
      card.appendChild(h);
    }

    const body = document.createElement('div');
    body.className = 'app-dialog-message';
    _dlgSetMessage(body, cfg.message);
    card.appendChild(body);

    let input = null;
    if (cfg.prompt) {
      input = document.createElement('input');
      input.type = 'text';
      input.className = 'app-dialog-input';
      input.value = cfg.defaultValue != null ? String(cfg.defaultValue) : '';
      if (cfg.placeholder) input.placeholder = cfg.placeholder;
      card.appendChild(input);
    }

    const actions = document.createElement('div');
    actions.className = 'app-dialog-actions';

    let cancelBtn = null;
    if (cfg.cancelText !== null) {
      cancelBtn = document.createElement('button');
      cancelBtn.className = 'app-dialog-btn app-dialog-cancel';
      cancelBtn.textContent = cfg.cancelText;
      actions.appendChild(cancelBtn);
    }

    const okBtn = document.createElement('button');
    okBtn.className = 'app-dialog-btn app-dialog-ok' + (cfg.danger ? ' is-danger' : '');
    okBtn.textContent = cfg.okText;
    actions.appendChild(okBtn);

    card.appendChild(actions);
    overlay.appendChild(card);
    document.body.appendChild(overlay);

    // ── Resolve + teardown ──
    let done = false;
    const prevFocus = document.activeElement;
    function close(result) {
      if (done) return;
      done = true;
      document.removeEventListener('keydown', onKey, true);
      overlay.classList.add('closing');
      setTimeout(() => overlay.remove(), 160);
      try { if (prevFocus && prevFocus.focus) prevFocus.focus(); } catch (e) { /* ignore */ }
      resolve(result);
    }

    const cancelResult = cfg.prompt ? null : false;
    const okResult = cfg.prompt ? '' : true;

    okBtn.onclick = () => close(cfg.prompt ? input.value : okResult);
    if (cancelBtn) cancelBtn.onclick = () => close(cancelResult);
    // Click on the dimmed backdrop = cancel (matches the cancel button).
    overlay.onclick = (e) => { if (e.target === overlay) close(cancelResult); };

    function onKey(e) {
      if (e.key === 'Escape') { e.preventDefault(); close(cancelResult); }
      else if (e.key === 'Enter') {
        // In a prompt the input has focus; Enter confirms with its value.
        e.preventDefault();
        close(cfg.prompt ? input.value : okResult);
      }
    }
    document.addEventListener('keydown', onKey, true);

    // Animate in + focus the most useful control.
    requestAnimationFrame(() => {
      overlay.classList.add('open');
      if (input) { input.focus(); input.select(); }
      else okBtn.focus();
    });
  });
}

/** Themed replacement for window.confirm — resolves true/false. */
function showConfirm(message, opts) {
  opts = opts || {};
  return _openDialog({
    message: message,
    title: opts.title,
    okText: opts.okText || _dlgT('dialog.confirm', '确定'),
    cancelText: opts.cancelText || _dlgT('dialog.cancel', '取消'),
    danger: !!opts.danger,
  });
}

/** Themed replacement for window.alert — resolves when dismissed. */
function showAlert(message, opts) {
  opts = opts || {};
  return _openDialog({
    message: message,
    title: opts.title,
    okText: opts.okText || _dlgT('dialog.ok', '好的'),
    cancelText: null,
  });
}

/** Themed replacement for window.prompt — resolves the string, or null. */
function showPrompt(message, opts) {
  opts = opts || {};
  return _openDialog({
    message: message,
    title: opts.title,
    prompt: true,
    defaultValue: opts.defaultValue != null ? opts.defaultValue : '',
    placeholder: opts.placeholder || '',
    okText: opts.okText || _dlgT('dialog.confirm', '确定'),
    cancelText: opts.cancelText || _dlgT('dialog.cancel', '取消'),
  });
}

if (typeof window !== 'undefined') {
  window.showConfirm = showConfirm;
  window.showAlert = showAlert;
  window.showPrompt = showPrompt;
}
