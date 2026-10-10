/** One active read plus the latest requested page; settlement releases all data.
 * Entry: createElementReader. Dependencies: the existing owner-scoped API port.
 */
import type { PageVisit, SlideElement, SlidesApi } from './contracts';

export function createElementReader(
  isCurrent: (visit: PageVisit) => boolean,
  receive: (visit: PageVisit, elements: SlideElement[]) => void,
) {
  let active = false;
  let pending: { api: SlidesApi; visit: PageVisit } | null = null;
  async function drain(api: SlidesApi, visit: PageVisit): Promise<void> {
    active = true;
    try {
      let result;
      try {
        result = await api.pageElements(visit.taskId, visit.page);
      } catch {
        // The read port can reject on transport failure; the current sidebar stays empty.
        return;
      }
      if (isCurrent(visit) && Array.isArray(result?.elements)) receive(visit, result.elements);
    } finally {
      active = false;
      const next = pending;
      pending = null;
      if (next && isCurrent(next.visit)) void drain(next.api, next.visit);
    }
  }
  return {
    load(api: SlidesApi, visit: PageVisit): void {
      if (active) pending = { api, visit };
      else void drain(api, visit);
    },
    clear(): void { pending = null; },
  };
}
