/** Demand-loaded, read-only experiment observations. Owns abort/stale-response
 * fences and its DOM island; workspace CAS and scientific decisions stay server-owned. */
import { createButton, createField } from '../../../../ui/index';

type RecordValue = Record<string, any>;
type ReadPage = (taskId: string, cursor: string, signal: AbortSignal) => Promise<RecordValue>;
type Translate = (key: string, fallback: string) => string;

export class ResearchObservationRecovery {
  private host: HTMLElement | null = null;
  private identity = '';
  private taskId = '';
  private page: RecordValue | null = null;
  private cursor = '';
  private error = '';
  private flight: AbortController | null = null;
  private epoch = 0;
  private read: ReadPage | null = null;
  private tr: Translate = (_key, fallback) => fallback;

  sync(host: HTMLElement, identity: string, read: ReadPage, tr: Translate): void {
    if (identity !== this.identity) { this.dispose(); this.identity = identity; }
    const changed = host !== this.host;
    this.host = host; this.read = read; this.tr = tr;
    if (changed) this.render();
  }

  suggestTask(taskId: string): void {
    if (!taskId || taskId === this.taskId) return;
    this.epoch++; this.flight?.abort(); this.flight = null;
    this.taskId = taskId; this.page = null; this.error = ''; this.cursor = '';
    this.render();
  }

  dispose(): void {
    this.epoch++; this.flight?.abort(); this.flight = null;
    this.host = null; this.identity = ''; this.taskId = ''; this.page = null;
    this.error = ''; this.cursor = ''; this.read = null;
  }

  private async load(cursor: string): Promise<void> {
    if (!this.read || !this.taskId || this.flight) return;
    const epoch = ++this.epoch, taskId = this.taskId;
    const flight = this.flight = new AbortController();
    this.error = ''; this.cursor = cursor; this.render();
    try {
      const page = await this.read(taskId, cursor, flight.signal);
      if (epoch !== this.epoch || flight.signal.aborted) return;
      if (typeof page?.found !== 'boolean' || !Array.isArray(page.observations)
          || page.observations.length > 16 || typeof page.next_cursor !== 'string'
          || (page.found && page.binding?.task_id !== taskId)
          || (!page.found && (page.binding !== null || page.observations.length || page.next_cursor))
          || page.observations.some((row: RecordValue) => !row || typeof row !== 'object'
            || row.binding?.task_id !== taskId
            || ['user_id', 'revision', 'basis_digest', 'direction_hash', 'lang'].some(key => row.binding?.[key] !== page.binding?.[key])
            || !Number.isSafeInteger(row.ordinal) || row.ordinal < 1)) {
        throw new Error(this.tr('evolution.research.observationInvalid', 'The observation response is invalid.'));
      }
      this.page = page;
    } catch (error) {
      if (epoch !== this.epoch || flight.signal.aborted) return;
      this.error = error instanceof Error ? error.message : String(error);
    } finally {
      if (epoch === this.epoch) { this.flight = null; this.render(); }
    }
  }

  private render(): void {
    if (!this.host) return;
    const doc = this.host.ownerDocument;
    const section = doc.createElement('section'); section.className = 'ui-stack';
    const title = doc.createElement('strong');
    title.textContent = this.tr('evolution.research.observationTitle', 'Retained experiment observations');
    const hint = doc.createElement('p');
    hint.textContent = this.tr('evolution.research.observationHint', 'Inspect tool results retained before an action finished. These observations have not been reviewed as research conclusions.');
    const field = createField({label: this.tr('evolution.research.observationTask', 'Experiment task'),
      controlId: 'research-observation-task', value: this.taskId});
    const input = field.control as HTMLInputElement;
    input.addEventListener('input', () => {
      this.epoch++; this.flight?.abort(); this.flight = null;
      this.taskId = input.value.trim(); this.page = null; this.error = ''; this.cursor = '';
      // Keep the editor and caret intact while changing the requested task.
      mainButton.disabled = !this.taskId;
      mainButton.textContent = this.tr('evolution.research.observationOpen', 'Inspect observations');
      controls.replaceChildren(mainButton); results.replaceChildren();
    });
    const controls = doc.createElement('div'); controls.className = 'ui-row';
    const mainButton = createButton({label: this.flight
      ? this.tr('evolution.research.observationLoading', 'Loading observations…')
      : this.tr('evolution.research.observationOpen', 'Inspect observations'), size: 'sm',
      disabled: Boolean(this.flight) || !this.taskId, onClick: () => { void this.load(''); }}, doc);
    controls.append(mainButton);
    if (this.page?.next_cursor) controls.append(createButton({
      label: this.tr('evolution.research.observationNext', 'Next observations'), size: 'sm',
      disabled: Boolean(this.flight), onClick: () => { void this.load(this.page!.next_cursor); }}, doc));
    if (this.error) controls.append(createButton({label: this.tr('evolution.research.observationRetry', 'Retry'),
      size: 'sm', onClick: () => { void this.load(this.cursor); }}, doc));
    const results = doc.createElement('div'); results.className = 'ui-stack';
    if (this.error) {
      const error = doc.createElement('p'); error.setAttribute('role', 'alert'); error.textContent = this.error; results.append(error);
    }
    if (this.page) {
      const note = doc.createElement('p');
      note.textContent = this.page.observations.length
        ? `${this.tr('evolution.research.observationRevision', 'Recorded with workspace revision')}: ${this.page.binding.revision}`
        : this.tr('evolution.research.observationEmpty', 'No retained observations were found for this task.');
      results.append(note);
    }
    for (const row of this.page?.observations || []) {
      const article = doc.createElement('article'); article.className = 'rsw-record rs-markdown';
      const heading = doc.createElement('strong'); heading.textContent = `${row.ordinal}: ${row.tool} · ${row.receipt?.status || ''}`;
      const details = doc.createElement('details');
      const summary = doc.createElement('summary'); summary.textContent = this.tr('evolution.research.observationExcerpt', 'Observed result excerpt');
      const pre = doc.createElement('pre'); pre.textContent = String(row.receipt?.result_excerpt || '');
      details.append(summary, pre); article.append(heading, details); results.append(article);
    }
    section.append(title, hint, field.el, controls, results); this.host.replaceChildren(section);
  }
}
