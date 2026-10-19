/** Browser send admission during observed boot/restart; server gates remain authoritative.
 * Entry points: setBootSendBlocked, isBootSendBlocked, subscribeBootReadiness.
 * Two lifecycle owners, no timer, persistence, transport or task state.
 */
type BootOwner = 'boot' | 'restart';
const blockedOwners = new Set<BootOwner>();
const listeners = new Set<() => void>();
export function isBootSendBlocked(): boolean { return blockedOwners.size > 0; }
export function setBootSendBlocked(owner: BootOwner, blocked: boolean): void {
  const before = isBootSendBlocked();
  if (blocked) blockedOwners.add(owner); else blockedOwners.delete(owner);
  if (before !== isBootSendBlocked()) for (const listener of listeners) listener();
}
export function subscribeBootReadiness(listener: () => void): () => void {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}
