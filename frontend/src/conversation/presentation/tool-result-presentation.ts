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
import type { Translator } from '../../i18n';
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

function record(value: unknown): UnknownRecord {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as UnknownRecord
    : EMPTY_RECORD;
}

function owns(value: object, field: PropertyKey): boolean {
  return Object.prototype.hasOwnProperty.call(value, field);
}

type AuthoritativeResultText = Readonly<{
  source: 'toolContent' | 'roundResult';
  text: string;
}>;

function serializeAuthoritativeResult(value: unknown): string {
  if (typeof value === 'string') return value;
  if (value === undefined) return 'undefined';
  try {
    return JSON.stringify(value) ?? String(value);
  } catch {
    return String(value);
  }
}

/**
 * Resolve only fields owned by the projected tool round itself.
 *
 * `round.results[0]` is presentation metadata. It may be a useful secondary
 * projection, but it is not allowed to replace the result the backend settled
 * for this call. Property presence is deliberate: an explicit empty/null
 * result remains authoritative and must not fall through to unrelated metadata.
 */
function authoritativeResultText(
  round: UnknownRecord,
): AuthoritativeResultText | null {
  if (owns(round, 'toolContent')) {
    return Object.freeze({
      source: 'toolContent',
      text: serializeAuthoritativeResult(round.toolContent),
    });
  }
  const roundResult = record(round.result);
  if (owns(roundResult, 'content')) {
    return Object.freeze({
      source: 'roundResult',
      text: serializeAuthoritativeResult(roundResult.content),
    });
  }
  return null;
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

/** Render a bounded, git-style line diff without reading browser state. */
function renderLineDiffHtml(oldText: string, newText: string): string {
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

    // Failed edits carry no diff, but the tool still returned a per-edit
    // `[N] FAIL ...` line to the model. Recover those lines from the
    // authoritative result text so a failed row stays expandable; compacted
    // rounds lose the raw text and fall back to the metadata detail.
    const failureLineByIndex = new Map<number, string>();
    const authoritative = authoritativeResultText(round);
    if (authoritative) {
      for (const line of authoritative.text.split('\n')) {
        const match = line.match(/^\[(\d+)\] FAIL (.+)$/);
        if (match) {
          const editNumber = Number(match[1]);
          if (!failureLineByIndex.has(editNumber)) {
            failureLineByIndex.set(editNumber, match[2]);
          }
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

      let failureHtml = '';
      if (failed) {
        const returned = failureLineByIndex.get(index + 1)
          || stringField(summary, 'detail');
        if (returned) {
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
        </summary>
        ${diffHtml}${failureHtml}
      </details>`;
    }).join('');

    return `<details class="ptool-batch-done-block" data-rn="${
      escapeHtmlText(round.roundNum)
    }">
         <summary class="ptool-line ptool-batch-done-header">
           <span class="ptool-icon">${header.iconHtml}</span>
           ${compactionLabelHtml}
           ${header.rootPillHtml}
           <span class="ptool-text">${header.queryHtml}</span>
           ${header.badgeHtml}
         </summary>${writeGateNoticeHtml(round, resultMetadata)}
         <div class="ptool-batch-done-list">${itemsHtml}</div>
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
    return renderSingleWriteResultHtml(
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

  function renderGenericResultHtml(
    roundValue: unknown,
    _resultMetadataValue: unknown,
    header: ToolResultHeaderHtml,
  ): string {
    const round = record(roundValue);
    if (stringField(round, 'status') !== 'done') return '';
    const authoritative = authoritativeResultText(round);
    if (!authoritative || !authoritative.text.trim()) return '';

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
    const totalChars = shown.length;
    const truncated = totalChars > RESULT_VIEW_MAX_CHARS;
    if (truncated) shown = shown.slice(0, RESULT_VIEW_MAX_CHARS);
    const lineCount = shown ? shown.split('\n').length : 0;
    const languageLabel = isJson
      ? 'json'
      : (header.toolDisplayLabel || stringField(round, 'toolName') || 'tool')
        .toLowerCase();
    const stats = translate('tool.resultStats', {
      lines: lineCount.toLocaleString(),
      chars: totalChars.toLocaleString(),
    });
    const truncationNoteHtml = truncated
      ? `<div class="ptool-result-trunc">${escapeHtmlText(translate(
        'tool.resultTruncated',
        { n: RESULT_VIEW_MAX_CHARS.toLocaleString() },
      ))}</div>`
      : '';

    return `<details class="ptool-result-block" data-tool-result-authority="${
      authoritative.source
    }" data-rn="${
      escapeHtmlText(round.roundNum)
    }">
       <summary class="ptool-line ptool-result-header">
         <span class="ptool-icon">${header.iconHtml}</span>
         ${renderCompactionLabelHtml(round)}
         ${header.rootPillHtml}
         <span class="ptool-text">${header.queryHtml}</span>
         ${header.repairedBadgeHtml || ''}
         ${header.badgeHtml}
         ${header.rightControlsHtml || ''}
       </summary>
       <div class="ptool-result-body">
         <pre class="ptool-result-pre"><div class="code-header"><span>${
           escapeHtmlText(languageLabel)
         } · ${escapeHtmlText(stats)}</span><button class="copy-btn" data-tofu-action="copyCode(this)">Copy</button></div><code>${
           escapeHtmlText(shown)
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
