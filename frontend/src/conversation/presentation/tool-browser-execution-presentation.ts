/**
 * Pure, bounded browser-JavaScript command-card presentation.
 * Shares chrome/interaction semantics with Run Command. Reads only durable
 * round data and explicit state snapshots; never reads today's browser state,
 * interprets numeric tab IDs as titles, or mutates executable arguments.
 */
import { escapeHtmlText } from '../../html-safety';
import type { Translator } from '../../i18n';
import { commandCardContextHtml, commandCardDescriptionHtml, commandCardFoldChipHtml } from './command-card-chrome';
import type { ToolCommandExecutionInteraction } from './tool-command-execution-presentation';

type UnknownRecord = Readonly<Record<string, unknown>>;
type BoundedText = Readonly<{ value: string; truncated: boolean }>;
type BrowserExecutionArguments = Readonly<{
  code: BoundedText;
  description: BoundedText;
  serializedArgumentsExceeded: boolean;
}>;

const SERIALIZED_ARGUMENTS_UNITS = 80_000;
const CODE_UNITS = 65_536;
const DESCRIPTION_UNITS = 4_096;
const RESULT_UNITS = 120_000;
const TAB_TITLE_UNITS = 240;
const INTERACTION_KEY_UNITS = 512;

export const TOOL_BROWSER_EXECUTION_PRESENTATION_LIMITS = Object.freeze({
  serializedArgumentsUnits: SERIALIZED_ARGUMENTS_UNITS,
  codeUnits: CODE_UNITS,
  descriptionUnits: DESCRIPTION_UNITS,
  resultUnits: RESULT_UNITS,
  tabTitleUnits: TAB_TITLE_UNITS,
  interactionKeyUnits: INTERACTION_KEY_UNITS,
});

export type ToolBrowserExecutionHeaderHtml = Readonly<{
  iconHtml: string;
  rootPillHtml: string;
  rightControlsHtml: string;
  timerHtml?: string;
}>;
export type ToolBrowserExecutionPresentation = Readonly<{
  renderBrowserExecutionHtml(
    round: unknown,
    firstResult: unknown,
    header: ToolBrowserExecutionHeaderHtml,
    interaction?: ToolCommandExecutionInteraction,
  ): string;
}>;
export type ToolBrowserExecutionPresentationDependencies = Readonly<{
  translate: Translator;
}>;

const EMPTY_RECORD: UnknownRecord = {};
const EMPTY_TEXT: BoundedText = { value: '', truncated: false };
const EMPTY_ARGUMENTS: BrowserExecutionArguments = {
  code: EMPTY_TEXT, description: EMPTY_TEXT, serializedArgumentsExceeded: false,
};
const CLOSED_INTERACTION: ToolCommandExecutionInteraction = {
  bodyExpanded: false, outputExpanded: false,
};

function record(value: unknown): UnknownRecord {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as UnknownRecord : EMPTY_RECORD;
}
function field(value: unknown, name: string): unknown {
  try { return record(value)[name]; } catch { return undefined; }
}
function stringField(value: unknown, name: string): string {
  const candidate = field(value, name);
  return typeof candidate === 'string' ? candidate : '';
}
function boundedText(value: unknown, limit: number): BoundedText {
  const text = typeof value === 'string' ? value : '';
  return { value: text.slice(0, limit), truncated: text.length > limit };
}
function projectArguments(value: unknown): BrowserExecutionArguments {
  let candidate = value;
  if (typeof candidate === 'string') {
    if (candidate.length > SERIALIZED_ARGUMENTS_UNITS) {
      return { ...EMPTY_ARGUMENTS, serializedArgumentsExceeded: true };
    }
    try { candidate = JSON.parse(candidate); } catch { return EMPTY_ARGUMENTS; }
  }
  return {
    code: boundedText(field(candidate, 'code'), CODE_UNITS),
    description: boundedText(field(candidate, 'description'), DESCRIPTION_UNITS),
    serializedArgumentsExceeded: false,
  };
}
function observedTabTitle(round: unknown, metadata: unknown): BoundedText {
  const stored = stringField(metadata, 'tabTitle').trim();
  if (stored) return boundedText(stored, TAB_TITLE_UNITS);
  // Prior rounds saved the observed title in the quoted display query.
  const query = boundedText(stringField(round, 'query'), 1_024).value;
  const quoted = /^Execute JS in "([\s\S]+)"$/.exec(query);
  return boundedText(quoted?.[1] ?? '', TAB_TITLE_UNITS);
}

export function createToolBrowserExecutionPresentation(
  dependencies: ToolBrowserExecutionPresentationDependencies,
): ToolBrowserExecutionPresentation {
  const { translate } = dependencies;
  function limitNoteHtml(
    key: 'toolBrowserExecution.argumentsLimit' | 'toolBrowserExecution.codeLimit'
      | 'toolBrowserExecution.descriptionLimit' | 'toolBrowserExecution.resultLimit',
    units: number,
  ): string {
    return `<div class="ptool-result-trunc">${escapeHtmlText(translate(key, { n: units }))}</div>`;
  }
  function renderBrowserExecutionHtml(
    round: unknown,
    metadata: unknown,
    header: ToolBrowserExecutionHeaderHtml,
    interaction: ToolCommandExecutionInteraction = CLOSED_INTERACTION,
  ): string {
    if (stringField(round, 'toolName') !== 'browser_execute_js') return '';
    const args = projectArguments(field(round, 'toolArgs'));
    const running = stringField(round, 'status') === 'searching';
    const error = stringField(round, 'status') === 'error'
      || stringField(metadata, 'badge') === 'error';
    const result = running ? EMPTY_TEXT : boundedText(field(round, 'toolContent'), RESULT_UNITS);
    const key = boundedText(stringField(round, 'toolCallId'), INTERACTION_KEY_UNITS).value;
    const hasCode = Boolean(args.code.value);
    const hasOutput = Boolean(result.value);
    const bodyOpen = hasCode && interaction.bodyExpanded === true;
    const outputOpen = hasOutput && interaction.outputExpanded === true;
    const title = observedTabTitle(round, metadata);
    const tabName = title.value || translate('toolBrowserExecution.tabUnknown');
    const tabHtml = `<span class="ptool-cmd-tab" title="${escapeHtmlText(tabName)}">${escapeHtmlText(tabName)}${title.truncated ? '…' : ''}</span>`;
    const description = commandCardDescriptionHtml(args.description.value || translate('toolBrowserExecution.executeJs'));
    const status = running
      ? `${header.timerHtml ?? ''}<span class="ptool-spinner"></span>`
      : `<span class="ptool-cmd-status">${escapeHtmlText(translate(error ? 'toolBrowserExecution.error' : 'toolBrowserExecution.ok'))}</span>`;
    const classes = `ptool-cmd-block ptool-cmd-js ${running ? 'ptool-cmd-running' : error ? 'ptool-cmd-err' : 'ptool-cmd-ok'}${bodyOpen ? ' cmd-open' : ''}${hasOutput ? ' ptool-cmd-hasoutput' : ''}${outputOpen ? ' output-open' : ''}`;
    const code = hasCode
      ? `<div class="ptool-cmd-body ptool-cmd-js-code"><pre class="ptool-cmd-code"><code>${escapeHtmlText(args.code.value)}</code></pre>${args.code.truncated ? limitNoteHtml('toolBrowserExecution.codeLimit', CODE_UNITS) : ''}</div>` : '';
    const output = hasOutput
      ? `<div class="ptool-cmd-output-wrap"><pre class="ptool-cmd-output"><code>${escapeHtmlText(result.value)}</code></pre>${result.truncated ? limitNoteHtml('toolBrowserExecution.resultLimit', RESULT_UNITS) : ''}</div>` : '';
    return `<div class="${classes}" data-rn="${escapeHtmlText(field(round, 'roundNum'))}"${hasCode ? ` data-cmd-key="${escapeHtmlText(key)}"` : ''}${hasOutput ? ` data-output-key="${escapeHtmlText(key)}"` : ''}>
      <div class="ptool-cmd-header">
        <span class="ptool-cmd-icon">${header.iconHtml}</span>${description}
        <div class="ptool-cmd-meta"><span class="ptool-cmd-identity">${commandCardContextHtml('browser', translate)}${header.rootPillHtml}${tabHtml}</span>
        <span class="ptool-cmd-right">${hasCode ? commandCardFoldChipHtml('code', bodyOpen, translate) : ''}${hasOutput ? commandCardFoldChipHtml('output', outputOpen, translate) : ''}${header.rightControlsHtml}<span class="ptool-cmd-verdict">${status}</span></span></div>
      </div>
      ${args.serializedArgumentsExceeded ? limitNoteHtml('toolBrowserExecution.argumentsLimit', SERIALIZED_ARGUMENTS_UNITS) : ''}${args.description.truncated ? limitNoteHtml('toolBrowserExecution.descriptionLimit', DESCRIPTION_UNITS) : ''}${code}${output}
    </div>`;
  }
  return Object.freeze({ renderBrowserExecutionHtml });
}
