import {
  featureRegistry,
  readLiveRuntimeBinding,
  writeLiveRuntimeBinding,
} from '../../feature-registry';
import { escapeHtml as escape } from '../../html-safety';
import type { I18nKey } from '../../i18n';
import type { PaperSaveScope } from './library';
import {
  paperAttachPush,
  paperDetachPush,
  paperIngestEvent,
  taskReplayIngestPage,
  type PaperPushEvent,
  type PaperPushState,
} from './push-transport';
import {
  canonicalPaperHash,
  paperSourceRetryRequired,
} from './source-start';

type JsonObject = Record<string, unknown>;
type QAStatus = 'running' | 'done' | 'error' | 'aborted';
const PAPER_QA_SOURCE_MAX_CHARS = 1_000_000;

interface QAToolRound extends JsonObject {
  roundNum?: unknown;
  toolName?: unknown;
  status?: string;
  results?: unknown;
  _elapsed?: string;
  toolContent?: unknown;
  searchDiag?: unknown;
  engineBreakdown?: unknown;
  vertical?: unknown;
  verticals?: unknown;
}

interface QAMessage extends PaperPushState {
  role: 'user' | 'assistant';
  content: string;
  timestamp?: number;
  toolRounds?: QAToolRound[];
  status?: QAStatus;
  _qaTaskId?: string;
}

interface QAPollResponse {
  ok: boolean;
  status?: number;
  json(): Promise<JsonObject>;
}

interface PaperQAApi {
  qaStart(body: JsonObject): Promise<JsonObject>;
  qaPoll(taskId: string, cursor: number): Promise<QAPollResponse | null>;
  qaAbort?(taskId: string): Promise<unknown>;
}

type QAElement = HTMLElement & { _qaCls?: string; _qaSig?: string };

type PaperQAWindow = Window & {
  Api?: { paper?: PaperQAApi };
  Icon?: (name: string, size?: number) => string;
  t?: (key: string) => string;
  renderMarkdown?: (text: string) => string;
  renderToolRoundsHTML?: (rounds: QAToolRound[], running: boolean) => string;
  errorEnvelopeMessage?: (error: unknown) => string;
  debugLog?: (message: string, level?: string) => void;
  _paperQAHistory?: QAMessage[];
  _paperQAStreaming?: boolean;
  _paperQAAbort?: AbortController | null;
  _paperQAAbortRequested?: boolean;
  _paperParsedText?: string;
  _activePaperId?: string;
  _paperHash?: string;
  _i18nLang?: string;
  _paperReportModel?: string;
  _paperFileName?: string;
  _paperActiveTab?: string;
  _ensurePaperText?: () => Promise<boolean>;
  _saveActivePaperState?: (scope?: PaperSaveScope) => unknown;
  _switchPaperTab?: (tab: string) => void;
  _setPaperMobileView?: (view: string) => void;
  _qaMsgInnerHtml?: typeof qaMsgInnerHtml;
  _renderPaperQA?: typeof renderPaperQA;
  _sendPaperQuestion?: typeof sendPaperQuestion;
  _pollQATask?: typeof pollQATask;
  _applyQAEvent?: typeof applyQAEvent;
  _paperAskQuestion?: typeof paperAskQuestion;
  _quotePaperSelection?: typeof quotePaperSelection;
  _askAboutPaperSelection?: typeof askAboutPaperSelection;
  _hidePaperQuoteBar?: typeof hidePaperQuoteBar;
  _handlePaperTextSelection?: typeof handlePaperTextSelection;
  _destroyPaperQA?: typeof destroyPaperQA;
};

function globals(): PaperQAWindow {
  return featureRegistry as unknown as PaperQAWindow;
}

function api(): PaperQAApi {
  const paper = globals().Api?.paper;
  if (!paper) throw new Error('Paper Q&A API unavailable');
  return paper;
}

function stringValue(value: unknown): string {
  return typeof value === 'string' ? value : '';
}

function activePaperHash(): string {
  return stringValue(readLiveRuntimeBinding('_paperHash')).trim();
}

function setActivePaperHash(value: unknown): void {
  writeLiveRuntimeBinding('_paperHash', stringValue(value));
}

function translate(key: I18nKey, fallback: string = key): string {
  const helper = globals().t;
  return typeof helper === 'function' ? helper(key) : fallback;
}

function icon(name: string, size: number): string {
  const helper = globals().Icon;
  return typeof helper === 'function' ? helper(name, size) : '';
}

function history(): QAMessage[] {
  const state = globals();
  state._paperQAHistory ??= [];
  return state._paperQAHistory;
}

function errorMessage(error: unknown, fallback: string): string {
  const helper = globals().errorEnvelopeMessage;
  if (typeof helper === 'function') {
    const message = helper(error);
    if (message) return message;
  }
  if (error instanceof Error && error.message) return error.message;
  return stringValue(error) || fallback;
}

function qaStartIsCurrent(assistant: QAMessage, paperId: string): boolean {
  return String(globals()._activePaperId || '') === paperId
    && assistant.status === 'running'
    && history().includes(assistant);
}

/** Build one Q&A bubble while preserving the classic rendering contract. */
export function qaMsgInnerHtml(message: QAMessage): string {
  const isUser = message.role === 'user';
  let inner = '';
  const toolRounds = message.toolRounds ?? [];
  const renderTools = globals().renderToolRoundsHTML;
  if (!isUser && toolRounds.length && typeof renderTools === 'function') {
    inner += '<div class="paper-qa-tools">'
      + renderTools(toolRounds, message.status === 'running') + '</div>';
  }
  if (isUser) {
    inner += `<div class="paper-qa-msg-content">${escape(message.content)}</div>`;
  } else if (message.content) {
    const render = globals().renderMarkdown;
    const body = typeof render === 'function'
      ? render(message.content)
      : escape(message.content);
    inner += `<div class="paper-qa-msg-content">${body}</div>`;
  } else if (message.status === 'running') {
    inner += '<div class="paper-qa-msg-content paper-qa-thinking">'
      + '<span class="thinking-dot"></span></div>';
  }
  return inner;
}

/** Reconcile message nodes in place so streaming only repaints the last bubble. */
export function renderPaperQA(): void {
  const container = document.getElementById('paperQAMessages');
  if (!container) return;
  const messages = history();
  if (messages.length === 0) {
    container.innerHTML = '<div class="paper-qa-empty"><div class="paper-qa-empty-icon">'
      + `${icon('messageCircle', 32)}</div>`
      + `<p>${escape(translate('paper.qaEmptyTitle'))}</p>`
      + `<p class="paper-qa-hint">${escape(translate('paper.qaEmptyHint'))}</p></div>`;
    return;
  }

  const first = container.firstElementChild;
  if (first && !first.classList.contains('paper-qa-msg')) container.innerHTML = '';
  const nearBottom = container.scrollHeight - container.scrollTop
    - container.clientHeight < 80;
  let changed = false;
  while (container.children.length > messages.length) {
    container.lastElementChild?.remove();
    changed = true;
  }

  messages.forEach((message, index) => {
    const className = `paper-qa-msg ${message.role === 'user'
      ? 'paper-qa-user' : 'paper-qa-assistant'}`;
    const inner = qaMsgInnerHtml(message);
    let node = container.children[index] as QAElement | undefined;
    if (!node) {
      node = document.createElement('div') as QAElement;
      container.appendChild(node);
    }
    if (node._qaCls !== className) {
      node.className = className;
      node._qaCls = className;
    }
    if (node._qaSig !== inner) {
      node.innerHTML = inner;
      node._qaSig = inner;
      changed = true;
    }
  });
  if (changed && nearBottom) container.scrollTop = container.scrollHeight;
}

const activeMessages = new Set<QAMessage>();

/** Fold a task event into the current assistant message. */
export function applyQAEvent(
  assistant: QAMessage,
  event: PaperPushEvent,
): boolean {
  const rounds = assistant.toolRounds ??= [];
  if (event.type === 'tool_start') {
    rounds.push({
      roundNum: event.roundNum,
      toolName: event.toolName,
      query: event.query,
      toolCallId: event.toolCallId,
      toolArgs: event.toolArgs,
      attentionKind: event.attentionKind,
      parentToolCallId: event.parentToolCallId,
      status: 'searching',
      results: null,
    });
    return true;
  }
  if (event.type === 'tool_done') {
    const round = rounds.find((candidate) => candidate.roundNum === event.roundNum);
    if (round) {
      round.status = 'done';
      if (event.elapsed != null) round._elapsed = `${String(event.elapsed)}s`;
      if (event.toolContent) round.toolContent = event.toolContent;
      if (event.results) round.results = event.results;
      if (event.searchDiag) round.searchDiag = event.searchDiag;
      if (event.engineBreakdown) round.engineBreakdown = event.engineBreakdown;
      if (event.vertical) round.vertical = event.vertical;
      if (event.verticals) round.verticals = event.verticals;
    }
    return true;
  }
  if (event.type === 'delta') {
    assistant.content += stringValue(event.delta);
    return true;
  }
  if (event.type === 'delta_reset') {
    assistant.content = '';
    return true;
  }
  return false;
}

/** Poll remains the availability floor; push and poll share one sequence gate. */
export async function pollQATask(
  taskId: string,
  assistant: QAMessage,
  startPaperId: string,
): Promise<void> {
  let cursor = 0;
  const state = globals();
  assistant._qaTaskId = taskId;
  activeMessages.add(assistant);
  paperAttachPush(assistant, taskId, {
    isCurrent: () => startPaperId === globals()._activePaperId,
    onEvent(event) {
      let dirty = Boolean(paperIngestEvent(
        assistant,
        event,
        (_current, accepted) => applyQAEvent(assistant, accepted),
      ));
      if (event.type === 'done') {
        assistant.status = 'done';
        if (event.answer) assistant.content = stringValue(event.answer);
        dirty = true;
      } else if (event.type === 'error') {
        assistant.status = 'error';
        dirty = true;
      }
      if (dirty) renderPaperQA();
    },
  });

  try {
    while (true) {
      if (state._paperQAAbortRequested) {
        state._paperQAAbortRequested = false;
        break;
      }
      const response = await api().qaPoll(taskId, cursor);
      if (!response?.ok) {
        if (response?.status === 404) {
          assistant.status = 'error';
          assistant.content ||= translate('paper.qaExpired', 'Q&A task expired.');
          break;
        }
        throw new Error(`HTTP ${response?.status ?? '?'}`);
      }
      const data = await response.json();
      if (data.ok === false) throw new Error(stringValue(data.error) || 'Poll failed');
      const replay = taskReplayIngestPage(
        assistant,
        data,
        (_current, event) => applyQAEvent(assistant, event),
        cursor,
      );
      cursor = replay.nextCursor;
      if (data.status === 'done') {
        assistant.status = 'done';
        if (data.answer) assistant.content = stringValue(data.answer);
        if (startPaperId === globals()._activePaperId) renderPaperQA();
        break;
      }
      if (data.status === 'error') {
        assistant.status = 'error';
        assistant.content += '\n\n' + icon('alertTriangle', 14) + ' '
          + `${translate('paper.qaError', 'Error')}: `
          + errorMessage(data.error, 'Error');
        if (startPaperId === globals()._activePaperId) renderPaperQA();
        break;
      }
      if (startPaperId === globals()._activePaperId) renderPaperQA();
      await new Promise<void>((resolve) => { window.setTimeout(resolve, 700); });
    }
  } finally {
    paperDetachPush(assistant);
    activeMessages.delete(assistant);
  }
}

/** Start a paper-grounded question through hash-first source resolution. */
export async function sendPaperQuestion(): Promise<void> {
  const input = document.getElementById('paperQAInput') as HTMLTextAreaElement | null;
  const question = input?.value.trim() ?? '';
  const state = globals();
  if (!question || state._paperQAStreaming) return;
  const paperHash = canonicalPaperHash(activePaperHash());
  if (!paperHash && !state._paperParsedText) {
    const available = await state._ensurePaperText?.();
    if (!available) {
      state.debugLog?.(
        'No paper text available — PDF may be scanned or parsing failed',
        'warning',
      );
      return;
    }
  }
  let paperText = stringValue(state._paperParsedText);

  const messages = history();
  const recent = messages.slice(-10).map((message) => ({
    role: message.role,
    content: message.content,
  }));
  messages.push({ role: 'user', content: question, timestamp: Date.now() });
  const assistant: QAMessage = {
    role: 'assistant', content: '', timestamp: Date.now(),
    toolRounds: [], status: 'running',
  };
  messages.push(assistant);
  if (input) input.value = '';
  state._paperQAStreaming = true;
  renderPaperQA();

  const startPaperId = state._activePaperId || '';
  try {
    const startBody: JsonObject = {
      question,
      lang: state._i18nLang === 'zh' ? 'zh' : 'en',
      history: recent,
      model: state._paperReportModel || undefined,
      title: state._paperFileName || '',
    };
    if (paperHash) startBody.paper_hash = paperHash;
    else startBody.paper_text = paperText;

    let data: JsonObject;
    try {
      data = await api().qaStart(startBody);
    } catch (error: unknown) {
      if (
        !paperHash
        || !paperSourceRetryRequired(error)
        || !qaStartIsCurrent(assistant, startPaperId)
      ) throw error;
      if (!paperText) {
        const available = await state._ensurePaperText?.();
        if (!qaStartIsCurrent(assistant, startPaperId)) return;
        if (!available) throw error;
        paperText = stringValue(state._paperParsedText);
      }
      if (!paperText) throw error;
      console.warn(
        '[Paper:QA] Stored source unavailable — retrying start with paper text',
      );
      data = await api().qaStart({
        ...startBody,
        paper_text: paperText.slice(0, PAPER_QA_SOURCE_MAX_CHARS),
      });
    }
    const taskId = stringValue(data.task_id);
    if (data.ok !== true || !taskId) {
      throw new Error(stringValue(data.error) || 'Q&A start failed');
    }
    if (!qaStartIsCurrent(assistant, startPaperId)) {
      await api().qaAbort?.(taskId);
      return;
    }
    const resolvedHash = canonicalPaperHash(data.paper_hash);
    if (resolvedHash) {
      setActivePaperHash(resolvedHash);
    }
    await pollQATask(taskId, assistant, startPaperId);
  } catch (error: unknown) {
    assistant.status = 'error';
    assistant.content += '\n\n' + icon('alertTriangle', 14) + ' '
      + `${translate('paper.qaError', 'Error')}: `
      + errorMessage(error, 'Q&A failed');
    renderPaperQA();
    console.warn('[Paper:QA] failed:', error);
  } finally {
    state._paperQAStreaming = false;
    state._paperQAAbort = null;
    state._saveActivePaperState?.('qa');
  }
}

/** Switch to Q&A, seed a complete question and send it. */
export function paperAskQuestion(value: string): void {
  const text = String(value || '').trim();
  if (!text) return;
  const state = globals();
  if (state._paperActiveTab !== 'qa') state._switchPaperTab?.('qa');
  state._setPaperMobileView?.('reader');
  const input = document.getElementById('paperQAInput') as HTMLTextAreaElement | null;
  if (!input) return;
  input.value = text;
  input.focus();
  window.setTimeout(() => { void sendPaperQuestion(); }, 100);
}

export function quotePaperSelection(): void {
  const selection = window.getSelection();
  const text = selection?.toString().trim();
  const input = document.getElementById('paperQAInput') as HTMLTextAreaElement | null;
  if (!selection || !text || !input) return;
  const state = globals();
  if (state._paperActiveTab !== 'qa') state._switchPaperTab?.('qa');
  state._setPaperMobileView?.('reader');
  input.value = `> ${text.replace(/\n/g, '\n> ')}\n\n${input.value}`;
  input.focus();
  selection.removeAllRanges();
  hidePaperQuoteBar();
}

export function askAboutPaperSelection(): void {
  const selection = window.getSelection();
  const text = selection?.toString().trim();
  const input = document.getElementById('paperQAInput') as HTMLTextAreaElement | null;
  if (!selection || !text || !input) return;
  const state = globals();
  if (state._paperActiveTab !== 'qa') state._switchPaperTab?.('qa');
  state._setPaperMobileView?.('reader');
  input.value = `> ${text.replace(/\n/g, '\n> ')}\n\nExplain this part of the paper.`;
  selection.removeAllRanges();
  hidePaperQuoteBar();
  window.setTimeout(() => { void sendPaperQuestion(); }, 100);
}

export function hidePaperQuoteBar(): void {
  const pdfBar = document.getElementById('paperQuoteBtn');
  const reportBar = document.getElementById('paperReportQuoteBtn');
  if (pdfBar) pdfBar.style.display = 'none';
  if (reportBar) reportBar.style.display = 'none';
}

export function handlePaperTextSelection(): void {
  const selection = window.getSelection();
  const text = selection?.toString().trim();
  const pdfBar = document.getElementById('paperQuoteBtn');
  const reportBar = document.getElementById('paperReportQuoteBtn');
  if (reportBar) reportBar.style.display = 'none';
  if (pdfBar) pdfBar.style.display = 'none';
  if (!selection || !text || text.length < 3 || selection.rangeCount === 0) return;

  const viewer = document.getElementById('paperPdfViewer');
  if (pdfBar && viewer?.contains(selection.anchorNode)) {
    const rect = selection.getRangeAt(0).getBoundingClientRect();
    const left = document.querySelector<HTMLElement>('.paper-left');
    if (!left) return;
    const bounds = left.getBoundingClientRect();
    pdfBar.style.display = 'flex';
    pdfBar.style.top = `${rect.top - bounds.top - 40}px`;
    pdfBar.style.left = `${Math.max(
      4, rect.left - bounds.left + rect.width / 2 - 80,
    )}px`;
    return;
  }

  const report = document.getElementById('paperReportContent');
  if (reportBar && report?.contains(selection.anchorNode)) {
    const rect = selection.getRangeAt(0).getBoundingClientRect();
    const right = document.querySelector<HTMLElement>('.paper-right');
    if (!right) return;
    const bounds = right.getBoundingClientRect();
    reportBar.style.display = 'flex';
    reportBar.style.top = `${Math.max(4, rect.top - bounds.top - 40)}px`;
    reportBar.style.left = `${Math.max(
      4, rect.left - bounds.left + rect.width / 2 - 80,
    )}px`;
  }
}

export function destroyPaperQA(): void {
  const state = globals();
  state._paperQAAbortRequested = activeMessages.size > 0;
  activeMessages.forEach((message) => {
    paperDetachPush(message);
    const taskId = message._qaTaskId;
    if (taskId && state.Api?.paper?.qaAbort) {
      void state.Api.paper.qaAbort(taskId).catch(() => undefined);
    }
  });
  activeMessages.clear();
}

export function installPaperQAGlobals(): void {
  const target = globals();
  target._qaMsgInnerHtml = qaMsgInnerHtml;
  target._renderPaperQA = renderPaperQA;
  target._sendPaperQuestion = sendPaperQuestion;
  target._pollQATask = pollQATask;
  target._applyQAEvent = applyQAEvent;
  target._paperAskQuestion = paperAskQuestion;
  target._quotePaperSelection = quotePaperSelection;
  target._askAboutPaperSelection = askAboutPaperSelection;
  target._hidePaperQuoteBar = hidePaperQuoteBar;
  target._handlePaperTextSelection = handlePaperTextSelection;
  target._destroyPaperQA = destroyPaperQA;
}

installPaperQAGlobals();
