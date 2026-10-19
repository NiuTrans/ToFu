/** Responsibility: bounded, content-free pre-handler interaction evidence.
 * Entry: createBrowserInteractionContext; dependencies: injected document/clock.
 * Retains one semantic label and optional conversation ID, never DOM, link URLs,
 * text, key values, arguments or transcripts. Disposal releases both listeners.
 */
export function createBrowserInteractionContext(
  document: Pick<Document, 'addEventListener' | 'removeEventListener'>,
  now: () => number,
): { context(startMs: number, endMs?: number): Record<string, unknown>; dispose(): void } {
  let disposed = false;
  let latest: { at: number; kind: string; conversationId: string } | null = null;
  const receive = (event: Event): void => {
    if (disposed || (event.type === 'keydown'
      && !['Enter', ' '].includes((event as KeyboardEvent).key))) return;
    const target = event.target as Element | null;
    if (!target || typeof target.closest !== 'function') return;
    const conversation = target.closest<HTMLElement>('.conv-item[data-conv-id]');
    const kind = conversation ? 'conversation-switch' : target.closest('a[href]') ? 'link'
      : target.closest('summary, [aria-expanded]') ? 'disclosure' : 'control';
    latest = { at: now(), kind, conversationId: conversation?.dataset.convId?.slice(0, 64) ?? '' };
  };
  document.addEventListener('click', receive, true);
  document.addEventListener('keydown', receive, true);
  return {
    context(startMs, endMs = startMs) {
      const age = latest ? startMs - latest.at : -1;
      // An animation frame/task can begin just before our capturing listener.
      // Keep activations inside that measured interval, with a signed age;
      // interactions delivered after its end must never be attached.
      if (!latest || latest.at > endMs || age > 30_000) return {};
      return { interaction: latest.kind, interactionAgeMs: Math.round(age),
        ...(latest.conversationId ? { interactionConversationId: latest.conversationId } : {}) };
    },
    dispose() {
      if (disposed) return;
      disposed = true;
      latest = null;
      document.removeEventListener('click', receive, true);
      document.removeEventListener('keydown', receive, true);
    },
  };
}
