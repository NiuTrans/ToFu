/* ═══════════════════════════════════════════════════════════════════
   core/error_envelope.js — extracted from core.js (split 2026-05-28)

   Typed error envelope helpers: ERROR_KIND_LABELS, isErrorEnvelope, normalize/render/kind/message.

   This file is concatenated by lib/js_bundler.py AFTER the slim
   core.js shell — symbols share `window` scope so no exports needed.
   ═══════════════════════════════════════════════════════════════════ */

/* ── Typed error envelope helpers ───────────────────────────────────────
 *
 * Backend emits errors as a typed dict (see lib/error_envelope.py):
 *   { kind, severity, retryable, message, hint, detail, model, context,
 *     source, raw }
 *
 * The frontend stores the dict verbatim on assistantMsg.error and renders
 * via renderErrorEnvelope() below. Legacy plain strings (e.g. very old
 * persisted messages, or frontend-set "Server offline" strings) are
 * normalised through normalizeErrorEnvelope() so the renderer never has
 * to branch on shape.
 */
const ERROR_KIND_LABELS = {
  quota:                 'Quota exhausted',
  ratelimit:             'Rate limited',
  permission:            'Permission denied',
  no_slot:               'No keys available',
  dispatch_exhausted:    'All keys exhausted',
  timeout:               'Timed out',
  network:               'Network error',
  endpoint_unreachable:  'Endpoint unreachable',
  content_filter:        'Content filter',
  invalid_image:         'Image rejected',
  prompt_too_long:       'Prompt too long',
  stream_only:           'Stream-only model',
  model_limit:           'Model limit',
  tool_rounds_exhausted: 'Tool budget',
  tool_timeout:          'Tool timeout',
  premature_close:       'Stream cut off',
  abnormal_stop:         'Abnormal stop',
  aborted:               'Stopped',
  server_offline:        'Server offline',
  internal:              'Internal error',
  generic:               'Error',
};

function isErrorEnvelope(obj) {
  return !!obj && typeof obj === 'object'
    && typeof obj.kind === 'string'
    && typeof obj.message === 'string';
}

function normalizeErrorEnvelope(err) {
  if (err == null || err === '') return null;
  if (isErrorEnvelope(err)) return err;
  if (typeof err === 'string') {
    /* Legacy or frontend-stamped strings — wrap in a generic envelope so
     * the renderer logic stays branch-free. The 'server offline' string
     * stamped by the frontend stream-timer gets its own kind so the UI
     * can recognize it without regex. */
    const isServerOffline = /server offline/i.test(err);
    return {
      kind: isServerOffline ? 'server_offline' : 'generic',
      severity: 'warning',
      retryable: isServerOffline,
      message: err,
      hint: '',
      detail: err.slice(0, 300),
      model: '', context: '', source: 'frontend-legacy',
      raw: err,
    };
  }
  /* Unknown shape — best effort. */
  try {
    return {
      kind: 'generic',
      severity: 'error', retryable: false,
      message: 'Unknown error',
      hint: '',
      detail: JSON.stringify(err).slice(0, 300),
      model: '', context: '', source: 'frontend-unknown', raw: '',
    };
  } catch (_e) {
    return {
      kind: 'generic',
      severity: 'error', retryable: false,
      message: 'Unknown error',
      hint: '', detail: '', model: '', context: '', source: 'frontend-unknown', raw: '',
    };
  }
}

/* Guarded t(): this module is bundled AFTER i18n.js in production so t() is
 * always present (→ zh, the primary UI). Several Node extraction-and-eval
 * harnesses eval this file standalone; fall back to the key so t() never
 * throws ReferenceError there. Never English. */
function _envT(key, params) {
  return (typeof t === 'function') ? t(key, params) : key;
}

/* Is this envelope RECOVERABLE by the offline-recovery path?
 *
 * ONLY ``server_offline`` qualifies. That kind is stamped by the frontend
 * health-check / poll circuit breaker (`_forceFinishDeadStream`) AFTER a task
 * was already running server-side — so its finishReason becomes
 * ``server_offline`` and `_recoverOfflineConversations` can re-fetch the conv
 * and adopt the server's completed result.
 *
 * ``network`` is deliberately EXCLUDED: it is stamped at ``context:'chat-start'``
 * when the POST that STARTS the turn fails — no task ever ran, finishReason is
 * never ``server_offline``, and a Recover button would scan, find nothing, and
 * no-op while telling the user "your result may be saved". That is the exact
 * false-hope we refuse. ``premature_close``/``abnormal_stop`` are UPSTREAM
 * (server↔gateway) failures emitted only after server-side retries are
 * exhausted — there is no saved result either; their correct action is Retry. */
function _envIsRecoverable(env) {
  return !!env && env.kind === 'server_offline';
}

function renderErrorEnvelope(err) {
  const env = normalizeErrorEnvelope(err);
  if (!env) return '';
  const sev = env.severity === 'error' ? 'error' : 'warning';
  const _recoverable = _envIsRecoverable(env);
  /* For the recoverable connection-drop case, override the "Server offline"
   * jargon title + (often empty) hint with truthful, actionable copy: the
   * result is likely safe on the server, don't regenerate, click Recover. */
  const kindLabel = _recoverable
    ? _envT('err.conn.title')
    : (ERROR_KIND_LABELS[env.kind] || env.kind || 'Error');
  const detail = env.detail || env.raw || '';
  const detailBlock = detail
    ? `<div class="error-block-detail" title="${escapeHtml(detail)}">${escapeHtml(detail.length > 220 ? detail.slice(0, 220) + '…' : detail)}</div>`
    : '';
  const hintText = _recoverable ? _envT('err.conn.hint') : env.hint;
  const hintBlock = hintText
    ? `<div class="error-block-hint">${escapeHtml(hintText)}</div>`
    : '';
  const ctx = env.context
    ? `<span class="error-block-ctx">[${escapeHtml(env.context)}]</span>`
    : '';
  /* Inline Recover button — reuses the existing offline-recovery path (no
   * second mechanism): it reconnects and adopts the server's completed
   * content, then clears the stale error. NOT a regenerate. SVG glyph per
   * §3.4 (never emoji). */
  const recoverBtn = _recoverable
    ? `<div class="error-block-actions" style="margin-top:10px">`
      + `<button class="error-block-recover-btn" type="button"`
      + ` title="${escapeHtml(_envT('err.conn.recoverTip'))}"`
      + ` onclick="_recoverOfflineConversations('manual_button')"`
      + ` style="display:inline-flex;align-items:center;gap:6px;padding:5px 12px;`
      + `font-size:12px;font-weight:600;cursor:pointer;color:inherit;`
      + `background:rgba(245,158,11,0.14);border:1px solid currentColor;`
      + `border-radius:6px;line-height:1.2">`
      + `${(typeof Icon === 'function') ? Icon('refresh', 12) : ''}<span>${escapeHtml(_envT('err.conn.recover'))}</span></button>`
      + `</div>`
    : '';
  return (
    `<div class="error-block error-block--${escapeHtml(sev)} error-block--kind-${escapeHtml(env.kind)}" data-error-kind="${escapeHtml(env.kind)}">` +
      `<div class="error-block-title"><span class="error-block-kind">${escapeHtml(kindLabel)}</span>${ctx}</div>` +
      `<div class="error-block-message">${escapeHtml(env.message)}</div>` +
      hintBlock +
      detailBlock +
      recoverBtn +
    `</div>`
  );
}

function errorEnvelopeKind(err) {
  const env = normalizeErrorEnvelope(err);
  return env ? env.kind : '';
}

function errorEnvelopeMessage(err) {
  const env = normalizeErrorEnvelope(err);
  return env ? env.message : '';
}

window.renderErrorEnvelope = renderErrorEnvelope;
window.normalizeErrorEnvelope = normalizeErrorEnvelope;
window.errorEnvelopeKind = errorEnvelopeKind;
window.errorEnvelopeMessage = errorEnvelopeMessage;
window.isErrorEnvelope = isErrorEnvelope;
