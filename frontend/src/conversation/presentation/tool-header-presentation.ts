/**
 * Tool-card header policy over immutable projected rounds.
 *
 * Entry point: createToolHeaderPresentation. Backend title/meta producers stay
 * separate; this owner selects visible copy and badges, including old saved
 * batch counts. It never rewrites history, parses execution output, or owns
 * settlement. Dependencies: shared escaping, translator, tool predicates, and the
 * existing write-gate refusal port. No DOM, browser globals, or retained state.
 */
import { escapeHtmlText } from '../../html-safety';

import type { Translator } from '../../i18n';

import { renderToolArtifactHeaderHtml } from './tool-artifact-presentation';
import { renderRepoMapHeaderHtml } from './tool-repo-map-presentation';
import { isBrowserToolRound, isProjectToolRound } from './tool-round-presentation';
import type { WriteGateRefusalPresentation } from './write-gate-refusal';

type UnknownRecord = Readonly<Record<string, unknown>>;

export type ToolHeaderPresentation = Readonly<{
  queryText(round: unknown): string;
  artifactHeaderHtml(round: unknown): string;
  repoMapHeaderHtml(round: unknown): string;
  renderBadgeHtml(round: unknown, metadata: unknown): string;
}>;

export type ToolHeaderPresentationDependencies = Readonly<{

  translate: Translator;
  writeGateRefusal: Pick<
    WriteGateRefusalPresentation, 'resolveRefusal' | 'renderBadgeHtml'
  >;
}>;

const EMPTY_RECORD: UnknownRecord = Object.freeze({});
export const TOOL_HEADER_PRESENTATION_LIMITS = Object.freeze({
  normalizationUnits: 8192,
  argumentsUnits: 120_000,
  edits: 128,
  badgeUnits: 2000,
});
const BATCH_EDIT_PREFIXES: Readonly<Record<string, string>> = Object.freeze({
  edit_file: 'Edit',
  apply_diffs: 'Patch',
  insert_contents: 'Insert into',
});
const SINGLE_WRITE_NAMES = new Set(['write_file', 'apply_diff', 'insert_content']);
const COUNT_BADGE = /^(\d+)(?:\/(\d+))? (?:edits|insertions|inserted)$/;

function record(value: unknown): UnknownRecord {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as UnknownRecord : EMPTY_RECORD;
}

function text(value: unknown): string {
  return typeof value === 'string' ? value : '';
}

function positiveNumber(value: unknown): number {
  return typeof value === 'number' && Number.isFinite(value) && value > 0
    ? value : 0;
}

function batchPrefix(name: string): string {
  return Object.prototype.hasOwnProperty.call(BATCH_EDIT_PREFIXES, name)
    ? BATCH_EDIT_PREFIXES[name] : '';
}

function firstMetadata(round: UnknownRecord): UnknownRecord {
  return Array.isArray(round.results) ? record(round.results[0]) : EMPTY_RECORD;
}

/** Oversized/malformed historical inputs remain verbatim, not partly rewritten. */
function historicalEdits(round: UnknownRecord): readonly unknown[] | null {
  let args = round.toolArgs;
  if (typeof args === 'string') {
    if (args.length > TOOL_HEADER_PRESENTATION_LIMITS.argumentsUnits) return null;
    try { args = JSON.parse(args) as unknown; } catch { args = null; }
  }
  const edits = record(args).edits;
  const candidate = Array.isArray(edits)
    ? edits : firstMetadata(round).editSummaries;
  return Array.isArray(candidate) && candidate.length > 0
    && candidate.length <= TOOL_HEADER_PRESENTATION_LIMITS.edits
    ? candidate : null;
}

/** Recognize the old producer's complete heading, never regex-edit a user path. */
function queryText(roundValue: unknown): string {
  const round = record(roundValue);
  const query = text(round.query);
  const name = text(round.toolName);
  const prefix = batchPrefix(name);
  if (!prefix || query.length > TOOL_HEADER_PRESENTATION_LIMITS.normalizationUnits
    || !/ \(\d+ (?:edits|insertions)\)(?: — |$)/.test(query)) return query;

  const edits = historicalEdits(round);
  if (!edits) return query;
  const paths = new Set<string>();
  let pathUnits = 0;
  for (const edit of edits) {
    const path = text(record(edit).path);
    pathUnits += path.length;
    if (!path || pathUnits > TOOL_HEADER_PRESENTATION_LIMITS.normalizationUnits) {
      return query;
    }
    paths.add(path);
  }
  // This is a closed legacy signature, not a new title builder. Unrecognized
  // labels (localized, partial, or custom) are deliberately left untouched.
  const target = paths.size === 1 || (name !== 'edit_file' && paths.size <= 3)
    ? [...paths].join(', ') : `${paths.size} files`;
  const heading = `${prefix} ${target}`;
  const unit = name === 'insert_contents' ? 'insertions' : 'edits';
  const oldHeading = `${heading} (${edits.length} ${unit})`;
  return query === oldHeading || query.startsWith(`${oldHeading} — `)
    ? heading + query.slice(oldHeading.length) : query;
}

function badgeHtml(label: string, className: string, title = ''): string {
  const limit = TOOL_HEADER_PRESENTATION_LIMITS.badgeUnits;
  const shown = label.length > limit ? `${label.slice(0, limit)}…` : label;
  const titleAttribute = title ? ` title="${escapeHtmlText(title)}"` : '';
  return `<span class="ptool-badge ${className}"${titleAttribute}>${
    escapeHtmlText(shown)
  }</span>`;
}

export function createToolHeaderPresentation(
  dependencies: ToolHeaderPresentationDependencies,
): ToolHeaderPresentation {
  const { translate, writeGateRefusal } = dependencies;

  function renderBadgeHtml(roundValue: unknown, metadataValue: unknown): string {
    const round = record(roundValue);
    const meta = record(metadataValue);
    const name = text(round.toolName);
    const batch = Boolean(batchPrefix(name));
    const write = batch || SINGLE_WRITE_NAMES.has(name);
    // Refusal facts outrank cosmetic suppression, even if an older producer
    // omitted a badge or left a counted badge alongside structured refusal.
    const refusal = writeGateRefusal.resolveRefusal(round, meta);
    if (refusal) return writeGateRefusal.renderBadgeHtml(refusal);

    if (round.status === 'error'
      || (round.status === 'failed' && (name === 'read_tool_artifact' || name === 'search_tool_artifact'))
      || (name === 'write_file' && meta.writeOk === false && !text(meta.badge))) {
      return badgeHtml(translate('tool.failed'), 'ptool-badge-err');
    }

    const label = text(meta.badge);
    const counted = batch && label.length <= TOOL_HEADER_PRESENTATION_LIMITS.badgeUnits
      ? COUNT_BADGE.exec(label) : null;
    if (label && !counted) {
      const className = meta.awaitTimedOut ? 'ptool-badge-warn'
        : write ? (meta.writeOk === false ? 'ptool-badge-err' : 'ptool-badge-ok')
          : meta.memoryOk === true ? 'ptool-badge-ok' : 'ptool-badge-info';
      return badgeHtml(label, className);
    }
    if (batch) {
      const summaries = Array.isArray(meta.editSummaries) ? meta.editSummaries : [];
      const failures = summaries.slice(0, TOOL_HEADER_PRESENTATION_LIMITS.edits)
        .filter((summary) => record(summary).status === 'fail');
      const failed = meta.writeOk === false || meta.type === 'error'
        || failures.length > 0
        || Boolean(counted?.[2] && Number(counted[1]) < Number(counted[2]));
      // The result presenter puts a detailed failure in the collapsed header
      // only for a rendered batch card. Sparse history still needs a verdict.
      const hasInlineFailure = (name === 'edit_file' || summaries.length > 1)
        && failures.some((summary) => text(record(summary).detail)
          .slice(0, TOOL_HEADER_PRESENTATION_LIMITS.normalizationUnits).trim());
      return failed && !hasInlineFailure
        ? badgeHtml(translate('tool.failed'), 'ptool-badge-err') : '';
    }

    // Only batch edits suppress fallback telemetry. Single writes and every
    // other tool keep the established explicit -> tokens -> chars precedence.
    const tokens = positiveNumber(round.toolTokens);
    const chars = positiveNumber(meta.fetchedChars);
    if (tokens) {
      const label = tokens >= 1000
        ? `${(tokens / 1000).toFixed(tokens >= 10000 ? 0 : 1)}k tok` : `${tokens} tok`;
      const charsTitle = chars ? ` (${chars.toLocaleString()} chars)` : '';
      return badgeHtml(label, 'ptool-badge-info',
        `Tokens consumed by this tool result${charsTitle}`);
    }
    if (chars) {
      return badgeHtml(chars > 1000 ? `${Math.round(chars / 1000)}k chars` : `${chars} chars`,
        'ptool-badge-info');
    }
    const results = Array.isArray(round.results) ? round.results : [];
    if (!isProjectToolRound(round) && !isBrowserToolRound(round) && !results.length) {
      const elapsed = text(round._elapsed);
      return badgeHtml(`done${elapsed ? ` · ${elapsed}` : ''}`, 'ptool-badge-ok');
    }
    return '';
  }

  return Object.freeze({
    queryText,
    artifactHeaderHtml: (round: unknown) => renderToolArtifactHeaderHtml(round, translate),
    repoMapHeaderHtml: (round: unknown) => renderRepoMapHeaderHtml(round, translate),
    renderBadgeHtml,
  });
}
