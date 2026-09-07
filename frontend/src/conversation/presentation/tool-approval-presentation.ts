/**
 * Pure presentation policy for pending write-tool approvals.
 *
 * Responsibility: project bounded risk metadata and render the approval card.
 * Entry point: `createToolApprovalPresentation`. Dependencies: generated i18n
 * and shared HTML escaping. The retained caller owns dispatch order and passes
 * only explicitly trusted icon/query HTML slots. Approval authority remains in
 * `resolveWriteApproval`; this owner emits a static action command whose id is
 * read from an escaped data attribute rather than interpolated into code.
 */

import { escapeHtmlText } from '../../html-safety';
import type { I18nArgs, I18nKey, Translator } from '../../i18n';

type UnknownRecord = Readonly<Record<string, unknown>>;

type BoundedText = Readonly<{
  value: string;
  truncated: boolean;
}>;

type LineProjection = Readonly<{
  lines: readonly BoundedText[];
  totalLines: number;
  inputUnits: number;
  inputTruncated: boolean;
  lineTruncated: boolean;
}>;

const APPROVAL_ID_UNITS = 512;
const DESCRIPTION_UNITS = 4_096;
const PATH_UNITS = 4_096;
const RISK_FIELDS = 32;
const RISK_LABEL_UNITS = 512;
const RISK_VALUE_UNITS = 2_000;
const RISK_VALUE_LINES = 64;
const BATCH_EDITS = 16;
const BATCH_PREVIEW_LINES = 12;
const SINGLE_PREVIEW_LINES = 30;
const CONTENT_PREVIEW_LINES = 12;
const PREVIEW_INPUT_UNITS = 120_000;
const CONTENT_INPUT_UNITS = 65_536;
const PREVIEW_LINE_UNITS = 2_000;
const COMMAND_UNITS = 65_536;

export const TOOL_APPROVAL_PRESENTATION_LIMITS = Object.freeze({
  approvalIdUnits: APPROVAL_ID_UNITS,
  descriptionUnits: DESCRIPTION_UNITS,
  pathUnits: PATH_UNITS,
  riskFields: RISK_FIELDS,
  riskLabelUnits: RISK_LABEL_UNITS,
  riskValueUnits: RISK_VALUE_UNITS,
  riskValueLines: RISK_VALUE_LINES,
  batchEdits: BATCH_EDITS,
  batchPreviewLines: BATCH_PREVIEW_LINES,
  singlePreviewLines: SINGLE_PREVIEW_LINES,
  contentPreviewLines: CONTENT_PREVIEW_LINES,
  previewInputUnits: PREVIEW_INPUT_UNITS,
  contentInputUnits: CONTENT_INPUT_UNITS,
  previewLineUnits: PREVIEW_LINE_UNITS,
  commandUnits: COMMAND_UNITS,
});

export type ToolApprovalHeaderHtml = Readonly<{
  iconHtml: string;
  queryHtml: string;
}>;

export type ToolApprovalPresentation = Readonly<{
  renderApprovalHtml(round: unknown, header: ToolApprovalHeaderHtml): string;
}>;

export type ToolApprovalPresentationDependencies = Readonly<{
  translate: Translator;
}>;

const EMPTY_RECORD: UnknownRecord = Object.freeze({});

function record(value: unknown): UnknownRecord {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as UnknownRecord
    : EMPTY_RECORD;
}

function field(value: unknown, name: string): unknown {
  try {
    return record(value)[name];
  } catch {
    return undefined;
  }
}

function safeText(value: unknown): string {
  if (value === null || value === undefined) return '';
  if (typeof value === 'string') return value;
  if (
    typeof value === 'number'
    || typeof value === 'boolean'
    || typeof value === 'bigint'
  ) return String(value);
  try {
    return String(value);
  } catch {
    return '';
  }
}

function boundedText(value: unknown, limit: number): BoundedText {
  const text = safeText(value);
  return {
    value: text.length > limit ? `${text.slice(0, limit)}…` : text,
    truncated: text.length > limit,
  };
}

function safeNonNegativeInteger(value: unknown, fallback: number): number {
  return typeof value === 'number'
    && Number.isSafeInteger(value)
    && value >= 0
    ? value
    : fallback;
}

function trustedHtml(value: unknown): string {
  return typeof value === 'string' ? value : '';
}

function projectLines(
  value: unknown,
  inputLimit: number,
  lineLimit: number,
  displayLimit: number,
): LineProjection {
  const raw = safeText(value);
  const inputTruncated = raw.length > inputLimit;
  const text = raw.slice(0, inputLimit);
  const lines: BoundedText[] = [];
  let lineStart = 0;
  let totalLines = 1;
  let lineTruncated = false;

  function retainLine(end: number): void {
    if (lines.length >= displayLimit) return;
    const projection = boundedText(text.slice(lineStart, end), lineLimit);
    if (projection.truncated) lineTruncated = true;
    lines.push(projection);
  }

  for (let index = 0; index < text.length; index += 1) {
    if (text[index] !== '\n') continue;
    retainLine(index);
    lineStart = index + 1;
    totalLines += 1;
  }
  retainLine(text.length);

  return {
    lines,
    totalLines,
    inputUnits: raw.length,
    inputTruncated,
    lineTruncated,
  };
}

function lineHtml(line: BoundedText, kind: 'add' | 'del'): string {
  const sign = kind === 'add' ? '+' : '-';
  return `<div class="ptool-diff-line ptool-diff-${kind}"><span class="ptool-diff-sign">${sign}</span><span class="ptool-diff-code">${escapeHtmlText(line.value)}</span></div>`;
}

export function createToolApprovalPresentation(
  dependencies: ToolApprovalPresentationDependencies,
): ToolApprovalPresentation {
  const { translate } = dependencies;

  function translatedHtml<K extends I18nKey>(
    key: K,
    ...args: I18nArgs<K>
  ): string {
    return escapeHtmlText(translate(key, ...args));
  }

  function previewLimitHtml(limit: number): string {
    return `<div class="ptool-preview-limit">${translatedHtml(
      'toolApproval.previewLimit',
      { n: limit },
    )}</div>`;
  }

  function moreLinesHtml(count: number, kind: 'add' | 'del'): string {
    if (count <= 0) return '';
    return `<div class="ptool-diff-line ptool-diff-${kind} ptool-diff-ellipsis"><span class="ptool-diff-sign"> </span><span class="ptool-diff-code">${translatedHtml(
      'toolApproval.moreLines',
      { count },
    )}</span></div>`;
  }

  function renderRiskFields(metadata: UnknownRecord): string {
    const candidate = field(metadata, 'riskFields');
    if (!Array.isArray(candidate) || candidate.length === 0) return '';

    const rows: string[] = [];
    const retainedCount = Math.min(candidate.length, RISK_FIELDS);
    for (let index = 0; index < retainedCount; index += 1) {
      const riskField = record(candidate[index]);
      const rawLabel = field(riskField, 'label');
      if (rawLabel === null || rawLabel === undefined) continue;
      const label = boundedText(rawLabel, RISK_LABEL_UNITS);
      const value = boundedText(field(riskField, 'value'), RISK_VALUE_UNITS);
      const projection = projectLines(
        value.value,
        RISK_VALUE_UNITS + 1,
        RISK_VALUE_UNITS + 1,
        RISK_VALUE_LINES,
      );
      let body = projection.lines.map((line) => lineHtml(line, 'add')).join('');
      body += moreLinesHtml(
        Math.max(0, projection.totalLines - RISK_VALUE_LINES),
        'add',
      );
      rows.push(
        `<div class="ptool-risk-field"><div class="ptool-risk-label">${escapeHtmlText(label.value)}</div>${body}</div>`,
      );
    }

    const description = boundedText(
      field(metadata, 'description'),
      DESCRIPTION_UNITS,
    );
    const noteHtml = description.value
      ? `<div class="ptool-cmd-desc">${escapeHtmlText(description.value)}</div>`
      : '';
    const omittedHtml = candidate.length > retainedCount
      ? `<div class="ptool-preview-limit">${translatedHtml(
        'toolApproval.riskFieldsLimit',
        { shown: retainedCount, total: candidate.length },
      )}</div>`
      : '';
    return `<div class="ptool-diff-preview">${noteHtml}${rows.join('')}${omittedHtml}</div>`;
  }

  function renderEditLines(
    value: unknown,
    kind: 'add' | 'del',
    declaredLines: unknown,
    declaredUnits: unknown,
    limit: number,
    includeTotals: boolean,
  ): string {
    const projection = projectLines(
      value,
      PREVIEW_INPUT_UNITS,
      PREVIEW_LINE_UNITS,
      limit,
    );
    let html = projection.lines.map((line) => lineHtml(line, kind)).join('');
    const totalLines = safeNonNegativeInteger(
      declaredLines,
      projection.totalLines,
    );
    const totalUnits = safeNonNegativeInteger(
      declaredUnits,
      projection.inputUnits,
    );
    const omittedLines = Math.max(0, totalLines - limit);
    if (omittedLines > 0 && includeTotals) {
      html += `<div class="ptool-diff-line ptool-diff-${kind} ptool-diff-ellipsis"><span class="ptool-diff-sign"> </span><span class="ptool-diff-code">${translatedHtml(
        'toolApproval.moreLinesWithTotals',
        { count: omittedLines, totalLines, totalChars: totalUnits },
      )}</span></div>`;
    } else {
      html += moreLinesHtml(omittedLines, kind);
    }
    if (projection.inputTruncated || projection.lineTruncated) {
      html += previewLimitHtml(
        projection.inputTruncated ? PREVIEW_INPUT_UNITS : PREVIEW_LINE_UNITS,
      );
    }
    return html;
  }

  function renderBatch(metadata: UnknownRecord): string {
    const editsValue = field(metadata, 'editSummaries');
    if (field(metadata, 'batchMode') !== true || !Array.isArray(editsValue)) {
      return '';
    }

    const retainedCount = Math.min(editsValue.length, BATCH_EDITS);
    const path = boundedText(field(metadata, 'path'), PATH_UNITS).value || '?';
    const headerKey = editsValue.length === 1
      ? 'toolApproval.oneEditAcross'
      : 'toolApproval.manyEditsAcross';
    let batchHtml = `<div class="ptool-batch-header">${translatedHtml(
      headerKey,
      { count: editsValue.length, path },
    )}</div>`;

    for (let index = 0; index < retainedCount; index += 1) {
      const edit = record(editsValue[index]);
      let diffLines = renderEditLines(
        field(edit, 'search'),
        'del',
        field(edit, 'searchLines'),
        undefined,
        BATCH_PREVIEW_LINES,
        false,
      );
      diffLines += '<div class="ptool-diff-separator"></div>';
      diffLines += renderEditLines(
        field(edit, 'replace'),
        'add',
        field(edit, 'replaceLines'),
        undefined,
        BATCH_PREVIEW_LINES,
        false,
      );

      const description = boundedText(
        field(edit, 'description'),
        DESCRIPTION_UNITS,
      );
      const descriptionHtml = description.value
        ? escapeHtmlText(description.value)
        : translatedHtml('toolApproval.editFallback', { index: index + 1 });
      const editPath = boundedText(field(edit, 'path'), PATH_UNITS).value || '?';
      const searchLines = safeNonNegativeInteger(field(edit, 'searchLines'), -1);
      const replaceLines = safeNonNegativeInteger(field(edit, 'replaceLines'), -1);
      batchHtml += `<details class="ptool-batch-edit"${index === 0 ? ' open' : ''}>
          <summary class="ptool-batch-summary"><span class="ptool-batch-idx">#${index + 1}</span> <span class="ptool-batch-path">${escapeHtmlText(editPath)}</span> <span class="ptool-batch-desc">${descriptionHtml}</span> <span class="ptool-batch-stats">${translatedHtml(
            'toolApproval.editStats',
            {
              searchLines: searchLines >= 0 ? searchLines : '?',
              replaceLines: replaceLines >= 0 ? replaceLines : '?',
            },
          )}</span></summary>
          <div class="ptool-diff-preview">${diffLines}</div>
        </details>`;
    }

    const declaredCount = safeNonNegativeInteger(
      field(metadata, 'editCount'),
      editsValue.length,
    );
    const omitted = Math.max(
      0,
      Math.max(declaredCount, editsValue.length) - retainedCount,
    );
    if (omitted > 0) {
      batchHtml += `<div class="ptool-batch-more">${translatedHtml(
        'toolApproval.moreEdits',
        { count: omitted },
      )}</div>`;
    }
    return `<div class="ptool-batch-preview">${batchHtml}</div>`;
  }

  function renderSingleDiff(metadata: UnknownRecord): string {
    const search = field(metadata, 'search');
    const replace = field(metadata, 'replace');
    if (search === null || search === undefined
        || replace === null || replace === undefined) return '';

    let diffLines = renderEditLines(
      search,
      'del',
      field(metadata, 'searchLines'),
      field(metadata, 'searchChars'),
      SINGLE_PREVIEW_LINES,
      true,
    );
    diffLines += '<div class="ptool-diff-separator"></div>';
    diffLines += renderEditLines(
      replace,
      'add',
      field(metadata, 'replaceLines'),
      field(metadata, 'replaceChars'),
      SINGLE_PREVIEW_LINES,
      true,
    );
    return `<div class="ptool-diff-preview">${diffLines}</div>`;
  }

  function renderCommand(metadata: UnknownRecord): string {
    const commandValue = field(metadata, 'command');
    if (commandValue === null || commandValue === undefined) return '';
    const command = boundedText(commandValue, COMMAND_UNITS);
    const description = boundedText(
      field(metadata, 'description'),
      DESCRIPTION_UNITS,
    );
    const descriptionHtml = description.value
      ? `<div class="ptool-cmd-desc">${escapeHtmlText(description.value)}</div>`
      : '';
    const limitHtml = command.truncated || description.truncated
      ? previewLimitHtml(command.truncated ? COMMAND_UNITS : DESCRIPTION_UNITS)
      : '';
    return `<div class="ptool-diff-preview">${descriptionHtml}<pre class="ptool-cmd-code" style="margin:0;padding:8px 12px;font-size:12px;"><code>$ ${escapeHtmlText(command.value)}</code></pre>${limitHtml}</div>`;
  }

  function renderContent(metadata: UnknownRecord): string {
    const contentValue = field(metadata, 'contentPreview');
    if (!contentValue) return '';
    const projection = projectLines(
      contentValue,
      CONTENT_INPUT_UNITS,
      PREVIEW_LINE_UNITS,
      CONTENT_PREVIEW_LINES,
    );
    let previewContent = projection.lines
      .map((line) => lineHtml(line, 'add'))
      .join('');
    const contentLines = safeNonNegativeInteger(
      field(metadata, 'contentLines'),
      projection.totalLines,
    );
    if (contentLines > CONTENT_PREVIEW_LINES) {
      previewContent += `<div class="ptool-diff-line ptool-diff-add ptool-diff-ellipsis"><span class="ptool-diff-sign"> </span><span class="ptool-diff-code">${translatedHtml(
        'toolApproval.moreLinesUnknown',
      )}</span></div>`;
    }
    if (projection.inputTruncated || projection.lineTruncated) {
      previewContent += previewLimitHtml(
        projection.inputTruncated ? CONTENT_INPUT_UNITS : PREVIEW_LINE_UNITS,
      );
    }
    const contentUnits = safeNonNegativeInteger(
      field(metadata, 'contentChars'),
      projection.inputUnits,
    );
    return `<div class="ptool-diff-preview">${previewContent}<div class="ptool-write-meta">${translatedHtml(
      'toolApproval.writeMeta',
      { lines: contentLines, chars: contentUnits },
    )}</div></div>`;
  }

  function approvalButtonsHtml(approvalId: BoundedText): string {
    const approveLabel = translatedHtml('toolApproval.approve');
    const rejectLabel = translatedHtml('toolApproval.reject');
    if (approvalId.truncated) {
      return `<div class="ptool-preview-limit">${translatedHtml(
        'toolApproval.approvalIdLimit',
        { n: APPROVAL_ID_UNITS },
      )}</div>
         <div class="ptool-approval-btns">
           <button class="ptool-approve-btn" type="button" disabled><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"><polyline points="20 6 9 17 4 12"/></svg> ${approveLabel}</button>
           <button class="ptool-reject-btn" type="button" disabled><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg> ${rejectLabel}</button>
         </div>`;
    }
    const escapedId = escapeHtmlText(approvalId.value);
    return `<div class="ptool-approval-btns">
           <button class="ptool-approve-btn" type="button" data-approval-id="${escapedId}" data-tofu-action="event.stopPropagation();resolveWriteApproval(this.dataset.approvalId,true)"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"><polyline points="20 6 9 17 4 12"/></svg> ${approveLabel}</button>
           <button class="ptool-reject-btn" type="button" data-approval-id="${escapedId}" data-tofu-action="event.stopPropagation();resolveWriteApproval(this.dataset.approvalId,false)"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg> ${rejectLabel}</button>
         </div>`;
  }

  function renderApprovalHtml(
    roundValue: unknown,
    header: ToolApprovalHeaderHtml,
  ): string {
    if (field(roundValue, 'status') !== 'pending_approval') return '';
    const rawApprovalId = field(roundValue, 'approvalId');
    if (!rawApprovalId) return '';

    const approvalId = boundedText(rawApprovalId, APPROVAL_ID_UNITS);
    const metadata = record(field(roundValue, 'approvalMeta'));
    const detailHtml = renderRiskFields(metadata)
      || renderBatch(metadata)
      || renderSingleDiff(metadata)
      || renderCommand(metadata)
      || renderContent(metadata);

    return `<div class="ptool-pending-wrap">
         <div class="ptool-line ptool-pending">
           <span class="ptool-icon">${trustedHtml(header?.iconHtml)}</span>
           <span class="ptool-text">${trustedHtml(header?.queryHtml)}</span>
           <span class="ptool-badge ptool-badge-warn">${translatedHtml(
             'toolApproval.awaiting',
           )}</span>
         </div>
         ${detailHtml}
         ${approvalButtonsHtml(approvalId)}
       </div>`;
  }

  return Object.freeze({ renderApprovalHtml });
}
