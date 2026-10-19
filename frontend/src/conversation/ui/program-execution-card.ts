/**
 * One evidence-first program card for retained, classic and default surfaces.
 * Entry points: programExecutionCardHtml, renderProgramExecutionCard.
 * Receives projected rounds and injected localization; emits escaped markup.
 * No execution, transport or durable state. Disclosure/focus preservation
 * belongs to rich-card-reconciliation; evidence belongs to its typed selector.
 */
import { normalizeProgramPresentation, safeProgramPreview } from '../presentation/program-gateway-presentation';
import { toolFailureText } from '../presentation/tool-failure-presentation';
import { programByteSize, programExecutionPresentation } from '../presentation/program-execution-presentation';

import { boundedProgramDisplay, programActivityMembers, retainedInputDirectoryCounts, retainedProgramValueBytes } from '../presentation/program-activity';

import { escapeCodeText, highlightCodeToHtml } from '../presentation/code-highlight';
import { reconcileRichCardHtml } from './rich-card-reconciliation';

export type ProgramCardText = (
  key: string, fallback: string,
  values?: Readonly<Record<string, string | number>>,
) => string;

function plainText(
  _key: string, fallback: string,
  values?: Readonly<Record<string, string | number>>,
): string {
  return fallback.replace(/\{(\w+)\}/g, (token, name: string) => (
    values && Object.hasOwn(values, name) ? String(values[name]) : token
  ));
}

const REASONS: Readonly<Record<string, readonly [string, string]>> = {
  missing_program_result: ['ptc.reason.noResult', 'The program returned no result. Return data explicitly; completed tool calls must not be repeated.'],
  retained_result_unavailable: ['ptc.reason.retainedUnavailable', 'The retained result is unavailable. Choose whether to read it again before continuing.'],
  tool_not_done: ['ptc.reason.notDone', 'The prerequisite did not complete successfully. Review its receipt before continuing.'],
  tool_not_enabled: ['ptc.reason.toolName', 'The tool is unavailable. Returned to the model to check its full name and availability.'],
  incomplete_tool_output: ['ptc.reason.incomplete', 'The prerequisite output is incomplete. Recover complete evidence before continuing.'],
  missing_tool_output: ['ptc.reason.missing', 'The prerequisite provided no output. Review its receipt.'],
};

export function programExecutionCardHtml(
  round: unknown, text: ProgramCardText = plainText,
): string {
  const row = normalizeProgramPresentation(round);
  const gateway = row._programGatewayPresentation === true;
  const evidence = programExecutionPresentation(row);
  const running = row.status === 'searching' || row.programStatus === 'running';
  const failed = !running && row.programStatus === 'error';
  const needsModel = evidence.needsModel && !running;
  const status = running ? text('ptc.status.running', 'Executing program')
    : needsModel ? text('ptc.status.needsModel', 'Returned for model judgment')
      : failed ? text('ptc.status.failed', 'Failed')
        : row.programStatus === 'returned' ? text('ptc.status.returned', 'Returned')
          : row.programStatus === 'completed' ? text('ptc.status.completed', 'Completed')
          : text('ptc.status.incomplete', 'Incomplete');
  const tools = [...new Set(Array.isArray(row.childToolNames)
    ? row.childToolNames.filter((name): name is string => typeof name === 'string' && Boolean(name))
    : [])];
  const count = Array.isArray(row.childCallIds) ? row.childCallIds.length : null;
  const summary: string[] = [];

  const activityCopy: Readonly<Record<string, readonly [string, string]>> = {
    inputs: ['ptc.activity.inputs', 'Script: look up prior results'],
    result: ['ptc.activity.result', 'Script: read saved results'],
    invoke: ['ptc.activity.invoke', 'Script: request tools'],
    keep: ['ptc.activity.keep', 'Script: save a result'],
    observe: ['ptc.activity.observe', 'Script: return evidence for judgment'],
  };
  const activityMembers = programActivityMembers(row.programCode);
  summary.push(...activityMembers.map(member => text(...activityCopy[member])));
  const attempts = evidence.toolCallAttempts;
  const done = evidence.successfulToolCalls;

  if (attempts === null) summary.push(text('ptc.calls.unavailable', 'Tool call counters not recorded / unavailable'));
  if (attempts !== null && attempts > 0) {
    summary.push(text('ptc.calls.attempts', '{n} tool call requests', { n: attempts }));
    if (!running && done !== null && done < attempts) {
      summary.push(text('ptc.calls.unsuccessful', '{n} did not complete successfully', { n: attempts - done }));
    }
  } else if (attempts === null && count !== null && count > 0) {
    summary.push(text('ptc.tasks', '{n} child tool calls', { n: count }));
  }
  if (evidence.resultReads) summary.push(text('ptc.measurement.reused', 'Saved results read {n} times', { n: evidence.resultReads }));
  const metrics: string[] = [];
  if (attempts === 0) metrics.push(text('ptc.calls.none', 'No source tools called again'));
  if (evidence.privateBytes !== null) metrics.push(text('ptc.measurement.private', 'Tool output delivered to program: {size} ({bytes} bytes)', {
    size: programByteSize(evidence.privateBytes), bytes: evidence.privateBytes.toLocaleString(),
  }));
  if (evidence.resultBytes !== null) metrics.push(text('ptc.measurement.returned', 'Program return value: {size} ({bytes} bytes)', {
    size: programByteSize(evidence.resultBytes), bytes: evidence.resultBytes.toLocaleString(),
  }));
  if (attempts !== null && done !== null) metrics.push(text('ptc.calls.successful', '{done} successful tool calls · {attempts} attempts', { done, attempts }));
  if (evidence.callStages !== null) metrics.push(text('ptc.measurement.stages', '{n} call request stages', { n: evidence.callStages }));
  if (evidence.parallelStages !== null) metrics.push(text('ptc.measurement.parallel', '{n} parallel batch requests', { n: evidence.parallelStages }));
  if (evidence.truncatedResults !== null) metrics.push(text('ptc.measurement.truncated', '{n} incomplete results', { n: evidence.truncatedResults }));
  const reasonKey = evidence.toolFailureCode === 'tool_not_enabled'
    ? 'tool_not_enabled' : evidence.reason;
  const reasonCopy = Object.hasOwn(REASONS, reasonKey) ? REASONS[reasonKey] : undefined;
  const reason = needsModel ? reasonCopy ? text(...reasonCopy) : evidence.reason : '';
  const resultValue = needsModel && row.programResult !== null && typeof row.programResult === 'object'
    ? (row.programResult as Record<string, unknown>).data : row.programResult;
  const result = !running && resultValue === null ? 'null'
    : !running && resultValue === '' ? '""' : boundedProgramDisplay(gateway ? row._programRawResult : resultValue);
  const source = String(row.programCode ?? '');
  const code = source.slice(0, 32_768) + (source.length > 32_768 ? '\n// … [source display truncated]' : '');
  const inputDirectory = row._programResultComplete !== false && activityMembers.includes('inputs') ? retainedInputDirectoryCounts(resultValue) : null;
  const savedResult = activityMembers.includes('keep')
    && row._programResultComplete !== false ? retainedProgramValueBytes(resultValue) : null;
  const compactResult = savedResult !== null
    ? text('ptc.preview.saved', 'Saved program result ({size})', { size: programByteSize(savedResult) })
    : inputDirectory
    ? text('ptc.preview.inputs', 'Retained inputs: {returned} returned / {total} total', inputDirectory)
    : gateway && failed ? toolFailureText(row._programRawResult, text('ptc.status.failed', 'Failed'))
      : row._programResultComplete === false ? '' : safeProgramPreview((gateway && typeof row._programRawResult === 'string'
        ? row._programRawResult : result).replace(/\s+/g, ' '));
  const resultPreview = !running && compactResult ? text('ptc.preview', 'Result: {value}', {
    value: compactResult.slice(0, 180) + (compactResult.length > 180 ? '…' : ''),
  }) : '';
  // Semantic receipts and failures belong in the glanceable header. Arbitrary
  // return values belong with evidence; raw JSON must not dominate the timeline.
  const previewInSummary = savedResult !== null || Boolean(inputDirectory) || failed || needsModel;
  if (resultPreview && previewInSummary) summary.push(resultPreview);
  if (tools.length) summary.push(tools.slice(0, 4).join(', ') + (tools.length > 4 ? '…' : ''));
  const language = 'javascript';
  const attention = running ? 'running' : needsModel ? 'handoff' : failed ? 'error'
    : row.programStatus === 'completed' || row.programStatus === 'returned' ? 'completed' : 'incomplete';
  const actionable = attention === 'handoff' || attention === 'error' || attention === 'incomplete';
  const resultBlock = result ? `<details data-rich-key="result:${running ? 'running' : needsModel ? 'observation' : failed ? 'error' : 'result'}" class="ptc-detail ptc-result${failed ? ' ptc-result-error' : ''}"${gateway && row._snapshotToolResult ? ' data-lazy-tool-result' : ''}${actionable && !gateway ? ' open' : ''}><summary>${escapeCodeText(text(needsModel ? 'ptc.observation' : 'ptc.result', needsModel ? 'Evidence for judgment' : 'Program result'))}</summary><div class="ptool-result-body"><pre>${escapeCodeText(result)}</pre></div></details>` : '';
  const codeBlock = code ? `<details data-rich-key="source" class="ptc-detail ptc-code"><summary>${escapeCodeText(text('ptc.code', 'Model-generated execution code'))}</summary><pre><code class="language-${language}">${highlightCodeToHtml(code, language)}</code></pre></details>` : '';
  // Source stays a separate disclosure so observing evidence never opens code.
  // All attribute values below are fixed, or escaped just like visible text.
  return `<details data-rich-key="program" data-program-attention="${attention}" class="ptc-card${running ? ' ptc-running' : ''}${needsModel ? ' ptc-needs-model' : ''}"${actionable ? ' open' : ''}>`
    + `<summary class="ptc-head ptool-line"><span class="ptc-chevron" aria-hidden="true">›</span><span class="ptc-title">${escapeCodeText(text('ptc.title', 'Program execution'))}</span><span class="ptc-status${failed ? ' ptc-status-error' : ''}">${escapeCodeText(status)}</span>${summary.length ? `<span class="ptc-meta">${escapeCodeText(summary.join(' · '))}</span>` : ''}</summary>`
    + '<div class="ptc-body">'
    + (resultPreview && !previewInSummary ? `<p class="ptc-preview">${escapeCodeText(resultPreview)}</p>` : '')
    + (tools.length ? `<div class="ptc-tools">${tools.map(name => `<span class="ptc-tool-chip">${escapeCodeText(name)}</span>`).join('')}</div>` : '')
    + (reason ? `<p class="ptc-observation">${escapeCodeText(reason)}</p>` : '')
    + resultBlock + codeBlock
    + (row.programStats && typeof row.programStats === 'object' ? `<details data-rich-key="technical" class="ptc-detail ptc-technical"><summary>${escapeCodeText(text('ptc.technical', 'Technical details'))}</summary><div class="ptc-measurements">${metrics.map(metric => `<span>${escapeCodeText(metric)}</span>`).join('')}</div><details data-rich-key="counters" class="ptc-detail"><summary>${escapeCodeText(text('ptc.counters', 'Raw execution counters'))}</summary><pre>${escapeCodeText(boundedProgramDisplay(row.programStats))}</pre></details></details>` : '')
    + '</div></details>';
}

export function renderProgramExecutionCard(
  node: HTMLElement, round: unknown, text?: ProgramCardText,
): void {
  reconcileRichCardHtml(node, programExecutionCardHtml(round, text));
}
