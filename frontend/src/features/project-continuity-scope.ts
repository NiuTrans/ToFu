/** Drawer-local scope fencing. No timers, scheduler or model selection.
 * Read epochs suppress out-of-order replies; writes recheck coordinator binding.
 */
import type { CoordinationBinding } from '../api/project-coordination.generated';
export interface ContinuityScope {
  path: string; conversationId: string; binding: CoordinationBinding;
  valid(): boolean;
  readBinding(path: string): Promise<CoordinationBinding>;
}
export const sameProjectPath = (a: string, b: string) => a.replace(/[/\\]+$/, '') === b.replace(/[/\\]+$/, '');
export function ownsContinuityScope(scope: ContinuityScope): boolean {
  return scope.valid() && scope.binding.availability === 'available'
    && scope.binding.conversationId === scope.conversationId
    && sameProjectPath(scope.binding.projectPath, scope.path);
}
export async function assertContinuityScope(scope: ContinuityScope): Promise<void> {
  if (!ownsContinuityScope(scope)) throw new Error('projectDispatch.stale');
  const binding = await scope.readBinding(scope.path);
  if (!ownsContinuityScope(scope) || binding.availability !== 'available'
    || binding.conversationId !== scope.conversationId || binding.revision !== scope.binding.revision
    || !sameProjectPath(binding.projectPath, scope.path)) throw new Error('projectDispatch.stale');
}
export function createContinuityReadFence(valid: () => boolean) {
  let epoch = 0;
  return {
    begin: () => ++epoch,
    accepts: (value: number) => value === epoch && valid(),
    invalidate: () => { epoch++; },
  };
}
/** A failed write may have committed. Retain its exact body/key for retry. */
export interface RetainedCommand<T> { body: T; key: string; busy: boolean; }
export function sameCommandScope(command: { projectPath: string; coordinatorId: string }, scope: ContinuityScope) {
  return sameProjectPath(command.projectPath, scope.path) && command.coordinatorId === scope.conversationId;
}
