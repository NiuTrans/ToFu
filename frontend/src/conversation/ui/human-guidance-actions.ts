/**
 * Browser action owner for Human Guidance responses.
 *
 * Responsibility: validate delegated card actions, own optimistic DOM state,
 * optionally translate free text, submit one response, and project terminal
 * UI feedback through explicit ports. Entry point:
 * `createHumanGuidanceActions`. This controller owns no conversation/Turn
 * state and never constructs selectors from untrusted guidance identifiers.
 */

import { escapeHtmlText } from '../../html-safety';
import type { I18nArgs, I18nKey, Translator } from '../../i18n';
import { TOOL_HUMAN_GUIDANCE_PRESENTATION_LIMITS } from '../presentation/tool-human-guidance-presentation';

type UnknownRecord = Readonly<Record<string, unknown>>;

export type HumanGuidanceActiveConversation = Readonly<{
  conversationId: string;
  autoTranslate: boolean;
}>;

export type HumanGuidanceResponseRequest = Readonly<{
  conversationId: string;
  guidanceId: string;
  responseText: string;
}>;

export type HumanGuidanceLateAnswerRequest = Readonly<{
  conversationId: string;
  turnId: string;
  guidanceId: string;
  responseText: string;
}>;

export type HumanGuidanceActions = Readonly<{
  submitFreeText(element: unknown): Promise<void>;
  submitChoice(element: unknown): Promise<void>;
  destroy(): void;
}>;

export type HumanGuidanceActionsDependencies = Readonly<{
  translate: Translator;
  activeConversation: () => HumanGuidanceActiveConversation | null;
  translateResponse: (source: string) => Promise<unknown>;
  submitResponse: (request: HumanGuidanceResponseRequest) => Promise<unknown>;
  submitLateAnswer: (
    request: HumanGuidanceLateAnswerRequest,
  ) => Promise<unknown>;
  markSubmitted: (
    conversationId: string,
    guidanceId: string,
    originalResponse: string,
  ) => void;
  requestExpiredRender: (conversationId: string) => void;
  renderConversationList: () => void;
  showToast: (message: string, kind: 'error' | 'warning') => void;
  log: (
    level: 'debug' | 'success' | 'warn' | 'error',
    message: string,
  ) => void;
  schedule: (callback: () => void, delayMs: number) => unknown;
}>;

const RESPONSE_UNITS =
  TOOL_HUMAN_GUIDANCE_PRESENTATION_LIMITS.responseUnits;
const IDENTIFIER_UNITS =
  TOOL_HUMAN_GUIDANCE_PRESENTATION_LIMITS.identifierUnits;
const CHOICE_LABEL_UNITS =
  TOOL_HUMAN_GUIDANCE_PRESENTATION_LIMITS.optionLabelUnits;
const OPTION_ITEMS = TOOL_HUMAN_GUIDANCE_PRESENTATION_LIMITS.optionItems;
const OPTION_NOTE_UNITS =
  TOOL_HUMAN_GUIDANCE_PRESENTATION_LIMITS.optionNoteUnits;

export const HUMAN_GUIDANCE_ACTION_LIMITS = Object.freeze({
  identifierUnits: IDENTIFIER_UNITS,
  choiceLabelUnits: CHOICE_LABEL_UNITS,
  responseUnits: RESPONSE_UNITS,
  optionItems: OPTION_ITEMS,
  optionNoteUnits: OPTION_NOTE_UNITS,
});

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
  if (typeof value === 'string') return value;
  if (
    typeof value === 'number'
    || typeof value === 'boolean'
    || typeof value === 'bigint'
  ) return String(value);
  return '';
}

function boundedIdentity(value: unknown, limit: number): string {
  const text = safeText(value);
  return text && text.length <= limit ? text : '';
}

function errorStatus(error: unknown): number | null {
  const status = field(error, 'status');
  return typeof status === 'number' && Number.isFinite(status) ? status : null;
}

function asElement(value: unknown): HTMLElement | null {
  return typeof HTMLElement !== 'undefined' && value instanceof HTMLElement
    ? value
    : null;
}

function cardFor(element: HTMLElement): HTMLElement | null {
  const card = element.closest('.hg-card');
  return card instanceof HTMLElement ? card : null;
}

function lateAnswerTurnId(card: HTMLElement): string {
  if (card.dataset.hgLateAnswer !== '1') return '';
  return boundedIdentity(card.dataset.turnId, IDENTIFIER_UNITS);
}
function matchingDatasetValue(
  values: readonly (string | undefined)[],
  limit: number,
): string {
  const present = values.filter((value): value is string => Boolean(value));
  if (present.length === 0 || new Set(present).size !== 1) return '';
  return boundedIdentity(present[0], limit);
}

function containsChinese(value: string): boolean {
  return /[\u4e00-\u9fff\u3400-\u4dbf]/.test(value);
}

export function createHumanGuidanceActions(
  dependencies: HumanGuidanceActionsDependencies,
): HumanGuidanceActions {
  const {
    translate,
    activeConversation,
    translateResponse,
    submitResponse,
    submitLateAnswer,
    markSubmitted,
    requestExpiredRender,
    renderConversationList,
    showToast,
    log,
    schedule,
  } = dependencies;
  const activeSubmissions = new Set<string>();
  let destroyed = false;

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

  function safeToast(message: string, kind: 'error' | 'warning'): void {
    if (destroyed) return;
    try {
      showToast(message, kind);
    } catch {
      // Toast failure must not alter response authority.
    }
  }

  function safeLog(
    level: 'debug' | 'success' | 'warn' | 'error',
    message: string,
  ): void {
    try {
      log(level, message);
    } catch {
      // Diagnostics are best effort.
    }
  }

  function readActiveConversation(): HumanGuidanceActiveConversation | null {
    try {
      const active = activeConversation();
      if (!active) return null;
      const conversationId = boundedIdentity(
        active.conversationId,
        IDENTIFIER_UNITS,
      );
      return conversationId
        ? Object.freeze({
          conversationId,
          autoTranslate: Boolean(active.autoTranslate),
        })
        : null;
    } catch {
      return null;
    }
  }

  function submissionKey(conversationId: string, guidanceId: string): string {
    return `${conversationId}\u0000${guidanceId}`;
  }

  async function submitPayload(
    active: HumanGuidanceActiveConversation,
    guidanceId: string,
    responseText: string,
    originalResponse: string,
    lateTurnId = '',
  ): Promise<boolean> {
    try {
      /* A card whose turn already settled has no live guidance request to
       * answer; its answer rides the answer_guidance attempt command, which
       * completes the interrupted question round and resumes the loop. */
      const result = lateTurnId
        ? await submitLateAnswer({
          conversationId: active.conversationId,
          turnId: lateTurnId,
          guidanceId,
          responseText,
        })
        : await submitResponse({
          conversationId: active.conversationId,
          guidanceId,
          responseText,
        });
      const error = safeText(field(result, 'error'));
      if (result == null || error) {
        safeLog('warn', `Human guidance submit failed: ${error || 'Unknown'}`);
        safeToast(translatedText('project.hgSubmitFailed'), 'error');
        return false;
      }
      safeLog('success', `Human guidance answered: ${guidanceId.slice(0, 16)}`);
      if (!destroyed) {
        try {
          markSubmitted(
            active.conversationId,
            guidanceId,
            originalResponse,
          );
        } catch {
          safeLog('warn', 'Human guidance presentation patch failed');
        }
        try {
          renderConversationList();
        } catch {
          safeLog('warn', 'Human guidance conversation-list refresh failed');
        }
      }
      return true;
    } catch (error) {
      safeLog('error', `Human guidance error: ${safeText(field(error, 'message'))}`);
      if (errorStatus(error) === 404) {
        safeToast(translatedText('project.hgExpiredToast'), 'warning');
        if (!destroyed) {
          try {
            requestExpiredRender(active.conversationId);
          } catch {
            safeLog('warn', 'Human guidance expired render request failed');
          }
        }
        return false;
      }
      safeToast(translatedText('project.hgNetworkError'), 'error');
      return false;
    }
  }

  function freeTextParts(element: HTMLElement): Readonly<{
    card: HTMLElement;
    textarea: HTMLTextAreaElement;
    guidanceId: string;
  }> | null {
    const card = cardFor(element);
    if (!card) return null;
    const textarea = element instanceof HTMLTextAreaElement
      ? element
      : card.querySelector<HTMLTextAreaElement>(
        'textarea.hg-textarea[data-gid]',
      );
    if (!(textarea instanceof HTMLTextAreaElement)) return null;
    const guidanceId = matchingDatasetValue(
      [element.dataset.gid, textarea.dataset.gid, card.dataset.gid],
      IDENTIFIER_UNITS,
    );
    return guidanceId ? { card, textarea, guidanceId } : null;
  }

  async function submitFreeText(value: unknown): Promise<void> {
    if (destroyed) return;
    const element = asElement(value);
    const parts = element ? freeTextParts(element) : null;
    if (!parts) return;
    const text = parts.textarea.value.trim();
    if (!text) {
      parts.textarea.classList.add('hg-shake');
      try {
        schedule(() => parts.textarea.classList.remove('hg-shake'), 500);
      } catch {
        parts.textarea.classList.remove('hg-shake');
      }
      return;
    }
    if (text.length > RESPONSE_UNITS) {
      safeToast(translatedText(
        'toolHumanGuidance.responseLimit',
        { n: RESPONSE_UNITS },
      ), 'error');
      return;
    }
    const active = readActiveConversation();
    if (!active) return;
    const key = submissionKey(active.conversationId, parts.guidanceId);
    if (
      parts.card.classList.contains('hg-submitting')
      || activeSubmissions.has(key)
    ) return;
    activeSubmissions.add(key);
    parts.card.classList.add('hg-submitting');

    const submitButton = parts.card.querySelector<HTMLButtonElement>(
      'button.hg-submit-btn',
    );
    const originalButtonHtml = submitButton?.innerHTML ?? '';
    let finalText = text;
    try {
      if (active.autoTranslate && containsChinese(text)) {
        if (submitButton) {
          submitButton.disabled = true;
          submitButton.innerHTML = `<span class="hg-spinner"></span> ${escapeHtmlText(
            translatedText('project.hgTranslating'),
          )}`;
        }
        try {
          const translated = safeText(await translateResponse(text));
          if (!translated || translated.length > RESPONSE_UNITS) {
            throw new Error('invalid translated Human Guidance response');
          }
          finalText = translated;
        } catch (error) {
          safeLog('warn', `Human guidance translation failed: ${
            safeText(field(error, 'message'))
          }`);
          safeToast(translatedText('project.hgTranslateFailed'), 'warning');
          finalText = text;
        }
      }
      const ok = await submitPayload(
        active,
        parts.guidanceId,
        finalText,
        text,
        lateAnswerTurnId(parts.card),
      );
      if (!ok && !destroyed) parts.card.classList.remove('hg-submitting');
    } finally {
      activeSubmissions.delete(key);
      if (submitButton && !destroyed) {
        submitButton.disabled = false;
        submitButton.innerHTML = originalButtonHtml;
      }
    }
  }

  function boundedOptionIndex(value: unknown): number | null {
    const text = safeText(value);
    if (!/^(?:0|[1-9]\d*)$/.test(text)) return null;
    const optionIndex = Number(text);
    return Number.isSafeInteger(optionIndex) && optionIndex < OPTION_ITEMS
      ? optionIndex : null;
  }

  function choiceParts(element: HTMLElement): Readonly<{
    card: HTMLElement;
    button: HTMLButtonElement;
    noteElement: HTMLTextAreaElement;
    guidanceId: string;
    choiceLabel: string;
    buttons: readonly HTMLButtonElement[];
  }> | null {
    const card = cardFor(element);
    if (!card) return null;
    const optionGroup = element.closest(
      '.hg-option-group[data-gid][data-option-index]',
    );
    if (!(optionGroup instanceof HTMLElement)) return null;
    const button = optionGroup.querySelector<HTMLButtonElement>(
      ':scope > button.hg-option-card[data-gid][data-label][data-option-index]',
    );
    const noteElement = optionGroup.querySelector<HTMLTextAreaElement>(
      ':scope > textarea.hg-option-note-input[data-gid][data-option-index]',
    );
    if (!(button instanceof HTMLButtonElement)
        || !(noteElement instanceof HTMLTextAreaElement)
        || (element !== button && element !== noteElement)) return null;
    const optionIndex = boundedOptionIndex(optionGroup.dataset.optionIndex);
    const guidanceId = matchingDatasetValue(
      [element.dataset.gid, optionGroup.dataset.gid, button.dataset.gid,
        noteElement.dataset.gid, card.dataset.gid],
      IDENTIFIER_UNITS,
    );
    const matchingOptionIndex = optionIndex !== null
      && [element.dataset.optionIndex, button.dataset.optionIndex,
        noteElement.dataset.optionIndex].every(
        (value) => boundedOptionIndex(value) === optionIndex,
      );
    const choiceLabel = boundedIdentity(
      button.dataset.label,
      CHOICE_LABEL_UNITS,
    );
    if (!guidanceId || !matchingOptionIndex || !choiceLabel) return null;
    const optionGroups = Array.from(card.querySelectorAll<HTMLElement>(
      '.hg-option-group[data-gid][data-option-index]',
    ));
    if (optionGroups.length === 0 || optionGroups.length > OPTION_ITEMS
        || optionGroups[optionIndex] !== optionGroup) return null;
    const buttons = optionGroups.map((group, index) => {
      if (group.dataset.gid !== guidanceId
          || boundedOptionIndex(group.dataset.optionIndex) !== index) return null;
      const candidate = group.querySelector<HTMLButtonElement>(
        ':scope > button.hg-option-card[data-gid][data-label][data-option-index]',
      );
      return candidate instanceof HTMLButtonElement
        && candidate.dataset.gid === guidanceId
        && boundedOptionIndex(candidate.dataset.optionIndex) === index
        ? candidate : null;
    });
    if (buttons.some((candidate) => candidate === null)
        || buttons[optionIndex] !== button) return null;
    return {
      card, button, noteElement, guidanceId, choiceLabel,
      buttons: buttons as readonly HTMLButtonElement[],
    };
  }

  function choiceNote(parts: { noteElement: HTMLTextAreaElement }): string {
    /* One optional bounded note belongs to the selected option itself. It
     * rides the exact original label as a `user_note: …` line, while drafts
     * for every unselected option stay local and never cross transport. */
    return parts.noteElement.value.trim().slice(0, OPTION_NOTE_UNITS);
  }

  async function submitChoice(value: unknown): Promise<void> {
    if (destroyed) return;
    const element = asElement(value);
    const parts = element ? choiceParts(element) : null;
    if (!parts) return;
    const note = choiceNote(parts);
    const active = readActiveConversation();
    if (!active) return;
    const key = submissionKey(active.conversationId, parts.guidanceId);
    if (
      parts.card.classList.contains('hg-submitting')
      || activeSubmissions.has(key)
    ) return;
    activeSubmissions.add(key);
    parts.card.classList.add('hg-submitting');
    const previous = parts.buttons.map((button) => ({
      button,
      disabled: button.disabled,
      selected: button.classList.contains('hg-selected'),
    }));
    for (const button of parts.buttons) {
      button.classList.toggle('hg-selected', button === parts.button);
      button.disabled = true;
    }
    try {
      let finalNote = note;
      if (note && active.autoTranslate && containsChinese(note)) {
        try {
          const translated = safeText(await translateResponse(note));
          if (!translated || translated.length > RESPONSE_UNITS) {
            throw new Error('invalid translated Human Guidance note');
          }
          finalNote = translated;
        } catch (error) {
          safeLog('warn', `Human guidance note translation failed: ${
            safeText(field(error, 'message'))
          }`);
          safeToast(translatedText('project.hgTranslateFailed'), 'warning');
          finalNote = note;
        }
      }
      const responseText = finalNote
        ? `${parts.choiceLabel}\nuser_note: ${finalNote}`
        : parts.choiceLabel;
      const originalResponse = note
        ? `${parts.choiceLabel}\nuser_note: ${note}`
        : parts.choiceLabel;
      const ok = await submitPayload(
        active,
        parts.guidanceId,
        responseText,
        originalResponse,
        lateAnswerTurnId(parts.card),
      );
      if (!ok && !destroyed) {
        for (const state of previous) {
          state.button.disabled = state.disabled;
          state.button.classList.toggle('hg-selected', state.selected);
        }
        parts.card.classList.remove('hg-submitting');
      }
    } finally {
      activeSubmissions.delete(key);
    }
  }

  function destroy(): void {
    destroyed = true;
    activeSubmissions.clear();
  }

  return Object.freeze({ submitFreeText, submitChoice, destroy });
}
