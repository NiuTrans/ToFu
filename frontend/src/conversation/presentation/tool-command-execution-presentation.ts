/**
 * Pure presentation policy for run_command and code_exec tool rounds.
 *
 * Responsibility: project and render bounded command execution HTML. Entry
 * point: `createToolCommandExecutionPresentation`. Dependencies: generated
 * i18n, shared HTML/image-source safety, and typed trusted chevron/status
 * helpers. The retained caller owns timers, interrupt I/O, and interaction
 * state; it supplies those values only through explicitly named slots.
 */

import { escapeHtmlText } from '../../html-safety';
import type { Translator } from '../../i18n';
import { safeImageSource } from './image-source-policy';
import { plainToolStatus } from './tool-round-presentation';

type UnknownRecord = Readonly<Record<string, unknown>>;

type BoundedText = Readonly<{
  value: string;
  truncated: boolean;
  omittedUnits: number;
}>;

type CommandArgumentsProjection = Readonly<{
  command: BoundedText;
  description: BoundedText;
  serializedArgumentsExceeded: boolean;
}>;

type QrDescriptor = Readonly<{
  source: string;
  caption: string;
}>;

type QrProjection = Readonly<{
  descriptors: readonly QrDescriptor[];
  totalCandidates: number;
  omitted: boolean;
}>;

const SERIALIZED_ARGUMENTS_UNITS = 80_000;
const COMMAND_UNITS = 65_536;
const DESCRIPTION_UNITS = 4_096;
const LIVE_OUTPUT_UNITS = 20_000;
const RESULT_UNITS = 120_000;
const LEGACY_STATUS_TAIL_UNITS = 2_048;
const INTERACTION_KEY_UNITS = 512;
const QR_DESCRIPTORS_SCANNED = 64;
const QR_TILES = 16;
const QR_SOURCE_UNITS = 1_000_000;
const QR_CAPTION_UNITS = 512;

export const TOOL_COMMAND_EXECUTION_PRESENTATION_LIMITS = Object.freeze({
  serializedArgumentsUnits: SERIALIZED_ARGUMENTS_UNITS,
  commandUnits: COMMAND_UNITS,
  descriptionUnits: DESCRIPTION_UNITS,
  liveOutputUnits: LIVE_OUTPUT_UNITS,
  resultUnits: RESULT_UNITS,
  legacyStatusTailUnits: LEGACY_STATUS_TAIL_UNITS,
  interactionKeyUnits: INTERACTION_KEY_UNITS,
  qrDescriptorsScanned: QR_DESCRIPTORS_SCANNED,
  qrTiles: QR_TILES,
  qrSourceUnits: QR_SOURCE_UNITS,
  qrCaptionUnits: QR_CAPTION_UNITS,
});

export type ToolCommandExecutionHeaderHtml = Readonly<{
  iconHtml: string;
  rootPillHtml: string;
  timerHtml: string;
  interruptHtml: string;
  rightControlsHtml: string;
}>;

export type ToolCommandExecutionInteraction = Readonly<{
  bodyExpanded: boolean;
  outputExpanded: boolean;
}>;

export type ToolCommandExecutionPresentation = Readonly<{
  renderRunningCommandHtml(
    round: unknown,
    header: ToolCommandExecutionHeaderHtml,
    interaction: ToolCommandExecutionInteraction,
  ): string;
  renderSettledCommandHtml(
    round: unknown,
    firstResult: unknown,
    header: ToolCommandExecutionHeaderHtml,
    interaction: ToolCommandExecutionInteraction,
  ): string;
}>;

export type ToolCommandExecutionPresentationDependencies = Readonly<{
  translate: Translator;
}>;

const EMPTY_RECORD: UnknownRecord = {};
const EMPTY_BOUNDED_TEXT: BoundedText = {
  value: '',
  truncated: false,
  omittedUnits: 0,
};
const EMPTY_ARGUMENTS: CommandArgumentsProjection = {
  command: EMPTY_BOUNDED_TEXT,
  description: EMPTY_BOUNDED_TEXT,
  serializedArgumentsExceeded: false,
};

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

function stringValue(value: unknown): string {
  return typeof value === 'string' ? value : '';
}

function stringField(value: unknown, name: string): string {
  return stringValue(field(value, name));
}

function boundedText(value: unknown, limit: number, tail = false): BoundedText {
  const text = stringValue(value);
  const truncated = text.length > limit;
  return {
    value: tail && truncated ? text.slice(-limit) : text.slice(0, limit),
    truncated,
    omittedUnits: truncated ? text.length - limit : 0,
  };
}

function projectArguments(value: unknown): CommandArgumentsProjection {
  let candidate = value;
  if (typeof candidate === 'string') {
    if (candidate.length > SERIALIZED_ARGUMENTS_UNITS) {
      return { ...EMPTY_ARGUMENTS, serializedArgumentsExceeded: true };
    }
    try {
      candidate = JSON.parse(candidate);
    } catch {
      return EMPTY_ARGUMENTS;
    }
  }
  return {
    command: boundedText(field(candidate, 'command'), COMMAND_UNITS),
    description: boundedText(
      field(candidate, 'description'),
      DESCRIPTION_UNITS,
    ),
    serializedArgumentsExceeded: false,
  };
}

function firstDefinedString(...values: unknown[]): string {
  for (const value of values) {
    if (value !== null && value !== undefined) return stringValue(value);
  }
  return '';
}

function commandRound(value: unknown): boolean {
  const toolName = stringField(value, 'toolName');
  return toolName === 'run_command' || toolName === 'code_exec';
}

function progressStatusOf(round: unknown): string {
  const reason = stringField(round, '_partialOutputTerminalReason');
  if (reason === 'cancelling') return 'cancelling';
  if (reason === 'cancelled' || reason === 'cancelled-partial') {
    return 'cancelled-partial';
  }
  if (field(round, '_partialOutputTruncated') === true) return 'overflow-loss';
  if (field(round, '_partialOutputSpooling') === true) return 'spooling';
  const stored = stringField(round, '_partialOutputStatus');
  if (stored && stored !== 'running') return stored;
  return 'running';
}

function progressStatusLabel(status: string): string {
  switch (status) {
    case 'spooling': return 'Spooling…';
    case 'cancelling': return 'Cancelling…';
    case 'cancelled-partial': return 'Cancelled · partial output';
    case 'overflow-loss': return 'Output truncated';
    default: return '';
  }
}

function interactionKey(round: unknown): string {
  return boundedText(
    stringField(round, 'toolCallId'),
    INTERACTION_KEY_UNITS,
  ).value;
}

function commandIsCollapsible(
  command: BoundedText,
  description: BoundedText,
): boolean {
  return Boolean(description.value && command.value) && (
    command.truncated
    || command.value.length > 100
    || command.value.includes('\n')
  );
}

function projectQrDescriptors(value: unknown): QrProjection {
  const candidatesValue = field(value, 'qrImages');
  const candidates = Array.isArray(candidatesValue) ? candidatesValue : [];
  const descriptors: QrDescriptor[] = [];
  let index = 0;
  while (
    index < candidates.length
    && index < QR_DESCRIPTORS_SCANNED
    && descriptors.length < QR_TILES
  ) {
    const candidate = candidates[index];
    index += 1;
    const rawSource = stringField(candidate, 'uri');
    if (!rawSource || rawSource.length > QR_SOURCE_UNITS) continue;
    const source = safeImageSource(rawSource);
    if (!source) continue;
    const caption = boundedText(
      stringField(candidate, 'filename') || 'qr.png',
      QR_CAPTION_UNITS,
    ).value;
    descriptors.push({ source, caption });
  }
  return {
    descriptors,
    totalCandidates: candidates.length,
    omitted: index < candidates.length,
  };
}

function projectLegacyOutput(rawContent: string, command: string): BoundedText {
  const prefix = `$ ${command}\n`;
  const withoutPrefix = rawContent.startsWith(prefix)
    ? rawContent.slice(prefix.length)
    : rawContent;
  const projection = boundedText(withoutPrefix, RESULT_UNITS);
  let value = projection.value;
  if (!projection.truncated) {
    value = value
      .replace(/\n?\[exit code: -?\d+\]\s*$/, '')
      .replace(/\n?\[Command timed out\].*$/, '')
      .replace(/\n?\[Command interrupted by[^\n]*\].*$/, '')
      .trim();
  } else {
    value = value.trimStart();
  }
  return { ...projection, value };
}

export function createToolCommandExecutionPresentation(
  dependencies: ToolCommandExecutionPresentationDependencies,
): ToolCommandExecutionPresentation {
  const { translate } = dependencies;

  function limitNoteHtml(
    key:
      | 'toolCommandExecution.argumentsLimit'
      | 'toolCommandExecution.commandLimit'
      | 'toolCommandExecution.descriptionLimit'
      | 'toolCommandExecution.outputLimit',
    units: number,
  ): string {
    return `<div class="ptool-result-trunc">${escapeHtmlText(translate(
      key,
      { n: units },
    ))}</div>`;
  }

  function descriptionHtml(
    description: BoundedText,
    collapsible: boolean,
  ): string {
    if (!description.value) return '';
    const escaped = escapeHtmlText(description.value);
    if (!collapsible) {
      return `<span class="ptool-cmd-desc-inline" title="${escaped}">${escaped}</span>`;
    }
    return `<span class="ptool-cmd-desc-inline ptool-cmd-desc-toggle" title="${escaped}" data-tofu-action="_cmdBodyToggle(this,event)">${escaped}</span>`;
  }

  function grepInterceptBadgeHtml(round: unknown, metadata?: unknown): string {
    if (
      field(round, 'grepSearchIntercepted') !== true
      && field(metadata, 'grepSearchIntercepted') !== true
    ) return '';
    return `<span class="ptool-grep-intercept">${escapeHtmlText(
      translate('toolCmd.grepSearchIntercepted'),
    )}</span>`;
  }

  function qrStripHtml(value: unknown): string {
    const projection = projectQrDescriptors(value);
    if (!projection.descriptors.length) return '';
    const tilesHtml = projection.descriptors.map((descriptor) => {
      const source = escapeHtmlText(descriptor.source);
      const caption = escapeHtmlText(descriptor.caption);
      return `<figure class="ptool-qr-tile">
             <img src="${source}" alt="${caption}" loading="lazy"
                  data-tofu-action="event.stopPropagation();_openImageFullscreen(this.src)" />
           </figure>`;
    }).join('');
    const count = projection.descriptors.length;
    const label = count > 1
      ? `${count} ${escapeHtmlText(translate('project.qrScanMulti'))}`
      : escapeHtmlText(translate('project.qrScan'));
    const limitHtml = projection.omitted
      ? `<div class="ptool-result-trunc">${escapeHtmlText(translate(
        'toolCommandExecution.qrLimit',
        { shown: count, total: projection.totalCandidates },
      ))}</div>`
      : '';
    return `<div class="ptool-qr-strip">
           <div class="ptool-qr-label">${label}</div>
           <div class="ptool-qr-grid">${tilesHtml}</div>${limitHtml}
         </div>`;
  }

  function renderRunningCommandHtml(
    round: unknown,
    header: ToolCommandExecutionHeaderHtml,
    interaction: ToolCommandExecutionInteraction,
  ): string {
    if (!commandRound(round)) return '';
    const argumentsProjection = projectArguments(field(round, 'toolArgs'));
    const command = boundedText(stringField(round, 'query'), COMMAND_UNITS);
    const description = argumentsProjection.description;
    const collapsible = commandIsCollapsible(command, description);
    const key = collapsible ? interactionKey(round) : '';
    const bodyOpen = Boolean(collapsible && key && interaction.bodyExpanded);
    const partial = boundedText(
      stringField(round, '_partialOutput'),
      LIVE_OUTPUT_UNITS,
      true,
    );
    const shownPartial = partial.truncated
      ? `${translate('toolCommandExecution.liveOutputElided', {
        n: partial.omittedUnits,
      })}${partial.value}`
      : partial.value;
    const liveOutputHtml = shownPartial
      ? `<pre class="ptool-cmd-output ptool-cmd-output-live"><code>${escapeHtmlText(
        shownPartial,
      )}</code></pre>`
      : '';
    const argumentsLimitHtml = argumentsProjection.serializedArgumentsExceeded
      ? limitNoteHtml(
        'toolCommandExecution.argumentsLimit',
        SERIALIZED_ARGUMENTS_UNITS,
      )
      : '';
    const commandLimitHtml = command.truncated
      ? limitNoteHtml('toolCommandExecution.commandLimit', COMMAND_UNITS)
      : '';
    const descriptionLimitHtml = description.truncated
      ? limitNoteHtml(
        'toolCommandExecution.descriptionLimit',
        DESCRIPTION_UNITS,
      )
      : '';

    const progressStatus = progressStatusOf(round);
    const runningLabel = progressStatus === 'running'
      ? translate('toolCommandExecution.running')
      : progressStatusLabel(progressStatus);
    return `<div class="ptool-cmd-block ptool-cmd-running${bodyOpen ? ' cmd-open' : ''}"${collapsible ? ` data-cmd-key="${escapeHtmlText(key)}"` : ''} data-progress-status="${escapeHtmlText(progressStatus)}">
           <div class="ptool-cmd-header">
             <span class="ptool-cmd-icon">${header.iconHtml}</span>
             ${header.rootPillHtml}
             ${descriptionHtml(description, collapsible)}
             ${grepInterceptBadgeHtml(round)}
             <span class="ptool-cmd-label">${escapeHtmlText(runningLabel)}</span>
             ${header.timerHtml}${header.interruptHtml}
             <span class="ptool-spinner"></span>
           </div>${argumentsLimitHtml}${descriptionLimitHtml}
           <pre class="ptool-cmd-code${collapsible ? ' ptool-cmd-collapsible' : ''}"><code>$ ${escapeHtmlText(command.value)}</code></pre>${commandLimitHtml}
           ${qrStripHtml(round)}${liveOutputHtml}
         </div>`;
  }

  function renderSettledCommandHtml(
    round: unknown,
    firstResult: unknown,
    header: ToolCommandExecutionHeaderHtml,
    interaction: ToolCommandExecutionInteraction,
  ): string {
    if (!commandRound(round)) return '';
    const metadata = record(firstResult);
    const argumentsProjection = projectArguments(field(round, 'toolArgs'));
    const command = boundedText(firstDefinedString(
      field(metadata, 'command'),
      argumentsProjection.command.value || undefined,
      field(round, 'query'),
    ), COMMAND_UNITS);
    const description = boundedText(firstDefinedString(
      field(metadata, 'description'),
      argumentsProjection.description.value,
    ), DESCRIPTION_UNITS);
    const rawContent = stringField(round, 'toolContent');
    const rawTail = rawContent.slice(-LEGACY_STATUS_TAIL_UNITS);
    const exitMatch = rawTail.match(/\[exit code: (-?\d+)\]\s*$/);
    const timedOut = field(metadata, 'timedOut') === true
      || rawTail.includes('[Command timed out]');
    const interrupted = field(metadata, 'interrupted') === true
      || rawTail.includes('[Command interrupted by');
    const rawExitCode = field(metadata, 'exitCode');
    const hasStructuredExit = (rawExitCode !== null
      && rawExitCode !== undefined)
      || field(metadata, 'notRun') === true;
    let exitCode: string | number = (
      typeof rawExitCode === 'string' || typeof rawExitCode === 'number'
    )
      ? rawExitCode
      : timedOut
        ? 'timeout'
        : exitMatch
          ? exitMatch[1]
          : '?';
    const rawOutput = field(metadata, 'output');
    const outputProjection = rawOutput !== null && rawOutput !== undefined
      ? boundedText(rawOutput, RESULT_UNITS)
      : projectLegacyOutput(rawContent, command.value);
    const output = outputProjection.value;
    const notRun = field(metadata, 'notRun') === true
      || exitCode === 'not-run'
      || (
        !hasStructuredExit
        && Boolean(rawContent)
        && !exitMatch
        && !timedOut
        && !interrupted
      );
    if (notRun && exitCode === '?') exitCode = 'not-run';
    const isOk = !notRun && (exitCode === '0' || exitCode === 0);
    const statusClassName = notRun
      ? 'ptool-cmd-notrun'
      : interrupted
        ? 'ptool-cmd-interrupted'
        : timedOut
          ? 'ptool-cmd-timeout'
          : isOk
            ? 'ptool-cmd-ok'
            : 'ptool-cmd-err';
    const metadataBadge = stringField(metadata, 'badge');
    const notRunBadge = metadataBadge
      && field(metadata, 'recovered') !== true
      && metadataBadge !== `exit ${exitCode}`
      ? metadataBadge
      : translate('toolCommandExecution.notRun');
    const hasRealExit = !notRun && !interrupted && !timedOut
      && exitCode !== '?' && exitCode !== null;
    const finishChipHtml = hasRealExit
      ? `<span class="ptool-cmd-finish">${escapeHtmlText(
        translate('toolCmd.finished'),
      )}</span>`
      : '';
    const statusLabel = notRun
      ? notRunBadge
      : interrupted
        ? plainToolStatus(
          translate('toolCmd.interruptedBadge'),
          'interrupted',
        )
        : timedOut
          ? translate('toolCommandExecution.timeout')
          : translate('toolCommandExecution.exitCode', { code: exitCode });
    const reasonProjection = notRun
      ? boundedText(
        stringField(metadata, 'reason') || output || rawContent,
        RESULT_UNITS,
      )
      : EMPTY_BOUNDED_TEXT;
    const collapsible = commandIsCollapsible(command, description);
    const key = interactionKey(round);
    const bodyOpen = Boolean(collapsible && key && interaction.bodyExpanded);
    const hasOutput = !notRun && Boolean(output);
    const outputOpen = Boolean(hasOutput && key && interaction.outputExpanded);
    const outputLimitHtml = outputProjection.truncated
      ? limitNoteHtml('toolCommandExecution.outputLimit', RESULT_UNITS)
      : '';
    const reasonLimitHtml = reasonProjection.truncated
      ? limitNoteHtml('toolCommandExecution.outputLimit', RESULT_UNITS)
      : '';
    let outputHtml = '';
    if (notRun && reasonProjection.value) {
      outputHtml = `<div class="ptool-cmd-reason">${escapeHtmlText(
        reasonProjection.value,
      )}</div>${reasonLimitHtml}`;
    } else if (hasOutput) {
      outputHtml = `<div class="ptool-cmd-output-wrap">
           <pre class="ptool-cmd-output"><code>${escapeHtmlText(output)}</code></pre>${outputLimitHtml}
         </div>`;
    }
    const argumentsLimitHtml = argumentsProjection.serializedArgumentsExceeded
      ? limitNoteHtml(
        'toolCommandExecution.argumentsLimit',
        SERIALIZED_ARGUMENTS_UNITS,
      )
      : '';
    const commandLimitHtml = command.truncated
      ? limitNoteHtml('toolCommandExecution.commandLimit', COMMAND_UNITS)
      : '';
    const descriptionLimitHtml = description.truncated
      ? limitNoteHtml(
        'toolCommandExecution.descriptionLimit',
        DESCRIPTION_UNITS,
      )
      : '';

    return `<div class="ptool-cmd-block ${statusClassName}${bodyOpen ? ' cmd-open' : ''}${hasOutput ? ' ptool-cmd-hasoutput' : ''}${outputOpen ? ' output-open' : ''}" data-rn="${escapeHtmlText(field(round, 'roundNum'))}"${collapsible ? ` data-cmd-key="${escapeHtmlText(key)}"` : ''}${hasOutput ? ` data-output-key="${escapeHtmlText(key)}"` : ''}>
         <div class="ptool-cmd-header"${hasOutput ? ` role="button" aria-expanded="${outputOpen ? 'true' : 'false'}" data-tofu-action="_cmdHeaderToggle(this,event)"` : ''}>
           <span class="ptool-cmd-icon">${header.iconHtml}</span>
           ${header.rootPillHtml}
           ${descriptionHtml(description, collapsible)}
            ${grepInterceptBadgeHtml(round, metadata)}
            <span class="ptool-cmd-chips">${finishChipHtml}<span class="ptool-cmd-status">${escapeHtmlText(statusLabel)}</span></span>
            ${header.rightControlsHtml}
         </div>${argumentsLimitHtml}${descriptionLimitHtml}
         <pre class="ptool-cmd-code${collapsible ? ' ptool-cmd-collapsible' : ''}"><code>$ ${escapeHtmlText(command.value)}</code></pre>${commandLimitHtml}
         ${qrStripHtml(metadata)}${outputHtml}
       </div>`;
  }

  return Object.freeze({
    renderRunningCommandHtml,
    renderSettledCommandHtml,
  });
}
