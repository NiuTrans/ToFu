/** Reader-owned deferral for optional enrichment while text is selected.
 * Entry points: selectionIntersects, createDeferredEnrichment.
 * Dependencies: DOM only. At most one selection listener per renderer document;
 * no timers, polling or observer. Empty/disposed queues release their document.
 */
export function selectionIntersects(element: Element): boolean {
  const selection = element.ownerDocument.getSelection();
  if (!selection || selection.isCollapsed) return false;
  for (let index = 0; index < selection.rangeCount; index++) {
    if (selection.getRangeAt(index).intersectsNode(element)) return true;
  }
  return false;
}

export function createDeferredEnrichment(reportFailure: (error: unknown) => void) {
  const jobs = new Set<() => boolean>();
  let document: Document | null = null;
  const attempt = (job: () => boolean): boolean => {
    try { return job(); } catch (error) { reportFailure(error); return true; }
  };
  function clear(): void {
    document?.removeEventListener('selectionchange', flush);
    document = null; jobs.clear();
  }
  function flush(): void {
    for (const job of jobs) if (attempt(job)) jobs.delete(job);
    if (!jobs.size) clear();
  }
  return {
    run(ownerDocument: Document, job: () => boolean): void {
      if (document && document !== ownerDocument) clear();
      if (attempt(job)) return;
      jobs.add(job);
      if (!document) {
        document = ownerDocument;
        document.addEventListener('selectionchange', flush);
      }
    },
    clear,
  };
}
