/** Checklist display metadata; accepted revisions remain owned by core. Entry points below; dependencies are explicit imports and ports. */
import type { I18nKey } from "../../../i18n";
export type ChecklistText = (key: I18nKey, fallback: string | Record<string, number>) => string;
export interface ChecklistItem { status?: string; content?: unknown }
export interface ChecklistMeta {
  todos?: (ChecklistItem | null)[]; todoDisplayUpdateCount?: unknown; todoUpdateCount?: unknown;
  todoRejected?: unknown; todoNoop?: unknown; todoRejectReason?: unknown; todoExplanation?: unknown;
  todoAutoPopped?: unknown[];
  todoBreadcrumbs?: { label?: string; checklist_id?: string }[];
  todoDisplayHistory?: { done?: unknown; total?: unknown; revision?: unknown; operation?: string }[];
}
export interface ChecklistRound {
  roundNum?: string | number; results?: ChecklistMeta[]; todos?: (ChecklistItem | null)[];
  todoDisplayUpdateCount?: unknown; toolContent?: unknown;
}
