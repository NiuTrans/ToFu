/**
 * Pure presentation policy for write-gate refusals on projected tool rounds.
 *
 * Responsibility: normalize structured and legacy refusal facts, localize the
 * terminal warning badge, and render the explanatory notice shown inside
 * write/edit cards. Entry points are returned by
 * `createWriteGateRefusalPresentation`. Dependencies: generated i18n, the
 * trusted shared-icon port, and shared HTML escaping; no DOM or browser state.
 */

import { escapeHtmlText } from '../../html-safety';
import type { Translator } from '../../i18n';

type UnknownRecord = Readonly<Record<string, unknown>>;
type IconHtml = (name: string, size?: number | string) => string;

type KnownWriteGateRefusalKind =
  | 'stale'
  | 'read_first'
  | 'partial_stale'
  | 'partial_read_first'
  | 'content_ref';

export type WriteGateRefusalInfo = Readonly<{
  kind: string;
  paths: readonly string[];
  skipped: number;
  proceeded: number;
}>;

export type WriteGateRefusalPresentation = Readonly<{
  resolveRefusal(
    round: unknown,
    resultMetadata: unknown,
  ): WriteGateRefusalInfo | null;
  renderBadgeHtml(refusal: WriteGateRefusalInfo | null): string;
  renderNoticeHtml(refusal: WriteGateRefusalInfo | null): string;
}>;

export type WriteGateRefusalPresentationDependencies = Readonly<{
  translate: Translator;
  iconHtml: IconHtml;
}>;

const EMPTY_RECORD: UnknownRecord = Object.freeze({});
const PATHS_PLACEHOLDER = '\u0000TOFU_WRITE_GATE_PATHS\u0000';

const WRITE_GATE_TOOL_NAMES = new Set([
  'write_file',
  'edit_file',
  'apply_diff',
  'apply_diffs',
  'insert_content',
  'insert_contents',
]);

const LEGACY_BADGE_KINDS: Readonly<Record<string, KnownWriteGateRefusalKind>> =
  Object.freeze({
    stale: 'stale',
    'read first': 'read_first',
    'partial: stale': 'partial_stale',
    'partial: read first': 'partial_read_first',
    'ref failed': 'content_ref',
  });

function record(value: unknown): UnknownRecord {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as UnknownRecord
    : EMPTY_RECORD;
}

function stringField(value: unknown, field: string): string {
  const candidate = record(value)[field];
  return typeof candidate === 'string' ? candidate : '';
}

function nonNegativeCount(value: unknown): number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value > 0
    ? value
    : 0;
}

function freezeRefusal(
  kind: string,
  paths: readonly string[] = [],
  skipped = 0,
  proceeded = 0,
): WriteGateRefusalInfo {
  return Object.freeze({
    kind,
    paths: Object.freeze([...paths]),
    skipped,
    proceeded,
  });
}

export function createWriteGateRefusalPresentation(
  dependencies: WriteGateRefusalPresentationDependencies,
): WriteGateRefusalPresentation {
  const { translate, iconHtml } = dependencies;

  function resolveRefusal(
    roundValue: unknown,
    metadataValue: unknown,
  ): WriteGateRefusalInfo | null {
    const round = record(roundValue);
    const metadata = record(metadataValue);
    if (!WRITE_GATE_TOOL_NAMES.has(stringField(round, 'toolName'))) return null;

    const structured = record(metadata.refusal);
    const structuredKind = stringField(structured, 'kind');
    if (structuredKind) {
      const paths = Array.isArray(structured.paths)
        ? structured.paths.filter((path): path is string => (
          typeof path === 'string' && path.length > 0
        ))
        : [];
      return freezeRefusal(
        structuredKind,
        paths,
        nonNegativeCount(structured.skipped),
        nonNegativeCount(structured.proceeded),
      );
    }

    const legacyKind = LEGACY_BADGE_KINDS[stringField(metadata, 'badge')];
    return legacyKind ? freezeRefusal(legacyKind) : null;
  }

  function badgeLabel(kind: string): string {
    switch (kind) {
      case 'stale':
        return translate('tool.gateStaleBadge');
      case 'read_first':
        return translate('tool.gateReadFirstBadge');
      case 'partial_stale':
        return translate('tool.gatePartialStaleBadge');
      case 'partial_read_first':
        return translate('tool.gatePartialReadFirstBadge');
      case 'content_ref':
        return translate('tool.gateContentRefBadge');
      default:
        return kind;
    }
  }

  function refusalTitle(refusal: WriteGateRefusalInfo): string {
    switch (refusal.kind) {
      case 'stale':
        return translate('tool.gateStaleTitle');
      case 'read_first':
        return translate('tool.gateReadFirstTitle');
      case 'partial_stale':
        return translate('tool.gatePartialStaleTitle', {
          skipped: refusal.skipped,
        });
      case 'partial_read_first':
        return translate('tool.gatePartialReadFirstTitle', {
          skipped: refusal.skipped,
        });
      case 'content_ref':
        return translate('tool.gateContentRefTitle');
      default:
        return '';
    }
  }

  function refusalText(refusal: WriteGateRefusalInfo): string {
    switch (refusal.kind) {
      case 'stale':
        return translate('tool.gateStaleText', {
          paths: PATHS_PLACEHOLDER,
        });
      case 'read_first':
        return translate('tool.gateReadFirstText', {
          paths: PATHS_PLACEHOLDER,
        });
      case 'partial_stale':
        return translate('tool.gatePartialStaleText', {
          paths: PATHS_PLACEHOLDER,
          skipped: refusal.skipped,
          proceeded: refusal.proceeded,
        });
      case 'partial_read_first':
        return translate('tool.gatePartialReadFirstText', {
          paths: PATHS_PLACEHOLDER,
          skipped: refusal.skipped,
          proceeded: refusal.proceeded,
        });
      case 'content_ref':
        return translate('tool.gateContentRefText');
      default:
        return '';
    }
  }

  function renderBadgeHtml(refusal: WriteGateRefusalInfo | null): string {
    if (!refusal) return '';
    const title = refusalTitle(refusal);
    const titleAttribute = title
      ? ` title="${escapeHtmlText(title)}"`
      : '';
    return `<span class="ptool-badge ptool-badge-warn ptool-badge-gate"${
      titleAttribute
    }>${escapeHtmlText(badgeLabel(refusal.kind))}</span>`;
  }

  function renderNoticeHtml(refusal: WriteGateRefusalInfo | null): string {
    if (!refusal) return '';
    const title = refusalTitle(refusal);
    const text = refusalText(refusal);
    if (!title || !text) return '';

    const pathsHtml = refusal.paths.map((path) => {
      const basename = path.split('/').filter(Boolean).pop() || path;
      return `<code class="ptool-gate-note-path" title="${escapeHtmlText(path)}">${
        escapeHtmlText(basename)
      }</code>`;
    }).join(', ');
    const pathReplacement = pathsHtml
      || escapeHtmlText(translate('tool.gateTargetGeneric'));
    const textHtml = escapeHtmlText(text)
      .split(escapeHtmlText(PATHS_PLACEHOLDER))
      .join(pathReplacement);

    return '\n             <div class="ptool-gate-note">'
      + `<span class="ptool-gate-note-icon icon-box">${iconHtml('shield', 13)}</span>`
      + '<div class="ptool-gate-note-body">'
      + `<div class="ptool-gate-note-title">${escapeHtmlText(title)}</div>`
      + `<div class="ptool-gate-note-text">${textHtml}</div>`
      + '</div></div>';
  }

  return Object.freeze({
    resolveRefusal,
    renderBadgeHtml,
    renderNoticeHtml,
  });
}
