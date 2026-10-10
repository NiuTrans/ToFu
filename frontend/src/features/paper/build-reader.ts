/** Build-bound PDF loading. One visible lease and one replacement lease share the
 * same lifecycle for build preview adapters; PdfReader owns rendering/atomic swaps.
 */
import type { PaperBuildApi } from '../../api/paper-build';
import { BuildViewerLease } from './build-viewer-lease';
import type { PdfReader, ReaderSource } from './pdf-reader';

export interface BuildReaderSource { documentId: string; buildId: string; url: string }
export class BuildReader {
  private current: { source: BuildReaderSource; viewer: BuildViewerLease } | null = null;
  private pending: BuildViewerLease | null = null;
  private closed = false;

  constructor(private readonly reader: PdfReader, private readonly api: PaperBuildApi,
    private readonly lost: (kind: 'current' | 'pending') => void) {}

  get loading(): boolean { return this.pending !== null; }
  get source(): BuildReaderSource | null { return this.current?.source ?? null; }

  async load(source: BuildReaderSource, initialView?: ReaderSource['initialView'], beforeLoad?: () => void): Promise<boolean> {
    this.cancelLoad();
    if (this.closed) return false;
    const viewer = new BuildViewerLease(this.api, source.documentId, source.buildId, () => {
      if (this.pending === viewer) { this.reader.cancelLoad(); this.lost('pending'); }
      else if (this.current?.viewer === viewer) this.lost('current');
    });
    this.pending = viewer;
    let published = false;
    try {
      if (!await viewer.retain() || this.closed || this.pending !== viewer) return false;
      beforeLoad?.();
      const loaded = await this.reader.load({ id: source.buildId, url: this.api.resolve(source.url),
        loadBytes: signal => this.api.bytes(source.url, signal), ...(initialView ? { initialView } : {}) },
      this.current?.source.documentId === source.documentId);
      if (!loaded || this.closed || this.pending !== viewer) return false;
      this.current?.viewer.close(); this.current = { source, viewer }; published = true;
      return true;
    } finally {
      if (this.pending === viewer) this.pending = null;
      if (!published) viewer.close();
    }
  }

  cancelLoad(): void {
    this.pending?.close(); this.pending = null; this.reader.cancelLoad();
  }
  close(): void {
    this.closed = true; this.cancelLoad(); this.current?.viewer.close(); this.current = null;
  }
}
