/** Only explicit paper navigation loads the reader; no research task authority here. */
import { featureRegistry } from '../../../../feature-registry';
async function ready(): Promise<void> {
  await import('../../../paper');
  await import('../../../paper/panel-owners');
}
export async function openResearchReader(arxivId?: string): Promise<void> {
  await ready();
  const reader = featureRegistry as unknown as {
    enterPaperMode?: () => Promise<void>;
    _fetchArxivPaper?: (id: string) => Promise<unknown>;
  };
  await reader.enterPaperMode?.();
  if (arxivId) await reader._fetchArxivPaper?.(arxivId);
}
