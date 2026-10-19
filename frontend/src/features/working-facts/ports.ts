/** Owner-scoped working facts ports; DTOs are generated from the server schema. */
import type { WorkingFactsView, WorkingFactOwnerDecision, WorkingFactConsumersView } from '../../api/working-facts.generated';

export interface WorkingFactsPorts {
  load(conversationId: string, signal: AbortSignal): Promise<WorkingFactsView>;
  save(conversationId: string, command: WorkingFactOwnerDecision, signal: AbortSignal): Promise<WorkingFactsView>;
  consumers(conversationId: string, cursor: string, signal: AbortSignal): Promise<WorkingFactConsumersView>;
}
export type Text = (key: string, fallback: string) => string;
export interface Draft { text: string; note: string; edited?: boolean }
