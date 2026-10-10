/** Owned chat preview orchestration. Server TaskRuntime owns build scheduling;
 * the browser retains one pending submission, one task subscription and one panel.
 */
import { paperBuildApi, type PaperBuildApi } from '../../api/paper-build';
import { apiFailure } from '../../api/errors';
import { paperDocumentsApi, paperDocumentKey, paperDocumentSelection,
  type PaperDocumentsApi, type PaperDocumentDiscovery } from '../../api/paper-documents';
import type { PaperBuildAcceptedV1, PaperBuildRequestV1, PaperDocumentV1,
  PaperDocumentRecordV1, PaperDocumentOpenRequestV1 } from '../../api/paper-build.generated';
import { PAPER_BUILD_LIMITS as limits } from '../../api/paper-build.generated';
import { readLiveRuntimeBinding } from '../../feature-registry';
import { createLifecycleScope } from '../../lifecycle';
import { readBrowserStorage, writeBrowserStorage } from '../../core/browser-storage';
import { t, type Translator } from '../../i18n';
import type { PdfJsModule } from './pdf-loader';
import type { PaperPreviewChange } from './preview-changes';
import { PaperPreviewPanel } from './preview-panel';
import { SavedReading } from './saved-reading';

interface PreviewOptions {
  api: PaperBuildApi; documentsApi: PaperDocumentsApi; host(): HTMLElement | null; activeConversation(): string | null;
  translate: Translator; loadPdfJs?: () => Promise<PdfJsModule>; pollMs?: number; debounceMs?: number;
}
interface DiscoveryHint { path: string; projectRoot?: string }
interface DiscoveryDemand { conversationId: string; hint?: DiscoveryHint; explicit: boolean; epoch: number }
interface Subscription { accepted: PaperBuildAcceptedV1; controller: AbortController }
interface OpenDemand { request: PaperDocumentOpenRequestV1; epoch: number; restore: boolean; retry: boolean }
const preferenceKey = 'tofu_paper_preview_closed';

/** Compact, server-greppable debug line: real error code + request id + message. */
function failureDebug(error: unknown): string {
  const failure = apiFailure(error);
  const parts = [
    failure.code != null ? `code=${String(failure.code)}` : '',
    failure.requestId ? `request_id=${failure.requestId}` : '',
    failure.status ? `status=${failure.status}` : '',
    failure.message ? `message=${failure.message}` : '',
  ].filter(Boolean);
  return parts.join(' ');
}
function closedConversations(): string[] {
  try {
    const value: unknown = JSON.parse(readBrowserStorage(preferenceKey) ?? '[]');
    return Array.isArray(value) ? value.filter((id): id is string => typeof id === 'string' && id.length <= 160)
      .slice(-limits.preview_closed_conversations) : [];
  } catch { return []; }
}
function pause(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    const done = (): void => { clearTimeout(timer); signal.removeEventListener('abort', done); resolve(); };
    const timer = setTimeout(done, ms);
    if (signal.aborted) done(); else signal.addEventListener('abort', done, { once: true });
  });
}

export function createPaperPreview(overrides: Partial<PreviewOptions> = {}): {
  observeChanges(changes: PaperPreviewChange[]): void; activateConversation(id: string | null): void;
  discover(conversationId: string, hint?: DiscoveryHint): void;
  openDocument(conversationId: string, hint?: DiscoveryHint): void;
  close(): void; destroy(): void;
} {
  const options: PreviewOptions = { api: paperBuildApi, documentsApi: paperDocumentsApi, host: () => {
    const host = document.querySelector<HTMLElement>('.chat-wrapper');
    return host && getComputedStyle(host).display !== 'none' ? host : null;
  },
    activeConversation: () => (readLiveRuntimeBinding('activeConvId') as string | null) ?? null,
    translate: t, pollMs: 650, debounceMs: 180, ...overrides };
  const scope = createLifecycleScope();
  let active: string | null = null;
  let panel: PaperPreviewPanel | null = null;
  let reopen: HTMLButtonElement | null = null;
  let closed = closedConversations();
  let latest: PaperPreviewChange | null = null;
  let pending: PaperPreviewChange | null = null;
  let requesting = false;
  let subscription: Subscription | null = null;
  let generation = 0;
  let timer: ReturnType<typeof setTimeout> | null = null;

  let documents: PaperDocumentDiscovery | null = null;
  let selected: PaperDocumentV1 | null = null;
  let saved: PaperDocumentRecordV1 | null = null;
  let openEpoch = 0;
  let openController: AbortController | null = null;
  let openPending: OpenDemand | null = null;
  let opening = false;
  const reading = new SavedReading(options.documentsApi, (documentId) => {
    if (saved?.document_id !== documentId || scope.signal.aborted || !panel) return;
    panel.setNotice(options.translate('preview.readingSaveFailed'), { label: options.translate('paper.retry'),
      run: () => { if (active) requestOpen({ conv_id: active, document_id: documentId }, false, true); } });
  });
  const captureReading = (final = false): void => {
    const state = panel?.readingState();
    const id = panel?.element.dataset.documentId;
    if (id && state) reading.capture(id, state);
    reading.flush(id, final);
  };
  const releaseReading = (): void => {
    const id = saved?.document_id;
    captureReading(true);
    if (id) reading.release(id);
  };
  const stopOpen = (): void => {
    openEpoch++; openPending = null; openController?.abort();
  };
  let discoveryEpoch = 0;
  let discoveryController: AbortController | null = null;
  let discoveryPending: DiscoveryDemand | null = null;
  let discoveryRunning = false;
  let selecting: string | null = null;
  let selectionEpoch = 0;
  let selectionPending: { value: PaperDocumentV1; conversationId: string } | null = null;
  const stopDiscovery = (): void => {
    discoveryEpoch++; discoveryPending = null; discoveryController?.abort();
  };
  const documentChange = (value: PaperDocumentV1): PaperPreviewChange => ({
    conversationId: active!, changeId: crypto.randomUUID(), source: value.source,
    ...(value.source === 'local' ? { path: value.path, mainFile: value.main_file, projectRoot: value.project_root }
      : { path: value.main_file ?? '', mainFile: value.main_file ?? undefined,
        projectId: value.project_id, mcpServer: value.mcp_server }),
  });
  const presentDocuments = (): void => {
    const choices = [...(documents?.documents ?? [])];
    if (selected && !choices.some(value => paperDocumentKey(value) === paperDocumentKey(selected))) choices.unshift(selected);
    panel?.setDocuments(choices.slice(0, limits.discovery_candidates), selected, documents?.overleaf_servers ?? []);
    panel?.setCompileAvailable(saved?.can_compile ?? true);
  };

  const detach = (accepted: PaperBuildAcceptedV1): void => {
    void options.api.detach(accepted.task_id, accepted.subscriber_id).catch(() => {
      // Server subscription expiry bounds work when a disconnect loses detach.
    });
  };
  const cancelWatch = (): void => {
    if (!subscription) return;
    const previous = subscription; subscription = null;
    previous.controller.abort(); detach(previous.accepted);
  };
  const invalidate = (): void => {
    generation++; pending = null;
    if (timer) clearTimeout(timer); timer = null;
    cancelWatch(); panel?.cancelLoad();
  };
  const rememberClosed = (value: boolean): void => {
    if (!active) return;
    closed = closed.filter((id) => id !== active);
    if (value) closed.push(active);
    closed = closed.slice(-limits.preview_closed_conversations); writeBrowserStorage(preferenceKey, JSON.stringify(closed));
  };
  const ensurePanel = (focusBefore?: HTMLElement | null): PaperPreviewPanel | null => {
    if (panel) return panel;
    const host = options.host(); if (!host) return null;
    reopen?.remove(); reopen = null;
    panel = new PaperPreviewPanel({ host, api: options.api, translate: options.translate,
      loadPdfJs: options.loadPdfJs, close, refresh, focusBefore,
      documentsApi: options.documentsApi,
      discover: () => { if (active) requestDiscovery(active, undefined, true); },
      onReading: (id, state) => { if (document.visibilityState !== 'hidden') reading.capture(id, state); },
      importArxiv: (arxiv, report, signal) => {
        const conversationId = active;
        if (!conversationId) return Promise.resolve(null);
        return options.documentsApi.importArxiv({ conv_id: conversationId, arxiv }, signal)
          .catch(error => { report('failed', apiFailure(error).message); return null; });
      },
      select: (value) => { void selectDocument(value); } });
    presentDocuments();
    if (latest) panel.setFilename(latest.file ?? latest.path);
    return panel;
  };
  const showReopen = (): void => {
    if (reopen || (!saved && !latest && !documents?.documents.length) || !active) return;
    const host = options.host(); if (!host) return;
    reopen = document.createElement('button'); reopen.type = 'button'; reopen.className = 'paper-preview-reopen';
    reopen.textContent = options.translate('preview.paper');
    reopen.setAttribute('aria-label', options.translate('preview.open'));
    reopen.onclick = () => {
      if (scope.signal.aborted) return;
      if (active) requestDiscovery(active, undefined, true);
    };
    host.append(reopen);
  };
  function close(): void {
    releaseReading(); stopOpen();
    rememberClosed(true); selectionEpoch++; selectionPending = null;
    stopDiscovery(); invalidate(); panel?.destroy(true); panel = null; showReopen();
  }
  function refresh(): void {
    if (selecting === active) return;
    if (saved?.can_compile === false) return;
    if (selected) { stopDiscovery(); queue(documentChange(selected)); return; }
    if (latest) queue({ ...latest, changeId: crypto.randomUUID() });
    else if (active) requestDiscovery(active, undefined, true);
  }
  function activateConversation(id: string | null): void {
    if (active === id) return;
    releaseReading(); stopOpen();
    selectionEpoch++; selectionPending = null;
    stopDiscovery(); invalidate(); panel?.destroy(); panel = null; reopen?.remove(); reopen = null;
    active = id; latest = null; selected = null; documents = null; saved = null;
  }
  async function selectDocument(value: PaperDocumentV1): Promise<void> {
    if (!active || scope.signal.aborted) return;
    if (selecting) { selectionPending = { value, conversationId: active }; return; }
    const conversationId = active;
    releaseReading(); stopOpen(); stopDiscovery(); invalidate();
    const epoch = ++selectionEpoch;
    selecting = conversationId;
    panel?.setSelectionBusy(true);
    panel?.setStatus(options.translate('preview.selecting'), true);
    try {
      const result = await options.documentsApi.select(paperDocumentSelection(conversationId, value));
      if (epoch !== selectionEpoch || active !== conversationId || scope.signal.aborted) return;
      const changedDocument = paperDocumentKey(selected) !== paperDocumentKey(result.selected);
      selected = result.selected; latest = documentChange(selected); saved = null;
      if (changedDocument && panel) {
        const focusBefore = panel.focusBefore; panel.destroy(); panel = null; ensurePanel(focusBefore);
      }
      presentDocuments(); panel?.setNotice(''); panel?.setStatus(options.translate('preview.ready'));
      requestOpen({ conv_id: conversationId, source: selected }, true);
    } catch (error) {
      if (epoch === selectionEpoch) { presentDocuments(); panel?.setError(apiFailure(error).message, failureDebug(error)); }
    } finally {
      selecting = null;
      if (active === conversationId) panel?.setSelectionBusy(false);
      const next = selectionPending; selectionPending = null;
      if (next && active === next.conversationId) void selectDocument(next.value);
      void drainDiscovery();
    }
  }
  async function drainDiscovery(): Promise<void> {
    if (discoveryRunning || selecting === active) return;
    discoveryRunning = true;
    try {
      while (discoveryPending && !scope.signal.aborted) {
        const demand = discoveryPending; discoveryPending = null;
        const controller = new AbortController(); discoveryController = controller;
        try {
          // A read path is an invalidation hint, not an explicit main-file choice.
          // Scan the owned roots so a template sibling cannot disappear from the chooser.
          const result = await options.documentsApi.discover({ conv_id: demand.conversationId,
            ...(demand.hint?.projectRoot ? { project_root: demand.hint.projectRoot } : {}) }, controller.signal);
          if (controller.signal.aborted || demand.epoch !== discoveryEpoch || active !== demand.conversationId) continue;
          const choice = saved?.source ?? result.selected;
          const changedDocument = paperDocumentKey(selected) !== paperDocumentKey(choice);
          const focusBefore = panel?.focusBefore;
          if (changedDocument) {
            // First discovery fills an empty reader. Preserve a source drawer
            // opened while that scan was running; existing identities still retire.
            const replacePanel = Boolean(selected || latest || panel?.reader.state.sourceId);
            invalidate();
            if (replacePanel) { panel?.destroy(); panel = null; }
          }
          documents = result; selected = choice;
          if (selected && !result.documents.some(item => paperDocumentKey(item) === paperDocumentKey(selected))) {
            documents = { ...result, documents: [selected, ...result.documents] };
          }
          if (selected) latest = documentChange(selected);
          else latest = null;
          if (demand.explicit) ensurePanel(focusBefore);
          presentDocuments();
          panel?.setNotice(saved?.can_compile === false ? options.translate('preview.sourceUnavailable')
            : result.diagnostics[0]?.message ?? '');
          if (!subscription && !requesting && !pending && !panel?.reader.state.sourceId) panel?.setStatus(options.translate(saved?.can_compile === false ? 'preview.noSavedBuild' : selected ? 'preview.ready'
            : result.documents.length ? 'preview.chooseDocument' : 'preview.noDocuments'));
          if (!panel) showReopen();
        } catch (error) {
          if (!controller.signal.aborted && demand.epoch === discoveryEpoch) {
            // Explicit open owns an error surface even when no file was selected.
            if (demand.explicit && (saved?.build || panel?.reader.state.sourceId)) {
              panel?.setNotice(options.translate(saved?.can_compile === false ? 'preview.sourceUnavailable' : 'preview.discoveryFailed'));
            } else if (demand.explicit) ensurePanel()?.setError(apiFailure(error).message, failureDebug(error));
            else console.warn('[PaperPreview] discovery unavailable:', apiFailure(error).message);
          }
        } finally { if (discoveryController === controller) discoveryController = null; }
      }
    } finally { discoveryRunning = false; }
  }
  function requestDiscovery(conversationId: string, hint?: DiscoveryHint, explicit = false): void {
    if (scope.signal.aborted || options.activeConversation() !== conversationId || !options.host()) return;
    activateConversation(conversationId);
    if (explicit) {

      // Opening another conversation is independent of a pending settings write.
      rememberClosed(false); ensurePanel()?.focus();
      if (!latest && !selected && !documents) panel?.setDocuments([], null);
      if (!subscription && !pending && !panel?.reader.state.sourceId) panel?.setStatus(options.translate('preview.discovering'), true);
    }
    if (explicit || !saved) requestOpen({ conv_id: conversationId }, (explicit || !!panel) && !panel?.reader.state.sourceId);
    // One in-flight request plus one trailing latest demand. No polling or archive cache.
    discoveryEpoch++;
    discoveryPending = { conversationId, hint, explicit: explicit || !!panel, epoch: discoveryEpoch };
    void drainDiscovery();
  }
  function requestOpen(request: PaperDocumentOpenRequestV1, restore: boolean, retry = false): void {
    openEpoch++; openController?.abort();
    openPending = { request, restore, retry, epoch: openEpoch };
    void drainOpen();
  }
  async function drainOpen(): Promise<void> {
    if (opening || scope.signal.aborted) return;
    opening = true;
    try {
      while (openPending && !scope.signal.aborted) {
        const demand = openPending; openPending = null;
        const controller = new AbortController(); openController = controller;
        const stale = (): boolean => controller.signal.aborted || demand.epoch !== openEpoch
          || active !== demand.request.conv_id || scope.signal.aborted;
        try {
          const result = await options.documentsApi.open(demand.request, controller.signal);
          if (stale()) continue;
          saved = result.document;
          if (!saved) continue;
          const position = reading.open(saved);
          selected = saved.source; latest = documentChange(selected);
          presentDocuments();
          const target = panel;
          if (demand.restore && target && saved.build) {
            target.setStatus(options.translate('preview.loading'), true);
            await target.applyBuild(saved.build, position);
            if (stale() || panel !== target) continue;
          } else if (target?.element.dataset.documentId === saved.document_id) {
            const state = target.readingState();
            if (state) reading.capture(saved.document_id, state);
          }
          target?.setNotice(saved.can_compile ? '' : options.translate('preview.sourceUnavailable'));
          if (demand.restore && saved.build && saved.status === 'failed') target?.setStatus(options.translate('preview.failed'));
          if (!saved.build && target && !target.reader.state.sourceId) target.setStatus(options.translate(saved.can_compile ? 'preview.ready' : 'preview.noSavedBuild'));
          if (demand.retry) reading.flush(saved.document_id);
          if (!panel) showReopen();
        } catch (error) {
          if (!stale() && demand.restore) panel?.setError(apiFailure(error).message, failureDebug(error));
          else if (!stale()) panel?.setNotice(options.translate('preview.readingSaveFailed'), {
            label: options.translate('paper.retry'), run: () => requestOpen(demand.request, false, true) });
        } finally { if (openController === controller) openController = null; }
      }
    } finally { opening = false; }
  }
  async function watch(current: Subscription, epoch: number): Promise<void> {
    const { accepted, controller } = current;
    const stale = (): boolean => controller.signal.aborted || generation !== epoch || scope.signal.aborted;
    let renewedAt = Date.now();
    try {
      while (!stale()) {
        const task = await options.api.task(accepted.task_id, controller.signal);
        if (stale()) return;
        if (task.status !== 'pending' && task.status !== 'running') {
          if (task.result) {
            const target = panel;
            if (task.result.superseded) target?.setStatus(options.translate('preview.outdated'));
            else if (task.status === 'done' && task.result.receipt.status === 'success') {
              if (task.result.retained !== false) {
                target?.setStatus(options.translate('preview.loading'), true);
                await target?.applyBuild(task.result);
              }
              if (task.result.retained === false) target?.setStatus(options.translate('preview.unverified'));
              if (!stale() && active) requestOpen({ conv_id: active, document_id: accepted.document_id },
                task.result.retained === false && !target?.reader.state.sourceId);
            } else {
              await target?.applyBuild(task.result);
              target?.setStatus(options.translate('preview.failed'));
            }
          } else if (task.error) panel?.setError(apiFailure(task.error).message, failureDebug(task.error));
          else panel?.setStatus(options.translate(task.status === 'aborted' ? 'preview.cancelled' : 'preview.failed'));
          return;
        }
        if (Date.now() - renewedAt >= 30000) {
          const renewal = await options.api.renew(accepted.task_id, accepted.subscriber_id, controller.signal);
          if (!renewal.renewed) throw new Error(options.translate('preview.expired'));
          renewedAt = Date.now();
        }
        await pause(options.pollMs!, controller.signal);
      }
    } catch (error) {
      if (!stale()) panel?.setError(apiFailure(error).message, failureDebug(error));
    } finally {
      if (subscription === current) { subscription = null; detach(accepted); }
    }
  }
  async function submit(): Promise<void> {
    if (requesting || scope.signal.aborted) return;
    requesting = true;
    try {
      while (pending) {
        const change = pending; pending = null;
        const epoch = generation;
        const body: PaperBuildRequestV1 = { source: change.source, conv_id: change.conversationId,
          request_id: change.changeId, subscriber_id: crypto.randomUUID(),
          ...(selected?.document_id ? { document_id: selected.document_id } : {}),
          ...(change.source === 'local' ? { path: change.path } : { project_id: change.projectId, file: change.file }),
          ...(change.mainFile ? { main_file: change.mainFile } : {}),
          ...(change.projectRoot ? { project_root: change.projectRoot } : {}),
          ...(change.mcpServer ? { mcp_server: change.mcpServer } : {}) };
        try {
          // Receive the acceptance even after a close, then detach its exact
          // subscriber. Cancelling this POST could lose the accepted task ID.
          const accepted = await options.api.start(body);
          if (epoch !== generation || scope.signal.aborted || active !== change.conversationId) { detach(accepted); continue; }
          const target = ensurePanel();
          if (!target) { detach(accepted); continue; }
          target.setStatus(options.translate('preview.compiling'), true);
          const current: Subscription = { accepted, controller: new AbortController() };
          subscription = current; void watch(current, epoch);
        } catch (error) {
          if (epoch === generation) panel?.setError(apiFailure(error).message, failureDebug(error));
        }
      }
    } finally { requesting = false; }
  }
  function queue(change: PaperPreviewChange): void {
    if (scope.signal.aborted) return;
    stopOpen(); invalidate(); latest = change;
    if (closed.includes(change.conversationId)) { showReopen(); return; }
    pending = change;
    ensurePanel();
    panel?.setStatus(options.translate('preview.compiling'), true);
    timer = setTimeout(() => { timer = null; void submit(); }, options.debounceMs);
  }
  scope.listen(window, 'pagehide', () => activateConversation(null));
  return {
    activateConversation, close,
    discover: (id, hint) => requestDiscovery(id, hint),
    openDocument: (id, hint) => requestDiscovery(id, hint, true),
    observeChanges(changes) {
      if (!options.host()) { activateConversation(null); return; }
      const conversationId = options.activeConversation();
      if (!conversationId) return;
      activateConversation(conversationId);
      const change = changes.filter((item) => item.conversationId === conversationId).at(-1);
      if (change) {
        stopDiscovery();
        if (selected && selected.source !== change.source) {
          panel?.setNotice(options.translate('preview.otherDocument')); requestDiscovery(conversationId); return;
        }
        if (selected?.source === 'overleaf') {
          if (change.projectId !== selected.project_id || change.mcpServer !== selected.mcp_server) {
            panel?.setNotice(options.translate('preview.otherDocument')); requestDiscovery(conversationId); return;
          }
          queue({ ...change, mainFile: selected.main_file ?? undefined, mcpServer: selected.mcp_server });
        } else if (selected?.source === 'local' && change.source === 'local') {
          const root = selected.project_root.replace(/\/$/, '');
          const path = change.path.startsWith(root + '/') ? change.path.slice(root.length + 1) : change.path;
          const directory = selected.path.replace(/[^/]+$/, '');
          const selectedPath = selected.path;
          const otherMain = documents?.documents.some(item => item.source === 'local'
            && item.project_root === root && item.path === path && path !== selectedPath);
          if ((change.projectRoot && change.projectRoot !== root) || !path.startsWith(directory) || otherMain) {
            panel?.setNotice(options.translate('preview.otherDocument'));
            requestDiscovery(conversationId); return;
          }
          queue({ ...change, mainFile: selected.main_file, projectRoot: selected.project_root });
        } else queue(change);
      }
    },
    destroy() { const id = saved?.document_id; activateConversation(null); scope.destroy(); reading.close(id); },
  };
}
