/** Request-owned reading-state writes. Per-document normal and final keepalive
 * request share writer sequences; the server fences other windows and reorders
 * late saves. Dirty state coalesces per document, never per scroll event.
 */
import type { PaperDocumentsApi } from '../../api/paper-documents';
import { PAPER_BUILD_LIMITS as limits, type PaperReadingRecordV1,
  type PaperReadingStateV1, type PaperReadingStateRequestV1 } from '../../api/paper-build.generated';

interface Entry {
  id: string; revision: number; writer: string; sequence: number; acknowledged: number;
  state: PaperReadingStateV1 | null; pending: boolean; blocked: boolean; closed: boolean;
}
interface Write { entry: Entry; body: PaperReadingStateRequestV1 }
const fields: ReadonlyArray<keyof PaperReadingStateV1> = ['build_id', 'page', 'fraction', 'horizontal', 'scale', 'fit', 'reading_mode'];
const equal = (a: PaperReadingStateV1 | null, b: PaperReadingStateV1 | null): boolean => a === b
  || !!a && !!b && fields.every(key => a[key] === b[key]);

export class SavedReading {
  private readonly entries = new Map<string, Entry>();
  private readonly ordinary = new Map<string, Write>();
  private readonly final = new Map<string, Write>();
  private timer: ReturnType<typeof setTimeout> | null = null;
  private deadline: ReturnType<typeof setTimeout> | null = null;
  private closing = false;
  private stopped = false;

  constructor(private readonly api: Pick<PaperDocumentsApi, 'saveReading'>,
    private readonly onError: (documentId: string, error: unknown) => void) {}

  open(record: PaperReadingRecordV1): PaperReadingStateV1 | null {
    if (this.closing || this.stopped) return record.reading;
    const existing = this.entries.get(record.document_id);
    if (existing) existing.closed = false;
    if (existing && !existing.blocked && (existing.pending
      || this.ordinary.get(existing.id)?.entry === existing || this.final.get(existing.id)?.entry === existing)) return existing.state;
    this.entries.delete(record.document_id);
    this.entries.set(record.document_id, { id: record.document_id, revision: record.reading_revision,
      writer: crypto.randomUUID().replaceAll('-', ''), sequence: 0, acknowledged: 0,
      state: record.reading, pending: false, blocked: false, closed: false });
    return record.reading;
  }

  capture(documentId: string, state: PaperReadingStateV1): void {
    const entry = this.entries.get(documentId);
    if (!entry || entry.closed || entry.blocked || this.closing || equal(entry.state, state)) return;
    entry.state = { ...state }; entry.sequence++; entry.pending = true;
    if (this.timer === null) this.timer = setTimeout(() => {
      this.timer = null; this.drain();
    }, limits.reading_save_delay_ms);
  }

  flush(documentId?: string, final = false): void {
    if (this.timer !== null) clearTimeout(this.timer); this.timer = null;
    const entry = documentId ? this.entries.get(documentId) : undefined;
    if (final && entry?.pending && this.ordinary.has(entry.id) && !this.final.has(entry.id) && !entry.blocked) this.send(entry, true);
    this.drain();
  }

  release(documentId: string): void {
    const entry = this.entries.get(documentId);
    if (!entry) return;
    this.flush(documentId, true);
    entry.closed = true;
    this.finish();
  }

  close(documentId?: string): void {
    if (this.closing) return;
    this.closing = true;
    this.deadline = setTimeout(() => this.stop(), limits.reading_save_timeout_ms);
    this.flush(documentId, true);
    this.finish();
  }

  private drain(): void {
    if (this.stopped) return;
    for (const entry of this.entries.values()) {
      if (entry.pending && !entry.blocked && !this.ordinary.has(entry.id) && !this.final.has(entry.id)) this.send(entry, false);
    }
    this.finish();
  }

  private send(entry: Entry, final: boolean): void {
    if (this.stopped || !entry.state) return;
    const write: Write = { entry, body: { expected_revision: entry.revision,
      writer_id: entry.writer, sequence: entry.sequence, state: { ...entry.state } } };
    entry.pending = false;
    const writes = final ? this.final : this.ordinary;
    writes.set(entry.id, write);
    let response: ReturnType<PaperDocumentsApi['saveReading']>;
    try { response = this.api.saveReading(entry.id, write.body); }
    catch (error) { response = Promise.reject(error); }
    void response.then(() => {
      if (this.entries.get(entry.id) === entry) entry.acknowledged = Math.max(entry.acknowledged, write.body.sequence);
    }).catch(error => {
      if (this.stopped || this.entries.get(entry.id) !== entry || write.body.sequence < entry.acknowledged) return;
      entry.blocked = true; entry.pending = false; this.onError(entry.id, error);
    }).finally(() => {
      if (writes.get(entry.id) === write) writes.delete(entry.id);
      this.drain(); this.finish();
    });
  }

  private finish(): void {
    for (const [id, entry] of this.entries) {
      if (entry.closed && !entry.pending && !this.ordinary.has(id) && !this.final.has(id)) this.entries.delete(id);
    }
    if (this.closing && !this.ordinary.size && !this.final.size && ![...this.entries.values()].some(entry => entry.pending)) this.stop();
  }
  private stop(): void {
    this.stopped = true; this.entries.clear(); this.ordinary.clear(); this.final.clear();
    if (this.timer !== null) clearTimeout(this.timer); this.timer = null;
    if (this.deadline !== null) clearTimeout(this.deadline); this.deadline = null;
  }
}
