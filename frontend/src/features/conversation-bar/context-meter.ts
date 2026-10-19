/** Context usage presentation only; accounting and compaction commands stay in the runtime adapter. */
import { createAnchoredDisclosure, positionDisclosure } from './disclosure';
import { createAlert, createButton } from '../../ui';
import type { WorkingFactsPorts } from '../working-facts/ports';

export interface ContextMeterSnapshot {
  used: number;
  limit: number | null;
  known: boolean;
  exact: boolean;
  pctRaw: number;
  pct: number | null;
  zone: string;
  compactions: number;
  convId: string | null;
  busy: boolean;

  compacting?: boolean;
  compactionPending?: boolean;
  hasHistory: boolean;
}
export interface ContextMeterOptions {
  host: HTMLElement;
  translate: (key: string, fallback: string) => string;
  read: () => ContextMeterSnapshot;
  onCompact: (convId: string) => void;
  onHistory: (convId: string) => void;
  workingFacts?: WorkingFactsPorts;
}

export function createContextMeter(options: ContextMeterOptions) {
  const { host, translate: tt } = options;
  const doc = host.ownerDocument;
  const el = doc.createElement('button');
  el.type = 'button';
  el.id = 'contextHealthBar';
  el.className = 'ctx-health-bar';
  const span = (className: string, parent: HTMLElement) => {
    const node = doc.createElement('span');
    node.className = className;
    parent.append(node);
    return node;
  };
  const label = span('ctx-meter-label', el);
  const track = span('ctx-meter-track', el);
  track.setAttribute('aria-hidden', 'true');
  span('ctx-meter-fill', track);
  const pct = span('ctx-bar-pct', el);
  const cap = span('ctx-bar-cap', el);
  const project = host.querySelector('#projectBar');
  if (project) project.after(el);
  else host.insertBefore(el, host.querySelector('#bgActivity, #turnNav'));
  const panel = doc.createElement('div');
  panel.id = 'ctxBarPopover';
  panel.className = 'ctx-bar-popover';
  panel.setAttribute('role', 'dialog');
  panel.setAttribute('aria-modal', 'false');
  el.setAttribute('aria-haspopup', 'dialog');
  el.setAttribute('aria-controls', panel.id);
  el.setAttribute('aria-expanded', 'false');
  const heading = doc.createElement('h3');
  heading.id = 'ctxBarPopoverHeading';
  panel.setAttribute('aria-labelledby', heading.id);
  panel.append(heading);
  const details = doc.createElement('dl');
  panel.append(details);
  const rows = ['usage', 'capacity', 'compactions'].map((key) => {
    const term = doc.createElement('dt');
    const value = doc.createElement('dd');
    details.append(term, value);
    return { key, term, value };
  });
  const button = (className: string) => {
    const node = doc.createElement('button');
    node.type = 'button';
    node.className = 'ctx-pop-item ' + className;
    panel.append(node);
    return node;
  };
  const compact = button('ctx-pop-compact');
  const facts = createButton({ label: tt('workingFacts.title', 'Current basis'), variant: 'ghost', size: 'sm',
    onClick: () => void openFacts() }, doc);
  facts.dataset.openWorkingFacts = ''; panel.append(facts);
  const factsError = doc.createElement('div'); panel.append(factsError);
  const history = button('ctx-pop-history');
  const close = button('ctx-pop-close');
  const disclosure = createAnchoredDisclosure({ anchor: el, panel, onClose: () => el.setAttribute('aria-expanded', 'false') });
  let current: ContextMeterSnapshot;
  let disposed = false;
  let factsGeneration = 0, closeFacts: (() => void) | null = null;
  function retireFacts() { factsGeneration++; closeFacts?.(); closeFacts = null; factsError.replaceChildren(); }
  async function openFacts() {
    const id = options.read().convId, ports = options.workingFacts;
    if (!id || !ports || disposed) return;
    retireFacts(); const generation = factsGeneration; facts.disabled = true;
    try {
      const feature = await import('../working-facts/dialog');
      if (disposed || generation !== factsGeneration || options.read().convId !== id) return;
      disclosure.close(true); closeFacts = feature.openWorkingFacts(id, ports, tt);
    } catch (cause) {
      if (!disposed && generation === factsGeneration) factsError.replaceChildren(createAlert({ variant: 'error',
        title: tt('workingFacts.loadError', 'Facts could not be loaded'), body: String(cause) }).el);
    } finally {
      if (!disposed && generation === factsGeneration) facts.disabled = !options.read().convId;
    }
  }
  let live: { el: HTMLElement; body: HTMLElement; text: string; convId: string } | null = null;
  const text = (node: HTMLElement, value: string) => { if (node.textContent !== value) node.textContent = value; };
  const format = (n: number) => n >= 1_000_000 ? (n / 1_000_000).toFixed(n >= 10_000_000 ? 0 : 1) + 'M'
    : n >= 1_000 ? (n / 1_000).toFixed(n >= 10_000 ? 0 : 1) + 'k' : String(n);
  function update(snapshot: ContextMeterSnapshot) {

    if (disposed) return;
    const compacting = Boolean(snapshot.compacting);
    if (compacting) el.dataset.compacting = '1'; else delete el.dataset.compacting;
    if (current && current.convId !== snapshot.convId) { disclosure.close(false); closeLiveSummary(); retireFacts(); }
    current = snapshot;
    facts.textContent = tt('workingFacts.title', 'Current basis');
    facts.hidden = !options.workingFacts; facts.disabled = !snapshot.convId;
    const context = tt('conversationBar.context', 'Context');
    const unknown = tt('conversationBar.unknown', 'Unknown');
    const estimated = tt('conversationBar.estimated', 'Estimated');
    const percent = snapshot.known && snapshot.used > 0 ? snapshot.pct + '%' : '—';
    text(label, context);
    text(pct, percent);
    text(cap, snapshot.limit == null ? '—' : format(snapshot.limit) + (snapshot.exact ? '' : ' ≈'));
    const fill = (Math.max(0, Math.min(1, Number.isFinite(snapshot.pctRaw) ? snapshot.pctRaw : 0)) * 100).toFixed(2) + '%';
    if (el.style.getPropertyValue('--ctx-fill') !== fill) el.style.setProperty('--ctx-fill', fill);
    if (el.dataset.zone !== snapshot.zone) el.dataset.zone = snapshot.zone;
    el.setAttribute('aria-label', context + ': ' + (snapshot.known && snapshot.used > 0 ? percent : unknown)
      + (compacting ? ' · ' + tt('conversationBar.compacting', 'Compacting…') : ''));
    text(heading, tt('conversationBar.contextDetails', 'Context details'));
    const defaults = ['Usage', 'Capacity', 'Compactions'];
    const values = [snapshot.used > 0 ? String(snapshot.used) : unknown,
      snapshot.limit == null ? unknown : String(snapshot.limit) + (snapshot.exact ? '' : ' · ' + estimated), String(snapshot.compactions)];
    rows.forEach((row, i) => { text(row.term, tt('conversationBar.' + row.key, defaults[i])); text(row.value, values[i]); });
    text(compact, snapshot.compactionPending || compacting ? tt('conversationBar.compacting', 'Compacting…')
      : snapshot.busy ? tt('compactNow.busy', 'Cannot compact while a task is running') : tt('compactNow.action', 'Compact context now'));
    const ownedFocus = doc.activeElement === compact;
    compact.disabled = Boolean(snapshot.busy || snapshot.compactionPending || compacting || !snapshot.convId);
    if (ownedFocus && compact.disabled) close.focus({ preventScroll: true });
    text(history, tt('compactNow.viewHistory', 'View compaction history'));
    history.hidden = !snapshot.hasHistory;

    history.disabled = !snapshot.hasHistory;
    text(close, tt('conversationBar.close', 'Close'));
  }
  function toggle() {

    if (disposed) return;
    if (disclosure.isOpen()) { disclosure.close(true); return; }
    update(options.read());
    el.setAttribute('aria-expanded', 'true');
    disclosure.open();
  }
  el.addEventListener('click', toggle);
  compact.addEventListener('click', () => {
    const snapshot = options.read();
    disclosure.close(true);
    if (snapshot.convId && !snapshot.busy && !snapshot.compacting && !snapshot.compactionPending) options.onCompact(snapshot.convId);
  });
  history.addEventListener('click', () => {
    const snapshot = options.read();
    disclosure.close(true);
    if (snapshot.convId) options.onHistory(snapshot.convId);
  });
  close.addEventListener('click', () => disclosure.close(true));
  function closeLiveSummary() { live?.el.remove(); live = null; }
  update(options.read());
  return {
    el, update,
    openLiveSummary(convId: string) {

      if (disposed || options.read().convId !== convId || !el.isConnected) return;
      closeLiveSummary();
      const card = doc.createElement('div');
      card.id = 'ctxLiveSummary';
      card.className = 'ctx-live-summary';
      const head = doc.createElement('div');
      head.className = 'ctx-live-summary-head';
      head.textContent = tt('compactNow.streaming', 'Generating compaction summary…');
      const body = doc.createElement('div');
      body.className = 'ctx-live-summary-body';
      card.append(head, body);
      doc.body.append(card);
      positionDisclosure(el, card);
      live = { el: card, body, text: '', convId };
    },
    appendLiveSummary(convId: string, chunk: string) {
      if (!live || live.convId !== convId || !chunk) return;
      // Reconstructible streaming preview, not the durable summary owner.
      live.text = (live.text + chunk).slice(-24_000);
      live.body.textContent = live.text;
      live.body.scrollTop = live.body.scrollHeight;
    },
    closeLiveSummary,
    destroy() { disposed = true; retireFacts(); disclosure.destroy(); closeLiveSummary(); el.removeEventListener('click', toggle); el.remove(); },
  };
}
