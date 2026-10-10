/**
 * Pure HTML presentation for settled tool results.
 *
 * Responsibility: render compaction visibility, write/edit diffs, batch-edit
 * summaries, and the bounded generic result viewer from projected tool-round
 * values. Entry points are returned by `createToolResultPresentation`.
 * Dependencies: generated i18n, shared HTML escaping, and the typed write-gate
 * refusal presenter. The caller supplies already-rendered trusted header slots;
 * this owner reads no DOM, browser global, or mutable runtime state.
 */

import { escapeHtmlText } from '../../html-safety';

import { renderToolArtifactHeaderHtml, renderToolArtifactRequestHtml, toolArtifactFailureText } from './tool-artifact-presentation';
import { renderRepoMapHeaderHtml } from './tool-repo-map-presentation';
import { resolveToolResultText, toolResultSurfacePolicy } from './tool-result-surfaces';
import type { Translator } from '../../i18n';
import {
  detectCodeLanguage,
  highlightCodeToHtml,
} from './code-highlight';
import type {
  WriteGateRefusalPresentation,
} from './write-gate-refusal';

type UnknownRecord = Readonly<Record<string, unknown>>;

export type ToolResultHeaderHtml = Readonly<{
  iconHtml: string;
  queryHtml: string;
  rootPillHtml: string;
  badgeHtml: string;
  repairedBadgeHtml?: string;
  rightControlsHtml?: string;
  toolDisplayLabel?: string;
}>;

export type ToolResultPresentation = Readonly<{
  renderCompactionLabelHtml(round: unknown): string;
  renderWriteResultHtml(
    round: unknown,
    resultMetadata: unknown,
    header: ToolResultHeaderHtml,
  ): string;
  renderGenericResultHtml(
    round: unknown,
    resultMetadata: unknown,
    header: ToolResultHeaderHtml,
  ): string;
}>;

export type ToolResultPresentationDependencies = Readonly<{
  translate: Translator;
  renderMarkdown?: (source: string) => string;
  writeGateRefusal: Pick<
    WriteGateRefusalPresentation,
    'resolveRefusal' | 'renderNoticeHtml'
  >;
}>;

const EMPTY_RECORD: UnknownRecord = Object.freeze({});
const RESULT_VIEW_MAX_CHARS = 120_000;
const COMPACTION_LAYER_EXPLANATIONS: Readonly<Record<string, string>> =
  Object.freeze({
    L0: 'Result exceeded its token budget — the model received a bounded preview plus re-read instructions; the full text never entered context.',
    L1: 'Aged out of the hot tail (60 most-recent tool calls) — replaced with a short marker on the next LLM call.',
    L3: 'Replaced by an LLM-generated summary in the transcript archive.',
  });
const OPERATION_PILL_KINDS: Readonly<Record<string, Readonly<{
  className: string;
  iconHtml: string;
}>>> = Object.freeze({
  replace: Object.freeze({
    className: 'ptool-op--replace',
    iconHtml: '<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M16 3l4 4-4 4"/><path d="M20 7H4"/><path d="M8 21l-4-4 4-4"/><path d="M4 17h16"/></svg>',
  }),
  insert_after: Object.freeze({
    className: 'ptool-op--insert',
    iconHtml: '<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="3" x2="12" y2="15"/><polyline points="6 9 12 15 18 9"/><line x1="4" y1="21" x2="20" y2="21"/></svg>',
  }),
  insert_before: Object.freeze({
    className: 'ptool-op--insert',
    iconHtml: '<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="21" x2="12" y2="9"/><polyline points="6 15 12 9 18 15"/><line x1="4" y1="3" x2="20" y2="3"/></svg>',
  }),
});

/* Batch-edit tools whose settled failure verdict renders as one compact
   outcome card instead of a raw log dump. */
const EDIT_FAILURE_TOOL_NAMES: ReadonlySet<string> = new Set([
  'edit_file',
  'apply_diff',
  'apply_diffs',
  'insert_content',
  'insert_contents',
]);

/* Mirror of the edit-failure taxonomy stamped server-side
   (lib/project_mod/write_tools/_ops.py EK_*): every edit-surface failure
   carries one of these kinds as a bracketed tag on its `[N] FAIL` line. The
   card renders the tag as a per-category chip so the repair move is visible
   at a glance; the full model-facing reason stays on the row tooltip and in
   the expanded detail block. Kinds are closed — an unrecognized bracket
   stays part of the message rather than minting a chip. */
const EDIT_ERROR_KINDS: Readonly<Record<string, Readonly<{
  className: string;
  explanation: string;
}>>> = Object.freeze({
  'match-not-found': Object.freeze({
    className: 'ptool-edit-kind--missing',
    explanation: 'Search/anchor text is absent from the file — re-read the file, then widen or fix the anchor.',
  }),
  'file-not-found': Object.freeze({
    className: 'ptool-edit-kind--missing',
    explanation: 'The target file does not exist — verify the path first.',
  }),
  'ambiguous-match': Object.freeze({
    className: 'ptool-edit-kind--input',
    explanation: 'The anchor matches more than one location — add surrounding lines or set replace_all.',
  }),
  'invalid-edit': Object.freeze({
    className: 'ptool-edit-kind--input',
    explanation: 'The edit entry itself is malformed (missing field or unknown operation).',
  }),
  'echo-content': Object.freeze({
    className: 'ptool-edit-kind--input',
    explanation: 'The inserted content merely repeats the anchor/context — supply the new text only.',
  }),
  'path-invalid': Object.freeze({
    className: 'ptool-edit-kind--system',
    explanation: 'The path was rejected (outside roots, readonly, …).',
  }),
  'read-failed': Object.freeze({
    className: 'ptool-edit-kind--system',
    explanation: 'The file exists but could not be read.',
  }),
  'write-failed': Object.freeze({
    className: 'ptool-edit-kind--system',
    explanation: 'The computed edit could not be persisted.',
  }),
  'mapping-failed': Object.freeze({
    className: 'ptool-edit-kind--system',
    explanation: 'A normalized match lost its line mapping.',
  }),
});

const EDIT_FAILURE_LIMITS = Object.freeze({
  rows: 30,
  tooltipChars: 4000,
  detailChars: 2000,
});

type EditOutcomeEntry = Readonly<{

  body?: string;
  index: number;
  ok: boolean;
  path: string;
  operation: string;
  kind: string;
  message: string;
}>;

/* A settled error round carries the sparse model projection
   (``{"status":"error","summary":"Applied 0/1 edits…"}``), never the internal
   envelope. Unwrap that one semantic field so outcome lines parse the same
   as the legacy plain-text form. Anything else stays byte-verbatim. */
function unwrapEditResultProjection(text: string): string {
  const trimmed = text.trim();
  if (!trimmed.startsWith('{') || trimmed.length > 200_000) return text;
  try {
    const parsed = JSON.parse(trimmed) as unknown;
    if (parsed !== null && typeof parsed === 'object' && !Array.isArray(parsed)) {
      const value = parsed as UnknownRecord;
      const status = typeof value.status === 'string' ? value.status : '';
      if (
        (status === 'error' || status === 'partial'
          || typeof value.contractVersion === 'string')
        && typeof value.summary === 'string'
      ) {
        return value.summary;
      }
    }
  } catch {
    // The prefix resembled JSON; verbatim display remains authoritative.
  }
  return text;
}

function knownEditErrorKind(value: string): string {
  return Object.prototype.hasOwnProperty.call(EDIT_ERROR_KINDS, value)
    ? value
    : '';
}

/** Split a leading ``[kind]`` tag off an outcome body; closed taxonomy. */
function splitEditErrorKind(rest: string): Readonly<{
  kind: string;
  message: string;
}> {
  const match = rest.match(/^\[([a-z][a-z0-9-]*)\]\s*:?\s*([\s\S]*)$/);
  if (match && knownEditErrorKind(match[1])) {
    return Object.freeze({ kind: match[1], message: match[2].trim() });
  }
  return Object.freeze({ kind: '', message: rest.trim() });
}

function parseEditOutcomeBody(body: string): Readonly<{
  path: string;
  operation: string;
  kind: string;
  message: string;
}> {
  // Kind-only, no path: ``[invalid-edit] Missing required field …``.
  if (body.startsWith('[')) {
    const tagged = splitEditErrorKind(body);
    if (tagged.kind) {
      return Object.freeze({
        path: '', operation: '', kind: tagged.kind, message: tagged.message,
      });
    }
  }
  // edit_file shape: ``path [operation] [kind]: message`` (kind optional).
  const operationMatch = body.match(
    /^(\S+)\s+\[(replace|insert_after|insert_before)\]\s*([\s\S]*)$/,
  );
  if (operationMatch) {
    const tagged = splitEditErrorKind(operationMatch[3]);
    return Object.freeze({
      path: operationMatch[1],
      operation: operationMatch[2],
      kind: tagged.kind,
      message: tagged.message,
    });
  }
  // Legacy batch shape: ``path: message``. The token must look path-like —
  // a free-text gate refusal (``Refusing to write …``) stays whole.
  const legacyMatch = body.match(/^(\S+?):\s+([\s\S]*)$/);
  if (legacyMatch && /[./\\]/.test(legacyMatch[1])) {
    return Object.freeze({
      path: legacyMatch[1],
      operation: '',
      kind: '',
      message: legacyMatch[2].trim(),
    });
  }
  return Object.freeze({
    path: '', operation: '', kind: '', message: body.trim(),
  });
}

/* Parse the model-facing batch outcome text into per-edit entries. Lines
   that do not open a new ``[N] OK/FAIL`` marker continue the previous
   entry's message (multi-line hints such as the "Most similar block"
   excerpt ride the same failure). */
function parseEditOutcomeEntries(text: string): EditOutcomeEntry[] {
  const entries: Array<{

    body: string;
    index: number;
    ok: boolean;
    path: string;
    operation: string;
    kind: string;
    message: string;
  }> = [];
  for (const line of text.split('\n')) {
    const head = line.match(/^\[(\d+)\]\s+(OK|FAIL)\b\s*:?\s*([\s\S]*)$/);
    if (head) {
      const parsed = parseEditOutcomeBody(head[3]);
      entries.push({

        body: head[3],
        index: Number(head[1]),
        ok: head[2] === 'OK',
        path: parsed.path,
        operation: parsed.operation,
        kind: parsed.kind,
        message: parsed.message,
      });
    } else if (entries.length) {
      const last = entries[entries.length - 1];
      last.message = `${last.message}\n${line}`;

      last.body = `${last.body}\n${line}`;
    }
  }
  return entries.slice(0, EDIT_FAILURE_LIMITS.rows).map((entry) => {
    let message = entry.message.replace(/\s+$/, '');
    if (message.length > EDIT_FAILURE_LIMITS.tooltipChars) {
      message = `${message.slice(0, EDIT_FAILURE_LIMITS.tooltipChars)}…`;
    }
    const body = entry.body.replace(/\s+$/, '').slice(0, EDIT_FAILURE_LIMITS.tooltipChars);
    return Object.freeze({ ...entry, message, body });
  });
}

function record(value: unknown): UnknownRecord {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as UnknownRecord
    : EMPTY_RECORD;
}

/* Track B: the `paper_propose_edits` agent tool answers with a first-line
 * `proposal_id: <32hex>` marker followed by the model-authored summary. The
 * marker mints an interactive review card skeleton; the proposal-card owner
 * hydrates it (fetch + per-hunk accept/reject) once it mounts. */
export const PAPER_PROPOSAL_TOOL_NAME = 'paper_propose_edits';
const PAPER_PROPOSAL_ID_PATTERN = /proposal_id:\s*([0-9a-f]{32})\b/;
const PAPER_PROPOSAL_SUMMARY_MAX_CHARS = 2000;

/** Parse the proposal id off the tool result's first line; '' when absent. */
export function extractPaperProposalId(content: string): string {
  const firstLine = content.split('\n', 1)[0] ?? '';
  return PAPER_PROPOSAL_ID_PATTERN.exec(firstLine)?.[1] ?? '';
}

function owns(value: object, field: PropertyKey): boolean {
  return Object.prototype.hasOwnProperty.call(value, field);
}

/* await_agents: the generic viewer renders the settled JSON verbatim, and the
 * count badge (`done N/M`) only ever counts the AWAITED ids. A narrow
 * ids=[...] wait therefore read as the whole swarm state ("0/1 done" while 2
 * other agents ran). When the backend's self-describing fields are present
 * (`swarm_status`), derive a scope badge that names the awaited set and the
 * full in-flight roster so the header row is truthful on its own. Older
 * backends without the roster simply render no extra badge. */
type AwaitScope = Readonly<{ awaited: readonly string[]; running: number }>;

function _awaitScope(round: UnknownRecord): AwaitScope | null {
  if (stringField(round, 'toolName') !== 'await_agents') return null;
  const authoritative = authoritativeResultText(round);
  if (!authoritative) return null;
  const text = authoritative.text.trim();
  if (!text.startsWith('{') || text.length > 200_000) return null;
  let parsed: UnknownRecord;
  try { parsed = record(JSON.parse(text)); } catch { return null; }
  const roster = Array.isArray(parsed.swarm_status) ? parsed.swarm_status : null;
  if (!roster || !roster.length) return null;
  const running = roster.filter(
    (entry) => stringField(entry, 'status') === 'running').length;
  const awaited = Array.isArray(parsed.awaited_ids)
    ? parsed.awaited_ids.map((id) => stringField({ id }, 'id')).filter(Boolean)
    : [];
  return Object.freeze({ awaited, running });
}

function renderAwaitScopeBadgeHtml(
  round: UnknownRecord, translate: Translator,
): string {
  const scope = _awaitScope(round);
  if (!scope) return '';
  const label = scope.awaited.length
    ? translate('tool.awaitScope', {
      ids: scope.awaited.join(', '), count: scope.running })
    : translate('tool.awaitScopeAll', { count: scope.running });
  const title = translate('tool.awaitScopeTip');
  return `<span class="ptool-badge ptool-badge-info" title="${
    escapeHtmlText(title)}">${escapeHtmlText(label)}</span>`;
}

type AuthoritativeResultText = Readonly<{
  source: 'toolContent' | 'roundResult';
  text: string;
}>;

/**
 * Resolve only fields owned by the projected tool round itself.
 *
 * `round.results[0]` is presentation metadata. It may be a useful secondary
 * projection, but it is not allowed to replace the result the backend settled
 * for this call. Property presence is deliberate: an explicit empty/null
 * result remains authoritative and must not fall through to unrelated metadata.
 */
function authoritativeResultText(round: UnknownRecord): AuthoritativeResultText | null {
  return resolveToolResultText(round);
}

function stringField(value: unknown, field: string): string {
  const candidate = record(value)[field];
  return typeof candidate === 'string' ? candidate : '';
}

function positiveFiniteNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) && value > 0
    ? value
    : null;
}

function parseToolArguments(value: unknown): UnknownRecord | null {
  let parsed = value;
  if (typeof value === 'string') {
    try {
      parsed = JSON.parse(value) as unknown;
    } catch {
      return null;
    }
  }
  if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) {
    return null;
  }
  return parsed as UnknownRecord;
}

function formatTokenCount(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return '0';
  if (value >= 1e6) {
    return `${(value / 1e6).toFixed(value >= 1e7 ? 0 : 1)}M`;
  }
  if (value >= 1e3) {
    return `${(value / 1e3).toFixed(value >= 1e4 ? 0 : 1)}k`;
  }
  return String(value | 0);
}

function stripDuplicatePathPrefix(description: string, path: string): string {
  if (!description) return '';
  const match = description.match(/^([^\s:]{1,80}):\s+(.+)$/);
  if (!match || !path) return description;
  const prefix = match[1];
  const segments = path.split('/').filter(Boolean);
  const basename = segments[segments.length - 1] || '';
  const stem = basename.replace(/\.[^./]+$/, '');
  const candidates = new Set([path, basename, stem, ...segments]);
  return candidates.has(prefix) ? match[2] : description;
}

/** Render a bounded, git-style line diff without reading browser state.
 * Exported for the paper proposal review card, which renders the same
 * search/replace hunks through this exact diff presentation. */
export function renderLineDiffHtml(oldText: string, newText: string): string {
  const oldLines = oldText ? oldText.split('\n') : [];
  const newLines = newText ? newText.split('\n') : [];
  if (!oldLines.length && !newLines.length) return '';

  if (!newLines.length || (newLines.length === 1 && newLines[0] === '')) {
    const linesHtml = oldLines.map((line) => (
      '<div class="bdiff-line bdiff-del"><span class="bdiff-sign">-</span>'
      + `<code>${escapeHtmlText(line)}</code></div>`
    )).join('');
    return `<div class="bdiff-block">${linesHtml}</div>`;
  }
  if (!oldLines.length || (oldLines.length === 1 && oldLines[0] === '')) {
    const linesHtml = newLines.map((line) => (
      '<div class="bdiff-line bdiff-add"><span class="bdiff-sign">+</span>'
      + `<code>${escapeHtmlText(line)}</code></div>`
    )).join('');
    return `<div class="bdiff-block">${linesHtml}</div>`;
  }

  const oldCount = oldLines.length;
  const newCount = newLines.length;
  if (oldCount + newCount > 300) {
    const deletedHtml = oldLines.map((line) => (
      '<div class="bdiff-line bdiff-del"><span class="bdiff-sign">-</span>'
      + `<code>${escapeHtmlText(line)}</code></div>`
    )).join('');
    const addedHtml = newLines.map((line) => (
      '<div class="bdiff-line bdiff-add"><span class="bdiff-sign">+</span>'
      + `<code>${escapeHtmlText(line)}</code></div>`
    )).join('');
    return `<div class="bdiff-block">${deletedHtml}`
      + `<div class="bdiff-sep"></div>${addedHtml}</div>`;
  }

  const longestCommonSubsequence = Array.from(
    { length: oldCount + 1 },
    () => new Uint16Array(newCount + 1),
  );
  for (let oldIndex = 1; oldIndex <= oldCount; oldIndex += 1) {
    for (let newIndex = 1; newIndex <= newCount; newIndex += 1) {
      longestCommonSubsequence[oldIndex][newIndex] = (
        oldLines[oldIndex - 1] === newLines[newIndex - 1]
          ? longestCommonSubsequence[oldIndex - 1][newIndex - 1] + 1
          : Math.max(
            longestCommonSubsequence[oldIndex - 1][newIndex],
            longestCommonSubsequence[oldIndex][newIndex - 1],
          )
      );
    }
  }

  const operations: Array<Readonly<{
    type: 'ctx' | 'add' | 'del';
    text: string;
  }>> = [];
  let oldIndex = oldCount;
  let newIndex = newCount;
  while (oldIndex > 0 || newIndex > 0) {
    if (
      oldIndex > 0
      && newIndex > 0
      && oldLines[oldIndex - 1] === newLines[newIndex - 1]
    ) {
      operations.push({ type: 'ctx', text: oldLines[oldIndex - 1] });
      oldIndex -= 1;
      newIndex -= 1;
    } else if (
      newIndex > 0
      && (
        oldIndex === 0
        || longestCommonSubsequence[oldIndex][newIndex - 1]
          >= longestCommonSubsequence[oldIndex - 1][newIndex]
      )
    ) {
      operations.push({ type: 'add', text: newLines[newIndex - 1] });
      newIndex -= 1;
    } else {
      operations.push({ type: 'del', text: oldLines[oldIndex - 1] });
      oldIndex -= 1;
    }
  }
  operations.reverse();
  const linesHtml = operations.map((operation) => {
    const sign = operation.type === 'del'
      ? '-'
      : operation.type === 'add' ? '+' : ' ';
    return `<div class="bdiff-line bdiff-${operation.type}">`
      + `<span class="bdiff-sign">${sign}</span>`
      + `<code>${escapeHtmlText(operation.text)}</code></div>`;
  }).join('');
  return `<div class="bdiff-block">${linesHtml}</div>`;
}

function renderOperationPillHtml(operationName: string): string {
  const kind = OPERATION_PILL_KINDS[operationName];
  if (!kind) return '';
  return `<span class="ptool-op ${kind.className}" title="operation: ${
    operationName
  }">${kind.iconHtml}${operationName}</span>`;
}

function renderWriteCardHtml(
  round: UnknownRecord,
  header: ToolResultHeaderHtml,
  diffHtml: string,
  gateNoticeHtml: string,
  compactionLabelHtml: string,
  failureReasonHtml = '',
): string {
  return `<details class="ptool-batch-done-block" data-rn="${
    escapeHtmlText(round.roundNum)
  }">
             <summary class="ptool-line ptool-batch-done-header">
               <span class="ptool-icon">${header.iconHtml}</span>
               ${compactionLabelHtml}
               ${header.rootPillHtml}
               <span class="ptool-text">${header.queryHtml}</span>
               ${header.badgeHtml}
               ${failureReasonHtml}
             </summary>${gateNoticeHtml}
             <div class="ptool-batch-done-list">
               <div class="ptool-batch-done-single">${diffHtml}</div>
             </div>
           </details>`;
}

export function createToolResultPresentation(
  dependencies: ToolResultPresentationDependencies,
): ToolResultPresentation {
  const { translate, writeGateRefusal } = dependencies;

  function renderCompactionLabelHtml(roundValue: unknown): string {
    const round = record(roundValue);
    const rawLayer = stringField(round, 'compactionLayer').trim();
    if (!rawLayer) return '';
    const layerCssToken = rawLayer.toLowerCase().replace(/[^a-z0-9_-]/g, '-');
    const compactedFromChars = positiveFiniteNumber(round.compactedFromChars);
    const compactedToChars = positiveFiniteNumber(round.compactedToChars);
    // L0/unchanged stamp real pre/post token counts server-side; other
    // layers only carry char deltas, so keep the chars/4 estimate there.
    const useRealTokens = rawLayer === 'L0' || rawLayer === 'unchanged';
    const rawTokens = useRealTokens
      ? positiveFiniteNumber(round.rawToolTokens)
      : null;
    const fromTokens = rawTokens ?? (compactedFromChars
      ? Math.max(1, Math.round(compactedFromChars / 4))
      : null);
    const toTokens = positiveFiniteNumber(round.toolTokens) ?? (
      compactedToChars ? Math.max(1, Math.round(compactedToChars / 4)) : null);
    const reduction = fromTokens && toTokens
      ? `${formatTokenCount(fromTokens)}→${formatTokenCount(toTokens)}`
      : '';
    const explanation = COMPACTION_LAYER_EXPLANATIONS[rawLayer]
      || 'This tool result has been replaced by a placeholder.';
    const title = `Compacted (${rawLayer})${
      reduction ? ` — ${reduction} tokens` : ''
    }\n${explanation}`;
    return `<span class="ptool-compaction-label ptool-compaction-${
      layerCssToken
    }" title="${escapeHtmlText(title)}">`
      + `<span class="ptool-compaction-text">COMPACTED ${
        escapeHtmlText(rawLayer)
      }</span>`
      + (reduction
        ? `<span class="ptool-compaction-delta">${reduction}</span>`
        : '')
      + '</span>';
  }

  function writeGateNoticeHtml(
    round: UnknownRecord,
    resultMetadata: UnknownRecord,
  ): string {
    return writeGateRefusal.renderNoticeHtml(
      writeGateRefusal.resolveRefusal(round, resultMetadata),
    );
  }

  function renderSingleWriteResultHtml(
    round: UnknownRecord,
    resultMetadata: UnknownRecord,
    header: ToolResultHeaderHtml,
    compactionLabelHtml: string,
  ): string {
    if (stringField(round, 'toolName') !== 'write_file') return '';
    const result = authoritativeResultText(round);
    const failed = resultMetadata.writeOk === false
      || ['error', 'rejected', 'aborted', 'unanswerable'].includes(
        stringField(round, 'status'))
      || Boolean(result?.text.startsWith('Write failed:'));
    if (failed) {
      const reason = (result?.text || translate('tool.failed'))
        .slice(0, RESULT_VIEW_MAX_CHARS);
      return renderWriteCardHtml(
        round, header,
        `<div class="ptool-batch-fail-detail"><code>${escapeHtmlText(reason)}</code></div>`,
        writeGateNoticeHtml(round, resultMetadata), compactionLabelHtml,
        inlineFailureReasonHtml(reason),
      );
    }
    const toolArguments = parseToolArguments(round.toolArgs);
    const content = stringField(toolArguments, 'content');
    if (!content) return '';
    const diffHtml = renderLineDiffHtml('', content);
    if (!diffHtml) return '';
    return renderWriteCardHtml(
      round,
      header,
      diffHtml,
      writeGateNoticeHtml(round, resultMetadata),
      compactionLabelHtml,
    );
  }

  function renderSingleDiffResultHtml(
    round: UnknownRecord,
    resultMetadata: UnknownRecord,
    header: ToolResultHeaderHtml,
    compactionLabelHtml: string,
  ): string {
    if (Array.isArray(resultMetadata.editSummaries)) return '';
    const toolName = stringField(round, 'toolName');
    if (toolName !== 'apply_diff' && toolName !== 'insert_content') return '';
    const toolArguments = parseToolArguments(round.toolArgs);
    if (!toolArguments) return '';
    const search = stringField(toolArguments, 'search');
    const anchor = stringField(toolArguments, 'anchor');
    if (!search && !anchor) return '';
    const isInsert = !search && Boolean(anchor);
    const content = stringField(toolArguments, 'content');
    const position = stringField(toolArguments, 'position');
    const oldText = search || anchor;
    const newText = isInsert
      ? `${position === 'before' ? `${content}\n` : ''}${anchor}${
        position !== 'before' ? `\n${content}` : ''
      }`
      : stringField(toolArguments, 'replace');
    const diffHtml = renderLineDiffHtml(oldText, newText);
    if (!diffHtml) return '';
    return renderWriteCardHtml(
      round,
      header,
      diffHtml,
      writeGateNoticeHtml(round, resultMetadata),
      compactionLabelHtml,
    );
  }

  const INLINE_FAILURE_REASON_LIMIT = 110;

  /* A gate/executor refusal used to hide inside the collapsed <details> of
     the failed edit, so a rejected edit_file looked identical to a running
     one until expanded. Render a one-line truncation directly on the failed
     row's summary line (and on the collapsed batch header) — the full text
     stays in the title tooltip and the expanded detail block. */
  function inlineFailureReasonHtml(reason: string): string {
    const oneLine = reason.replace(/\s+/g, ' ').trim();
    if (!oneLine) return '';
    const shown = oneLine.length > INLINE_FAILURE_REASON_LIMIT
      ? `${oneLine.slice(0, INLINE_FAILURE_REASON_LIMIT)}…`
      : oneLine;
    return `<span class="ptool-batch-fail-reason" title="${
      escapeHtmlText(oneLine)
    }">${escapeHtmlText(shown)}</span>`;
  }

  function renderEditErrorKindChipHtml(kind: string): string {
    if (!kind) return '';
    const descriptor = EDIT_ERROR_KINDS[kind];
    const title = `${kind}\n${descriptor.explanation}`;
    return `<span class="ptool-edit-kind ${descriptor.className}" title="${
      escapeHtmlText(title)
    }">${escapeHtmlText(kind)}</span>`;
  }

  /* Compact failure disclosure for a SETTLED-failed batch-edit round
     (status error/rejected/…). The generic error row dumps the whole
     model-facing log inline; this card renders one row per edit — status
     icon, operation pill, target, per-kind chip, and a one-line truncated
     reason — with the full reason on the row tooltip and in the expanded
     detail block. Done-status partial failures keep the batch-diff card. */
  function renderEditFailureCardHtml(
    round: UnknownRecord,
    resultMetadata: UnknownRecord,
    header: ToolResultHeaderHtml,
    compactionLabelHtml: string,
  ): string {
    const toolName = stringField(round, 'toolName');
    if (!EDIT_FAILURE_TOOL_NAMES.has(toolName)) return '';
    if (
      !['error', 'rejected', 'aborted', 'unanswerable'].includes(
        stringField(round, 'status'))
    ) {
      return '';
    }

    const refusal = writeGateRefusal.resolveRefusal(round, resultMetadata);
    const authoritative = authoritativeResultText(round);
    const deferred = record(round._snapshotToolResult);
    const isDeferred = typeof deferred.preview === 'string' && typeof deferred.chars === 'number';
    const unwrapped = unwrapEditResultProjection(authoritative?.text
      ?? (isDeferred ? String(deferred.preview) : ''));
    const parseSource = unwrapped.trim();
    const entries = parseEditOutcomeEntries(parseSource);
    // A gate/whole-call failure has no [N] outcome lines. Empty metadata
    // rows must not suppress that retained error or produce empty folds.
    let wholeCallKind = '';
    let wholeCallReason = entries.length ? '' : parseSource;
    const singleMatch = wholeCallReason.match(
      /^(?:Diff|Insert|Write) failed(?:\s+\[([a-z][a-z0-9-]*)\])?:\s*([\s\S]*)$/,
    );
    if (singleMatch) {
      wholeCallKind = knownEditErrorKind(singleMatch[1] || '');
      wholeCallReason = singleMatch[2].trim();
    }
    const unavailableReason = translate(isDeferred
      ? 'tool.resultDeferred' : 'tool.editFailureUnavailable');

    const editSummaries = Array.isArray(resultMetadata.editSummaries)
      ? resultMetadata.editSummaries.map(record)
      : [];

    // A compacted round loses its raw outcome text; the handler-stamped
    // per-edit metadata still carries target/status/detail for every edit.
    // The metadata detail line reuses the same ``path [op] [kind]: message``
    // shape, so it parses through the same body splitter.
    const fallbackEntries: EditOutcomeEntry[] = editSummaries.map(
      (summary, index) => {
        const parsed = parseEditOutcomeBody(stringField(summary, 'detail'));
        return Object.freeze({
          index: index + 1,
          ok: stringField(summary, 'status') !== 'fail',
          path: parsed.path || stringField(summary, 'path'),
          operation: parsed.operation || stringField(summary, 'operation'),
          kind: parsed.kind || (stringField(summary, 'status') === 'fail' ? wholeCallKind : ''),
          message: (parsed.message || (stringField(summary, 'status') === 'fail'
            ? wholeCallReason || unavailableReason : ''))
            .slice(0, EDIT_FAILURE_LIMITS.tooltipChars),
        });
      },
    );
    const rows = entries.length
      ? entries
      : fallbackEntries.slice(0, EDIT_FAILURE_LIMITS.rows);

    // Fail closed: a failed verdict with no parseable outcome and no metadata
    // still renders one honest row — never the fake applied-diff of the
    // settled lanes below. Single-edit tools report one unprefixed
    // ``Diff/Insert failed [kind]: message`` line; surface its kind chip and
    // target path too.
    if (!rows.length) {
      rows.push(Object.freeze({
        index: 1,
        ok: false,
        path: stringField(parseToolArguments(round.toolArgs), 'path'),
        operation: '',
        kind: wholeCallKind,
        message: (wholeCallReason || unavailableReason)
          .slice(0, EDIT_FAILURE_LIMITS.tooltipChars),
      }));
    }

    const isMultiFile = rows.some(
      (row) => row.path && row.path !== rows[0].path,
    );

    const rowsHtml = rows.map((row) => {
      const summary = editSummaries[row.index - 1] || EMPTY_RECORD;
      const fullPath = row.path || stringField(summary, 'path');
      const basename = fullPath
        ? fullPath.split('/').filter(Boolean).pop() || fullPath
        : '';
      const rawDescription = stripDuplicatePathPrefix(
        stringField(summary, 'description'),
        fullPath,
      );
      const description = rawDescription || basename || `Edit ${row.index}`;
      const pathHtml = isMultiFile && basename
        ? `<span class="ptool-batch-path" title="${
          escapeHtmlText(fullPath)
        }">${escapeHtmlText(basename)}</span>`
        : '';
      const operationPillHtml = renderOperationPillHtml(
        row.operation || stringField(summary, 'operation'),
      );

      if (row.ok) {
        const okIconHtml = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>';
        const okDetailHtml = row.message
          ? `<span class="ptool-edit-ok-detail" title="${
            escapeHtmlText(row.message)
          }">${escapeHtmlText(row.message.split('\n')[0])}</span>`
          : '';
        return `<div class="ptool-batch-done-edit ptool-batch-ok">
          <div class="ptool-batch-done-summary">
            <span class="ptool-batch-status">${okIconHtml}</span>
            <span class="ptool-batch-idx">${row.index}</span>
            ${operationPillHtml}
            <span class="ptool-batch-desc">${escapeHtmlText(description)}</span>
            ${pathHtml}
            ${okDetailHtml}
          </div>
        </div>`;
      }

      const failIconHtml = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>';
      const outsideWorkspace = summary.outsideWorkspace === true;
      const reason = outsideWorkspace
        ? translate('tool.outsideWorkspaceItem')
        : row.message;
      const shown = reason.length > EDIT_FAILURE_LIMITS.detailChars
        ? `${reason.slice(0, EDIT_FAILURE_LIMITS.detailChars)}…`
        : reason;
      const detailHtml = shown
        ? `<div class="ptool-batch-fail-detail"><code>${
          escapeHtmlText(shown)
        }</code></div>`
        : '';
      return `<details class="ptool-batch-done-edit ptool-batch-fail">
        <summary class="ptool-batch-done-summary">
          <span class="ptool-batch-status">${failIconHtml}</span>
          <span class="ptool-batch-idx">${row.index}</span>
          ${operationPillHtml}
          <span class="ptool-batch-desc">${escapeHtmlText(description)}</span>
          ${pathHtml}
          ${outsideWorkspace ? '' : renderEditErrorKindChipHtml(row.kind)}
          ${outsideWorkspace ? '' : inlineFailureReasonHtml(reason)}
        </summary>
        ${detailHtml}
      </details>`;
    }).join('');

    let headerFailureHtml = '';
    if (!refusal) {
      for (const row of rows) {
        if (row.ok) continue;
        headerFailureHtml = inlineFailureReasonHtml(row.message);
        if (headerFailureHtml) break;
      }
    }

    return `<details class="ptool-batch-done-block"${isDeferred ? ' data-lazy-tool-result=""' : ''} data-rn="${
      escapeHtmlText(round.roundNum)
    }">
         <summary class="ptool-line ptool-batch-done-header">
           <span class="ptool-icon">${header.iconHtml}</span>
           ${compactionLabelHtml}
           ${header.rootPillHtml}
           <span class="ptool-text">${header.queryHtml}</span>
           ${header.badgeHtml}
           ${headerFailureHtml}
         </summary>${writeGateNoticeHtml(round, resultMetadata)}
         <div class="ptool-result-body">
           ${isDeferred ? `<p class="ptool-result-trunc">${escapeHtmlText(translate('tool.resultDeferred'))}</p>` : ''}
           <div class="ptool-batch-done-list">${rowsHtml}</div>
         </div>
       </details>`;
  }


  function renderBatchEditResultHtml(
    round: UnknownRecord,
    resultMetadata: UnknownRecord,
    header: ToolResultHeaderHtml,
    compactionLabelHtml: string,
  ): string {
    const toolName = stringField(round, 'toolName');
    const isUnifiedEditTool = toolName === 'edit_file';
    const isBatchEditTool = isUnifiedEditTool
      || toolName === 'apply_diffs'
      || toolName === 'insert_contents';
    const rawSummaries = resultMetadata.editSummaries;
    if (
      !isBatchEditTool
      || !Array.isArray(rawSummaries)
      || rawSummaries.length <= (isUnifiedEditTool ? 0 : 1)
    ) {
      return '';
    }
    const editSummaries = rawSummaries.map(record);
    const toolArguments = parseToolArguments(round.toolArgs);
    const rawParsedEdits = toolArguments?.edits;
    const parsedEdits = Array.isArray(rawParsedEdits)
      ? rawParsedEdits.map(record)
      : null;
    const firstPath = stringField(editSummaries[0], 'path');
    const isMultiFile = editSummaries.some(
      (summary) => stringField(summary, 'path') !== firstPath,
    );

    /* A structured refusal (outside-workspace) gets one brief notice under
       the header; the per-item verbose model-facing text and the 110-char
       truncated header reason are noise next to it. */
    const refusal = writeGateRefusal.resolveRefusal(round, resultMetadata);

    const deferred = record(round._snapshotToolResult);
    const isDeferred = typeof deferred.preview === 'string' && typeof deferred.chars === 'number';

    // Failed edits carry no diff, but the tool still returned a per-edit
    // `[N] FAIL ...` line to the model. Recover those lines from the
    // authoritative result text so a failed row stays expandable; compacted
    // rounds lose the raw text and fall back to the metadata detail.
    const failureLineByIndex = new Map<number, string>();
    const authoritative = authoritativeResultText(round);
    if (authoritative) {
      for (const entry of parseEditOutcomeEntries(unwrapEditResultProjection(authoritative.text))) {
        if (!entry.ok && !failureLineByIndex.has(entry.index)) {
          failureLineByIndex.set(entry.index, entry.body || entry.message);
        }
      }
    }

    const itemsHtml = editSummaries.map((summary, index) => {
      const failed = stringField(summary, 'status') === 'fail';
      const statusIconHtml = failed
        ? '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>'
        : '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>';
      const statusClassName = failed ? 'ptool-batch-fail' : 'ptool-batch-ok';
      const fullPath = stringField(summary, 'path');
      const rawDescription = stripDuplicatePathPrefix(
        stringField(summary, 'description'),
        fullPath,
      );
      const description = rawDescription || `Edit ${index + 1}`;
      const basename = fullPath
        ? fullPath.split('/').filter(Boolean).pop() || fullPath
        : '';
      const pathHtml = isMultiFile && basename
        ? `<span class="ptool-batch-path" title="${
          escapeHtmlText(fullPath)
        }">${escapeHtmlText(basename)}</span>`
        : '';

      const parsedEdit = parsedEdits?.[index] || EMPTY_RECORD;
      let operationName = stringField(summary, 'operation');
      if (!operationName || operationName === '?') {
        if (toolName === 'apply_diffs') {
          operationName = 'replace';
        } else if (toolName === 'insert_contents') {
          operationName = stringField(parsedEdit, 'position') === 'before'
            ? 'insert_before'
            : 'insert_after';
        }
      }
      const operationPillHtml = renderOperationPillHtml(operationName);

      let diffHtml = '';
      if (!failed && parsedEdits?.[index]) {
        const operation = stringField(parsedEdit, 'operation');
        const search = stringField(parsedEdit, 'search');
        const anchor = stringField(parsedEdit, 'anchor');
        const content = stringField(parsedEdit, 'content');
        const position = stringField(parsedEdit, 'position');
        const isInsert = operation.startsWith('insert_')
          || (!search && Boolean(anchor) && !operation);
        const oldText = search || anchor;
        const insertBefore = operation === 'insert_before'
          || position === 'before';
        const newText = isInsert
          ? `${insertBefore ? `${content}\n` : ''}${anchor}${
            !insertBefore ? `\n${content}` : ''
          }`
          : operation === 'replace'
            ? content
            : stringField(parsedEdit, 'replace');
        if (oldText || newText) {
          diffHtml = renderLineDiffHtml(oldText, newText);
        }
      }

      const returned = failed
        ? failureLineByIndex.get(index + 1) || stringField(summary, 'detail')
          || translate(isDeferred ? 'tool.resultDeferred' : 'tool.editFailureUnavailable')
        : '';
      const outsideWorkspace = summary.outsideWorkspace === true;
      let failureHtml = '';
      if (failed) {
        if (outsideWorkspace) {
          failureHtml = `<div class="ptool-batch-fail-detail"><code>${
            escapeHtmlText(translate('tool.outsideWorkspaceItem'))
          }</code></div>`;
        } else if (returned) {
          const shown = returned.length > 2000
            ? `${returned.slice(0, 2000)}…`
            : returned;
          failureHtml = `<div class="ptool-batch-fail-detail"><code>${
            escapeHtmlText(shown)
          }</code></div>`;
        }
      }

      return `<details class="ptool-batch-done-edit ${statusClassName}">
        <summary class="ptool-batch-done-summary">
          <span class="ptool-batch-status">${statusIconHtml}</span>
          <span class="ptool-batch-idx">${index + 1}</span>
          ${operationPillHtml}
          <span class="ptool-batch-desc">${escapeHtmlText(description)}</span>
          ${pathHtml}
          ${outsideWorkspace ? '' : inlineFailureReasonHtml(returned)}
        </summary>
        ${diffHtml}${failureHtml}
      </details>`;
    }).join('');

    let headerFailureHtml = '';
    if (!refusal) {
      for (let index = 0; index < editSummaries.length; index += 1) {
        if (stringField(editSummaries[index], 'status') !== 'fail') continue;
        headerFailureHtml = inlineFailureReasonHtml(
          failureLineByIndex.get(index + 1)
            || stringField(editSummaries[index], 'detail')
            || translate(isDeferred ? 'tool.resultDeferred' : 'tool.editFailureUnavailable'),
        );
        if (headerFailureHtml) break;
      }
    }

    return `<details class="ptool-batch-done-block"${isDeferred ? ' data-lazy-tool-result=""' : ''} data-rn="${
      escapeHtmlText(round.roundNum)
    }">
         <summary class="ptool-line ptool-batch-done-header">
           <span class="ptool-icon">${header.iconHtml}</span>
           ${compactionLabelHtml}
           ${header.rootPillHtml}
           <span class="ptool-text">${header.queryHtml}</span>
           ${header.badgeHtml}
           ${headerFailureHtml}
         </summary>${writeGateNoticeHtml(round, resultMetadata)}
         <div class="ptool-result-body">
           ${isDeferred ? `<p class="ptool-result-trunc">${escapeHtmlText(translate('tool.resultDeferred'))}</p>` : ''}
           <div class="ptool-batch-done-list">${itemsHtml}</div>
         </div>
       </details>`;
  }

  function renderWriteResultHtml(
    roundValue: unknown,
    resultMetadataValue: unknown,
    header: ToolResultHeaderHtml,
  ): string {
    const round = record(roundValue);
    const resultMetadata = record(resultMetadataValue);
    const compactionLabelHtml = renderCompactionLabelHtml(round);
    return renderEditFailureCardHtml(
      round,
      resultMetadata,
      header,
      compactionLabelHtml,
    ) || renderSingleWriteResultHtml(
      round,
      resultMetadata,
      header,
      compactionLabelHtml,
    ) || renderSingleDiffResultHtml(
      round,
      resultMetadata,
      header,
      compactionLabelHtml,
    ) || renderBatchEditResultHtml(
      round,
      resultMetadata,
      header,
      compactionLabelHtml,
    );
  }

  /* Proposal review skeleton: the round header keeps the standard tool-row
   * chrome; the body carries the model-authored summary plus a mount point
   * the proposal-card owner hydrates into per-hunk diffs and decisions. */
  function renderProposalSkeletonHtml(
    round: UnknownRecord,
    header: ToolResultHeaderHtml,
    proposalId: string,
    summary: string,
  ): string {
    const summaryHtml = summary
      ? `<p class="paper-proposal-summary">${escapeHtmlText(summary)}</p>`
      : '';
    return `<details class="ptool-result-block paper-proposal-block" data-rn="${
      escapeHtmlText(round.roundNum)
    }" open>
       <summary class="ptool-line ptool-result-header">
         <span class="ptool-icon">${header.iconHtml}</span>
         ${renderCompactionLabelHtml(round)}
         ${header.rootPillHtml}
         <span class="ptool-text">${header.queryHtml}</span>
         ${header.badgeHtml}
       </summary>
       <div class="ptool-result-body">
         ${summaryHtml}
         <div class="paper-proposal" data-paper-proposal="${
           escapeHtmlText(proposalId)
         }"><p class="paper-proposal-loading">${escapeHtmlText(translate('paperProposal.loading'))}</p></div>
       </div>
     </details>`;
  }

  function renderGenericResultHtml(
    roundValue: unknown,
    _resultMetadataValue: unknown,
    header: ToolResultHeaderHtml,
  ): string {
    const round = record(roundValue);
    if (!['done', 'completed', 'error', 'failed', 'aborted', 'interrupted', 'unanswerable']
      .includes(stringField(round, 'status'))) return '';
    const deferred = record(round._snapshotToolResult);
    const isDeferred = typeof deferred.preview === 'string' && typeof deferred.chars === 'number';
    const requestHtml = renderToolArtifactRequestHtml(round, translate);
    const authoritative = (isDeferred
      ? { source: 'deferred', text: String(deferred.preview) }
      : authoritativeResultText(round)) || (requestHtml ? { source: 'request', text: '' } : null);
    if (!authoritative || (!isDeferred && !authoritative.text.trim() && !requestHtml)) return '';

    if (stringField(round, 'toolName') === PAPER_PROPOSAL_TOOL_NAME) {
      const proposalId = extractPaperProposalId(authoritative.text);
      if (proposalId) {
        const firstBreak = authoritative.text.indexOf('\n');
        const summary = (firstBreak < 0 ? '' : authoritative.text.slice(firstBreak + 1))
          .trim()
          .slice(0, PAPER_PROPOSAL_SUMMARY_MAX_CHARS);
        return renderProposalSkeletonHtml(round, header, proposalId, summary);
      }
    }

    const awaitScopeBadgeHtml = renderAwaitScopeBadgeHtml(round, translate);
    const queryHtml = renderRepoMapHeaderHtml(round, translate) || header.queryHtml;
    const artifactFailure = ['error', 'failed'].includes(stringField(round, 'status'))
      && renderToolArtifactHeaderHtml(round, translate)
      ? toolArtifactFailureText(authoritative.text, translate) : '';
    const failureHtml = artifactFailure
      ? `<span class="ptool-artifact-failure">${escapeHtmlText(artifactFailure)}</span>` : '';
    const openingHtml = `<details class="ptool-result-block"${isDeferred ? ' data-lazy-tool-result=""' : ''} data-tool-result-authority="${
      authoritative.source
    }" data-rn="${escapeHtmlText(round.roundNum)}">
       <summary class="ptool-line ptool-result-header">
         <span class="ptool-icon">${header.iconHtml}</span>
         ${renderCompactionLabelHtml(round)}
         ${header.rootPillHtml}
         <span class="ptool-text">${queryHtml}${failureHtml}</span>
         ${header.repairedBadgeHtml || ''}
         ${header.badgeHtml}
         ${awaitScopeBadgeHtml}
         ${header.rightControlsHtml || ''}
       </summary>${requestHtml}`;
    // The mounted renderer requests a summary-only first paint. Avoid JSON
    // parsing/highlighting and large DOM bodies until the reader opens it.
    // Snapshot references retain their existing transport-owned preview path.
    if (round._deferToolResultBody === true && !isDeferred) {
      return `${openingHtml}<div class="ptool-result-body" data-deferred-tool-body=""></div></details>`;
    }

    if (!authoritative.text.trim() && !isDeferred) {
      return `${openingHtml}</details>`;
    }

    const reportPolicy = toolResultSurfacePolicy(round.toolName);
    if (!isDeferred && reportPolicy.frontendFormat === 'report'
        && !authoritative.text.trimStart().startsWith('{')
        && !authoritative.text.trimStart().startsWith('[')
        && dependencies.renderMarkdown) {
      const truncated = authoritative.text.length > RESULT_VIEW_MAX_CHARS;
      const shown = authoritative.text.slice(0, RESULT_VIEW_MAX_CHARS);
      let html: string;
      try { html = dependencies.renderMarkdown(shown); }
      catch { html = escapeHtmlText(shown); }
      return `${openingHtml}<div class="ptool-result-body md-content">${html}${truncated
        ? `<div class="ptool-result-trunc">${escapeHtmlText(translate('tool.resultTruncated',
          { n: RESULT_VIEW_MAX_CHARS.toLocaleString() }))}</div>` : ''}</div></details>`;
    }

    let shown = authoritative.text.replace(/\s+$/, '');
    let isJson = false;
    if (shown.length <= 200_000) {
      const firstCharacter = shown.charAt(0);
      if (firstCharacter === '{' || firstCharacter === '[') {
        try {
          shown = JSON.stringify(JSON.parse(shown), null, 2);
          isJson = true;
        } catch {
          // The prefix resembled JSON; verbatim display remains authoritative.
        }
      }
    }
    const totalChars = isDeferred ? Number(deferred.chars) : shown.length;
    const truncated = !isDeferred && totalChars > RESULT_VIEW_MAX_CHARS;
    if (truncated) shown = shown.slice(0, RESULT_VIEW_MAX_CHARS);
    const lineCount = shown ? shown.split('\n').length : 0;
    const languageLabel = isJson
      ? 'json'
      : (header.toolDisplayLabel || stringField(round, 'toolName') || 'tool')
        .toLowerCase();
    // Highlight only when the language is certain: exact JSON from the
    // pretty-print step above, or a confident detection on the (possibly
    // truncated) body. Verbatim escaped display stays authoritative for
    // everything else — a guessed grammar must never garble tool output.
    const highlightLanguage = isJson
      ? 'json'
      : detectCodeLanguage(shown, stringField(round, 'toolName'));
    const codeHtml = highlightLanguage
      ? highlightCodeToHtml(shown, highlightLanguage)
      : escapeHtmlText(shown);
    const stats = translate('tool.resultStats', {
      lines: lineCount.toLocaleString(),
      chars: totalChars.toLocaleString(),
    });
    const truncationNoteHtml = isDeferred
      ? `<div class="ptool-result-trunc">${escapeHtmlText(translate('tool.resultDeferred'))}</div>`
      : truncated
      ? `<div class="ptool-result-trunc">${escapeHtmlText(translate(
        'tool.resultTruncated',
        { n: RESULT_VIEW_MAX_CHARS.toLocaleString() },
      ))}</div>`
      : '';

    return `${openingHtml}
       <div class="ptool-result-body">
          <pre class="ptool-result-pre"><div class="code-header"><span>${
            escapeHtmlText(languageLabel)
          } · ${escapeHtmlText(stats)}</span><button class="copy-btn" data-tofu-action="copyCode(this)">Copy</button></div><code>${
            codeHtml
          }</code></pre>
         ${truncationNoteHtml}
       </div>
     </details>`;
  }

  return Object.freeze({
    renderCompactionLabelHtml,
    renderWriteResultHtml,
    renderGenericResultHtml,
  });
}
