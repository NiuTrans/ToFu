/** Demand-scoped time presentation reads. Run output is authoritative; this
 * controller owns only disposable display data and cancels reads on navigation.
 * Entry: createTaskTimePresentations. Ports: authenticated read, repaint, error.
 */
import type { AutomationManagementPresentation, AutomationManagementPresentationPage } from '../../api/automation.generated';

export function createTaskTimePresentations(ports: {
  load(conversationId: string, attemptId: string, signal: AbortSignal): Promise<AutomationManagementPresentationPage>;
  changed(): void;
  failed?(error: unknown): void;
}) {
  let conversationId = '';
  let lifetime = new AbortController();
  const values = new Map<string, ReadonlyArray<AutomationManagementPresentation>>();
  const pending = new Map<string, { refresh: boolean; abort: AbortController }>();
  const readAt = new Map<string, number>();

  const read = (attemptId: string): void => {
    const visit = lifetime;
    const target = conversationId;
    const state = { refresh: false, abort: new AbortController() };
    pending.set(attemptId, state);
    void ports.load(target, attemptId, state.abort.signal).then((page) => {
      if (visit.signal.aborted || state.abort.signal.aborted || conversationId !== target) return;
      if (page.items.some((item) => item.conversationId !== target || item.attemptId !== attemptId)) {
        throw new Error('Task presentation escaped its owned target');
      }
      const previous = values.get(attemptId);
      if (JSON.stringify(previous ?? []) !== JSON.stringify(page.items)) {
        values.set(attemptId, page.items);
        ports.changed();
      }
    }).catch((error: unknown) => {
      if (!visit.signal.aborted && !state.abort.signal.aborted) ports.failed?.(error);
    }).finally(() => {
      if (visit !== lifetime || pending.get(attemptId) !== state) return;
      pending.delete(attemptId);
      readAt.set(attemptId, performance.now());
      if (state.refresh && !visit.signal.aborted) read(attemptId);
    });
  };

  return {
    values,
    refresh(target: string, attempts: ReadonlyArray<string>, force = false) {
      if (conversationId !== target) {
        lifetime.abort();
        for (const state of pending.values()) state.abort.abort();
        lifetime = new AbortController();
        conversationId = target;
        values.clear(); pending.clear(); readAt.clear();
      }
      const wanted = new Set(attempts);
      for (const [id, state] of pending) {
        if (!wanted.has(id)) { state.abort.abort(); pending.delete(id); }
      }
      for (const id of values.keys()) if (!wanted.has(id)) values.delete(id);
      for (const id of readAt.keys()) if (!wanted.has(id)) readAt.delete(id);
      for (const id of wanted) {
        const existing = pending.get(id);
        if (existing) { if (force) existing.refresh = true; continue; }
        if (force || !readAt.has(id)) read(id);
      }
    },
    dispose() {
      lifetime.abort(); conversationId = '';
      for (const state of pending.values()) state.abort.abort();
      values.clear(); pending.clear(); readAt.clear();
    },
  };
}
