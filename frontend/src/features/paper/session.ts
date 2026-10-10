import { showWorkspace, releaseWorkspace } from '../../application-shell/page-host';
import { featureRegistry } from '../../feature-registry';
import { escapeHtml as escape } from '../../html-safety';
import type { I18nKey } from '../../i18n';
import { mountPaperWorkbench, syncPaperWorkbench, openPaperWorkbench, leavePaperWorkbench } from './workbench';

import { scheduleAnimationFrame } from '../../conversation/ui/animation-frame-scheduler';
type LooseObject = Record<string, any>;
type PaperSessionWindow = Window & Record<string, any>;

function globals(): PaperSessionWindow {
  return featureRegistry as unknown as PaperSessionWindow;
}

let enterGeneration = 0;
let tabGeneration = 0;

const BACK_ICON = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="19" y1="12" x2="5" y2="12"/><polyline points="12 19 5 12 12 5"/></svg>';
const PAPER_ICON = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/><line x1="8" y1="7" x2="16" y2="7"/><line x1="8" y1="11" x2="14" y2="11"/></svg>';

function translate(key: I18nKey, fallback: string): string {
  const value = globals().t?.(key);
  return typeof value === 'string' && value && value !== key ? value : fallback;
}

function setModeChrome(active: boolean): void {
  if (active) showWorkspace('paper'); else releaseWorkspace('paper');
  const state = globals();
  const sidebar = document.getElementById('sidebar');
  if (active) {
    const closePreview = featureRegistry._paperPreviewActivate;
    if (typeof closePreview === 'function') closePreview(null);
    sidebar?.classList.add('paper-active');
    if (sidebar?.classList.contains('collapsed')) state.toggleSidebar?.();
    document.body?.classList.add('paper-mode-active');
  } else {
    sidebar?.classList.remove('paper-active');
    document.body?.classList.remove('paper-mode-active');
  }

  const container = document.getElementById('paperModeContainer');
  const chat = document.querySelector<HTMLElement>('.chat-wrapper');
  const input = document.querySelector<HTMLElement>('.input-area');
  if (container) container.style.display = active ? 'flex' : 'none';
  if (chat) chat.style.display = active ? 'none' : '';
  if (input) input.style.display = active ? 'none' : '';

  const button = document.getElementById('paperModeBtn');
  if (button) {
    button.classList.toggle('active', active);
    button.innerHTML = active
      ? `${BACK_ICON}<span class="topbar-tool-label">${translate('topbar.backToChat', 'Back')}</span>`
      : `${PAPER_ICON}<span class="topbar-tool-label">${translate('topbar.paper', 'Paper')}</span>`;
    button.title = active ? 'Back to Chat' : translate('paper.title', 'Paper Reader');
  }
}

function clearPaperState(): void {
  const state = globals();
  state._paperPdfUrl = '';
  state._paperPdfFilename = '';
  state._paperFileName = '';
  state._paperParsedText = '';
  state._paperArxivId = '';
  state._paperQAHistory = [];
  state._paperReportCache = '';
  state._paperReviewCache = '';
  state._paperReviewVenue = '';
  state._paperHash = '';
  state._paperImages = [];
  state._paperTotalPages = 0;
  state._babelTranslatedPages = {};
}

function restorePaperState(active: LooseObject): void {
  const state = globals();
  state._paperPdfUrl = active.pdfUrl || '';
  state._paperPdfFilename = active.pdfFilename || '';
  state._paperFileName = active.title || '';
  state._paperParsedText = active.parsedText || '';
  state._paperArxivId = active.arxivId || '';
  state._paperQAHistory = active.qaHistory || [];
  state._paperReportCache = '';
  state._paperReviewCache = '';
  state._paperReviewVenue = '';
  state._paperHash = active.paperHash || '';
  state._paperImages = Array.isArray(active.images) ? active.images : [];
  state._babelTranslatedPages = active.babelCache || {};
  state._paperTotalPages = active.pageCount || 0;
}

/** Enter Paper mode in two phases: paint synchronously, hydrate asynchronously. */
const landingLocales = new WeakMap<HTMLElement, string>();
export function reusePaperLanding(viewer: HTMLElement): boolean {
  const locale = document.documentElement.lang;
  const existing = viewer.firstElementChild?.classList.contains('paper-landing') === true;
  const reusable = existing && landingLocales.get(viewer) === locale;
  landingLocales.set(viewer, locale); return reusable;
}

export async function enterPaperMode(
  pdfUrl = '',
  fileName = '',
  parsedText = '',
  arxivId = '',
): Promise<void> {
  const state = globals();
  const generation = ++enterGeneration;
  const selectionRevision = state._getPaperSelectionRevision?.() || 0;
  if (state.imageGenMode) state.exitImageGenMode?.();
  state.paperMode = true;

  // This must stay before the first await: a slow library request must never
  // make the Paper button appear unresponsive.
  setModeChrome(true);
  mountPaperWorkbench();
  const library = Array.isArray(state._paperLibrary) ? state._paperLibrary : [];
  state._paperLibraryLoading = library.length === 0;
  state._renderPaperLibrary?.();
  state._showPaperLanding?.();

  try {
    await state._loadPaperLibrary?.();
  } catch (error: unknown) {
    console.warn('[Paper] loadPaperLibrary failed:', error);
  } finally {
    state._paperLibraryLoading = false;
  }
  if (!state.paperMode || generation !== enterGeneration) return;

  const sourceUnchanged = (state._getPaperSelectionRevision?.() || 0) === selectionRevision;
  if (sourceUnchanged && pdfUrl && !state._activePaperId) {
    state._createPaperEntry?.(fileName, pdfUrl, parsedText, arxivId);
  } else if (sourceUnchanged && pdfUrl) {
    state._paperPdfUrl = pdfUrl;
    state._paperFileName = fileName;
    state._paperParsedText = parsedText;
    state._paperArxivId = arxivId;
  } else if (sourceUnchanged) {
    const active = state._getActivePaperEntry?.() as LooseObject | null | undefined;
    if (active) restorePaperState(active);
    else clearPaperState();
  }

  if (sourceUnchanged) state._paperActiveTab = 'overview';
  if (!Array.isArray(state._paperQAHistory)) state._paperQAHistory = [];
  if (!state._paperReportCache) state._paperReportCache = '';
  state._updatePaperTitles?.();
  state._renderPaperLibrary?.();
  if (sourceUnchanged) {
    if (state._paperPdfUrl) void state._loadPaperPdf?.(state._paperPdfUrl);
    else state._showPaperLanding?.();
    switchPaperTab('overview');
    setPaperMobileView('pdf');
  }

  try { state._populatePaperReportModelDropdown?.(); }
  catch (error: unknown) {
    console.warn('[Paper] populate report model dropdown failed:', error);
  }
  try {
    const review = state._reportView?.('review');
    state._populatePaperReportModelDropdown?.(review);
  } catch (error: unknown) {
    console.warn('[Paper] populate review model dropdown failed:', error);
  }
  try { state._applyReaderPrefs?.(); }
  catch (error: unknown) {
    console.warn('[Paper] applyReaderPrefs failed:', error);
  }
  state.debugLog?.('Paper Mode: ENTER', 'success');
}

function restoreChatTitle(): void {
  const state = globals();
  const topbar = document.getElementById('topbarTitle');
  if (!topbar) return;
  const conversations = Array.isArray(state.conversations) ? state.conversations : [];
  const conversation = state.activeConvId
    ? conversations.find((item: LooseObject) => item?.id === state.activeConvId)
    : null;
  const title = conversation?.title;
  topbar.textContent = !title || title === 'New Chat'
    ? translate('chat.newConversation', 'New Chat') : title;
  topbar.title = '';
}

/** Tear down only client resources; durable server tasks remain resumable. */
export function exitPaperMode(): void {
  const state = globals();
  enterGeneration += 1;
  tabGeneration += 1;
  leavePaperWorkbench();
  void state._saveActivePaperState?.();
  state._teardownReadingTracker?.(true);
  state.paperMode = false;
  state._destroyPaperSession?.();

  try { restoreChatTitle(); }
  catch (error: unknown) { console.warn('[Paper] restore topbar title failed:', error); }
  setModeChrome(false);
  state._scheduleReflow?.();

  try { state._paperQAAbort?.abort?.(); }
  catch (error: unknown) { console.warn('[Paper] QA abort failed:', error); }
  state._paperQAAbort = null;

  for (const stream of [state._paperReportStream, state._paperReviewStream]) {
    if (stream?.pollTimer) window.clearTimeout(stream.pollTimer);
    if (stream) stream.pollTimer = null;
  }
  const viewer = document.getElementById('paperPdfViewer');
  if (viewer) viewer.innerHTML = '';
  state.debugLog?.('Paper Mode: EXIT', 'info');
}

export function togglePaperMode(): void | Promise<void> {
  return globals().paperMode ? exitPaperMode() : enterPaperMode();
}

function paintNoText(view: LooseObject | null | undefined): void {
  const container = view?.containerId
    ? document.getElementById(String(view.containerId))
    : null;
  if (container) {
    container.innerHTML = '<div class="paper-report-empty"><p>'
      + escape(translate('paper.reportNoText', 'No paper text available. Load a PDF first.'))
      + '</p></div>';
  }
}

/** Switch Paper tabs and dispatch only the selected tab's lazy initializer. */
export function switchPaperTab(tab: string): void {
  mountPaperWorkbench();
  syncPaperWorkbench(tab);
  leavePaperWorkbench();
  const state = globals();
  const generation = ++tabGeneration;
  if (['report', 'review'].includes(String(state._paperActiveTab))
      && tab !== state._paperActiveTab) {
    state._teardownReadingTracker?.(true);
  }
  state._paperActiveTab = tab;
  document.querySelectorAll<HTMLElement>('.paper-tab-btn').forEach((button) => {
    button.classList.toggle('active', button.dataset.tab === tab);
  });
  document.querySelectorAll<HTMLElement>('.paper-tab-panel').forEach((panel) => {
    panel.style.display = panel.dataset.tab === tab ? '' : 'none';
  });

  if (tab === 'overview') {
    void openPaperWorkbench(async () => { await state._loadOrGenerateReport?.(undefined, { readOnly: true }); });
  }

  if (tab === 'report' || tab === 'review') {
    try {
      const sidebar = document.getElementById('sidebar');
      if (sidebar && !sidebar.classList.contains('collapsed')) state.toggleSidebar?.();
    } catch (error: unknown) {
      console.warn('[Paper] auto-collapse sidebar failed:', error);
    }
    const view = state._reportView?.(tab) as LooseObject | null | undefined;
    const recoverable = Boolean(
      state._paperParsedText || state._paperHash
      || state._paperPdfUrl || state._paperPdfFilename,
    );
    if (!recoverable) {
      paintNoText(view);
    } else if (tab === 'review') {
      const load = (): void => {
        if (generation === tabGeneration && state._paperActiveTab === 'review') {
          state._loadOrGenerateReport?.(view);
        }
      };
      try {
        Promise.resolve(state._populateReviewVenueDropdown?.())
          .then(load)
          .catch((error: unknown) => {
            console.warn('[Paper:Review] venue resolve failed, loading with fallback:', error);
            load();
          });
      } catch (error: unknown) {
        console.warn('[Paper:Review] venue resolve failed, loading with fallback:', error);
        load();
      }
      state._restoreRebuttalPanel?.();
      state._syncReviewSegState?.(true);
    } else {
      state._loadOrGenerateReport?.(view);
    }
  }

  if (tab === 'qa') state._renderPaperQA?.();
  if (tab === 'translate') state._initBabelPdfTab?.();
  if (tab === 'podcast') state._initPodcastTab?.();
  if (tab === 'video') state._initVideoTab?.();
}

export function setPaperMobileView(view: string): void {
  const state = globals();
  const selected = view === 'reader' ? 'reader' : 'pdf';
  document.querySelector('.paper-body')?.setAttribute('data-paper-view', selected);
  document.querySelectorAll<HTMLElement>('.paper-mobile-switch-btn').forEach((button) => {
    button.classList.toggle('active', button.dataset.view === selected);
  });
  if (selected === 'pdf' && state._paperPdfDoc && typeof state.paperFitWidth === 'function') {
    scheduleAnimationFrame(window, () => {
      try { state.paperFitWidth(); }
      catch (error: unknown) { console.warn('[Paper] mobile fit-width failed:', error); }
    });
  }
}

export function installPaperSessionOwner(): void {
  const target = globals();
  target.enterPaperMode = enterPaperMode;
  target._reusePaperLanding = reusePaperLanding;
  target.exitPaperMode = exitPaperMode;
  target.togglePaperMode = togglePaperMode;
  target._switchPaperTab = switchPaperTab;
  target._setPaperMobileView = setPaperMobileView;
}

installPaperSessionOwner();
