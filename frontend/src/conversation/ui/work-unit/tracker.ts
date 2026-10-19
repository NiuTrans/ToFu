/** One demand-scoped WorkUnit observer/poller. Mounted cards share requests; server snapshots alone settle work.
 * Entries die with their last DOM owner. In-flight receipts are fenced by entry and authenticated-owner identity.
 */
import { createLifecycleScope, type LifecycleScope } from '../../../lifecycle';
import { createWorkUnitView } from './view';
import { renderWorkUnitShell } from './shell';
import { statusLabel } from './labels';
import { phrase, readWorkUnit, ready, record, terminal, type Scene, type WorkAction, type WorkState, type WorkUnit, type WorkUnitPorts } from './contracts';
type View = ReturnType<typeof createWorkUnitView>;
interface Entry {
  id: string; owner: number; unit: WorkUnit | null; views: Map<HTMLElement, View>;
  state: WorkState; loading: boolean; fingerprint: string;
  parent?: { id: string; operation: string };
}
export function createWorkUnitTracker(ports: WorkUnitPorts) {
  const doc = ports.document, win = doc.defaultView!;
  const entries = new Map<string, Entry>();
  let scope: LifecycleScope | null = null, observer: MutationObserver | null = null;
  let scanTimer = 0, pollTimer = 0, delay = 2000, destroyed = false;
  const valid = (entry: Entry) => !destroyed && entries.get(entry.id) === entry && ports.ownerId() === entry.owner
    && [...entry.views.keys()].some(card => card.isConnected);
  const draw = (entry: Entry) => { if (valid(entry) && entry.unit) for (const [card, view] of entry.views) if (card.isConnected) view.update(entry.unit, entry.state); };
  function stopWatching(): void {
    observer?.disconnect(); observer = null; scope?.destroy(); scope = null; scanTimer = 0; pollTimer = 0;
  }
  function prune(): void {
    for (const [id, entry] of entries) {
      for (const [card, view] of entry.views) if (!card.isConnected || ports.ownerId() !== entry.owner) { view.dispose(); entry.views.delete(card); }
      if (!entry.views.size) entries.delete(id);
    }
  }
  function schedule(): void {
    if (!scope || pollTimer || doc.hidden) return;
    if (![...entries.values()].some(entry => !entry.unit || !terminal(entry.unit))) return;
    pollTimer = scope.timeout(() => {
      pollTimer = 0; prune();
      if (!entries.size) { stopWatching(); return; }
      for (const entry of entries.values()) if (!entry.unit || !terminal(entry.unit)) void refresh(entry);
      schedule();
    }, delay);
  }
  async function refresh(entry: Entry): Promise<void> {
    if (!valid(entry) || entry.loading || doc.hidden) return;
    entry.loading = true;
    try {
      const snapshot = await ports.getSnapshot(entry.id);
      if (!valid(entry)) return;
      const unit = readWorkUnit(snapshot);
      if (!unit) throw new Error('WorkUnit snapshot unavailable');
      const previous = entry.unit, fingerprint = JSON.stringify(unit);
      delay = fingerprint === entry.fingerprint ? Math.min(5000, delay + 1500) : 2000;
      const needsDraw = fingerprint !== entry.fingerprint || entry.state.transportError;
      entry.fingerprint = fingerprint; entry.unit = unit; entry.state.transportError = false;
      // Drop only obsolete gate receipts; an accepted answer never manufactures a terminal task.
      for (const key of entry.state.operations.keys()) if (key.startsWith('gate:') && key !== 'gate:' + unit.pendingGate?.guidanceId) entry.state.operations.delete(key);
      if (needsDraw) draw(entry);
      if (terminal(unit)) {
        if (pollTimer && ![...entries.values()].some(item => !item.unit || !terminal(item.unit))) { scope?.cancelTimeout(pollTimer); pollTimer = 0; }
        if (previous && !terminal(previous)) ports.notify?.(statusLabel(unit, ports.text), unit.title || '');
        if (entry.parent) {
          const parent = entries.get(entry.parent.id);
          if (parent && valid(parent)) {
            parent.state.operations.delete(entry.parent.operation);
            if (parent.state.scenes) { parent.state.scenes = undefined; void loadScenes(parent); }
            void refresh(parent); draw(parent);
          }
          entry.parent = undefined;
        }
      }
    } catch {
      if (valid(entry)) {
        entry.state.transportError = true;
        if (entry.unit) draw(entry);
        else for (const card of entry.views.keys()) {
          const note = card.querySelector<HTMLElement>('.ptool-wu-activity');
          if (note) note.textContent = ports.text('workUnit.initialError', 'Progress is temporarily unavailable. Reconnecting…');
        }
      }
    } finally { entry.loading = false; if (valid(entry)) schedule(); }
  }
  async function loadScenes(entry: Entry): Promise<void> {
    if (!valid(entry) || !entry.unit || !ready(entry.unit) || entry.unit.kind !== 'motion-video' || entry.state.scenesLoading || entry.state.scenes) return;
    entry.state.scenesLoading = true; entry.state.scenesError = false; draw(entry);
    try {
      const result = record(await ports.scenes(entry.id));
      if (!valid(entry)) return;
      if (!Array.isArray(result?.scenes)) throw new Error('No scenes receipt');
      entry.state.scenes = result.scenes.filter(scene => typeof record(scene)?.scene_id === 'string').slice(0, 24) as Scene[];
    } catch { if (valid(entry)) entry.state.scenesError = true; }
    finally { entry.state.scenesLoading = false; draw(entry); }
  }
  async function action(entry: Entry, intent: WorkAction): Promise<void> {
    if (!valid(entry)) return;
    if (intent.name === 'refresh') { await refresh(entry); return; }
    if (!entry.unit) return;
    if (intent.name === 'scenes') { await loadScenes(entry); return; }
    if (intent.name === 'gate' && (terminal(entry.unit) || !intent.guidanceId || intent.guidanceId !== entry.unit.pendingGate?.guidanceId || !intent.response?.trim())) return;
    if (intent.name === 'abort' && (terminal(entry.unit) || !entry.unit.links?.abort)) return;
    if (intent.name === 'regenerate' && (!ready(entry.unit) || !entry.state.scenes?.some(scene => scene.scene_id === intent.sceneId))) return;
    const key = intent.name === 'gate' ? 'gate:' + intent.guidanceId : intent.name === 'regenerate' ? 'regenerate:' + intent.sceneId : 'abort';
    const prior = entry.state.operations.get(key);
    if (prior?.busy || prior?.accepted) return;
    const operation = { busy: true, accepted: false, error: '' }; entry.state.operations.set(key, operation); draw(entry);
    try {
      const result = record(await (intent.name === 'gate' ? ports.humanResponse(intent.guidanceId!, intent.response!)
        : intent.name === 'regenerate' ? ports.regenerate(entry.id, intent.sceneId!) : ports.abort(entry.id)));
      if (!valid(entry) || entry.state.operations.get(key) !== operation) return;
      if (!result || result.ok === false) throw new Error('Mutation did not return an accepted receipt');
      if (intent.name === 'regenerate') {
        const childId = typeof result.task_id === 'string' ? result.task_id : '';
        if (!childId || childId === entry.id) throw new Error('Missing revision task');
        const card = [...entry.views.keys()].find(node => node.isConnected);
        const host = card?.querySelector('.ptool-wu-revision-tasks');
        if (host) {
          host.insertAdjacentHTML('beforeend', renderWorkUnitShell(childId, 'motion-video',
            phrase(ports.text, 'workUnit.revisionTitle', 'Revising scene {scene}', { scene: intent.sceneId }), ports.text('workUnit.video', 'Video'), ports.text));
          scan(); const child = entries.get(childId); if (child) child.parent = { id: entry.id, operation: key };
        }
      }
      operation.accepted = true;
      void refresh(entry);
    } catch {
      if (valid(entry)) operation.error = ports.text('workUnit.actionError', 'The action was not confirmed. Your input is retained; check the progress and try again.');
    } finally { operation.busy = false; draw(entry); }
  }
  function ensureWatching(): void {
    if (scope || destroyed) return;
    scope = createLifecycleScope(win);
    observer = new win.MutationObserver(records => {
      // Token streaming and our own text/progress updates cannot create work.
      // Observe only card insertion/removal, including replacement parent subtrees.
      if (records.some(change => [...change.addedNodes, ...change.removedNodes].some(node =>
        node instanceof win.Element && (node.matches('.ptool-work-card') || node.querySelector('.ptool-work-card'))))) demand();
    });
    observer.observe(doc.body, { childList: true, subtree: true });
    scope.listen(doc, 'visibilitychange', () => {
      if (doc.hidden) { if (pollTimer) scope?.cancelTimeout(pollTimer); pollTimer = 0; }
      else { demand(); for (const entry of entries.values()) if (!entry.unit || !terminal(entry.unit)) void refresh(entry); }
    });
    scope.listen(win, 'tofu:language-change', () => { for (const entry of entries.values()) draw(entry); });
  }
  function scan(): void {
    if (destroyed) return;
    prune();
    const owner = ports.ownerId();
    if (owner !== null) for (const card of doc.querySelectorAll<HTMLElement>('.ptool-work-card[data-wu-task]')) {
      const id = card.dataset.wuTask;
      if (!id || !card.querySelector('.ptool-wu-live')) continue;
      let entry = entries.get(id);
      if (!entry) { entry = { id, owner, unit: null, state: { operations: new Map() }, views: new Map(), loading: false, fingerprint: '' }; entries.set(id, entry); }
      if (!entry.views.has(card)) {
        const owned = entry;
        const view = createWorkUnitView(card, id, ports, intent => { void action(owned, intent); }); entry.views.set(card, view);
        if (entry.unit) view.update(entry.unit, entry.state);
      }
      if (!entry.unit) void refresh(entry);
    }
    if (!entries.size) { stopWatching(); return; }
    ensureWatching(); schedule();
  }
  function demand(): void {
    if (destroyed || scanTimer) return;
    ensureWatching(); scanTimer = scope!.timeout(() => { scanTimer = 0; scan(); }, 50);
  }
  return {
    demand,
    redraw(): void { for (const entry of entries.values()) draw(entry); },
    snapshot() { return { tasks: entries.size, views: [...entries.values()].reduce((n, entry) => n + entry.views.size, 0),
      requests: [...entries.values()].filter(entry => entry.loading).length, polling: Boolean(pollTimer), observing: Boolean(observer) }; },
    dispose(): void { destroyed = true; stopWatching(); for (const entry of entries.values()) for (const view of entry.views.values()) view.dispose(); entries.clear(); },
  };
}
