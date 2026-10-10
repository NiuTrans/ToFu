/** Display-only context taxonomy shared by typed and retained views.
 * The injection registry owns known reasons; explicit unknown context kinds
 * still fold, but unknown interventions never do. No durable bytes change.
 */
import { SYSTEM_NOTE_DISPLAY_POLICY } from '../ui/system-note-presentation.generated';

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
