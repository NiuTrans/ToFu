/** Research manuscript presentation. The workspace owns publication/CAS; this
 * instance owns one shared PDF reader, build leases, controls and focus. It never
 * compiles or subscribes to research tasks. Destroy it when its page is hidden.
 */
import { createLifecycleScope } from '../../../../lifecycle';
import { t, type Translator } from '../../../../i18n';
import { paperBuildApi, paperBuildArtifactPath, type PaperBuildApi } from '../../../../api/paper-build';
import { createPdfReader, type PdfReader, type ReaderSource } from '../../../paper/pdf-reader';
import type { PdfJsModule } from '../../../paper/pdf-loader';
import { BuildReader, type BuildReaderSource } from '../../../paper/build-reader';
import { ReadingFocus } from '../../../paper/reading-focus';
import { ReaderNavigation, readerButton, readerIcon, showReaderSearch, showReaderOutline } from '../../../paper/reader-controls';

export interface ManuscriptBuild { document_id: string; build_id: string }
export interface ManuscriptView {
  documentId: string; buildId: string; reading: boolean;
  position: NonNullable<ReaderSource['initialView']>;
}
interface Options {
  api?: PaperBuildApi; translate?: Translator; loadPdfJs?: () => Promise<PdfJsModule>;
  onReading?: (view: ManuscriptView) => void;
}
type ControlLabel = 'preview.search' | 'preview.reading' | 'preview.details' | 'paper.retry'
  | 'common.close' | 'paper.fitWidth' | 'preview.outline' | 'preview.leftColumn' | 'preview.rightColumn';

export class ManuscriptPreview {
  readonly element = document.createElement('div');
  readonly reader: PdfReader;
  private readonly scope = createLifecycleScope();
  private readonly tr: Translator;
  private readonly api: PaperBuildApi;
  private readonly builds: BuildReader;
  private readonly navigation: ReaderNavigation;
  private readonly focus: ReadingFocus;
  private readonly notice = document.createElement('p');
  private readonly drawer = document.createElement('div');
  private readonly search: HTMLButtonElement;
  private readonly more: HTMLButtonElement;
  private readonly reading: HTMLButtonElement;
  private readonly download = document.createElement('a');
  private drawerScope: AbortController | null = null;
  private opener: HTMLElement | null = null;
  private requested: BuildReaderSource | null = null;
  private generation = 0;
  private readingMode = false;
  private initialView: ManuscriptView | null = null;
  private loadMessage: 'preview.loading' | 'preview.readerUnavailable' | 'preview.readingExpired' | null = 'preview.loading';
  private readingProblem: { retry: () => void; unavailable: boolean } | null = null;
  private readonly onReading?: Options['onReading'];

  constructor(readonly host: HTMLElement, options: Options = {}) {
    this.tr = options.translate ?? t; this.api = options.api ?? paperBuildApi;
    this.onReading = options.onReading;
    this.element.id = 'researchManuscriptReader';
    this.element.className = 'paper-preview-panel rsw-manuscript-reader';
    this.element.setAttribute('role', 'region'); this.element.setAttribute('aria-label', this.tr('preview.paper'));
    const toolbar = document.createElement('div'); toolbar.className = 'paper-preview-toolbar';
    const filename = document.createElement('span'); filename.className = 'paper-preview-filename';
    filename.textContent = 'manuscript.pdf';
    this.navigation = new ReaderNavigation(() => this.reader, this.tr, this.scope.signal);
    this.search = this.button('preview.search', () => this.showSearch(), 'search');
    this.reading = this.button('preview.reading', () => this.setReading(!this.readingMode), 'reading');
    this.reading.setAttribute('aria-pressed', 'false');
    this.more = this.button('preview.details', () => this.showMore(), 'more');
    toolbar.append(filename, this.navigation.element, this.search, this.reading, this.more);
    this.notice.className = 'paper-preview-notice'; this.notice.textContent = this.tr('preview.loading');
    this.notice.setAttribute('role', 'status');
    this.drawer.id = 'researchManuscriptDrawer'; this.drawer.className = 'paper-preview-drawer'; this.drawer.hidden = true;
    this.drawer.setAttribute('role', 'region');
    for (const button of [this.search, this.more]) {
      button.setAttribute('aria-controls', this.drawer.id); button.setAttribute('aria-expanded', 'false');
    }
    this.download.className = 'paper-preview-button'; this.download.download = 'manuscript.pdf';
    this.download.innerHTML = readerIcon('download'); this.download.append(this.tr('preview.download'));
    this.download.hidden = true;
    const viewport = document.createElement('div'); viewport.className = 'paper-preview-viewport';
    this.element.append(toolbar, this.notice, this.drawer, viewport); host.append(this.element);
    this.focus = new ReadingFocus(this.element, [toolbar, this.notice, this.drawer, viewport], () => this.setReading(false));
    this.reader = createPdfReader(viewport, { loadPdfJs: options.loadPdfJs,
      onChange: state => {
        this.navigation.update(state); this.search.hidden = !state.pages;
        this.element.classList.toggle('paper-preview-has-document', state.pages > 0);
        this.reportReading();
      }, onError: () => this.failure('preview.readerUnavailable') });
    this.builds = new BuildReader(this.reader, this.api, kind => {
      if (kind === 'current') this.download.hidden = true;
      this.failure('preview.readingExpired');
    });
    this.scope.listen(this.element, 'keydown', event => {
      const key = event as KeyboardEvent;
      if (key.key === 'Escape' && (!this.drawer.hidden || this.readingMode)) {
        key.preventDefault(); key.stopPropagation();
        if (!this.drawer.hidden) this.closeDrawer(); else this.setReading(false);
      } else if ((key.ctrlKey || key.metaKey) && key.key.toLowerCase() === 'f') {
        key.preventDefault(); this.showSearch();
      }
    });
  }

  show(build: ManuscriptBuild, saved?: ManuscriptView | null): void {
    if (this.requested?.documentId === build.document_id && this.requested.buildId === build.build_id) return;
    this.requested = { documentId: build.document_id, buildId: build.build_id,
      url: paperBuildArtifactPath(build.document_id, build.build_id, 'document.pdf') };
    this.initialView = this.builds.source?.documentId !== build.document_id
      && saved?.documentId === build.document_id ? saved : null;
    void this.load();
  }

  view(): ManuscriptView | null {
    const source = this.builds.source; const state = this.reader.state;
    if (!source || source.buildId !== state.sourceId) return null;
    return { documentId: source.documentId, buildId: source.buildId, reading: this.readingMode,
      position: { ...this.reader.anchor(), scale: state.scale, fit: state.fit } };
  }

  destroy(): void {
    this.generation++; this.focus.leave(false); this.drawerScope?.abort();
    this.scope.destroy(); this.builds.close(); this.reader.destroy(); this.element.remove();
  }

  setReadingProblem(retry: (() => void) | null, unavailable = false): void {
    this.readingProblem = retry ? { retry, unavailable } : null; this.renderNotice();
  }

  private reportReading(): void {
    if (!this.builds || this.builds.loading || this.scope.signal.aborted) return;
    const view = this.view(); if (view) this.onReading?.(view);
  }

  private async load(): Promise<void> {
    const source = this.requested; if (!source || this.scope.signal.aborted) return;
    const saved = this.initialView;
    const generation = ++this.generation;
    this.loadMessage = 'preview.loading'; this.renderNotice();
    try {
      const loaded = await this.builds.load(source, saved?.position,
        () => { if (saved) this.setReading(saved.reading); });
      if (!loaded || generation !== this.generation || this.scope.signal.aborted) return;
      this.element.dataset.buildId = source.buildId; this.element.dataset.documentId = source.documentId;
      this.download.href = this.api.resolve(source.url); this.download.hidden = false;
      this.initialView = null; this.closeDrawer(false); this.loadMessage = null; this.renderNotice(); this.reportReading();
    } catch { if (generation === this.generation && !this.scope.signal.aborted) this.failure('preview.readerUnavailable'); }
  }

  private failure(key: 'preview.readerUnavailable' | 'preview.readingExpired'): void {
    if (this.scope.signal.aborted) return;
    this.loadMessage = key; this.renderNotice();
  }

  private renderNotice(): void {
    const problem = this.readingProblem;
    const key = this.loadMessage ?? (problem ? problem.unavailable ? 'preview.readingUnavailable' : 'preview.readingSaveFailed' : null);
    this.notice.hidden = !key; this.notice.textContent = key ? this.tr(key) : '';
    if (this.loadMessage && this.loadMessage !== 'preview.loading') {
      this.notice.append(' ', this.button('paper.retry', () => { void this.load(); }));
    } else if (!this.loadMessage && problem) {
      this.notice.append(' ', this.button('paper.retry', problem.retry));
    }
  }

  private button(key: ControlLabel, action: () => void, glyph?: Parameters<typeof readerButton>[4]): HTMLButtonElement {
    return readerButton(this.tr(key), this.tr(key), action, this.scope.signal, glyph);
  }

  private setReading(reading: boolean): void {
    if (reading && !this.focus.enter(this.tr('preview.paper'), this.reading)) return;
    if (!reading) this.focus.leave();
    this.readingMode = reading; this.closeDrawer(false);
    const label = this.tr(reading ? 'preview.exitReading' : 'preview.reading');
    this.reading.title = label; this.reading.setAttribute('aria-label', label);
    this.reading.setAttribute('aria-pressed', String(reading));
    this.reportReading();
  }

  private closeDrawer(restoreFocus = true): void {
    this.drawerScope?.abort(); this.drawerScope = null; this.drawer.hidden = true; this.drawer.replaceChildren();
    for (const button of [this.search, this.more]) button.setAttribute('aria-expanded', 'false');
    if (restoreFocus && this.opener?.isConnected) this.opener.focus({ preventScroll: true });
    this.opener = null;
  }

  private openDrawer(opener: HTMLButtonElement): { body: HTMLElement; signal: AbortSignal } {
    this.closeDrawer(false); this.opener = opener; this.drawerScope = new AbortController();
    this.drawer.hidden = false; opener.setAttribute('aria-expanded', 'true');
    const close = this.button('common.close', () => this.closeDrawer(), 'close');
    close.classList.add('paper-preview-drawer-close');
    const body = document.createElement('div'); this.drawer.append(close, body);
    close.focus({ preventScroll: true });
    return { body, signal: this.drawerScope.signal };
  }

  private showSearch(): void {
    const { body, signal } = this.openDrawer(this.search);
    showReaderSearch(this.reader, body, this.tr, signal);
  }

  private showMore(): void {
    const { body, signal } = this.openDrawer(this.more);
    const controls = document.createElement('div'); controls.className = 'paper-preview-detail-controls';
    const content = document.createElement('div');
    controls.append(this.download,
      this.button('paper.fitWidth', () => this.reader.setScale('fit'), 'fit'),
      this.button('preview.outline', () => { void showReaderOutline(this.reader, content, this.tr, signal); }),
      this.button('preview.leftColumn', () => this.focusColumn('left')),
      this.button('preview.rightColumn', () => this.focusColumn('right')));
    body.append(controls, content);
  }

  private focusColumn(side: 'left' | 'right'): void {
    this.closeDrawer(false); this.reader.container.focus({ preventScroll: true });
    void this.reader.focusColumn(side).catch(() => this.failure('preview.readerUnavailable'));
  }
}
