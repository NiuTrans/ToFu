/** One read-only historical evidence page. Workspace identity fences reads;
 * disposal aborts transport and releases rows. No polling or durable cache. */
import { createButton } from '../../../../ui/index';
type RecordValue = Record<string, any>;
type Translate = (key: string, fallback: string) => string;
type ReadPage = (direction: string, lang: string, cursor: number, signal: AbortSignal) => Promise<RecordValue>;

export class ResearchHistory {
  private host: HTMLElement | null = null;
  private workspace: RecordValue = {};
  private key = '';
  private page: RecordValue | null = null;
  private error = '';
  private loading = false;
  private cursor = 0;
  private flight: AbortController | null = null;
  private epoch = 0;
  private copy = '';
  private focusAction = '';
  private read: ReadPage | null = null;
  private tr: Translate = (_key, fallback) => fallback;

  sync(host: HTMLElement | null, workspace: RecordValue, read: ReadPage, tr: Translate): void {
    const key = JSON.stringify([workspace.direction, workspace.lang, workspace.revision, workspace.history?.head]);
    const copy = tr('evolution.research.historyOpen', 'Browse experiment history');
    const unchanged = key === this.key && host === this.host && copy === this.copy;
    if (key !== this.key) { this.dispose(); this.key = key; }
    this.copy = copy; this.host = host; this.workspace = workspace; this.read = read; this.tr = tr;
    if (!unchanged) this.render();
  }

  dispose(): void {
    this.epoch++; this.flight?.abort(); this.flight = null;
    this.page = null; this.error = ''; this.loading = false; this.cursor = 0;
    this.host = null; this.key = ''; this.focusAction = ''; this.copy = ''; this.read = null; this.workspace = {};
  }

  private async load(cursor: number): Promise<void> {
    if (this.loading || !this.read) return;
    const epoch = ++this.epoch;
    this.flight?.abort(); const flight = this.flight = new AbortController();
    this.cursor = cursor; this.loading = true; this.error = ''; this.render();
    try {
      const payload = await this.read(this.workspace.direction, this.workspace.lang, cursor, flight.signal);
      if (epoch !== this.epoch || flight.signal.aborted) return;
      const page = payload?.history_page;
      if (!page || page.revision !== this.workspace.revision || page.head !== this.workspace.history?.head
          || typeof page.page !== 'string' || !/^[a-f0-9]{64}$/.test(page.page)
          || !Array.isArray(page.runs) || !page.runs.length || page.runs.length > 16
          || page.runs.some((row: unknown) => !row || typeof row !== 'object' || Array.isArray(row))
          || page.total !== this.workspace.history?.count
          || (page.next_cursor !== null && (!Number.isSafeInteger(page.next_cursor) || page.next_cursor <= 0
              || page.next_cursor >= (cursor || page.total)))) {
        throw new Error(this.tr('evolution.research.historyChanged', 'Research changed. Reload the workspace before browsing its history.'));
      }
      this.page = page;
    } catch (error) {
      if (epoch !== this.epoch || flight.signal.aborted) return;
      this.error = error instanceof Error ? error.message : String(error);
    } finally {
      if (epoch === this.epoch) { this.loading = false; this.flight = null; this.render(); }
    }
  }

  private render(): void {
    const host = this.host;
    if (!host) return;
    const doc = host.ownerDocument;
    const opened = new Set([...host.querySelectorAll<HTMLDetailsElement>('details[open]')].map(el => el.dataset.evidenceKey));
    const nodes: Node[] = [];
    const buttons = new Map<string, HTMLButtonElement>();
    const note = doc.createElement('p');
    note.textContent = `${this.tr('evolution.research.historyCount', 'Archived observations')}: ${this.workspace.history?.count || 0}. ${this.tr('evolution.research.historyHint', 'Historical evidence matching the current hypothesis and protocol still participates in decisions and audits.')}`;
    nodes.push(note);
    const controls = doc.createElement('div'); controls.className = 'ui-row';
    const button = (label: string, cursor: number, action = 'latest') => {
      const control = createButton({label, size: 'sm', disabled: this.loading,
        onClick: () => { this.focusAction = action; void this.load(cursor); }}, doc);
      buttons.set(action, control); return control;
    };
    controls.append(button(this.loading ? this.tr('evolution.research.historyLoading', 'Loading history…')
      : this.page ? this.tr('evolution.research.historyLatest', 'Latest archived results')
      : this.tr('evolution.research.historyOpen', 'Browse experiment history'), 0));
    if (this.page?.next_cursor !== null && this.page?.next_cursor !== undefined) {
      controls.append(button(this.tr('evolution.research.historyOlder', 'Older results'), this.page.next_cursor, 'older'));
    }
    if (this.error) controls.append(button(this.tr('evolution.research.historyRetry', 'Retry this page'), this.cursor, 'retry'));
    nodes.push(controls);
    if (this.error) { const error = doc.createElement('p'); error.setAttribute('role', 'alert'); error.textContent = this.error; nodes.push(error); }
    for (const row of this.page?.runs || []) {
      const article = doc.createElement('article'); article.className = 'rsw-record rs-markdown';
      const title = doc.createElement('strong'); title.textContent = `${row.id}: ${row.scientific_outcome}`;
      const details = doc.createElement('details');
      details.dataset.evidenceKey = `${this.page?.page}:${row.id}`;
      details.open = opened.has(details.dataset.evidenceKey);
      const summary = doc.createElement('summary'); summary.textContent = this.tr('evolution.research.historyEvidence', 'Observation and evidence references');
      const body = doc.createElement('pre'); body.textContent = JSON.stringify(row, null, 2);
      const evidence = doc.createElement('div'); evidence.className = 'rs-markdown'; evidence.append(body);
      details.append(summary, evidence); article.append(title, details); nodes.push(article);
    }
    host.setAttribute('aria-busy', String(this.loading)); host.replaceChildren(...nodes);
    if (!this.loading && this.focusAction) {
      (buttons.get(this.focusAction) || buttons.get('latest'))?.focus({preventScroll: true});
      this.focusAction = '';
    }
  }
}
