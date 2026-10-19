/** Resolve project links through catalog metadata, never synthesize a transcript.
 * Entry: resolveMissing; dependencies supplied by the retained navigation bridge.
 */
export function createProjectConversationNavigation() {
  let generation = 0;
  return {
    invalidate() { generation++; },

    checkpoint() { const own = generation; return () => own === generation; },

    prepareDraft(text: string, ports: {
      hasDraft(): boolean;
      startDraft(): void;
      writeText(text: string): void;
    }): boolean {
      if (ports.hasDraft()) return false;
      generation++;
      ports.startDraft();
      ports.writeText(text);
      return true;
    },
    resolveMissing<Row extends { id: string }>(id: string, ports: {
      contains(id: string): boolean;
      fetch(id: string): Promise<Row | null>;
      merge(row: Row): void;
      navigate(id: string): void;
      error(error: unknown): void;
    }): Promise<void> | null {
      const own = ++generation;
      if (ports.contains(id)) return null;
      return ports.fetch(id).then(row => {
        if (own !== generation) return;
        if (!row || row.id !== id) throw new Error('Conversation not found');
        ports.merge(row);
        if (!ports.contains(id)) throw new Error('Conversation unavailable');
        ports.navigate(id);
      }).catch(error => { if (own === generation) ports.error(error); });
    },
  };
}
