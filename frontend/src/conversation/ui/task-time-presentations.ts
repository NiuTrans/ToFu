/** Read-only time records from accepted automation outputs; plain text only.
 * Entry: appendTaskTimePresentations. No clock, transport or Turn mutation.
 */
import type { ConversationTurnViewModel } from '../presentation/conversation-view-model';

export function appendTaskTimePresentations(node: HTMLElement, turn: ConversationTurnViewModel): void {
  for (const item of turn.metadata.taskTimePresentations ?? []) {
    if (item.attemptId !== turn.attemptId || item.turnId !== turn.turnId) continue;
    const record = node.ownerDocument.createElement('span');
    record.className = 'automation-task-start-record';
    record.dataset.automationPresentationId = item.id;
    record.setAttribute('role', 'note');
    record.textContent = item.output.text;
    node.appendChild(record);
  }
}
