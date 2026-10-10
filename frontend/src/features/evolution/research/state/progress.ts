/** The machine-readable progress contract owns validation and enums. */
import { isResearchProgressV1, type ResearchProgressV1 } from '../../../../api/research-progress.generated';
export function projectResearchProgress(value: unknown): ResearchProgressV1 | null {
  return isResearchProgressV1(value) ? { ...value, counts: { ...value.counts } } : null;
}
