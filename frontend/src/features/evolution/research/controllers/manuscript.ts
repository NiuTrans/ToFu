/** Research reading lifecycle. Publication comes from the workspace; reading
 * revisions use PaperDocumentsApi/SavedReading. One reader and metadata request
 * live while visible. Tab changes release PDF leases and keep one view anchor.
 */
import { paperDocumentsApi, type PaperDocumentsApi } from '../../../../api/paper-documents';
import type { PaperReadingStateV1 } from '../../../../api/paper-build.generated';
import { createLifecycleScope } from '../../../../lifecycle';
import { SavedReading } from '../../../paper/saved-reading';
import { ManuscriptPreview, type ManuscriptBuild, type ManuscriptView } from '../components/manuscript-preview';

interface Context { direction: string; lang: 'en' | 'zh' }
type Preview = Pick<ManuscriptPreview, 'host' | 'show' | 'view' | 'destroy' | 'setReadingProblem'>;
interface Options {
  api?: Pick<PaperDocumentsApi, 'openResearch' | 'saveReading'>;
  preview?: (host: HTMLElement, onReading: (view: ManuscriptView) => void) => Preview;
}
function savedView(documentId: string, state: PaperReadingStateV1 | null): ManuscriptView | null {
  if (!state) return null;
  return { documentId, buildId: state.build_id, reading: state.reading_mode,
    position: { page: state.page, fraction: state.fraction, horizontal: state.horizontal, scale: state.scale, fit: state.fit } };
}

export class ResearchManuscript {
  private readonly scope = createLifecycleScope();
  private readonly api: NonNullable<Options['api']>;
  private readonly makePreview: NonNullable<Options['preview']>;
  private readonly saved: SavedReading;
  private preview: Preview | null = null;
  private pending: AbortController | null = null;
  private context: Context | null = null;
  private build: ManuscriptBuild | null = null;
  private view: ManuscriptView | null = null;
  private host: HTMLElement | null = null;
  private key = '';
  private opened = false;
  private problem: 'open' | 'save' | null = null;
  private pageHidden = false;

  constructor(options: Options = {}) {
    this.api = options.api ?? paperDocumentsApi;
    this.makePreview = options.preview ?? ((host, onReading) => new ManuscriptPreview(host, { onReading }));
    this.saved = new SavedReading(this.api, id => {
      if (id === this.build?.document_id && !this.scope.signal.aborted) {
        this.problem = 'save'; this.updateProblem();
      }
    });
    this.scope.listen(document, 'visibilitychange', () => {
      if (document.visibilityState === 'hidden') this.flush();
    });
    this.scope.listen(window, 'pagehide', () => { this.pageHidden = true; this.release(); });
    this.scope.listen(window, 'pageshow', () => { this.pageHidden = false; this.render(); });
  }

  show(host: HTMLElement | null, context: Context, build: ManuscriptBuild | null): void {
    if (this.scope.signal.aborted) return;
    const key = build ? JSON.stringify([context.direction, context.lang, build.document_id]) : '';
    if (key !== this.key) {
      this.release(); this.key = key; this.view = null; this.opened = false; this.problem = null;
    }
    if (host !== this.host) this.release();
    this.host = host; this.context = context; this.build = build; this.render();
  }

  hide(): void { this.release(); this.host = null; }

  destroy(): void {
    this.release(); this.saved.close(this.build?.document_id); this.scope.destroy();
    this.host = null; this.context = null; this.build = null; this.view = null;
  }

  private capture(view = this.preview?.view()): void {
    if (!view || view.documentId !== this.build?.document_id) return;
    this.view = view;
    this.saved.capture(view.documentId, { build_id: view.buildId, ...view.position, reading_mode: view.reading });
  }
  private flush(): void { this.capture(); this.saved.flush(this.build?.document_id, true); }
  private release(): void {
    this.flush(); this.pending?.abort(); this.pending = null;
    this.preview?.destroy(); this.preview = null;
  }
  private render(): void {
    if (!this.host || !this.build || !this.context || this.pageHidden || this.scope.signal.aborted) return;
    if (!this.preview) this.preview = this.makePreview(this.host, view => this.capture(view));
    if (!this.opened && !this.problem) { if (!this.pending) void this.open(); return; }
    this.updateProblem(); this.preview.show(this.build, this.view);
  }
  private updateProblem(): void {
    this.preview?.setReadingProblem(this.problem ? () => { if (!this.pending) void this.open(); } : null, this.problem === 'open');
  }
  private async open(): Promise<void> {
    if (!this.context || !this.build || this.scope.signal.aborted) return;
    const pending = new AbortController(); this.pending = pending;
    const documentId = this.build.document_id;
    try {
      const result = await this.api.openResearch({ ...this.context, document_id: documentId }, pending.signal);
      if (pending.signal.aborted || this.pending !== pending || this.scope.signal.aborted) return;
      const current = this.preview?.view() ?? this.view;
      this.view = current ?? savedView(documentId, this.saved.open(result.document));
      // A retry refreshes the writer revision without moving an already visible PDF.
      if (current) { this.saved.open(result.document); this.capture(current); }
      this.opened = true; this.problem = null;
    } catch {
      if (pending.signal.aborted || this.pending !== pending || this.scope.signal.aborted) return;
      this.problem = 'open';
    } finally {
      if (this.pending === pending) { this.pending = null; this.render(); }
    }
  }
}
