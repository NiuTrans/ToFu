import { featureRegistry } from '../../feature-registry';
import {
  paperAttachPush,
  paperDetachPush,
  paperIngestEvent,
  taskReplayIngestPage,
  type PaperPushEvent,
  type PaperPushState,
} from './push-transport';

type JsonObject = Record<string, unknown>;
type RecommendStatus = 'pending' | 'running' | 'done' | 'error';

interface RecommendCard extends JsonObject {
  arxiv_id?: string;
  title?: string;
  authors?: unknown[];
  summary?: string;
  venue?: string;
  primary_category?: string;
  published?: string;
  why?: string;
}

interface RecommendCorrection extends JsonObject {
  note?: string;
  paper?: RecommendCard;
}

interface RecommendToolRound extends JsonObject {
  roundNum?: unknown;
  toolName?: unknown;
  query?: unknown;
  toolCallId?: unknown;
  status?: string;
  _elapsed?: string;
}

export interface RecommendStream extends PaperPushState {
  description: string;
  taskId: string | null;
  cursor: number;
  status: RecommendStatus;
  candidateCount: number;
  interpreted: boolean;
  researchCount: number;
  researchLabel: string;
  toolRounds: RecommendToolRound[];
  results: Array<RecommendCard | undefined>;
  correction: RecommendCorrection | null;
  llmError: boolean;
  aborted: boolean;
}

interface RecommendPollResponse {
  ok: boolean;
  status: number;
  json(): Promise<JsonObject>;
}

interface RecommendApi {
  recommendStart(description: string, limit: number): Promise<JsonObject>;
  recommendPoll(taskId: string, cursor: number): Promise<RecommendPollResponse | null>;
  recommendAbort(taskId: string): Promise<unknown>;
}

type RecElement = HTMLElement & { _recSig?: string; _recToolKey?: string };

type RecommendWindow = Window & {
  Api?: { paper?: RecommendApi };
  t?: (key: string) => string;
  escapeHtml?: (value: unknown) => string;
  debugLog?: (message: string, level?: string) => void;
  renderToolRoundsHTML?: (rounds: RecommendToolRound[], running: boolean) => string;
  _escWithInlineMath?: (value: unknown) => string;
  _persistRecommendedCard?: (card: RecommendCard | undefined) => unknown;
  _findLibraryEntryByArxiv?: (arxivId: string) => JsonObject | null;
  _fetchArxivPaper?: (reference: string, reuseId?: string) => unknown;
  _showPaperLanding?: () => void;
  _paperRecommendResults?: Array<RecommendCard | undefined>;
  _paperRecommendCorrection?: RecommendCard | null;
  _recStream?: RecommendStream | null;
  _recPaintScheduled?: boolean;
  _newRecStream?: typeof newRecStream;
  _submitPaperDescribe?: typeof submitPaperDescribe;
  _recommendPapers?: typeof recommendPapers;
  _pollRecommendTask?: typeof pollRecommendTask;
  _applyRecommendEvent?: typeof applyRecommendEvent;
  _renderRecommendError?: typeof renderRecommendError;
  _recCardInnerHtml?: typeof recCardInnerHtml;
  _recSkeletonInnerHtml?: typeof recSkeletonInnerHtml;
  _recCorrectionHtml?: typeof recCorrectionHtml;
  _paintRecommendFromState?: typeof paintRecommendFromState;
  _paintRecommendNow?: typeof paintRecommendNow;
  _openRecommendResult?: typeof openRecommendResult;
  _openRecommendCorrection?: typeof openRecommendCorrection;
  _destroyPaperRecommend?: typeof destroyPaperRecommend;
};

function globals(): RecommendWindow {
  return featureRegistry as unknown as RecommendWindow;
}

function state(): RecommendWindow {
  const target = globals();
  target._paperRecommendResults ??= [];
  target._paperRecommendCorrection ??= null;
  target._recStream ??= null;
  target._recPaintScheduled ??= false;
  return target;
}

function api(): RecommendApi {
  const paper = globals().Api?.paper;
  if (!paper) throw new Error('Paper recommendation API unavailable');
  return paper;
}

function escape(value: unknown): string {
  const helper = globals().escapeHtml;
  if (helper) return helper(value);
  const node = document.createElement('span');
  node.textContent = value == null ? '' : String(value);
  return node.innerHTML;
}

function translate(key: string): string {
  return globals().t?.(key) ?? key;
}

function inlineMath(value: unknown): string {
  return globals()._escWithInlineMath?.(value) ?? escape(value);
}

function persist(card: RecommendCard | undefined): void {
  if (card) void globals()._persistRecommendedCard?.(card);
}

function errorMessage(value: unknown, fallback: string): string {
  if (value && typeof value === 'object') {
    const row = value as JsonObject;
    if (typeof row.message === 'string') return row.message;
  }
  return typeof value === 'string' && value ? value : fallback;
}

export function newRecStream(description: string): RecommendStream {
  return {
    description,
    taskId: null,
    cursor: 0,
    status: 'pending',
    candidateCount: 0,
    interpreted: false,
    researchCount: 0,
    researchLabel: '',
    toolRounds: [],
    results: [],
    correction: null,
    llmError: false,
    aborted: false,
    _seqSeen: -1,
    _replayCursor: 0,
  };
}

export function submitPaperDescribe(): void {
  const input = document.getElementById('paperDescribeInput') as HTMLTextAreaElement | null;
  const description = input?.value?.trim() ?? '';
  if (!description) {
    globals().debugLog?.('Describe the paper you are looking for', 'warning');
    return;
  }
  void recommendPapers(description);
}

export async function recommendPapers(description: string): Promise<void> {
  const shared = state();
  const previous = shared._recStream;
  if (previous?.taskId && previous.status === 'running') {
    previous.aborted = true;
    paperDetachPush(previous);
    void api().recommendAbort(previous.taskId).catch(() => undefined);
  }
  const stream = newRecStream(description);
  shared._recStream = stream;
  shared._paperRecommendResults = stream.results;
  shared._paperRecommendCorrection = null;
  paintRecommendFromState();

  try {
    const start = await api().recommendStart(description, 6);
    const taskId = typeof start.task_id === 'string' ? start.task_id : '';
    if (start.ok !== true || !taskId) {
      throw new Error(errorMessage(start.error, 'recommend start failed'));
    }
    if (state()._recStream !== stream) return;
    stream.taskId = taskId;
    stream.status = 'running';
    await pollRecommendTask(stream);
  } catch (error: unknown) {
    console.error('[Paper] recommend failed:', error);
    if (state()._recStream === stream) {
      stream.status = 'error';
      renderRecommendError();
    }
  }
}

const RECOMMEND_POLL_MS = 600;

export async function pollRecommendTask(stream: RecommendStream): Promise<void> {
  if (!stream.taskId) return;
  paperAttachPush(stream, stream.taskId, {
    channel: 'paper',
    isCurrent: () => state()._recStream === stream && !stream.aborted,
    onEvent(event) {
      const dirty = paperIngestEvent(stream, event, applyRecommendEvent);
      if (dirty) paintRecommendFromState();
    },
    isTerminal: () => false,
  });

  try {
    while (state()._recStream === stream && !stream.aborted) {
      const response = await api().recommendPoll(stream.taskId, stream.cursor);
      if (state()._recStream !== stream || stream.aborted) break;
      if (!response?.ok) {
        if (response?.status === 404) {
          stream.status = 'error';
          paintRecommendFromState();
          break;
        }
        throw new Error(`HTTP ${response?.status ?? '?'}`);
      }
      const page = await response.json();
      if (page.ok !== true) {
        throw new Error(errorMessage(page.error, 'Poll failed'));
      }
      const replay = taskReplayIngestPage(
        stream, page, applyRecommendEvent, stream.cursor,
      );
      stream.cursor = replay.nextCursor;

      if (page.status === 'done') {
        stream.status = 'done';
        if (Array.isArray(page.results) && page.results.length >= stream.results.length) {
          stream.results = page.results.filter((row): row is RecommendCard => Boolean(
            row && typeof row === 'object'));
          state()._paperRecommendResults = stream.results;
        }
        // The terminal snapshot is authoritative even if retention or a
        // reconnect gap omitted the earlier interpret_done event. Never hide
        // final cards behind an unfulfilled skeleton-phase flag.
        stream.interpreted = true;
        stream.candidateCount = Math.max(stream.candidateCount, stream.results.length);
        if (page.correction && typeof page.correction === 'object') {
          stream.correction = page.correction as RecommendCorrection;
        }
        stream.llmError = Boolean(page.llmError);
        stream.results.forEach(persist);
        persist(stream.correction?.paper);
        paintRecommendFromState();
        break;
      }
      if (page.status === 'error') {
        stream.status = 'error';
        stream.llmError = Boolean(page.llmError);
        renderRecommendError();
        break;
      }
      paintRecommendFromState();
      await new Promise<void>((resolve) => window.setTimeout(resolve, RECOMMEND_POLL_MS));
    }
  } finally {
    paperDetachPush(stream);
  }
}

export function applyRecommendEvent(
  stream: RecommendStream,
  event: PaperPushEvent,
): boolean {
  switch (event.type) {
    case 'tool_start':
      stream.researchCount += 1;
      stream.researchLabel = typeof event.query === 'string'
        ? event.query.slice(0, 80) : '';
      stream.toolRounds.push({
        roundNum: event.roundNum,
        toolName: event.toolName,
        query: event.query || event.toolName,
        toolCallId: event.toolCallId || '',
        toolArgs: event.toolArgs || '',
        status: 'searching',
        results: null,
      });
      return true;
    case 'tool_done': {
      const round = stream.toolRounds.find((row) => row.roundNum === event.roundNum);
      if (round) {
        round.status = 'done';
        if (typeof event.elapsed === 'number') round._elapsed = `${event.elapsed.toFixed(1)}s`;
        for (const field of [
          'toolContent', 'results', 'searchDiag', 'engineBreakdown', 'verticals',
        ]) {
          if (event[field]) round[field] = event[field];
        }
      }
      return true;
    }
    case 'interpret_done':
      stream.interpreted = true;
      stream.candidateCount = typeof event.candidateCount === 'number'
        ? event.candidateCount : 0;
      return true;
    case 'candidate': {
      const index = typeof event.index === 'number' ? event.index : stream.results.length;
      const card = event.card && typeof event.card === 'object'
        ? event.card as RecommendCard : undefined;
      stream.results[index] = card;
      state()._paperRecommendResults = stream.results;
      persist(card);
      return true;
    }
    case 'correction': {
      stream.correction = event.correction && typeof event.correction === 'object'
        ? event.correction as RecommendCorrection : null;
      const paper = stream.correction?.paper ?? null;
      state()._paperRecommendCorrection = paper;
      persist(paper ?? undefined);
      return true;
    }
    case 'error':
      stream.llmError = Boolean(event.llmError);
      return true;
    default:
      return false;
  }
}

export function renderRecommendError(): void {
  const viewer = document.getElementById('paperPdfViewer');
  if (!viewer) return;
  viewer.innerHTML = '<div class="paper-error">'
    + escape(translate('paper.recommendFailed'))
    + '<br><button data-tofu-action="_showPaperLanding()" class="paper-retry-btn">'
    + escape(translate('paper.searchBack')) + '</button></div>';
}

export function recCardInnerHtml(card: RecommendCard, index: number): string {
  const authors = Array.isArray(card.authors) ? card.authors.map(String) : [];
  const authorText = authors.slice(0, 4).join(', ')
    + (authors.length > 4 ? ' et al.' : '');
  const meta: string[] = [];
  if (card.venue) meta.push('<span class="paper-card-venue">' + escape(card.venue) + '</span>');
  if (card.primary_category) {
    meta.push('<span class="paper-card-cat">' + escape(card.primary_category) + '</span>');
  }
  if (card.published) {
    meta.push('<span class="paper-card-date">' + escape(card.published) + '</span>');
  }
  meta.push('<span class="paper-card-id">arXiv:' + escape(card.arxiv_id) + '</span>');
  return '<div class="paper-result-num">' + (index + 1) + '</div>'
    + '<div class="paper-result-body"><div class="paper-result-title">'
    + inlineMath(card.title || card.arxiv_id) + '</div>'
    + (card.why
      ? '<div class="paper-result-why"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg><span>'
        + escape(card.why) + '</span></div>' : '')
    + (authorText
      ? '<div class="paper-result-authors">' + escape(authorText) + '</div>' : '')
    + (card.summary
      ? '<div class="paper-result-summary">' + inlineMath(card.summary) + '</div>' : '')
    + '<div class="paper-result-meta">' + meta.join('') + '</div></div>'
    + '<div class="paper-result-arrow"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 18 15 12 9 6"/></svg></div>';
}

export function recSkeletonInnerHtml(): string {
  return '<div class="paper-result-num paper-rec-sk-num"></div>'
    + '<div class="paper-result-body">'
    + '<div class="paper-rec-sk-line paper-rec-sk-title"></div>'
    + '<div class="paper-rec-sk-line paper-rec-sk-why"></div>'
    + '<div class="paper-rec-sk-line paper-rec-sk-meta"></div></div>';
}

export function recCorrectionHtml(
  correction: RecommendCorrection | null,
  t = translate,
): string {
  if (!correction?.note) return '';
  const paper = correction.paper;
  const offer = paper?.arxiv_id
    ? '<div class="paper-correction-offer paper-result-card" role="button" tabindex="0"'
      + ' data-tofu-action="_openRecommendCorrection()"'
      + ' data-tofu-action-keydown="if(event.key===\'Enter\'||event.key===\' \'){event.preventDefault();_openRecommendCorrection()}">'
      + '<div class="paper-result-body"><div class="paper-correction-offer-label">'
      + escape(t('paper.correctionActual')) + '</div>'
      + '<div class="paper-result-title">' + inlineMath(paper.title || paper.arxiv_id)
      + '</div><div class="paper-result-meta"><span class="paper-card-id">arXiv:'
      + escape(paper.arxiv_id) + '</span></div></div>'
      + '<div class="paper-result-arrow"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 18 15 12 9 6"/></svg></div></div>'
    : '';
  return '<div class="paper-correction" role="note">'
    + '<div class="paper-correction-icon"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg></div>'
    + '<div class="paper-correction-body"><div class="paper-correction-title">'
    + escape(t('paper.correctionTitle')) + '</div>'
    + '<div class="paper-correction-note">' + escape(correction.note) + '</div>'
    + offer + '</div></div>';
}

export function paintRecommendFromState(): void {
  const shared = state();
  if (shared._recPaintScheduled) return;
  shared._recPaintScheduled = true;
  const frame = typeof window.requestAnimationFrame === 'function'
    ? window.requestAnimationFrame.bind(window)
    : (callback: FrameRequestCallback) => window.setTimeout(() => callback(Date.now()), 16);
  frame(() => {
    state()._recPaintScheduled = false;
    try { paintRecommendNow(); } catch (error: unknown) {
      console.warn('[Paper:Recommend] paint failed:', error);
    }
  });
}

export function paintRecommendNow(): void {
  const stream = state()._recStream;
  if (!stream) return;
  const viewer = document.getElementById('paperPdfViewer');
  if (!viewer) return;
  const grounded = stream.results.filter(Boolean).length;
  const slots = stream.interpreted
    ? Math.max(grounded, stream.status === 'done' ? grounded : stream.candidateCount)
    : 0;
  let shell = viewer.querySelector<HTMLElement>('.paper-search[data-rec-shell]');
  if (!shell) {
    const header = '<div class="paper-search-head">'
      + '<button class="paper-search-back" data-tofu-action="_showPaperLanding()" title="'
      + escape(translate('paper.searchBack')) + '">'
      + '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="19" y1="12" x2="5" y2="12"/><polyline points="12 19 5 12 12 5"/></svg></button>'
      + '<div class="paper-search-head-text"><div class="paper-search-head-title">'
      + escape(translate('paper.recommendTitle')) + '</div>'
      + '<div class="paper-search-head-q">“' + escape(stream.description)
      + '”</div></div></div>';
    viewer.innerHTML = '<div class="paper-search" data-rec-shell="1">' + header
      + '<div class="paper-rec-status" data-rec-status aria-live="polite"></div>'
      + '<div class="paper-report-tools paper-rec-tools" data-rec-tools></div>'
      + '<div class="paper-rec-banner" data-rec-banner></div>'
      + '<div class="paper-search-hint" data-rec-hint hidden>'
      + escape(translate('paper.recommendHint')) + '</div>'
      + '<div class="paper-result-list" data-rec-list aria-live="polite" aria-relevant="additions"></div></div>';
    shell = viewer.querySelector<HTMLElement>('.paper-search[data-rec-shell]');
  }
  if (!shell) return;
  const statusElement = shell.querySelector<RecElement>('[data-rec-status]');
  const toolsElement = shell.querySelector<RecElement>('[data-rec-tools]');
  const bannerElement = shell.querySelector<RecElement>('[data-rec-banner]');
  const hintElement = shell.querySelector<HTMLElement>('[data-rec-hint]');
  const listElement = shell.querySelector<HTMLElement>('[data-rec-list]');
  if (!statusElement || !bannerElement || !listElement) return;

  let statusHtml = '';
  if (!stream.interpreted && stream.status !== 'error') {
    const text = stream.researchCount > 0
      ? translate('paper.recommendResearching').replace('{n}', String(stream.researchCount))
      : translate('paper.recommendInterpreting');
    statusHtml = '<span class="paper-rec-spin"></span>' + escape(text);
  } else if (stream.status === 'running' && grounded < slots) {
    statusHtml = '<span class="paper-rec-spin"></span>'
      + escape(translate('paper.recommendGrounding')
        .replace('{n}', String(grounded)).replace('{total}', String(slots)));
  }
  if (statusElement._recSig !== statusHtml) {
    statusElement.innerHTML = statusHtml;
    statusElement.hidden = !statusHtml;
    statusElement._recSig = statusHtml;
  }

  if (toolsElement) {
    const searching = stream.toolRounds.filter((round) => round.status === 'searching').length;
    const key = `${stream.toolRounds.length}:${searching}`;
    if (toolsElement._recToolKey !== key) {
      const render = globals().renderToolRoundsHTML;
      toolsElement.innerHTML = stream.toolRounds.length && render
        ? render(stream.toolRounds, stream.status === 'running') : '';
      toolsElement.hidden = stream.toolRounds.length === 0;
      toolsElement._recToolKey = key;
    }
  }

  const banner = recCorrectionHtml(stream.correction);
  if (bannerElement._recSig !== banner) {
    bannerElement.innerHTML = banner;
    bannerElement._recSig = banner;
  }
  if (hintElement) hintElement.hidden = grounded === 0;
  while (listElement.children.length > slots) listElement.lastElementChild?.remove();
  for (let index = 0; index < slots; index += 1) {
    const card = stream.results[index];
    let node = listElement.children[index] as RecElement | undefined;
    if (!node) {
      node = document.createElement('div') as RecElement;
      node.style.setProperty('--i', String(index));
      listElement.appendChild(node);
    }
    const status = card ? 'grounded' : 'searching';
    const signature = card ? `g:${card.arxiv_id || index}` : 'sk';
    if (node._recSig === signature) continue;
    if (card) {
      node.className = 'paper-result-card paper-rec-card';
      node.setAttribute('role', 'button');
      node.setAttribute('tabindex', '0');
      node.setAttribute('data-idx', String(index));
      node.setAttribute('data-status', status);
      node.onclick = () => openRecommendResult(index);
      node.onkeydown = (event: KeyboardEvent) => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          openRecommendResult(index);
        }
      };
      node.innerHTML = recCardInnerHtml(card, index);
    } else {
      node.className = 'paper-result-card paper-rec-card paper-rec-skeleton';
      node.removeAttribute('role');
      node.removeAttribute('tabindex');
      node.setAttribute('data-status', status);
      node.setAttribute('aria-hidden', 'true');
      node.onclick = null;
      node.onkeydown = null;
      node.innerHTML = recSkeletonInnerHtml();
    }
    node._recSig = signature;
  }
  const existingEmpty = shell.querySelector('.paper-search-empty');
  const showEmpty = stream.status === 'done' && grounded === 0 && !banner;
  if (showEmpty && !existingEmpty) {
    listElement.insertAdjacentHTML(
      'afterend', '<div class="paper-search-empty">'
        + escape(translate('paper.recommendNoResults')) + '</div>',
    );
  } else if (!showEmpty) existingEmpty?.remove();
}

export function openRecommendResult(index: number): void {
  const card = state()._paperRecommendResults?.[index];
  if (!card?.arxiv_id) return;
  const saved = globals()._findLibraryEntryByArxiv?.(card.arxiv_id);
  void globals()._fetchArxivPaper?.(
    card.arxiv_id, typeof saved?.id === 'string' ? saved.id : undefined,
  );
}

export function openRecommendCorrection(): void {
  const card = state()._paperRecommendCorrection;
  if (!card?.arxiv_id) return;
  const saved = globals()._findLibraryEntryByArxiv?.(card.arxiv_id);
  void globals()._fetchArxivPaper?.(
    card.arxiv_id, typeof saved?.id === 'string' ? saved.id : undefined,
  );
}

export function destroyPaperRecommend(): void {
  const stream = state()._recStream;
  if (!stream) return;
  stream.aborted = true;
  paperDetachPush(stream);
  if (stream.taskId && stream.status === 'running') {
    const recommendApi = globals().Api?.paper;
    if (recommendApi) {
      void recommendApi.recommendAbort(stream.taskId).catch(() => undefined);
    }
  }
}

export function installPaperRecommendGlobals(): void {
  const target = state();
  target._newRecStream = newRecStream;
  target._submitPaperDescribe = submitPaperDescribe;
  target._recommendPapers = recommendPapers;
  target._pollRecommendTask = pollRecommendTask;
  target._applyRecommendEvent = applyRecommendEvent;
  target._renderRecommendError = renderRecommendError;
  target._recCardInnerHtml = recCardInnerHtml;
  target._recSkeletonInnerHtml = recSkeletonInnerHtml;
  target._recCorrectionHtml = recCorrectionHtml;
  target._paintRecommendFromState = paintRecommendFromState;
  target._paintRecommendNow = paintRecommendNow;
  target._openRecommendResult = openRecommendResult;
  target._openRecommendCorrection = openRecommendCorrection;
  target._destroyPaperRecommend = destroyPaperRecommend;
}

installPaperRecommendGlobals();
