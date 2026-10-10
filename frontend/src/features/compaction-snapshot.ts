/** Read-only compaction-result presentation. Metadata, never prose, identifies producers. */
type Message = Record<string, any>;

function producerKind(message: Message, summaryKind: string): string {
  const handoff = message.role === 'user' && message._isMeta === true
    && message._compactionHandoff?.version === 'tofu.compaction-handoff/v1'
    && ['pre_turn', 'mid_turn'].includes(message._compactionHandoff?.phase)
    && typeof message.content === 'string';
  if (handoff) return summaryKind;
  if (message.role === 'assistant' && message._isCompactionSummary === true) return 'summary';
  if (message._isMeta === true && message._retainedUserHistoryVersion === 2
      && Array.isArray(message._retainedUserHistory)) return 'history';
  if (message._isMeta === true) return 'runtime';
  return 'unknown';
}

/** One render-local pass; no persistent cache or repeated per-row batch scans. */
export function compactionMessageKinds(messages: Message[], archive: Message = {}): string[] {
  const summaryKind = archive.receipt?.implementation === 'deterministic_recovery_receipt'
    ? 'recovery' : 'summary';
  const kinds = messages.map(message => producerKind(message || {}, summaryKind));
  for (let start = 0; start < messages.length; start++) {
    const message = messages[start];
    const calls = message?.tool_calls;
    if (message?.role !== 'assistant' || !Array.isArray(calls) || !calls.length) continue;
    let end = start + 1;
    while (end < messages.length && messages[end]?.role === 'tool') end++;
    // Display agrees with the backend legacy boundary: a whole synthetic
    // batch, not one matching call inside an open or mixed execution run.
    let complete = calls.length === end - start - 1;
    const counts = new Map<string, number>();
    if (complete) {
      for (const call of calls) {
        const id = typeof call?.id === 'string' ? call.id.trim() : '';
        if (!id || call?.function?.name !== 'context_compact') {
          complete = false;
          break;
        }
        counts.set(id, (counts.get(id) || 0) + 1);
      }
    }
    for (let index = start + 1; complete && index < end; index++) {
      const rawId = messages[index].tool_call_id;
      const id = typeof rawId === 'string' ? rawId.trim() : '';
      const remaining = counts.get(id) || 0;
      if (!remaining) complete = false;
      else counts.set(id, remaining - 1);
    }
    if (complete) {
      for (let index = start + 1; index < end; index++) kinds[index] = summaryKind;
    }
    start = end - 1;
  }
  return kinds;
}

export function compactionPostExport(archive: unknown, messages: unknown, postSnapshot: unknown) {
  return {
    archive, view: 'post',
    postSnapshot: postSnapshot ?? {
      stage: 'compaction_result', status: Array.isArray(messages) ? 'captured' : 'unavailable',
      includesRuntimeRecomposition: false, includesProviderAdaptation: false,
    },
    postMessages: Array.isArray(messages) ? messages : null,
  };
}

/** Scroll within the existing chronological list, without reordering its rows. */
export function jumpToCompactionSummary(container: HTMLElement): void {
  const target = container.querySelector<HTMLElement>('[data-compaction-kind="summary"], [data-compaction-kind="recovery"]');
  if (!target) return;
  target.tabIndex = -1;
  target.focus({ preventScroll: true });
  target.scrollIntoView?.({ block: 'nearest', behavior: 'auto' });
}
