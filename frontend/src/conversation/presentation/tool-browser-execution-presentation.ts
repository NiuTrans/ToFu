/**
 * Pure presentation policy for browser JavaScript execution tool rounds.
 *
 * Responsibility: parse and render bounded, localized browser-execution HTML.
 * Entry point: `createToolBrowserExecutionPresentation`. Dependencies: shared
 * HTML escaping, generated i18n, and the typed trusted tool-chevron asset. The
 * caller supplies already-rendered trusted header slots. This owner reads no
 * DOM, browser global, cache, or mutable runtime state and never mutates input.
 */

import { escapeHtmlText } from '../../html-safety';
import type { Translator } from '../../i18n';
import { TOOL_ROUND_CHEVRON_RIGHT_SVG } from './tool-round-icons';

type UnknownRecord = Readonly<Record<string, unknown>>;

type BoundedText = Readonly<{
  value: string;
  truncated: boolean;
}>;

type BrowserExecutionArguments = Readonly<{
  code: BoundedText;
  description: BoundedText;
  serializedArgumentsExceeded: boolean;
}>;

const SERIALIZED_ARGUMENTS_UNITS = 80_000;
const CODE_UNITS = 65_536;
const DESCRIPTION_UNITS = 4_096;
const RESULT_UNITS = 120_000;

export const TOOL_BROWSER_EXECUTION_PRESENTATION_LIMITS = Object.freeze({
  serializedArgumentsUnits: SERIALIZED_ARGUMENTS_UNITS,
  codeUnits: CODE_UNITS,
  descriptionUnits: DESCRIPTION_UNITS,
  resultUnits: RESULT_UNITS,
});

export type ToolBrowserExecutionHeaderHtml = Readonly<{
  iconHtml: string;
  rootPillHtml: string;
  rightControlsHtml: string;
}>;

export type ToolBrowserExecutionPresentation = Readonly<{
  renderBrowserExecutionHtml(
    round: unknown,
    firstResult: unknown,
    header: ToolBrowserExecutionHeaderHtml,
  ): string;
}>;

export type ToolBrowserExecutionPresentationDependencies = Readonly<{
  translate: Translator;
}>;

const EMPTY_RECORD: UnknownRecord = {};
const EMPTY_BOUNDED_TEXT: BoundedText = {
  value: '',
  truncated: false,
};
const EMPTY_ARGUMENTS: BrowserExecutionArguments = {
  code: EMPTY_BOUNDED_TEXT,
  description: EMPTY_BOUNDED_TEXT,
  serializedArgumentsExceeded: false,
};

function record(value: unknown): UnknownRecord {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as UnknownRecord
    : EMPTY_RECORD;
}

function stringField(value: unknown, field: string): string {
  const fieldValue = record(value)[field];
  return typeof fieldValue === 'string' ? fieldValue : '';
}

function boundedText(value: unknown, limit: number): BoundedText {
  const text = typeof value === 'string' ? value : '';
  const truncated = text.length > limit;
  return {
    value: text.slice(0, limit),
    truncated,
  };
}

function projectArguments(value: unknown): BrowserExecutionArguments {
  let candidate = value;
  if (typeof candidate === 'string') {
    if (
      candidate.length
      > SERIALIZED_ARGUMENTS_UNITS
    ) {
      return {
        ...EMPTY_ARGUMENTS,
        serializedArgumentsExceeded: true,
      };
    }
    try {
      candidate = JSON.parse(candidate);
    } catch {
      return EMPTY_ARGUMENTS;
    }
  }
  try {
    const fields = record(candidate);
    return {
      code: boundedText(
        fields.code,
        CODE_UNITS,
      ),
      description: boundedText(
        fields.description,
        DESCRIPTION_UNITS,
      ),
      serializedArgumentsExceeded: false,
    };
  } catch {
    return EMPTY_ARGUMENTS;
  }
}

export function createToolBrowserExecutionPresentation(
  dependencies: ToolBrowserExecutionPresentationDependencies,
): ToolBrowserExecutionPresentation {
  const { translate } = dependencies;

  function limitNoteHtml(
    key:
      | 'toolBrowserExecution.argumentsLimit'
      | 'toolBrowserExecution.codeLimit'
      | 'toolBrowserExecution.descriptionLimit'
      | 'toolBrowserExecution.resultLimit',
    units: number,
  ): string {
    return `<div class="ptool-result-trunc">${escapeHtmlText(translate(
      key,
      { n: units },
    ))}</div>`;
  }

  function renderBrowserExecutionHtml(
    roundValue: unknown,
    firstResultValue: unknown,
    header: ToolBrowserExecutionHeaderHtml,
  ): string {
    const round = record(roundValue);
    if (stringField(round, 'toolName') !== 'browser_execute_js') return '';
    const metadata = record(firstResultValue);
    const argumentsProjection = projectArguments(round.toolArgs);
    const isError = stringField(metadata, 'badge') === 'error';
    const statusLabel = translate(
      isError ? 'toolBrowserExecution.error' : 'toolBrowserExecution.ok',
    );
    const statusClassName = isError ? 'ptool-cmd-err' : 'ptool-cmd-ok';
    const result = boundedText(
      round.toolContent,
      RESULT_UNITS,
    );
    const resultLimitHtml = result.truncated
      ? limitNoteHtml(
        'toolBrowserExecution.resultLimit',
        RESULT_UNITS,
      )
      : '';
    const outputHtml = result.value
      ? `<div class="ptool-cmd-output-wrap">
           <button type="button" class="ptool-cmd-toggle" data-tofu-action="_cmdOutputToggle(this,event,'result')">${
             TOOL_ROUND_CHEVRON_RIGHT_SVG
           }${escapeHtmlText(translate('toolCmd.showResult'))}</button>
           <pre class="ptool-cmd-output"><code>${
             escapeHtmlText(result.value)
           }</code></pre>${resultLimitHtml}
         </div>`
      : '';
    const descriptionLimitHtml = argumentsProjection.description.truncated
      ? limitNoteHtml(
        'toolBrowserExecution.descriptionLimit',
        DESCRIPTION_UNITS,
      )
      : '';
    const descriptionHtml = argumentsProjection.description.value
      ? `<div class="ptool-cmd-desc">${
        escapeHtmlText(argumentsProjection.description.value)
      }</div>${descriptionLimitHtml}`
      : '';
    const codeLimitHtml = argumentsProjection.code.truncated
      ? limitNoteHtml(
        'toolBrowserExecution.codeLimit',
        CODE_UNITS,
      )
      : '';
    const codeHtml = argumentsProjection.code.value
      ? `<pre class="ptool-cmd-code"><code>${
        escapeHtmlText(argumentsProjection.code.value)
      }</code></pre>${codeLimitHtml}`
      : '';
    const argumentsLimitHtml = argumentsProjection.serializedArgumentsExceeded
      ? limitNoteHtml(
        'toolBrowserExecution.argumentsLimit',
        SERIALIZED_ARGUMENTS_UNITS,
      )
      : '';
    const query = stringField(round, 'query')
      || translate('toolBrowserExecution.executeJs');

    return `<div class="ptool-cmd-block ptool-cmd-js ${
      statusClassName
    }" data-rn="${escapeHtmlText(round.roundNum)}">
         <div class="ptool-cmd-header">
           <span class="ptool-cmd-icon">${header.iconHtml}</span>
           ${header.rootPillHtml}
           <span class="ptool-cmd-label">${escapeHtmlText(query)}</span>
           <span class="ptool-cmd-status">${
             escapeHtmlText(statusLabel)
           }</span>
           ${header.rightControlsHtml}
         </div>
         ${argumentsLimitHtml}${descriptionHtml}
         ${codeHtml}
         ${outputHtml}
       </div>`;
  }

  return Object.freeze({ renderBrowserExecutionHtml });
}
