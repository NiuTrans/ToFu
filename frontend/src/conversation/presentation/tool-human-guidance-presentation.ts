/**
 * Pure presentation policy for ask_human tool rounds.
 *
 * Responsibility: normalize legacy guidance payloads and render the awaiting,
 * expired, skipped, submitted, and superseded states plus the non-blocking
 * chat-reply card and its replied row through one bounded projection.
 * Entry point: `createToolHumanGuidancePresentation`. Dependencies are explicit
 * translation and Markdown ports. This owner reads no DOM/browser globals,
 * never mutates input, and keeps untrusted values in data attributes rather
 * than interpolating them into delegated action commands.
 */

import { escapeHtmlText } from '../../html-safety';
import type { I18nArgs, I18nKey, Translator } from '../../i18n';
import {
  record, field, scalarText as safeText, type BoundedText,
} from './tool-payload-values';

type GuidanceOption = Readonly<{
  originalLabel: BoundedText;
  displayLabel: BoundedText;
  displayDescription: BoundedText;
}>;

type GuidanceOptionsProjection = Readonly<{
  options: readonly GuidanceOption[];
  total: number;
  truncated: boolean;
  contentLimit: number | null;
}>;

export type ToolHumanGuidanceSlots = Readonly<{
  iconHtml: string;
  toolDisplayLabel?: unknown;
}>;

export type ToolHumanGuidancePresentation = Readonly<{
  renderGuidanceHtml(round: unknown, slots: ToolHumanGuidanceSlots): string;
}>;

export type ToolHumanGuidancePresentationDependencies = Readonly<{
  translate: Translator;
  renderMarkdown: (source: string) => string;
}>;

const IDENTIFIER_UNITS = 512;
const QUESTION_UNITS = 32_768;
const OPTIONS_JSON_UNITS = 65_536;
const OPTION_ITEMS = 16;
const OPTION_LABEL_UNITS = 1_024;
const OPTION_DESCRIPTION_UNITS = 8_192;
const OPTION_NOTE_UNITS = 4_096;
const TOOL_LABEL_UNITS = 512;
const SKIPPED_PREVIEW_UNITS = 60;
const RESPONSE_PREVIEW_UNITS = 80;
const RESPONSE_UNITS = 32_768;
const REPLY_QUOTE_UNITS = 4_096;

export const TOOL_HUMAN_GUIDANCE_PRESENTATION_LIMITS = Object.freeze({
  identifierUnits: IDENTIFIER_UNITS,
  questionUnits: QUESTION_UNITS,
  optionsJsonUnits: OPTIONS_JSON_UNITS,
  optionItems: OPTION_ITEMS,
  optionLabelUnits: OPTION_LABEL_UNITS,
  optionDescriptionUnits: OPTION_DESCRIPTION_UNITS,
  optionNoteUnits: OPTION_NOTE_UNITS,
  toolLabelUnits: TOOL_LABEL_UNITS,
  skippedPreviewUnits: SKIPPED_PREVIEW_UNITS,
  responsePreviewUnits: RESPONSE_PREVIEW_UNITS,
  responseUnits: RESPONSE_UNITS,
  replyQuoteUnits: REPLY_QUOTE_UNITS,
});

const EMPTY_OPTIONS: GuidanceOptionsProjection = Object.freeze({
  options: Object.freeze([]),
  total: 0,
  truncated: false,
  contentLimit: null,
});

function boundedText(value: unknown, limit: number): BoundedText {
  const text = safeText(value);
  const truncated = text.length > limit;
  return Object.freeze({
    value: truncated ? `${text.slice(0, limit)}…` : text,
    truncated,
  });
}

function arrayItem(value: readonly unknown[], index: number): unknown {
  try {
    return value[index];
  } catch {
    return undefined;
  }
}

function arrayLength(value: readonly unknown[]): number {
  try {
    return Number.isSafeInteger(value.length) && value.length >= 0
      ? value.length
      : 0;
  } catch {
    return 0;
  }
}

function optionArray(value: unknown): readonly unknown[] | null {
  if (Array.isArray(value)) return value;
  if (typeof value !== 'string' || value.length > OPTIONS_JSON_UNITS) {
    return null;
  }
  try {
    const parsed: unknown = JSON.parse(value);
    return Array.isArray(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

export function createToolHumanGuidancePresentation(
  dependencies: ToolHumanGuidancePresentationDependencies,
): ToolHumanGuidancePresentation {
  const { translate, renderMarkdown } = dependencies;

  function translatedText<K extends I18nKey>(
    key: K,
    ...args: I18nArgs<K>
  ): string {
    try {
      return safeText(translate(key, ...args)) || key;
    } catch {
      return key;
    }
  }

  function translatedHtml<K extends I18nKey>(
    key: K,
    ...args: I18nArgs<K>
  ): string {
    return escapeHtmlText(translatedText(key, ...args));
  }

  function trustedMarkdown(source: string): string {
    try {
      const html = renderMarkdown(source);
      return typeof html === 'string' ? html : escapeHtmlText(source);
    } catch {
      return escapeHtmlText(source);
    }
  }

  function limitHtml(limit: number): string {
    return `<div class="ptool-preview-limit">${translatedHtml(
      'toolHumanGuidance.contentLimit',
      { n: limit },
    )}</div>`;
  }

  function projectOptions(value: unknown): GuidanceOptionsProjection {
    const candidates = optionArray(value);
    if (!candidates) {
      return typeof value === 'string' && value.length > OPTIONS_JSON_UNITS
        ? Object.freeze({
          options: Object.freeze([]),
          total: 0,
          truncated: true,
          contentLimit: OPTIONS_JSON_UNITS,
        })
        : EMPTY_OPTIONS;
    }
    const total = arrayLength(candidates);
    const options: GuidanceOption[] = [];
    let truncated = total > OPTION_ITEMS;
    let contentLimit: number | null = null;
    const shown = Math.min(total, OPTION_ITEMS);
    for (let index = 0; index < shown; index += 1) {
      const source = record(arrayItem(candidates, index));
      const fallback = translatedText(
        'toolHumanGuidance.optionFallback',
        { index: index + 1 },
      );
      const originalLabel = boundedText(
        safeText(field(source, 'label')) || fallback,
        OPTION_LABEL_UNITS,
      );
      const displayLabel = boundedText(
        safeText(field(source, '_translatedLabel')) || originalLabel.value,
        OPTION_LABEL_UNITS,
      );
      const displayDescription = boundedText(
        safeText(field(source, '_translatedDescription'))
          || safeText(field(source, 'description')),
        OPTION_DESCRIPTION_UNITS,
      );
      truncated = truncated
        || originalLabel.truncated
        || displayLabel.truncated
        || displayDescription.truncated;
      if (
        contentLimit === null
        && (originalLabel.truncated || displayLabel.truncated)
      ) contentLimit = OPTION_LABEL_UNITS;
      if (contentLimit === null && displayDescription.truncated) {
        contentLimit = OPTION_DESCRIPTION_UNITS;
      }
      options.push(Object.freeze({
        originalLabel,
        displayLabel,
        displayDescription,
      }));
    }
    return Object.freeze({
      options: Object.freeze(options),
      total,
      truncated,
      contentLimit,
    });
  }

  function optionsLimitHtml(projection: GuidanceOptionsProjection): string {
    if (projection.total > OPTION_ITEMS) {
      return `<div class="ptool-preview-limit">${translatedHtml(
        'toolHumanGuidance.optionsLimit',
        { shown: OPTION_ITEMS, total: projection.total },
      )}</div>`;
    }
    return projection.contentLimit === null
      ? ''
      : limitHtml(projection.contentLimit);
  }

  function renderOptionCards(
    projection: GuidanceOptionsProjection,
    guidanceIdHtml: string,
    expired: boolean,
  ): string {
    const optionsHtml = projection.options.map((option, optionIndex) => {
      const descriptionHtml = option.displayDescription.value
        ? `<div class="hg-opt-desc">${trustedMarkdown(option.displayDescription.value)}</div>`
        : '';
      const staticCard = `<div class="hg-option-card hg-option-static${expired ? '' : ' hg-option-unavailable'}">
                  <div class="hg-opt-label">${escapeHtmlText(option.displayLabel.value)}</div>
                  ${descriptionHtml}
                </div>`;
      if (expired || option.originalLabel.truncated) return staticCard;
      const optionIndexHtml = String(optionIndex);
      return `<div class="hg-option-group" data-gid="${guidanceIdHtml}" data-option-index="${optionIndexHtml}">
              <button class="hg-option-card" data-gid="${guidanceIdHtml}" data-label="${escapeHtmlText(option.originalLabel.value)}" data-option-index="${optionIndexHtml}"
                      data-tofu-action="event.stopPropagation();submitHumanGuidanceChoice(this)">
                <div class="hg-opt-label">${escapeHtmlText(option.displayLabel.value)}</div>
                ${descriptionHtml}
              </button>
              <textarea class="hg-textarea hg-option-note-input" data-gid="${guidanceIdHtml}" data-option-index="${optionIndexHtml}" rows="2" maxlength="${OPTION_NOTE_UNITS}"
                        placeholder="${translatedHtml('project.hgChoiceNotePlaceholder')}"
                        data-tofu-action-keydown="if(event.key==='Enter'&&(event.ctrlKey||event.metaKey)){event.preventDefault();submitHumanGuidanceChoice(this)}"></textarea>
            </div>`;
    }).join('');
    /* Each live choice owns its own bounded optional note. The option index
     * pairs the note with the exact original label without placing either
     * value in executable action text; static/expired choices remain read-only. */
    return `<div class="hg-options-grid">${optionsHtml}</div>${optionsLimitHtml(projection)}`;
  }

  function renderInteractiveFreeText(guidanceIdHtml: string): string {
    return `<div class="hg-freetext-wrap">
      <textarea class="hg-textarea" id="hg-input-${guidanceIdHtml}" data-gid="${guidanceIdHtml}" rows="3" maxlength="${RESPONSE_UNITS}"
                placeholder="${translatedHtml('project.hgTextareaPlaceholder')}"
                data-tofu-action-keydown="if(event.key==='Enter'&&(event.ctrlKey||event.metaKey)){event.preventDefault();submitHumanGuidanceFreeText(this)}"></textarea>
      <div class="hg-freetext-actions">
        <button class="hg-submit-btn" data-gid="${guidanceIdHtml}" data-tofu-action="event.stopPropagation();submitHumanGuidanceFreeText(this)">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>
          ${translatedHtml('project.hgSubmit')}
        </button>
      </div>
    </div>`;
  }

  function renderBatchForm(
    round: unknown,
    guidanceIdHtml: string,
    expired: boolean,
  ): string {
    const rawQuestions = field(round, 'guidanceBatchQuestions');
    const questions: readonly unknown[] = Array.isArray(rawQuestions)
      ? rawQuestions : [];
    if (questions.length === 0) return '';

    const sections = questions.map((q, qi) => {
      const qRec = record(q);
      const qText = boundedText(
        safeText(field(qRec, 'question'))
          || translatedText('toolHumanGuidance.defaultQuestion'),
        QUESTION_UNITS,
      );
      const qType = safeText(field(qRec, 'response_type')) || 'free_text';
      const qOptions = projectOptions(field(qRec, 'options'));
      const qIndexHtml = String(qi);
      let qInput = '';

      if (qType === 'choice' && qOptions.options.length > 0) {
        const optionButtons = qOptions.options.map((opt, oi) => {
          const desc = opt.displayDescription.value
            ? `<div class="hg-opt-desc">${trustedMarkdown(opt.displayDescription.value)}</div>`
            : '';
          const optIndexHtml = String(oi);
          if (expired) {
            return `<div class="hg-option-card hg-option-static hg-option-unavailable">
              <div class="hg-opt-label">${escapeHtmlText(opt.displayLabel.value)}</div>${desc}</div>`;
          }
          return `<button class="hg-option-card hg-batch-choice" data-gid="${guidanceIdHtml}" data-batch-index="${qIndexHtml}" data-option-index="${optIndexHtml}" data-label="${escapeHtmlText(opt.originalLabel.value)}"
                    data-tofu-action="event.stopPropagation();selectBatchChoice(this)">
            <div class="hg-opt-label">${escapeHtmlText(opt.displayLabel.value)}</div>${desc}
          </button>`;
        }).join('');
        qInput = `<div class="hg-options-grid" data-batch-index="${qIndexHtml}">${optionButtons}</div>`;
      } else if (!expired) {
        qInput = `<textarea class="hg-textarea hg-batch-input" data-gid="${guidanceIdHtml}" data-batch-index="${qIndexHtml}" rows="2" maxlength="${RESPONSE_UNITS}"
          placeholder="${translatedHtml('project.hgTextareaPlaceholder')}"></textarea>`;
      }

      return `<div class="hg-batch-section" data-batch-index="${qIndexHtml}">
        <div class="hg-batch-qnum">${translatedHtml('toolHumanGuidance.batchQuestionNumber', { n: qi + 1 })}</div>
        <div class="hg-question">${trustedMarkdown(qText.value)}${qText.truncated ? limitHtml(QUESTION_UNITS) : ''}</div>
        ${qInput}
      </div>`;
    }).join('');

    const submitBtn = expired ? '' : `<div class="hg-batch-submit-wrap">
      <button class="hg-submit-btn" data-gid="${guidanceIdHtml}" data-tofu-action="event.stopPropagation();submitBatchAnswers(this)">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>
        ${translatedHtml('project.hgSubmitAll')}
      </button>
    </div>`;

    return `<div class="hg-batch-form">${sections}${submitBtn}</div>`;
  }

  /** Plain question text the chat reply quotes, so the model sees what it answers. */
  function replyQuote(round: unknown, responseType: string): string {
    let text = '';
    const batchQuestions = field(round, 'guidanceBatchQuestions');
    if (responseType === 'batch' && Array.isArray(batchQuestions)) {
      // Numbered like the form, so the numbered chat answers map back to them.
      batchQuestions.forEach((item, index) => {
        const question = safeText(field(item, 'question')).trim();
        if (!question || text.length > REPLY_QUOTE_UNITS) return;
        const numbered = `${index + 1}. ${question}`;
        text = text ? `${text}\n\n${numbered}` : numbered;
      });
    }
    if (!text) text = safeText(field(round, 'guidanceQuestion')).trim();
    return text.length > REPLY_QUOTE_UNITS
      ? `${text.slice(0, REPLY_QUOTE_UNITS - 1)}…`
      : text;
  }

  function renderAwaitingCard(round: unknown, chatReply = false): string {
    const guidanceId = boundedText(field(round, 'guidanceId'), IDENTIFIER_UNITS);
    if (!guidanceId.value) return '';
    /* A non-blocking question has no live request behind it: an ordinary
     * chat message answers it, so turn settlement never expires its card. */
    const expired = !chatReply && Boolean(field(round, '_turnSettled'));
    /* A settled turn whose settlement still offers answer_guidance for this
     * question keeps the card answerable: submitting completes the
     * interrupted tool round inside a new attempt. The turn id rides the
     * card so the action layer can route to the create-attempt command. */
    const lateTurnId = expired && Boolean(field(round, '_hgAnswerGuidance'))
      ? boundedText(field(round, '_turnId'), IDENTIFIER_UNITS)
      : null;
    const lateAnswerable = Boolean(
      lateTurnId && lateTurnId.value && !lateTurnId.truncated,
    );
    const interactive = !expired || lateAnswerable;
    const rawQuestion = safeText(field(round, 'guidanceQuestion'))
      || translatedText('toolHumanGuidance.defaultQuestion');
    const displayQuestion = boundedText(
      safeText(field(round, '_translatedQuestion')) || rawQuestion,
      QUESTION_UNITS,
    );
    const questionHtml = trustedMarkdown(displayQuestion.value);
    const isTranslating = Boolean(field(round, '_hgTranslating'));
    const translatingIndicator = isTranslating
      ? `<div class="hg-translating-indicator"><span class="hg-spinner"></span> ${translatedHtml('project.hgTranslatingQuestion')}</div>`
      : '';
    const responseType = safeText(field(round, 'guidanceType')) || 'free_text';
    const options = projectOptions(field(round, 'guidanceOptions'));
    const guidanceIdHtml = escapeHtmlText(guidanceId.value);
    let inputHtml = '';
    if (interactive && guidanceId.truncated) {
      inputHtml = `<div class="ptool-preview-limit hg-unavailable">${translatedHtml(
        'toolHumanGuidance.identifierLimit',
        { n: IDENTIFIER_UNITS },
      )}</div>`;
    } else if (responseType === 'batch') {
      inputHtml = renderBatchForm(round, guidanceIdHtml, !interactive);
    } else if (responseType === 'choice' && options.options.length > 0) {
      inputHtml = renderOptionCards(options, guidanceIdHtml, !interactive);
    } else if (interactive) {
      inputHtml = renderInteractiveFreeText(guidanceIdHtml);
      if (options.contentLimit !== null) {
        inputHtml += limitHtml(options.contentLimit);
      }
    }

    const lateAttributes = lateAnswerable && lateTurnId
      ? ` data-hg-late-answer="1" data-turn-id="${escapeHtmlText(lateTurnId.value)}"`
      : '';
    const replyAttributes = chatReply
      ? ` data-hg-reply="chat" data-hg-quote="${escapeHtmlText(replyQuote(round, responseType))}"`
      : '';
    const badgeHtml = chatReply
      ? translatedHtml('toolHumanGuidance.nonBlockingBadge')
      : translatedHtml(expired && !lateAnswerable ? 'project.hgExpired' : 'project.hgWaitingReply');
    const hintHtml = chatReply
      ? `\n    <div class="hg-reply-hint">${translatedHtml('toolHumanGuidance.nonBlockingHint')}</div>`
      : '';
    /* A rendered batch form already presents each question inside its own
     * section; the synthetic top-level summary would repeat every line. */
    const batchSectionsShown = responseType === 'batch' && inputHtml !== '';
    const questionBlock = batchSectionsShown
      ? ''
      : `<div class="hg-question">${questionHtml}${displayQuestion.truncated ? limitHtml(QUESTION_UNITS) : ''}</div>`;
    return `<div class="hg-card${expired && !lateAnswerable ? ' hg-expired' : ''}" data-gid="${guidanceIdHtml}"${lateAttributes}${replyAttributes}>
    <div class="hg-header">
      ${''}
      <span class="hg-title">${translatedHtml('project.hgPanelTitle')}</span>
      <span class="hg-badge">${badgeHtml}</span>
    </div>
    ${batchSectionsShown ? '' : translatingIndicator}
    ${questionBlock}
    ${inputHtml}${hintHtml}
  </div>`;
  }

  function renderSkippedRow(round: unknown, slots: ToolHumanGuidanceSlots): string {
    if (
      safeText(field(round, 'status')) !== 'done'
      || safeText(field(round, 'toolName')) !== 'ask_human'
      || !field(round, '_hgSkipped')
    ) return '';
    const skippedQuestion = boundedText(
      field(round, 'guidanceQuestion'),
      SKIPPED_PREVIEW_UNITS,
    );
    const label = boundedText(
      slots.toolDisplayLabel,
      TOOL_LABEL_UNITS,
    ).value || translatedText('toolHumanGuidance.label');
    return `<div class="ptool-line hg-skipped-line">
      <span class="ptool-icon">${safeText(slots.iconHtml)}</span>
      <span class="ptool-text">${escapeHtmlText(label)}${skippedQuestion.value ? ` — ${escapeHtmlText(skippedQuestion.value)}` : ''}</span>
      <span class="ptool-badge ptool-badge-skip">${translatedHtml('project.hgUnanswered')}</span>
    </div>`;
  }

  /** A blocking question the user bypassed by typing a chat message instead. */
  function renderSupersededRow(round: unknown, slots: ToolHumanGuidanceSlots): string {
    if (
      safeText(field(round, 'status')) !== 'done'
      || safeText(field(round, 'toolName')) !== 'ask_human'
      || field(round, 'guidanceSuperseded') !== true
    ) return '';
    const question = boundedText(
      field(round, 'guidanceQuestion'),
      SKIPPED_PREVIEW_UNITS,
    );
    const label = boundedText(
      slots.toolDisplayLabel,
      TOOL_LABEL_UNITS,
    ).value || translatedText('toolHumanGuidance.label');
    return `<div class="ptool-line hg-superseded-line">
      <span class="ptool-icon">${safeText(slots.iconHtml)}</span>
      <span class="ptool-text">${escapeHtmlText(label)}${question.value ? ` — ${escapeHtmlText(question.value)}` : ''}</span>
      <span class="ptool-badge ptool-badge-skip">${translatedHtml('toolHumanGuidance.supersededRow')}</span>
    </div>`;
  }

  /** A non-blocking question this browser answered with a chat message. */
  function renderRepliedRow(round: unknown, slots: ToolHumanGuidanceSlots): string {
    const response = boundedText(
      field(round, '_hgRepliedInChat'),
      RESPONSE_PREVIEW_UNITS,
    );
    const label = boundedText(
      slots.toolDisplayLabel,
      TOOL_LABEL_UNITS,
    ).value || translatedText('toolHumanGuidance.label');
    return `<div class="ptool-line hg-replied-line">
      <span class="ptool-icon">${safeText(slots.iconHtml)}</span>
      <span class="ptool-text">${escapeHtmlText(label)}${response.value ? ` — ${escapeHtmlText(response.value)}` : ''}</span>
      <span class="ptool-badge ptool-badge-ok">${translatedHtml('toolHumanGuidance.repliedInChat')}</span>
    </div>`;
  }

  function renderSubmittedRow(round: unknown, slots: ToolHumanGuidanceSlots): string {
    if (
      safeText(field(round, 'status')) !== 'submitted'
      || safeText(field(round, 'toolName')) !== 'ask_human'
    ) return '';
    const response = boundedText(
      field(round, '_hgUserResponse'),
      RESPONSE_PREVIEW_UNITS,
    );
    const label = boundedText(
      slots.toolDisplayLabel,
      TOOL_LABEL_UNITS,
    ).value || translatedText('toolHumanGuidance.label');
    return `<div class="ptool-line hg-submitted-line">
      <span class="ptool-icon">${safeText(slots.iconHtml)}</span>
      <span class="ptool-text">${escapeHtmlText(label)}${response.value ? ` — ${escapeHtmlText(response.value)}` : ''}</span>
      <span class="ptool-badge ptool-badge-done">${translatedHtml('project.hgAnswered')}</span>
      <span class="hg-submitted-spinner" title="${translatedHtml('project.hgWaitingContinue')}"></span>
    </div>`;
  }

  function renderGuidanceHtml(
    round: unknown,
    slots: ToolHumanGuidanceSlots,
  ): string {
    if (
      safeText(field(round, 'toolName')) === 'ask_human'
      && field(round, 'guidanceBlocking') === false
      && safeText(field(round, 'guidanceId'))
    ) {
      return typeof field(round, '_hgRepliedInChat') === 'string'
        ? renderRepliedRow(round, slots)
        : renderAwaitingCard(round, true);
    }
    if (safeText(field(round, 'status')) === 'awaiting_human') {
      return renderAwaitingCard(round);
    }
    return renderSupersededRow(round, slots)
      || renderSkippedRow(round, slots)
      || renderSubmittedRow(round, slots);
  }

  return Object.freeze({ renderGuidanceHtml });
}
