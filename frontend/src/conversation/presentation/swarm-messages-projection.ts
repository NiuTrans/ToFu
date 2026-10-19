/** Run-level communication view. Reads existing snapshot/receipt projections only;
 * no storage, transport, timers or inferred delivery state. */
import { mergeMessageReceipt, sameMessageReceipt } from './agent-message-receipts';
import { swarmRoundExplicitRunId } from './swarm-agent-presentation';
import type { SwarmMessagesSnapshot, SwarmMessageReceipt } from '../ui/swarm-messages';
type Row = Record<string, any>;
export function swarmMessagesProjection(turns: readonly Row[], swarms: readonly Row[] = []): SwarmMessagesSnapshot[] {
  const runs = new Map<string, SwarmMessagesSnapshot>();
  function add(run: string, snapshot: Row, overlay: Row[] = []) {
    if (!run) return;
    const previous = runs.get(run);
    const receipts = new Map<string, SwarmMessageReceipt>();
    for (const receipt of [...(previous?.messageReceipts || []), ...(snapshot.messageReceipts || []),
      ...(snapshot.agents || []).flatMap((agent: Row) => agent.messageReceipts || []), ...overlay]) {
      if (!sameMessageReceipt(receipt, receipt) || receipt.swarmRunId !== run) continue;
      const key = JSON.stringify([run, receipt.agentId, receipt.messageId]);
      receipts.set(key, mergeMessageReceipt(receipts.get(key) || {}, receipt) as SwarmMessageReceipt);
    }
    const participants = new Map((previous?.messageParticipants || []).map(person => [JSON.stringify([person.id, person.taskId]), person]));
    for (const person of snapshot.messageParticipants || []) participants.set(JSON.stringify([person.id, person.taskId]), person);
    runs.set(run, { swarm_id: run, messageReceipts: [...receipts.values()],
      messageReceiptsAvailable: snapshot.messageReceiptsAvailable ?? previous?.messageReceiptsAvailable,
      messageParticipants: [...participants.values()] });
  }
  for (const turn of turns) for (const round of turn.projection?.toolRounds || []) {
    if (!round._swarm && round.toolName !== 'spawn_agents' && !round._swarmSnapshot) continue;
    add(swarmRoundExplicitRunId(round), round._swarmSnapshot || { agents: round._swarmAgents || [] }, [...(round._swarmMessageReceipts || []), ...(round._swarmAgents || []).flatMap((agent: Row) => agent.messageReceipts || [])]);
  }
  for (const swarm of swarms) add(String(swarm.swarm_id || swarm.swarm_run_id || ''), swarm);
  return [...runs.values()];
}
