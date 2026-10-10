/** Chat/PDF split presentation. The shared PdfReader owns PDF resources;
 * this instance owns controls, focus, responsive layout and secondary drawers.
 */
import { ReaderNavigation, readerButton, readerIcon as icon, readerIcons as icons, showReaderSearch, showReaderOutline } from './reader-controls';
import '../../styles/application/24-paper-preview-sources.css';
import '../../styles/application/24-paper-preview-problems.css';
import '../../styles/application/24-paper-preview-layout.css';
import '../../styles/application/24-paper-preview-editor.css';
import '../../styles/application/24-paper-preview-selection.css';
import '../../styles/application/24-paper-preview-error-assist.css';
import '../../styles/application/24-paper-preview-proposal.css';

import { createLifecycleScope } from '../../lifecycle';
import { t, type Translator } from '../../i18n';
import { createPdfReader, type PdfReader, type ReaderState } from './pdf-reader';
import type { PaperArxivImportResultV1, PaperBuildResultV1, PaperDocumentV1, PaperReadingStateV1 } from '../../api/paper-build.generated';
import { paperBuildArtifactPath, type PaperBuildApi } from '../../api/paper-build';
import { paperDocumentKey, type PaperDocumentsApi } from '../../api/paper-documents';
import { createArxivImportForm, createOverleafSourceForm } from './preview-sources';
import { showSourceFile, showSourceTree, type SourceViewContext } from './preview-source-view';
import { BuildReader } from './build-reader';
import { ReadingFocus } from './reading-focus';
import type { PdfJsModule } from './pdf-loader';
import { PREVIEW_CHAT_PERCENT, clampPreviewSplit, persistPreviewSplit, readPreviewSplit } from './preview-layout';
import { prefillAndSendChatMessage } from './chat-composer';
import { nudgePreviewRebuild, registerPreviewRebuild } from './preview-rebuild';

const AI_SPARKLE_SVG = '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3l1.9 5.7L19.6 10.6l-5.7 1.9L12 18.2l-1.9-5.7-5.7-1.9 5.7-1.9z"/><path d="M19 15l.9 2.6 2.6.9-2.6.9L19 22l-.9-2.6-2.6-.9 2.6-.9z"/></svg>';

interface PanelOptions {
  host: HTMLElement; api: PaperBuildApi; close(): void; refresh(): void;
  translate?: Translator; loadPdfJs?: () => Promise<PdfJsModule>;
  discover?(): void; select?(document: PaperDocumentV1): void;
  documentsApi?: PaperDocumentsApi;
  importArxiv?(arxiv: string,
    report: (state: 'pending' | 'failed' | 'imported', detail?: string) => void,
    signal: AbortSignal): Promise<PaperArxivImportResultV1 | null>;
  onReading?(documentId: string, state: PaperReadingStateV1): void;

  focusBefore?: HTMLElement | null;
}
export class PaperPreviewPanel {
  readonly element = document.createElement('section');
  readonly reader: PdfReader;
  private readonly scope = createLifecycleScope();
  private readonly tr: Translator;
  private readonly filename: HTMLButtonElement;

  private readonly documents = document.createElement('select');
  private readonly notice = document.createElement('p');
  private readonly compile: HTMLButtonElement;
  private choices: PaperDocumentV1[] = [];
  private overleafServers: string[] = [];
  private readonly status = document.createElement('span');
  private readonly download = document.createElement('a');
  private readonly follow: HTMLButtonElement;
  private readonly reading: HTMLButtonElement;
  private readonly navigation: ReaderNavigation;
  private readonly buildReader: BuildReader;
  private readonly readingFocus: ReadingFocus;
  private readonly search: HTMLButtonElement;
  private readonly details: HTMLButtonElement;
  private readonly tabs = document.createElement('div');
  private readonly divider = document.createElement('div');
  private readonly drawer = document.createElement('div');
  private readonly modeButtons: HTMLButtonElement[] = [];
  readonly focusBefore: HTMLElement | null;
  private detailController: AbortController | null = null;
  private drawerOpener: HTMLElement | null = null;
  private readingMode = false;
  private canCompile = true;
  private selectionBusy = false;
  private result: PaperBuildResultV1 | null = null;
  private failure = '';
  private visibleDocument = '';
  private visibleVerified = false;
  private followEdits = false;
  private split: number = PREVIEW_CHAT_PERCENT.initial;

  constructor(private readonly options: PanelOptions) {
    this.tr = options.translate ?? t;
    this.focusBefore = options.focusBefore !== undefined ? options.focusBefore
      : document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const host = options.host;
    this.element.id = 'paperPreviewPanel';
    this.element.className = 'paper-preview-panel';
    this.element.setAttribute('role', 'complementary');
    this.element.setAttribute('aria-label', this.tr('preview.paper'));
    this.filename = this.button(this.tr('preview.chooseDocument'), this.tr('preview.paper'), () => this.showDocuments());
    this.filename.classList.add('paper-preview-filename');
    this.filename.setAttribute('aria-expanded', 'false');
    this.status.className = 'paper-preview-status'; this.status.setAttribute('role', 'status');
    const close = this.button(this.tr('preview.close'), '', () => options.close(), 'close');
    close.classList.add('paper-preview-close');
    this.documents.setAttribute('aria-label', this.tr('preview.chooseDocument'));
    this.documents.hidden = true;
    this.scope.listen(this.documents, 'change', () => {
      const choice = this.choices[Number(this.documents.value)];
      if (this.documents.value && choice) { options.select?.(choice); this.closeDrawer(); }
    });
    this.compile = this.button(this.tr('preview.compile'), this.tr('preview.compile'), options.refresh, 'refresh');
    this.compile.classList.add('paper-preview-compile');
    this.notice.className = 'paper-preview-notice'; this.notice.hidden = true;
    const toolbar = document.createElement('div'); toolbar.className = 'paper-preview-toolbar';
    this.navigation = new ReaderNavigation(() => this.reader, this.tr, this.scope.signal);
    this.follow = this.button(this.tr('preview.follow'), this.tr('preview.follow'), () => {
      this.followEdits = !this.followEdits; this.follow.setAttribute('aria-pressed', String(this.followEdits));
    });
    this.follow.setAttribute('aria-pressed', 'false');
    this.follow.disabled = true; this.follow.title = this.tr('preview.followUnavailable');
    this.download.className = 'paper-preview-button'; this.download.innerHTML = icon('download');
    this.download.setAttribute('aria-label', this.tr('preview.download'));
    this.download.title = this.tr('preview.download'); this.download.hidden = true;
    this.search = this.button(this.tr('preview.search'), '', () => this.showSearch(), 'search');
    this.reading = this.button(this.tr('preview.reading'), '', () => this.setReading(!this.readingMode), 'reading');
    this.reading.setAttribute('aria-pressed', 'false');
    this.details = this.button(this.tr('preview.details'), '', () => this.showDetails(), 'more');
    toolbar.append(this.filename, this.status, this.compile, this.navigation.element, this.search, this.reading, this.details, close);
    this.drawer.className = 'paper-preview-drawer'; this.drawer.hidden = true;
    this.drawer.id = 'paperPreviewDrawer'; this.drawer.setAttribute('role', 'region');
    for (const opener of [this.filename, this.search, this.details]) {
      opener.setAttribute('aria-controls', this.drawer.id); opener.setAttribute('aria-expanded', 'false');
    }
    const viewport = document.createElement('div'); viewport.className = 'paper-preview-viewport';
    viewport.setAttribute('aria-label', this.tr('preview.paper'));
    this.element.append(toolbar, this.notice, this.drawer, viewport);
    this.readingFocus = new ReadingFocus(this.element, [toolbar, this.notice, this.drawer, viewport], () => this.setReading(false));
    this.tabs.className = 'paper-preview-tabs'; this.tabs.setAttribute('role', 'tablist');
    for (const mode of ['chat', 'paper'] as const) {
      const tab = this.button(this.tr(mode === 'chat' ? 'preview.chat' : 'preview.paper'),
        this.tr(mode === 'chat' ? 'preview.chat' : 'preview.paper'), () => this.setMode(mode));
      tab.setAttribute('role', 'tab'); tab.dataset.mode = mode; this.tabs.append(tab); this.modeButtons.push(tab);
      tab.id = `paperPreviewTab-${mode}`;
      tab.setAttribute('aria-controls', mode === 'paper' ? this.element.id : 'chatContainer');
    }
    this.bindResponsivePanels();
    this.scope.listen(this.tabs, 'keydown', (event) => {
      const key = event as KeyboardEvent;
      if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(key.key)) return;
      key.preventDefault();
      const mode = key.key === 'Home' ? 'chat' : key.key === 'End' ? 'paper'
        : this.options.host.dataset.paperPreviewView === 'chat' ? 'paper' : 'chat';
      this.setMode(mode); this.modeButtons.find((button) => button.dataset.mode === mode)?.focus();
    });
    this.divider.className = 'paper-preview-divider'; this.divider.tabIndex = 0;
    this.divider.setAttribute('role', 'separator'); this.divider.setAttribute('aria-orientation', 'vertical');
    this.divider.setAttribute('aria-label', this.tr('preview.resize'));
    this.divider.setAttribute('aria-valuemin', String(PREVIEW_CHAT_PERCENT.minimum));
    this.divider.setAttribute('aria-valuemax', String(PREVIEW_CHAT_PERCENT.maximum));
    this.split = readPreviewSplit();
    this.setSplit(this.split);
    this.scope.listen(this.divider, 'keydown', (event) => {
      const key = event as KeyboardEvent;
      if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(key.key)) return;
      key.preventDefault(); this.setSplit(key.key === 'Home' ? PREVIEW_CHAT_PERCENT.minimum
        : key.key === 'End' ? PREVIEW_CHAT_PERCENT.maximum : this.split + (key.key === 'ArrowLeft' ? -3 : 3));
      persistPreviewSplit(this.split);
    });
    this.scope.listen(this.divider, 'pointerdown', (event) => this.startDrag(event as PointerEvent));
    this.scope.listen(this.element, 'keydown', (event) => {
      const key = event as KeyboardEvent;
      if (key.key === 'Escape') {
        key.preventDefault(); key.stopPropagation();
        if (!this.drawer.hidden) this.closeDrawer();
        else if (this.readingMode) { this.setReading(false); this.reading.focus(); }
        else options.close();
      } else if ((key.ctrlKey || key.metaKey) && key.key.toLowerCase() === 'f') {
        key.preventDefault(); this.showSearch();
      }
    });
    host.classList.add('paper-preview-open'); host.prepend(this.tabs); host.append(this.divider, this.element);
    // Track-B surfaces (source editor save, proposal accept) recompile through
    // the exact same path as this panel's compile button, debounced upstream.
    this.scope.add(registerPreviewRebuild(() => this.options.refresh()));
    this.setMode('paper');
    this.reader = createPdfReader(viewport, { loadPdfJs: options.loadPdfJs,
      onChange: (state) => this.updateReaderState(state),
      onError: () => this.setStatus(this.tr('preview.renderFailed')) });
    // Inverse SyncTeX: ctrl/cmd+click or double-click a PDF point to open its
    // source line in the drawer's read-only code view.
    const inverseJump = (raw: Event): void => {
      const event = raw as MouseEvent;
      if (event.type === 'click' && !event.ctrlKey && !event.metaKey) return;
      const pageElement = (event.target as HTMLElement).closest?.('.paper-reader-page');
      const receipt = this.result?.receipt;
      if (!pageElement || !receipt || receipt.status !== 'success') return;
      const size = this.reader.pageSize(Number((pageElement as HTMLElement).dataset.page));
      const rect = (pageElement as HTMLElement).getBoundingClientRect();
      if (!size || !rect.width || !rect.height) return;
      event.preventDefault();
      const request = new AbortController();
      this.scope.signal.addEventListener('abort', () => request.abort(), { once: true });
      void this.options.api.inverse(receipt.document_id, receipt.build_id, {
        page: Number((pageElement as HTMLElement).dataset.page),
        h: Math.max(0, (event.clientX - rect.left) / rect.width * size.width),
        v: Math.max(0, (event.clientY - rect.top) / rect.height * size.height),
      }, request.signal).then(result => {
        if (request.signal.aborted) return;
        if (result.file && result.line) this.openSourceAt(result.file, result.line);
        else this.setStatus(this.tr('preview.noSourceMapping'), false, true);
      }).catch(() => {
        if (!request.signal.aborted) this.setStatus(this.tr('preview.noSourceMapping'), false, true);
      });
    };
    this.scope.listen(viewport, 'click', inverseJump);
    this.scope.listen(viewport, 'dblclick', inverseJump);
    this.buildReader = new BuildReader(this.reader, this.options.api, kind => {
      if (kind === 'current') { this.follow.disabled = true; this.download.hidden = true; }
      this.setNotice(this.tr('preview.expired')); this.setStatus(this.tr('preview.expiredStatus'));
    });
    this.updateReaderState(this.reader.state);
  }

  setSelectionBusy(busy: boolean): void {
    this.selectionBusy = busy;
    this.documents.disabled = busy; this.updateCompileControls();
    this.drawer.querySelectorAll<HTMLInputElement | HTMLSelectElement | HTMLButtonElement>(
      '.paper-preview-overleaf-form input, .paper-preview-overleaf-form select, .paper-preview-overleaf-form button')
      .forEach(field => { field.disabled = busy; });
  }
  setDocuments(documents: PaperDocumentV1[], selected: PaperDocumentV1 | null, overleafServers: string[] = []): void {
    this.choices = documents;
    this.overleafServers = overleafServers;
    this.documents.replaceChildren();
    const empty = document.createElement('option'); empty.value = ''; empty.textContent = this.tr('preview.chooseDocument');
    this.documents.append(empty);
    for (const [index, choice] of documents.entries()) {
      const option = document.createElement('option'); option.value = String(index);
      option.textContent = choice.label; this.documents.append(option);
      if (paperDocumentKey(selected) === paperDocumentKey(choice)) this.documents.value = String(index);
    }
    this.documents.hidden = !documents.length;
    this.updateCompileControls();
    if (selected) this.setFilename(selected.label);
  }
  setCompileAvailable(available: boolean): void {
    this.canCompile = available;
    this.updateCompileControls();
  }
  private updateCompileControls(): void {
    this.compile.disabled = !this.canCompile || this.selectionBusy || !this.documents.value;
    const recompile = this.drawer.querySelector<HTMLButtonElement>('.paper-preview-recompile');
    if (recompile) recompile.disabled = this.compile.disabled;
  }
  setNotice(message: string, retry?: { label: string; run(): void }): void {
    this.notice.textContent = message.slice(0, 4096); this.notice.hidden = !message;
    if (retry) this.notice.append(' ', this.button(retry.label, retry.label, retry.run));
  }
  setFilename(name: string): void {
    this.filename.textContent = name.split(/[\\/]/).pop() ?? name;
    this.filename.title = name;
  }
  setStatus(text: string, busy = false, quiet = false): void {
    this.status.textContent = text; this.status.dataset.busy = String(busy);
    this.status.dataset.quiet = String(quiet);
    this.status.title = text;
    this.element.dataset.busy = String(busy);
  }
  setError(message: string, debug?: string): void {
    this.failure = message.slice(0, 2048);
    this.setNotice(this.failure);
    if (debug) {
      const copy = this.button(this.tr('preview.copyDebug'), this.tr('preview.copyDebug'), () => {
        void navigator.clipboard?.writeText(debug).then(() => { copy.textContent = this.tr('preview.copied'); });
      });
      copy.classList.add('paper-preview-copy-debug');
      this.notice.append(' ', copy);
    }
    this.setStatus(this.tr('preview.failed'));
  }
  async applyBuild(result: PaperBuildResultV1, reading?: PaperReadingStateV1 | null): Promise<boolean> {
    this.cancelLoad();
    this.result = result;
    this.failure = '';
    if (result.superseded || result.receipt.status !== 'success' || !result.pdf_url) return false;
    this.setNotice('');
    const source = result.receipt;
    if (this.visibleDocument === source.document_id && this.visibleVerified && !source.version_verified) {
      this.setStatus(this.tr('preview.unverified')); return false;
    }
    const published = await this.buildReader.load({ documentId: source.document_id, buildId: source.build_id, url: result.pdf_url },
      reading ?? undefined, () => { if (reading) this.setReading(reading.reading_mode); });
    if (!published || this.scope.signal.aborted) return false;
    this.visibleDocument = source.document_id;
    this.visibleVerified = source.version_verified;
    this.follow.disabled = !source.version_verified;
    this.follow.title = this.tr(source.version_verified ? 'preview.follow' : 'preview.followUnavailable');
    this.element.dataset.buildId = source.build_id;
    this.element.dataset.documentId = source.document_id;
    this.setFilename(source.main_file);
    this.download.href = this.options.api.resolve(result.pdf_url);
    this.download.download = source.main_file.replace(/\.[^.]+$/, '') + '.pdf'; this.download.hidden = false;
    this.setStatus(this.tr(source.version_verified ? 'preview.updated' : 'preview.unverified'), false, source.version_verified);
    if (!reading && this.followEdits && source.version_verified && result.location) this.reader.highlight(result.location);
    this.reportReading();
    return true;
  }
  readingState(): PaperReadingStateV1 | null {
    const state = this.reader.state;
    if (!this.visibleDocument || !state.sourceId || state.sourceId !== this.element.dataset.buildId) return null;
    return { build_id: state.sourceId, ...this.reader.anchor(), scale: state.scale, fit: state.fit, reading_mode: this.readingMode };
  }
  private reportReading(): void {
    if (this.buildReader?.loading) return;
    const state = this.readingState();
    if (state) this.options.onReading?.(this.visibleDocument, state);
  }
  focus(): void { this.reader.container.focus({ preventScroll: true }); }
  cancelLoad(): void {
    this.buildReader.cancelLoad();
  }
  destroy(restoreFocus = false): void {
    this.readingFocus.leave(false); this.buildReader.close();
    this.detailController?.abort(); this.scope.destroy(); this.reader.destroy();
    this.element.remove(); this.tabs.remove(); this.divider.remove();
    this.options.host.classList.remove('paper-preview-open');
    delete this.options.host.dataset.paperPreviewView;
    delete this.options.host.dataset.paperPreviewReading;
    this.options.host.style.removeProperty('--paper-chat-width');
    if (restoreFocus && this.focusBefore?.isConnected) this.focusBefore.focus({ preventScroll: true });
  }
  private button(label: string, text: string, action: () => void, glyph?: keyof typeof icons): HTMLButtonElement {
    return readerButton(label, text, action, this.scope.signal, glyph);
  }
  private updateReaderState(state: ReaderState): void {
    this.navigation.update(state); this.search.hidden = !state.pages;
    this.element.classList.toggle('paper-preview-has-document', state.pages > 0);
    if (this.reader) this.reportReading();
  }
  private setMode(mode: 'chat' | 'paper'): void {
    if (mode === 'chat') this.reader?.anchor();
    this.options.host.dataset.paperPreviewView = mode;
    for (const tab of this.modeButtons) {
      tab.setAttribute('aria-selected', String(tab.dataset.mode === mode));
      tab.tabIndex = tab.dataset.mode === mode ? 0 : -1;
    }
  }
  private setReading(reading: boolean): void {
    if (reading && !this.readingFocus.enter(this.tr('preview.paper'), this.reading)) return;
    if (!reading) this.readingFocus.leave();
    this.closeDrawer(false); this.readingMode = reading;
    this.options.host.dataset.paperPreviewReading = String(reading);
    this.reading.setAttribute('aria-pressed', String(reading));
    const label = this.tr(reading ? 'preview.exitReading' : 'preview.reading');
    this.reading.setAttribute('aria-label', label); this.reading.title = label;
    if (reading) this.setMode('paper');
    if (this.reader) this.reportReading();
  }
  private bindResponsivePanels(): void {
    const chat = this.options.host.querySelector<HTMLElement>('.chat-container');
    if (!chat) return;
    const original = ['id', 'role', 'aria-labelledby'].map(name => [name, chat.getAttribute(name)] as const);
    if (!chat.id) chat.id = 'paperPreviewChat';
    this.modeButtons[0]!.setAttribute('aria-controls', chat.id);
    const media = typeof matchMedia === 'function' ? matchMedia('(max-width: 800px)') : null;
    const adapt = (): void => {
      this.element.setAttribute('role', media?.matches ? 'tabpanel' : 'complementary');
      if (media?.matches) {
        this.element.setAttribute('aria-labelledby', this.modeButtons[1]!.id);
        chat.setAttribute('role', 'tabpanel'); chat.setAttribute('aria-labelledby', this.modeButtons[0]!.id);
      } else {
        this.element.removeAttribute('aria-labelledby');
        for (const [name, value] of original) if (name !== 'id') {
          if (value === null) chat.removeAttribute(name); else chat.setAttribute(name, value);
        }
      }
    };
    adapt(); if (media) this.scope.listen(media, 'change', adapt);
    this.scope.add(() => {
      for (const [name, value] of original) {
        if (value === null) chat.removeAttribute(name); else chat.setAttribute(name, value);
      }
    });
  }
  private setSplit(value: number): void {
    this.split = clampPreviewSplit(value);
    this.options.host.style.setProperty('--paper-chat-width', this.split + '%');
    this.divider.setAttribute('aria-valuenow', String(Math.round(this.split)));
  }
  private startDrag(event: PointerEvent): void {
    if (event.button !== 0) return;
    event.preventDefault(); this.divider.setPointerCapture(event.pointerId);
    const drag = createLifecycleScope(); const previous = document.body.style.userSelect;
    document.body.style.userSelect = 'none';
    let ended = false;
    const finish = (): void => {
      if (ended) return; ended = true;
      persistPreviewSplit(this.split);
      drag.destroy(); document.body.style.userSelect = previous;
      this.scope.signal.removeEventListener('abort', finish);
      if (this.divider.hasPointerCapture(event.pointerId)) this.divider.releasePointerCapture(event.pointerId);
    };
    drag.listen(this.divider, 'pointermove', (move) => {
      const rect = this.options.host.getBoundingClientRect();
      if (rect.width) this.setSplit(((move as PointerEvent).clientX - rect.left) / rect.width * 100);
    });
    drag.listen(this.divider, 'pointerup', finish); drag.listen(this.divider, 'pointercancel', finish);
    drag.listen(this.divider, 'lostpointercapture', finish); this.scope.signal.addEventListener('abort', finish, { once: true });
  }
  private closeDrawer(restoreFocus = true): void {
    this.detailController?.abort(); this.detailController = null;
    this.drawer.hidden = true;
    this.drawerOpener?.setAttribute('aria-expanded', 'false');
    if (restoreFocus && this.drawerOpener?.isConnected) this.drawerOpener.focus({ preventScroll: true });
    this.drawerOpener = null;
  }
  private beginDrawer(label: string, opener: HTMLElement): AbortSignal {
    this.closeDrawer(false);
    this.detailController = new AbortController(); this.drawerOpener = opener;
    this.drawerOpener.setAttribute('aria-expanded', 'true');
    this.drawer.replaceChildren(); this.drawer.hidden = false;
    this.drawer.setAttribute('aria-label', label);
    const close = this.button(this.tr('common.close'), '×', () => this.closeDrawer());
    close.classList.add('paper-preview-drawer-close'); this.drawer.append(close);
    return this.detailController.signal;
  }
  private showDocuments(): void {
    this.beginDrawer(this.tr('preview.chooseDocument'), this.filename);
    const sources = document.createElement('div'); sources.className = 'paper-preview-documents';
    sources.append(this.documents);
    if (this.options.discover) sources.append(this.button(this.tr('preview.discover'), this.tr('preview.discover'), this.options.discover));
    sources.append(this.button(this.tr('preview.overleaf'), this.tr('preview.overleaf'), () => {
      this.beginDrawer(this.tr('preview.overleaf'), this.filename);
      this.drawer.append(createOverleafSourceForm(this.overleafServers, this.tr, value => this.options.select?.(value)));
      this.drawer.querySelector<HTMLInputElement>('input')?.focus();
    }));
    if (this.options.importArxiv) {
      sources.append(this.button(this.tr('preview.arxivImport'), this.tr('preview.arxivImport'), () => {
        const signal = this.beginDrawer(this.tr('preview.arxivImport'), this.filename);
        const submit = this.options.importArxiv!;
        this.drawer.append(createArxivImportForm(this.tr,
          (arxiv, report) => submit(arxiv, report, signal),
          result => {
            if (result.document) this.options.select?.(result.document);
            else this.options.discover?.();
          }));
        this.drawer.querySelector<HTMLInputElement>('input')?.focus();
      }));
    }
    this.drawer.append(sources);
    (this.documents.hidden ? sources.querySelector<HTMLElement>('button') : this.documents)?.focus();
  }
  private showSearch(): void {
    const signal = this.beginDrawer(this.tr('preview.search'), this.search);
    showReaderSearch(this.reader, this.drawer, this.tr, signal);
  }
  private sourceContextFor(body: HTMLElement, signal: AbortSignal): SourceViewContext {
    const receipt = this.result!.receipt;
    const context: SourceViewContext = {
      api: this.options.api,
      documentsApi: this.options.documentsApi!,
      documentId: receipt.document_id,
      buildId: receipt.build_id,
      tr: this.tr,
      showTree: () => { void showSourceTree(body, context, signal); },
      showFile: (path, line) => { void showSourceFile(body, context, path, line, signal); },
      onSourceSaved: () => nudgePreviewRebuild(),
      submitChat: (message) => { prefillAndSendChatMessage(message); },
    };
    return context;
  }
  private openSourceAt(path: string, line?: number): void {
    if (!this.options.documentsApi || !this.result?.receipt) return;
    const signal = this.beginDrawer(this.tr('preview.sources'), this.details);
    const body = document.createElement('div');
    body.className = 'paper-preview-source-body';
    this.drawer.append(body);
    void showSourceFile(body, this.sourceContextFor(body, signal), path, line, signal);
  }
  private showSources(content: HTMLElement, signal: AbortSignal): void {
    const receipt = this.result?.receipt;
    if (!receipt || receipt.status !== 'success' || !this.options.documentsApi) {
      content.textContent = this.tr('preview.sourceEmpty');
      return;
    }
    const body = document.createElement('div');
    content.replaceChildren(body);
    void showSourceTree(body, this.sourceContextFor(body, signal), signal);
  }
  private showLayout(content: HTMLElement, signal: AbortSignal): void {
    const receipt = this.result?.receipt;
    if (!receipt || receipt.status !== 'success') {
      content.textContent = this.tr('preview.layoutEmpty');
      return;
    }
    content.textContent = this.tr('common.loading');
    void this.options.api.layout(receipt.document_id, receipt.build_id, signal)
      .then(report => {
        if (signal.aborted) return;
        const wrap = document.createElement('div');
        wrap.className = 'paper-preview-layout';
        if (!report.pages) { wrap.textContent = this.tr('preview.layoutEmpty'); content.replaceChildren(wrap); return; }
        const head = document.createElement('p');
        head.className = 'paper-preview-layout-head';
        head.textContent = this.tr('preview.layoutPages', { n: String(report.pages) })
          + (report.end?.fullness_page != null
            ? ' · ' + this.tr('preview.layoutFullness', {
                p: String(report.end.page), f: String(Math.round(report.end.fullness_page * 100)) })
            : '');
        wrap.append(head);
        if (report.sections.length) {
          const map = document.createElement('div');
          map.className = 'paper-preview-layout-map';
          for (const row of report.sections) {
            const jumpable = Boolean(row.file) && Boolean(this.options.documentsApi);
            const line = document.createElement(jumpable ? 'button' : 'div');
            line.className = 'paper-preview-layout-row';
            const page = document.createElement('span'); page.className = 'paper-preview-layout-page';
            page.textContent = `p.${row.page}`;
            const title = document.createElement('span'); title.className = 'paper-preview-layout-title';
            title.textContent = `${row.title || row.level}`;
            const where = document.createElement('span'); where.className = 'paper-preview-layout-where';
            where.textContent = `${row.file}:${row.line}`;
            line.append(page, title, where);
            if (jumpable) (line as HTMLButtonElement).onclick = () => this.openSourceAt(row.file, row.line);
            map.append(line);
          }
          wrap.append(map);
        }
        content.replaceChildren(wrap);
      })
      .catch(() => { if (!signal.aborted) content.textContent = this.tr('preview.layoutEmpty'); });
  }
  private sendErrorAssist(row: { severity: string; code: string; message: string; file: string; line: number | null }): void {
    const detail = row.code ? `${row.message} [${row.code}]` : row.message;
    prefillAndSendChatMessage(this.tr('paperEdit.errorAssistMessage', {
      message: detail,
      file: row.file || '?',
      line: String(row.line ?? '?'),
    }));
  }
  private showProblems(content: HTMLElement): void {
    const rows = this.result?.receipt?.diagnostics ?? [];
    const wrap = document.createElement('div');
    wrap.className = 'paper-preview-problems';
    if (!rows.length) {
      wrap.textContent = this.tr('preview.problemsEmpty');
      content.replaceChildren(wrap);
      return;
    }
    const sorted = [...rows].sort((a, b) => (a.severity === b.severity ? 0
      : a.severity === 'error' ? -1 : 1));
    for (const row of sorted.slice(0, 64)) {
      const jumpable = Boolean(row.file) && Boolean(this.options.documentsApi);
      const item = document.createElement(jumpable ? 'button' : 'div');
      item.className = `paper-preview-problem paper-preview-problem-${row.severity}`;
      const where = row.file ? `${row.file}${row.line ? ':' + row.line : ''} ` : '';
      item.textContent = `${row.severity === 'error' ? '✗' : '⚠'} ${where}${row.message}`;
      item.title = jumpable ? this.tr('preview.jumpToSource') : row.message;
      if (jumpable) (item as HTMLButtonElement).onclick = () => this.openSourceAt(row.file, row.line ?? undefined);
      if (row.severity === 'error') {
        // Error Assist: hand the diagnostic to the agent for a located fix +
        // paper_build re-verification. The row becomes a flex pair so the
        // nested button never sits inside the jump button.
        const pair = document.createElement('div');
        pair.className = 'paper-preview-problem-row';
        const assist = document.createElement('button');
        assist.type = 'button';
        assist.className = 'paper-preview-problem-assist';
        assist.innerHTML = AI_SPARKLE_SVG;
        assist.append(this.tr('paperEdit.errorAssist'));
        assist.title = this.tr('paperEdit.errorAssist');
        assist.setAttribute('aria-label', this.tr('paperEdit.errorAssist'));
        assist.onclick = () => this.sendErrorAssist(row);
        pair.append(item, assist);
        wrap.append(pair);
      } else {
        wrap.append(item);
      }
    }
    content.replaceChildren(wrap);
  }
  private showDetails(): void {
    const signal = this.beginDrawer(this.tr('preview.details'), this.details);
    let request: AbortController | null = null;
    const nextRequest = (): AbortSignal => {
      request?.abort(); request = new AbortController();
      const current = request;
      signal.addEventListener('abort', () => current.abort(), { once: true, signal: current.signal });
      return current.signal;
    };
    const controls = document.createElement('div'); const content = document.createElement('div');
    const recompile = this.button(this.tr('preview.refresh'), this.tr('preview.refresh'), this.options.refresh, 'refresh');
    recompile.classList.add('paper-preview-recompile'); recompile.disabled = this.compile.disabled;
    controls.className = 'paper-preview-detail-controls';
    controls.append(this.button(this.tr('preview.outline'), this.tr('preview.outline'), () => {
      const pending = nextRequest();
      void showReaderOutline(this.reader, content, this.tr, pending);
    }), this.button(this.tr('preview.problems'), this.tr('preview.problems'), () => {
      nextRequest();
      this.showProblems(content);
    }), this.button(this.tr('preview.layout'), this.tr('preview.layout'), () => {
      const pending = nextRequest();
      this.showLayout(content, pending);
    }), this.button(this.tr('preview.sources'), this.tr('preview.sources'), () => {
      const pending = nextRequest();
      this.showSources(content, pending);
    }), this.button(this.tr('preview.log'), this.tr('preview.log'), () => {
      const pending = nextRequest();
      if (this.failure) { content.textContent = this.failure; return; }
      const receipt = this.result?.receipt;
      if (!receipt) { content.textContent = this.tr('preview.logUnavailable'); return; }
      content.textContent = this.tr('common.loading');
      void this.options.api.log(receipt.document_id, receipt.build_id, pending).then((log) => {
        if (pending.aborted) return;
        const pre = document.createElement('pre'); pre.textContent = log.slice(0, 32768); content.replaceChildren(pre);
        if (log.length > pre.textContent.length) {
          const notice = document.createElement('p'); notice.textContent = this.tr('preview.logTruncated');
          const full = document.createElement('a'); full.textContent = this.tr('preview.fullLog');
          full.href = this.options.api.resolve(paperBuildArtifactPath(receipt.document_id, receipt.build_id, 'build.log'));
          full.download = 'build.log'; content.prepend(notice, full);
        }
      }).catch(() => { if (!pending.aborted) content.textContent = this.tr('preview.logUnavailable'); });
    }), recompile);
    const actions = document.createElement('div'); actions.className = 'paper-preview-detail-controls';
    this.download.textContent = this.tr('preview.download');
    actions.append(this.follow, this.download,
      this.button(this.tr('paper.fitWidth'), this.tr('preview.fit'), () => this.reader.setScale('fit')),
      this.button(this.tr('preview.chooseDocument'), this.tr('preview.chooseDocument'), () => this.showDocuments()));
    for (const side of ['left', 'right'] as const) {
      const label = this.tr(side === 'left' ? 'preview.leftColumn' : 'preview.rightColumn');
      const button = this.button(label, label, () => {
        this.closeDrawer(false); this.reader.container.focus({ preventScroll: true });
        void this.reader.focusColumn(side).catch(() => this.setStatus(this.tr('preview.renderFailed')));
      });
      button.disabled = !this.reader.state.pages; actions.append(button);
    }
    actions.append(this.button(this.tr('preview.resetLayout'), this.tr('preview.resetLayout'), () => {
      this.setSplit(PREVIEW_CHAT_PERCENT.initial); persistPreviewSplit(this.split); this.closeDrawer();
    }));
    const close = this.button(this.tr('preview.close'), this.tr('preview.close'), this.options.close);
    close.classList.add('paper-preview-menu-close'); actions.append(close);
    this.drawer.append(controls, actions, content);
    controls.querySelector<HTMLButtonElement>('button')?.focus();
  }
}
