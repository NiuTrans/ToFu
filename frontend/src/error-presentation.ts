/**
 * Typed error-envelope presentation policy.
 *
 * Responsibility: localize normalized API errors, repair legacy mojibake,
 * render safe error cards, and format bounded model-fallback causes.
 * Entry point: `createErrorEnvelopePresentation`; pure helpers are exported
 * for behavioral contracts. Dependencies: typed error normalization, the
 * shared HTML-safety owner, and injected translation/icon ports.
 */

import { normalizeErrorEnvelope, type ErrorEnvelope } from './api/errors';
import { escapeHtml } from './html-safety';
import type { Translator } from './i18n';

export const ERROR_KIND_LABELS: Readonly<Record<string, string>> = Object.freeze({
  quota: 'Quota exhausted',
  ratelimit: 'Rate limited',
  permission: 'Permission denied',
  no_slot: 'No keys available',
  dispatch_exhausted: 'All keys exhausted',
  timeout: 'Timed out',
  network: 'Network error',
  endpoint_unreachable: 'Endpoint unreachable',
  content_filter: 'Content filter',
  invalid_image: 'Image rejected',
  prompt_too_long: 'Prompt too long',
  stream_only: 'Stream-only model',
  model_limit: 'Model limit',
  tool_rounds_exhausted: 'Tool budget',
  tool_timeout: 'Tool timeout',
  tool_loop: 'Tool-call loop',
  premature_close: 'Stream cut off',
  abnormal_stop: 'Abnormal stop',
  aborted: 'Stopped',
  server_offline: 'Server offline',
  server_busy: 'Server busy',
  task_start_failed: 'Generation did not start',
  internal: 'Internal error',
  generic: 'Error',
  bad_request: 'Bad request',
  content_refused: 'Quality check failed',
  upstream_error: 'Upstream error',
  worker_lost: 'Worker lost',
  budget_exceeded: 'Budget exceeded',
  tool_not_available: 'Tool not available',
  tool_call_rejected: 'Tool call blocked',
});

export const FALLBACK_DETAIL_MAX = 160;

type DynamicTranslator = (
  key: string,
  params?: Readonly<Record<string, unknown>>,
) => string;

export interface ErrorEnvelopePresentationPorts {
  translate: Translator;
  iconHtml?: (name: string, size?: number) => string;
}

export interface FallbackCauseParts {
  kind: string;
  kindLabel: string;
  detail: string;
  shown: string;
  hasCause: boolean;
}

export interface ErrorEnvelopePresentation {
  renderErrorEnvelope(error: unknown): string;
  errorEnvelopeKind(error: unknown): string;
  errorEnvelopeKindLabel(error: unknown): string;
  errorEnvelopeMessage(error: unknown): string;
  fallbackKindLabel(kind: unknown): string;
  fallbackCauseParts(message: unknown): FallbackCauseParts;
}

/** Repair UTF-8 bytes that old clients decoded as latin-1/cp1252. */
export function repairErrorMojibake(text: string): string {
  if (!text) return text;
  let suspect = false;
  for (let index = 0; index < text.length; index += 1) {
    const code = text.charCodeAt(index);
    if ((code >= 0x80 && code <= 0xff) || code === 0x201a) {
      suspect = true;
      break;
    }
  }
  if (!suspect) return text;

  const bytes: number[] = [];
  for (let index = 0; index < text.length; index += 1) {
    const code = text.charCodeAt(index);
    if (code <= 0xff) {
      bytes.push(code);
      continue;
    }
    if (code === 0x201a) {
      bytes.push(0x82);
      continue;
    }
    return text;
  }

  let repaired: string;
  try {
    repaired = new TextDecoder('utf-8', { fatal: true }).decode(
      new Uint8Array(bytes),
    );
  } catch {
    return text;
  }
  const hasCjk = (value: string): boolean => /[\u4e00-\u9fff]/.test(value);
  return hasCjk(repaired) && !hasCjk(text) ? repaired : text;
}

/** Collapse an upstream HTML error page into its distinct visible text. */
export function distillFallbackDetail(detail: string): string {
  const firstTag = detail.indexOf('<');
  if (firstTag === -1 || !/<\/?[a-zA-Z][^>]*>/.test(detail)) return detail;
  const prefix = detail.slice(0, firstTag).trim();
  const stripped = detail.slice(firstTag)
    .replace(/<(script|style)\b[^>]*>[\s\S]*?<\/\1\s*>/gi, ' ')
    .replace(/<[^>]*>/g, '\u0001');
  const seen: string[] = [];
  for (const piece of stripped.split('\u0001')) {
    const compact = piece.replace(/\s+/g, ' ').trim();
    if (compact && !seen.includes(compact)) seen.push(compact);
  }
  const body = seen.join(' \u00b7 ');
  if (!body) return prefix || detail;
  return prefix ? `${prefix} ${body}` : body;
}

/**
 * Only a dropped browser/server connection can adopt a result that may have
 * completed remotely. Startup network failures and exhausted upstream streams
 * have no saved result, so offering Recover for them would promise a no-op.
 */
function isRecoverable(error: ErrorEnvelope | null): boolean {
  return error?.kind === 'server_offline';
}

/** Create presentation functions bound to the current i18n and icon owners. */
export function createErrorEnvelopePresentation(
  ports: ErrorEnvelopePresentationPorts,
): ErrorEnvelopePresentation {
  // titleKey/hintKey and extension kinds arrive over the wire, outside the
  // generated I18nKey union. Keep that dynamic probe at one local seam; calls
  // to fixed product keys below continue through the checked Translator port.
  const translateDynamic = ports.translate as unknown as DynamicTranslator;

  const resolveI18n = (
    key: unknown,
    params?: Readonly<Record<string, unknown>>,
  ): string | null => {
    if (typeof key !== 'string' || !key) return null;
    const text = translateDynamic(key, params);
    return text === key ? null : text;
  };

  const localizedTitle = (error: ErrorEnvelope): string | null => {
    const base = resolveI18n(error.titleKey);
    if (base == null) return null;
    const suffix = error.model
      ? resolveI18n('err.k._modelSuffix', { model: error.model }) || ''
      : '';
    return base + suffix;
  };

  const localizedHint = (error: ErrorEnvelope): string | null => {
    const body = resolveI18n(error.hintKey);
    if (body == null) return null;
    if (!body) return '';
    const heading = resolveI18n('err.k._howToFix') || 'How to fix:';
    return `${heading}\n${body}`;
  };

  const fallbackKindLabel = (kindValue: unknown): string => {
    const kind = typeof kindValue === 'string' ? kindValue : '';
    if (!kind) return '';
    return resolveI18n(`err.k.${kind}.chip`)
      || ERROR_KIND_LABELS[kind]
      || kind;
  };

  const kindLabel = (error: ErrorEnvelope | null): string => {
    if (!error) return '';
    if (isRecoverable(error)) return ports.translate('err.conn.title');
    return fallbackKindLabel(error.kind) || 'Error';
  };

  const errorEnvelopeKindLabel = (value: unknown): string => (
    kindLabel(normalizeErrorEnvelope(value))
  );

  const errorEnvelopeMessage = (value: unknown): string => {
    const error = normalizeErrorEnvelope(value);
    if (!error) return '';
    return localizedTitle(error) ?? repairErrorMojibake(error.message);
  };

  const errorEnvelopeKind = (value: unknown): string => (
    normalizeErrorEnvelope(value)?.kind ?? ''
  );

  const renderErrorEnvelope = (value: unknown): string => {
    const error = normalizeErrorEnvelope(value);
    if (!error) return '';
    const severity = error.severity === 'error' ? 'error' : 'warning';
    const recoverable = isRecoverable(error);
    const label = kindLabel(error);
    const detail = repairErrorMojibake(
      error.detail || (typeof error.raw === 'string' ? error.raw : ''),
    );
    const detailBlock = detail
      ? `<div class="error-block-detail" title="${escapeHtml(detail)}">${escapeHtml(
        detail.length > 220 ? `${detail.slice(0, 220)}…` : detail,
      )}</div>`
      : '';
    const translatedTitle = localizedTitle(error);
    const translatedHint = localizedHint(error);
    const hintText = recoverable
      ? ports.translate('err.conn.hint')
      : translatedHint ?? error.hint;
    const hintBlock = hintText
      ? `<div class="error-block-hint">${escapeHtml(hintText)}</div>`
      : '';
    const context = error.context
      ? `<span class="error-block-ctx">[${escapeHtml(error.context)}]</span>`
      : '';
    const recoverButton = recoverable
      ? '<div class="error-block-actions" style="margin-top:10px">'
        + '<button class="error-block-recover-btn" type="button"'
        + ` title="${escapeHtml(ports.translate('err.conn.recoverTip'))}"`
        + ' data-tofu-action="_recoverOfflineConversations(\'manual_button\')"'
        + ' style="display:inline-flex;align-items:center;gap:6px;padding:5px 12px;'
        + 'font-size:12px;font-weight:600;cursor:pointer;color:inherit;'
        + 'background:rgba(245,158,11,0.14);border:1px solid currentColor;'
        + 'border-radius:6px;line-height:1.2">'
        + `${ports.iconHtml?.('refresh', 12) ?? ''}<span>${escapeHtml(
          ports.translate('err.conn.recover'),
        )}</span></button></div>`
      : '';
    const message = translatedTitle ?? repairErrorMojibake(error.message);
    return `<div class="error-block error-block--${escapeHtml(severity)}`
      + ` error-block--kind-${escapeHtml(error.kind)}" data-error-kind="${escapeHtml(error.kind)}">`
      + `<div class="error-block-title"><span class="error-block-kind">${escapeHtml(label)}</span>${context}</div>`
      + `<div class="error-block-message">${escapeHtml(message)}</div>`
      + hintBlock + detailBlock + recoverButton + '</div>';
  };

  const fallbackCauseParts = (value: unknown): FallbackCauseParts => {
    const message = value && typeof value === 'object'
      ? value as Record<string, unknown>
      : {};
    const kind = typeof message.fallbackKind === 'string'
      ? message.fallbackKind : '';
    let detail = String(message.fallbackReason || '');
    if (kind && detail.startsWith(`${kind}:`)) {
      detail = detail.slice(kind.length + 1);
    }
    detail = detail.replace(/\s+/g, ' ').trim();
    const distilled = distillFallbackDetail(detail);
    const shown = distilled.length > FALLBACK_DETAIL_MAX
      ? `${distilled.slice(0, FALLBACK_DETAIL_MAX)}…`
      : distilled;
    const label = fallbackKindLabel(kind);
    return {
      kind,
      kindLabel: label,
      detail,
      shown,
      hasCause: Boolean(label || shown),
    };
  };

  return Object.freeze({
    renderErrorEnvelope,
    errorEnvelopeKind,
    errorEnvelopeKindLabel,
    errorEnvelopeMessage,
    fallbackKindLabel,
    fallbackCauseParts,
  });
}
