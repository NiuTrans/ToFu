/** Effect uncertainty is independent of the tool verdict and Turn completion.
 * Entry: toolEffectNotice. Reads only server-owned effect metadata and the
 * containing Turn lifecycle; no execution, replay decision or local state.
 */
import type { ToolExecutionEffect, TurnStatus } from '../../api/conversation-sync.generated';

export type ToolEffectNotice = 'reconcile' | 'idempotent' | null;

export function toolEffectNotice(effect: ToolExecutionEffect | undefined, turnStatus: TurnStatus, roundStatus?: string):
  ToolEffectNotice {
  if (!effect || effect.recovery === 'read_only') return null;
  const uncertain = (Boolean(effect.inspection) && roundStatus === 'error' && effect.state !== 'not_started')
    || effect.state === 'unknown'
    || (effect.state === 'entered' && turnStatus !== 'pending' && turnStatus !== 'running');
  if (!uncertain) return null;
  return effect.recovery === 'idempotent' ? 'idempotent' : 'reconcile';
}
