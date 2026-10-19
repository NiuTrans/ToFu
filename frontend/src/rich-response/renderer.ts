/** Native UI over persisted response data. No model markup, script or business state.
 * Entries own reader disclosure, compatible focus and abortable image requests.
 */
import { createButton, createProgress } from '../ui';
import { request, resolvePath } from '../api/transport';

import type { action, asset, metric } from './contract.generated';
import { parseRichResponse, validateRichResponse } from './validation';

import { renderTimeline } from './timeline';

import { renderDataChart } from './data-chart';

import { createCitationView } from './citations';

import { renderChoices, type ChoicePorts } from './choices';
import { renderSurfaceBlock } from './surface-blocks';
import { renderGuidanceForm } from './guidance-form';
import { renderResourcePanel } from './resource-panel';
import { createResponseLayout } from './layout';
import type { SurfaceContext, ResourceHub } from './resource-client';
export interface RichPorts {
  surfaceContext?: SurfaceContext;
  resources?: ResourceHub;

  choices?: ChoicePorts;

  /** Host-supplied server projection identity, never accepted from rich JSON. */
  interactionSnapshot?: string;

  conversationId?: string;
  authorizeArtifact?(id: string, conversationId: string, signal: AbortSignal): Promise<unknown>;
  text?(key: string, fallback: string): string;
  asset?(id: string, signal: AbortSignal): Promise<Blob>;
  openArtifact?(id: string): void;
}
interface Entry { node: HTMLDetailsElement; identity: string; dispose(): void; resource?: {provider:string; scope:string; update(block: import('./contract.generated').resource_panel):void}; form?: {binding:string; reload():void} }
interface Mount { entries: Map<string, Entry>; title: HTMLElement; summary: HTMLElement; layout: ReturnType<typeof createResponseLayout> }
const mounts = new WeakMap<HTMLElement, Mount>();
const IMAGE_TYPES = new Set(['image/png', 'image/jpeg', 'image/webp', 'image/gif']);

export function disposeRichResponse(host: HTMLElement): void {
  mounts.get(host)?.entries.forEach(entry => entry.dispose());
  mounts.delete(host);
}

/** A claimed pass cannot override an observable missed absolute budget. */
export function metricStatus(value: metric): metric['status'] {
  if (value.budget !== undefined && (value.direction === 'lower'
    ? value.candidate > value.budget : value.candidate < value.budget)) return 'failed';
  return value.status;
}

export function renderRichResponse(host: HTMLElement, value: unknown, ports: RichPorts = {}): boolean {
  const response = typeof value === 'string' ? parseRichResponse(value) : validateRichResponse(value);
  const text = (key: string, fallback: string) => ports.text?.(key, fallback) ?? fallback;
  if (!response) {
    disposeRichResponse(host);
    host.textContent = text('rich.invalid', 'Invalid or unsupported rich response. Original evidence remains available.');
    return false;
  }
  const doc = host.ownerDocument;
  const element = (tag: string, content = '') => {
    const node = doc.createElement(tag);
    node.textContent = content;
    return node;
  };
  const number = (value: number) => new Intl.NumberFormat(doc.documentElement.lang || 'en',
    { maximumSignificantDigits: 6 }).format(value);
  const baselineLabel = text('rich.baseline', 'Baseline');
  const candidateLabel = text('rich.candidate', 'Candidate');
  let state = mounts.get(host);
  if (!state) {
    host.replaceChildren();
    host.classList.add('rich-response');
    state = { entries: new Map(), title: element('h2'), summary: element('p'), layout:createResponseLayout(doc) };
    host.append(state.title, state.summary, state.layout.node);
    mounts.set(host, state);
  }
  if (state.title.textContent !== response.title) state.title.textContent = response.title;
  if (state.summary.textContent !== (response.summary ?? '')) state.summary.textContent = response.summary ?? '';
  const active = new Set<string>();

  const initialImages: Array<() => Promise<void>> = [];
  for (const block of response.blocks) {
    active.add(block.id);
    const scope=JSON.stringify(ports.surfaceContext ?? null);
    const identity = `${ports.conversationId ?? ''}\u0000${JSON.stringify([block, scope, (block.type === 'choice_prompt' || block.type === 'form_prompt') ? ports.interactionSnapshot : undefined])}`;
    const old = state.entries.get(block.id);
    if (old?.identity === identity) continue;
    const formBinding=JSON.stringify([block,scope,ports.conversationId]);
    if(block.type==='form_prompt'&&old?.form?.binding===formBinding){
      old.identity=identity;old.form.reload();continue;
    }
    if(block.type==='resource_panel' && old?.resource?.provider===[block.provider,block.connection,block.queue].join('\0') && old.resource.scope===scope){
      old.resource.update(block);old.identity=identity;old.node.firstElementChild!.textContent=block.title;continue;
    }
    const focus = doc.activeElement;
    const focusKey = focus && old?.node.contains(focus)
      ? (focus as HTMLElement).dataset.richControl ?? (focus === old.node.firstElementChild ? 'summary' : null) : null;
    const details = doc.createElement('details');
    details.className = 'rich-block';
    details.dataset.richBlock = block.id;
    details.open = old ? old.node.open : true;
    details.append(element('summary', block.title));
    const abort = new AbortController();
    const cleanups: Array<() => void> = [];
    const button = (label: string, key: string, fn?: () => void) => {
      const control = createButton({ label, size: 'sm' }, doc);
      control.dataset.richControl = key;
      if (fn) {
        control.addEventListener('click', fn);
        cleanups.push(() => control.removeEventListener('click', fn));
      }
      return control;
    };
    const actionButton = (item: action) => {
      const key = `${item.kind}:${item.targetId}`;
      if (item.kind.endsWith('_artifact')) {
        const control = button(item.label, key);
        const report = element('p'); report.setAttribute('role', 'status'); report.hidden = true;
        const activate = async () => {
          if (!ports.conversationId || abort.signal.aborted) return;
          control.disabled = true;
          try {
            const path = `/api/response-assets/artifacts/${encodeURIComponent(item.targetId)}?conversationId=${encodeURIComponent(ports.conversationId)}`;
            await (ports.authorizeArtifact?.(item.targetId, ports.conversationId, abort.signal)
              ?? request(path, { signal: abort.signal }));
            if (abort.signal.aborted) return;
            if (item.kind === 'download_artifact') {
              const link = doc.createElement('a'); link.href = resolvePath(path + '&download=1');
              link.download = item.targetId; doc.body.append(link); link.click(); link.remove();
            } else if (ports.openArtifact) ports.openArtifact(item.targetId);
            else {
              const intent = createButton({label:item.label,size:'sm'},doc);
              intent.hidden = true; intent.dataset.conversationAction = 'open-artifact';
              intent.dataset.operation = item.targetId; intent.dataset.artifactId = item.targetId;
              control.after(intent); intent.click(); intent.remove();
            }
            report.hidden = true;
          } catch {
            if (!abort.signal.aborted) { report.textContent = text('rich.imageError', 'Attachment unavailable.'); report.hidden = false; control.after(report); }
          } finally { if (!abort.signal.aborted) control.disabled = false; }
        };
        control.disabled = !ports.conversationId;
        control.addEventListener('click', activate);
        cleanups.push(() => control.removeEventListener('click', activate));
        return control;
      }
      return button(item.label, key, () => {
        const link = doc.createElement('a');
        const assetPath = `/api/response-assets/${encodeURIComponent(item.targetId)}`;
        link.href = resolvePath(item.kind === 'download_artifact'
          ? `/api/artifacts/${encodeURIComponent(item.targetId)}/raw`
          : assetPath + (item.kind === 'download_asset' ? '?download=1' : ''));
        if (item.kind.startsWith('download')) link.download = item.targetId;
        else { link.target = '_blank'; link.rel = 'noopener noreferrer'; }
        doc.body.append(link);
        link.click();
        link.remove();
      });
    };
    const table = (columns: string[], rows: string[][]) => {
      const wrap = element('div');
      wrap.className = 'rich-table-scroll';
      wrap.tabIndex = 0;
      wrap.dataset.richControl = 'table';
      wrap.setAttribute('role', 'region');
      wrap.setAttribute('aria-label', block.title);
      const table = doc.createElement('table');
      const head = doc.createElement('thead');
      const headings = doc.createElement('tr');
      columns.forEach(column => {
        const cell = element('th', column);
        cell.setAttribute('scope', 'col');
        headings.append(cell);
      });
      head.append(headings);
      table.append(head);
      const body = doc.createElement('tbody');
      rows.forEach(row => {
        const line = doc.createElement('tr');
        row.forEach(cell => line.append(element('td', cell)));
        body.append(line);
      });
      table.append(body);
      wrap.append(table);
      details.append(wrap);
    };
    const metrics = (items: metric[], cards: boolean) => {
      if (cards) {
        const grid = element('div');
        grid.className = 'rich-metric-grid';
        for (const item of items) {
          const card = element('section');
          card.className = 'rich-metric';
          card.dataset.status = metricStatus(item);
          card.append(element('h3', item.label), element('strong', `${number(item.candidate)} ${item.unit}`));
          card.append(element('p', `${baselineLabel}: ${number(item.baseline)} ${item.unit}`));
          const improvement = item.baseline === 0 ? null
            : (item.direction === 'lower' ? item.baseline - item.candidate : item.candidate - item.baseline) / item.baseline * 100;
          const change = improvement !== null && Number.isFinite(improvement)
            ? `${improvement >= 0 ? '+' : ''}${number(improvement)}% ${text('rich.improvement', 'relative improvement')}`
            : text('rich.undefinedRatio', 'Relative change unavailable: zero baseline.');
          card.append(element('p', change));
          const status = element('span', text(`rich.${metricStatus(item)}`, metricStatus(item)));
          status.className = 'rich-status';
          card.append(status);
          grid.append(card);
        }
        details.append(grid);
      }
      table([text('rich.metric', 'Metric'), baselineLabel, candidateLabel, text('rich.status', 'Acceptance'),
        text('rich.budget', 'Budget'), text('rich.note', 'Evidence')], items.map(item => [item.label,
        `${number(item.baseline)} ${item.unit}`, `${number(item.candidate)} ${item.unit}`,
        text(`rich.${metricStatus(item)}`, metricStatus(item)), item.budget === undefined ? '—' : `${number(item.budget)} ${item.unit}`,
        `${text(`rich.${item.direction}`, item.direction)} · ${item.note ?? ''}`]));
      if (items.some(item => metricStatus(item) === 'failed')) {
        const warning = element('p', text('rich.failedNotice', 'Acceptance failed for at least one metric. A relative improvement does not establish a passing result.'));
        warning.className = 'rich-warning';
        warning.setAttribute('role', 'note');
        details.append(warning);
      }
    };
    const image = (item: asset) => {
      const figure = doc.createElement('figure');
      const status = element('p', text('rich.loading', 'Loading image…'));
      status.setAttribute('role', 'status');
      const retry = button(text('rich.retry', 'Retry'), `retry:${item.id}`, () => void load());
      retry.hidden = true;
      let flight: AbortController | null = null;
      let image: HTMLImageElement | null = null;
      let objectUrl = '';
      let imageCleanup: (() => void) | null = null;
      const clearImage = () => {
        imageCleanup?.(); imageCleanup = null;
        image?.remove(); image = null;
        if (objectUrl) { URL.revokeObjectURL(objectUrl); objectUrl = ''; }
      };
      const unavailable = () => {
        status.hidden = false;
        status.textContent = text('rich.imageError', 'Image unavailable. The attachment may be inaccessible or expired.');
        retry.hidden = false;
      };
      const load = async () => {

        if (abort.signal.aborted) return;
        flight?.abort();
        clearImage();
        flight = new AbortController();
        const current = flight;
        retry.hidden = true;
        status.hidden = false;
        status.textContent = text('rich.loading', 'Loading image…');
        try {
          const blob = await (ports.asset?.(item.id, current.signal)
            ?? request<Blob>(`/api/response-assets/${encodeURIComponent(item.id)}`, { parse: 'blob', signal: current.signal }));
          if (abort.signal.aborted || current.signal.aborted) return;
          if (!IMAGE_TYPES.has(blob.type) || !blob.size) throw new Error('Invalid image response');
          const nextImage = doc.createElement('img');
          nextImage.alt = item.alt;
          nextImage.hidden = true;
          image = nextImage;
          const loaded = () => {
            if (abort.signal.aborted || current.signal.aborted || image !== nextImage) return;
            nextImage.hidden = false; status.hidden = true;
          };
          const failed = () => {
            if (abort.signal.aborted || current.signal.aborted || image !== nextImage) return;
            nextImage.hidden = true; unavailable();
          };
          nextImage.addEventListener('load', loaded);
          nextImage.addEventListener('error', failed);
          imageCleanup = () => { nextImage.removeEventListener('load', loaded); nextImage.removeEventListener('error', failed); nextImage.removeAttribute('src'); };
          objectUrl = URL.createObjectURL(blob);
          figure.prepend(nextImage);
          nextImage.src = objectUrl;
        } catch {
          if (!abort.signal.aborted && !current.signal.aborted) unavailable();
        }
      };
      cleanups.push(() => { flight?.abort(); clearImage(); });
      figure.append(status, retry, element('figcaption', item.caption ?? item.alt));
      const actions = element('div');
      actions.className = 'rich-actions';
      actions.append(actionButton({ kind: 'download_asset', label: text('rich.download', 'Download'), targetId: item.id }),
        button(text('rich.fullscreen', 'Fullscreen'), `fullscreen:${item.id}`, () => {
          if (figure.requestFullscreen) void figure.requestFullscreen().catch(() => {});
        }));
      figure.append(actions);
      details.append(figure);
      initialImages.push(load);
    };
    let resource: Entry['resource'];
    let form: Entry['form'];
    if (block.type === 'resource_panel') {
      const panel=renderResourcePanel(doc,block,{context:ports.surfaceContext,hub:ports.resources,text});
      details.append(panel.node);cleanups.push(()=>panel.dispose());
      resource={provider:[block.provider,block.connection,block.queue].join('\0'),scope,update:panel.update};
    }
    else if (block.type === 'metric_grid' || block.type === 'workflow' || block.type === 'notice') details.append(renderSurfaceBlock(doc,block,text));
    else if (block.type === 'form_prompt') {
      const view=renderGuidanceForm(doc,block,ports.choices);details.append(view.node);cleanups.push(()=>view.dispose());
      form={binding:formBinding,reload:view.reload};
    }
    else if (block.type === 'choice_prompt') {
      const choices = renderChoices(block, ports.choices, doc);
      details.append(choices.node); cleanups.push(() => choices.dispose());
    }
    else if (block.type === 'citations') {
      const sourceHost = element('div');
      const citations = createCitationView(sourceHost, { text });
      citations.update([block]); citations.renderBlock(sourceHost, block.id);
      details.append(sourceHost); cleanups.push(() => citations.dispose());
    }
    else if (block.type === 'data_chart') details.append(renderDataChart(doc, block, text));
    else if (block.type === 'timeline') details.append(renderTimeline(doc, block, text));
    else if (block.type === 'table') table(block.columns, block.rows);
    else if (block.type === 'comparison' || block.type === 'chart') {
      details.append(element('p', `${block.baselineLabel ?? baselineLabel} → ${block.candidateLabel ?? candidateLabel}`));
      metrics(block.metrics, block.type === 'comparison');
      if (block.type === 'chart') for (const item of block.metrics) {
        const row = element('div');
        row.className = 'rich-chart-row';
        row.append(element('h3', item.label));
        const scale = Math.max(item.baseline, item.candidate, item.budget ?? 0, 1);
        for (const [label, value] of [[baselineLabel, item.baseline], [candidateLabel, item.candidate]] as const) {
          row.append(element('span', `${label}: ${number(value)} ${item.unit}`));
          const bar = createProgress({ value: value / scale * 100, label: `${item.label} ${label}` });
          row.append(bar.el);
        }
        if (item.budget !== undefined) row.append(element('p', `${text('rich.budget', 'Budget')}: ${number(item.budget)} ${item.unit}`));
        details.append(row);
      }
    } else if (block.type === 'image_gallery') block.images.forEach(image);
    else if (block.type === 'evidence') {
      const list = element('dl');
      block.items.forEach(item => list.append(element('dt', item.label), element('dd', item.value)));
      details.append(list);
    } else if (block.type === 'artifact_reference') {
      details.append(actionButton({ kind: 'open_artifact', label: block.title, targetId: block.artifactId }));
    } else if (block.type === 'action_panel') {
      const actions = element('div'); actions.className = 'rich-actions';
      block.actions.forEach(action => actions.append(actionButton(action)));
      details.append(actions);
    }
    if (block.type === 'timeline') {
      const oldData = old?.node.querySelector<HTMLDetailsElement>('.rich-timeline-data');
      const newData = details.querySelector<HTMLDetailsElement>('.rich-timeline-data');
      if (oldData && newData) newData.open = oldData.open;
    }
    const entry: Entry = { node: details, identity, resource, form, dispose: () => { abort.abort(); cleanups.forEach(cleanup => cleanup()); } };
    if (old) { old.dispose(); old.node.replaceWith(details); } else host.append(details);
    state.entries.set(block.id, entry);
    if (focusKey) {
      const target = focusKey === 'summary' ? details.firstElementChild
        : [...details.querySelectorAll<HTMLElement>('[data-rich-control]')].find(node => node.dataset.richControl === focusKey);
      if (target && 'focus' in target) (target as HTMLElement).focus({ preventScroll: true });
    }
  }
  for (const [id, entry] of state.entries) if (!active.has(id)) {
    entry.dispose(); entry.node.remove(); state.entries.delete(id);
  }
  state.layout.update(response.layout,response.blocks.map(block=>({id:block.id,node:state!.entries.get(block.id)!.node})));
  void Promise.allSettled(initialImages.map(load => load()));
  return true;
}
