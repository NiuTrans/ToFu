/** Display-only context taxonomy shared by typed and retained views.
 * The injection registry owns known reasons; explicit unknown context kinds
 * still fold, but unknown interventions never do. No durable bytes change.
 */
import { SYSTEM_NOTE_DISPLAY_POLICY } from '../ui/system-note-presentation.generated';

import { injectionReceiptDisplaysFullPayload } from './tool-injection-presentation';

function noteRecord(value: unknown): Readonly<Record<string, unknown>> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Readonly<Record<string, unknown>> : {};
}

export function contextNotePolicy(value: unknown) {
  const note = noteRecord(value);
  const reason = String(note.reason || note.injectionId || '');
  return Object.prototype.hasOwnProperty.call(SYSTEM_NOTE_DISPLAY_POLICY, reason)
    ? SYSTEM_NOTE_DISPLAY_POLICY[reason] : undefined;
}

export function contextNoteDisplayKind(value: unknown): string {
  const note = noteRecord(value);
  if ((note.action === undefined || note.action === 'prompt')
      && note.noteKind === 'system-intervention') {
    return contextNotePolicy(note)?.noteKind ?? 'system-intervention';
  }
  return String(note.noteKind || 'system-intervention');
}

export function isContextInjectionNote(value: unknown): boolean {
  const note = noteRecord(value);
  if (note.action !== undefined && note.action !== 'prompt') return false;
  const policy = contextNotePolicy(note);
  if (policy && !policy.groupContext) return false;
  const kind = contextNoteDisplayKind(note);
  return kind === 'context-injection' || kind === 'session-context';
}

/** Adjacent notes from different consuming rounds/retries are not one commit. */
export function sameContextNoteScope(left: unknown, right: unknown): boolean {
  const a = noteRecord(left), b = noteRecord(right);
  if (typeof a.llmRound !== 'number' || !Number.isInteger(a.llmRound)
      || a.llmRound < 0) return false;
  return a.llmRound === b.llmRound
    && String(a.attemptId || '') === String(b.attemptId || '')
    && String(a.taskId || '') === String(b.taskId || '');
}

/** A dedicated receipt can replace a historical inbox note only with full
 * evidence, never because some other inbox item exists in this Turn. Sidecar
 * rounds are one-based; notes carry the zero-based producing model round.
 * Missing scope on just one side, partial previews and normalized/prefix text
 * are not proof. Both scopes absent remains a legacy round+exact-bytes case.
 * This is display-only: delivery/ACK and the canonical projection are untouched.
 */
export function isDuplicatedInboxDeliveryNote(
  value: unknown, inboxReceipts: readonly unknown[], peerReceipts: readonly unknown[] = [],
): boolean {
  const note = noteRecord(value);
  if (note.reason !== 'inbox_delivery'
      || (note.action !== undefined && note.action !== 'prompt')
      || typeof note.text !== 'string' || !note.text) return false;
  const payloads: string[] = [];
  const receipts = [
    ...inboxReceipts.map(item => ({ item, lane: 'inbox' as const })),
    ...peerReceipts.map(item => ({ item, lane: 'peer' as const })),
  ];
  for (const { item, lane } of receipts) {
    const receipt = noteRecord(item);
    if (typeof receipt.round !== 'number' || !Number.isInteger(receipt.round) || receipt.round < 1
        || !sameContextNoteScope(note, {
          llmRound: receipt.round - 1,
          taskId: receipt.taskId ?? receipt._taskId,
          attemptId: receipt.attemptId ?? receipt._attemptId,
        })) continue;
    const previews = receipt.previews;
    if (!Array.isArray(previews) || !injectionReceiptDisplaysFullPayload(receipt, lane)) continue;
    payloads.push(previews.map(value => noteRecord(value).text as string).join('\n\n'));
  }
  // Legacy inbox-lanes could append one carrier for completion + peer receipts.
  return payloads.includes(note.text)
    || (payloads.length > 1 && payloads.join('\n\n') === note.text);
}
