/** Typed, defensive projection of optional server revision metadata; no client verdicts. */
export type ResearchCycleRow = Record<string, unknown>;
export interface ResearchCycle extends ResearchCycleRow {
  status: string;
  revision_count: number | null;
  history: ResearchCycleRow[];
  issues: ResearchCycleRow[];
  next_action: string;
}
export function researchText(value: unknown): string {
  return typeof value === 'string' ? value.trim() : '';
}
export function researchNumber(value: unknown): number | null {
  if (typeof value !== 'number' && typeof value !== 'string') return null;
  if (typeof value === 'string' && !value.trim()) return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}
function rows(value: unknown): ResearchCycleRow[] {
  return Array.isArray(value) ? value.filter((item): item is ResearchCycleRow =>
    !!item && typeof item === 'object' && !Array.isArray(item)) : [];
}
export function projectResearchCycle(value: unknown): ResearchCycle | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const row = value as ResearchCycleRow;
  const count = researchNumber(row.revision_count);
  return { ...row, status: researchText(row.status),
    revision_count: count !== null && Number.isInteger(count) && count >= 0 ? count : null,
    history: rows(row.history), issues: rows(row.issues), next_action: researchText(row.next_action) };
}
