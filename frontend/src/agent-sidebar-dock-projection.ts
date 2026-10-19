/** Unions canonical assignment evidence for the dock; never writes overlays.
 * Retained recovery/render ports are deleted when canonical swarm cards migrate.
 * Activity without a unique owner cannot borrow another assignment's tools.
 */
import type { AgentDockItem } from './agent-sidebar-dock';
import { mergeSwarmAgent, swarmStatusAgents, normalizeSwarmAgent, swarmRoundExplicitRunId, swarmAgentWaitLabel, swarmFrameMatchesExecution, swarmFrameExecutionTaskId } from './conversation/presentation/swarm-agent-presentation';
import { escapeHtml } from './html-safety';
type Row = Record<string, any>;
interface Ports {
  turns: Row[];
  swarms: Row[];
  recover(round: Row, rounds: Row[]): Row[];
  renderPanel(round: Row, rounds: Row[]): string;
  renderActivity(agent: Row): HTMLElement;
  openConversation(id: string): void;
  translate(key: string, fallback: string, vars?: Record<string, string | number>): string;
}
interface Assignment {
  scope: string;
  turnId: string;
  producerTask: string;
  agent: Row;
  round: Row;
  rounds: Row[];
  ambiguous?: boolean;
  // Durable settlement outranks a provisional end marker, which outranks live state.
  authority: number;
}
interface Activity { frame: Row; index: number; agent: Row }
function statusLabel(agent: Row, tt: Ports['translate']): string {
  const phase = ({ pending: 'queued', done: 'complete', completed: 'complete', unknown: 'noResult', canceled: 'aborted', cancelled: 'aborted' } as Record<string, string>)[agent.status] || agent.status;
  let label = swarmAgentWaitLabel(agent, tt) || tt('swarm.phase.' + phase, phase);
  if (agent.status === 'running' && Number(agent.round) > 0) label += ' · ' + tt('bgActivity.agentRound', 'round {round}', { round: agent.round });
  return label;
}
function assignmentScope(turn: Row, round: Row, index: number, agent: Row): string {
  const run = swarmRoundExplicitRunId(round);
  // Array position is not assignment identity. Without run evidence retain
  // launch occurrence scope: a reused agent/tool ID alone cannot prove a union.
  const scope = run ? ['run', run] : ['launch', round.taskId, round.toolCallId,
    round.roundNum != null ? ['round', round.roundNum] : ['index', index]];
  return JSON.stringify([turn.turnId, round.attemptId, scope, agent.id]);
}
function activityScope({ frame, index, agent }: Activity): string {
  const run = String(frame.swarm_id || frame.swarm_run_id || '');
  const scope = run ? ['run', run] : frame.swarm_key
    ? ['session', frame.swarm_key, frame.created_at] : ['unbound', index];
  return JSON.stringify(['activity', frame.turnId, frame.attemptId, scope, agent.id]);
}
function partitionByTask<T>(entries: T[], taskOf: (entry: T) => string, otherTasks: string[] = [], allowExternalOnly = false): Map<string, T[]> {
  const ownTasks = new Set(entries.map(taskOf).filter(Boolean));
  const tasks = new Set([...ownTasks, ...otherTasks].filter(Boolean));
  const uniqueTask = tasks.size === 1 && (ownTasks.size === 1 || allowExternalOnly) ? [...tasks][0] : '';
  const groups = new Map<string, T[]>();
  for (const entry of entries) {
    const key = taskOf(entry) || uniqueTask;
    const group = groups.get(key) ?? []; group.push(entry); groups.set(key, group);
  }
  return groups;
}
function mergeAgentEvidence(base: Row, next: Row, settled?: Row): Row {
  const merged = mergeSwarmAgent(base, next);
  // Live merging protects running over unknown placeholders. A settled unknown
  // is an authoritative verdict instead, including after driver death.
  if (settled) { merged.status = settled.status; merged.phase = settled.phase || settled.status; }
  return merged;
}
/** Compare assignment scope before considering individual worker stamps. */
function matchesActivityScope({ frame, agent }: Activity, candidate: Assignment, allowMissingProducer = false): boolean {
  const run = String(frame.swarm_id || frame.swarm_run_id || '');
  return candidate.agent.id === agent.id && (!frame.turnId || frame.turnId === candidate.turnId)
    && swarmFrameMatchesExecution(frame, { ...candidate.round,
      taskId: candidate.producerTask || (allowMissingProducer ? swarmFrameExecutionTaskId(frame) : '') })
    && (run ? swarmRoundExplicitRunId(candidate.round) === run
      : Boolean(frame.swarm_key) && candidate.round._swarmKey === frame.swarm_key && !candidate.authority
        && !['done', 'completed', 'failed', 'error', 'aborted', 'cancelled', 'canceled', 'interrupted'].includes(candidate.agent.status));
}
export function agentDockProjection(ports: Ports): AgentDockItem[] {
  const observations: Activity[] = ports.swarms.flatMap((frame, index) =>
    (frame.agents || []).map((raw: Row) => ({ frame, index, agent: normalizeSwarmAgent(raw) })));
  const evidence = new Map<string, Assignment[]>();
  for (const turn of ports.turns) {
    const rounds: Row[] = turn.projection?.toolRounds || [];
    rounds.forEach((round, index) => {
      if (!round || !(round._swarm || round.toolName === 'spawn_agents' || round._swarmSnapshot)) return;
      // Launch-card merging keys by agent ID. Preserve raw worker stamps until
      // partitioning so reused IDs cannot inherit another execution's evidence.
      const agents = swarmStatusAgents(round, [...ports.recover(round, rounds),
        ...(round._swarmSnapshot?.settled ? [] : round._swarmAgents || [])]);
      const authority = round._swarmSnapshot?.settled ? 2 : round._swarmEndTime ? 1 : 0;
      for (const recorded of agents) {
        const agent = normalizeSwarmAgent(recorded);
        const scope = assignmentScope(turn, round, index, agent);
        const copies = evidence.get(scope) || [];
        copies.push({ scope, turnId: turn.turnId, producerTask: String(round.taskId || ''), agent, round, rounds, authority });
        evidence.set(scope, copies);
      }
    });
  }

  const assignments: Assignment[] = [];
  for (const copies of evidence.values()) {
    // A missing producer/worker stamp joins one known execution, never chooses
    // between two. Keys always include known stamps, even when only one exists.
    const activity = observations.filter(item => copies.some(copy => matchesActivityScope(item, copy, true)));
    const producerTasks = activity.map(item => swarmFrameExecutionTaskId(item.frame));
    const producerCount = new Set([...copies.map(copy => copy.producerTask), ...producerTasks].filter(Boolean)).size;
    for (const [producerTask, siblings] of partitionByTask(copies, copy => copy.producerTask, producerTasks,
      Boolean(swarmRoundExplicitRunId(copies[0].round)))) {
      const workerTasks = activity.filter(item => siblings.some(copy => matchesActivityScope(item, { ...copy, producerTask }, true)))
        .map(item => String(item.agent.executionTaskId || ''));
      const workerCount = new Set([...siblings.map(copy => String(copy.agent.executionTaskId || '')), ...workerTasks].filter(Boolean)).size;
      for (const [workerTask, duplicates] of partitionByTask(siblings, copy => String(copy.agent.executionTaskId || ''), workerTasks)) {
        const first = duplicates[0];
        const owner = { ...first, producerTask,
          ambiguous: (!producerTask && producerCount > 1) || (!workerTask && workerCount > 1) };
        for (const copy of duplicates.slice(1)) {
          const preferPrevious = owner.authority >= copy.authority;
          const authority = Math.max(owner.authority, copy.authority);
          const verdict = authority ? (preferPrevious ? owner.agent : copy.agent) : undefined;
          owner.agent = preferPrevious && authority ? mergeAgentEvidence(copy.agent, owner.agent, verdict)
            : mergeAgentEvidence(owner.agent, copy.agent, verdict);
          if (!preferPrevious) Object.assign(owner, {
            round: copy.round, rounds: copy.rounds, authority,
          });
        }
        assignments.push(owner);
      }
    }
  }

  // Missing stamps in activity are subject to the same ambiguity rule as
  // canonical copies. Include evidence already owned by another row so that
  // consuming a stamped frame cannot make an ambiguous remainder look unique.
  function activityTaskEvidence(activity: Activity, producerTask = swarmFrameExecutionTaskId(activity.frame)) {
    const canonical = assignments.filter(candidate => matchesActivityScope(activity, candidate, true));
    const peers = observations.filter(other => activityScope(other) === activityScope(activity));
    const producerTasks = [...canonical.map(candidate => candidate.producerTask),
      ...peers.map(other => swarmFrameExecutionTaskId(other.frame))];
    const workerTasks = [
      ...canonical.filter(candidate => !producerTask || !candidate.producerTask || producerTask === candidate.producerTask)
        .map(candidate => String(candidate.agent.executionTaskId || '')),
      ...peers.filter(other => !producerTask || !swarmFrameExecutionTaskId(other.frame)
        || producerTask === swarmFrameExecutionTaskId(other.frame)).map(other => String(other.agent.executionTaskId || '')),
    ];
    return { producerTasks, workerTasks };
  }

  // Resolve the complete bipartite evidence set before hydrating any worker.
  // Greedily taking the first activity row lets array order choose which of
  // two executions receives an unstamped launch's objective and tool records.
  const bindings = observations.map(activity => {
    const { agent } = activity;
    const { producerTasks, workerTasks } = activityTaskEvidence(activity);
    const ambiguous = (!swarmFrameExecutionTaskId(activity.frame) && new Set(producerTasks.filter(Boolean)).size > 1)
      || (!agent.executionTaskId && new Set(workerTasks.filter(Boolean)).size > 1);
    let candidates = assignments.filter(candidate => !ambiguous && !candidate.ambiguous && matchesActivityScope(activity, candidate)
      && (!agent.executionTaskId || !candidate.agent.executionTaskId
        || agent.executionTaskId === candidate.agent.executionTaskId));
    if (agent.executionTaskId) {
      const exact = candidates.filter(candidate => candidate.agent.executionTaskId === agent.executionTaskId);
      if (exact.length) candidates = exact;
    }
    return { activity, owner: candidates.length === 1 ? candidates[0] : undefined };
  });
  const workersByOwner = new Map<Assignment, Set<string>>();
  for (const { activity, owner } of bindings) if (owner && activity.agent.executionTaskId) {
    const workers = workersByOwner.get(owner) ?? new Set<string>();
    workers.add(String(activity.agent.executionTaskId)); workersByOwner.set(owner, workers);
  }
  const activityOnly = new Map<string, Activity[]>();
  for (const { activity, owner } of bindings) {
    if (owner && (workersByOwner.get(owner)?.size ?? 0) <= 1) {
      owner.agent = owner.authority ? mergeAgentEvidence(activity.agent, owner.agent, owner.agent)
        : mergeAgentEvidence(owner.agent, activity.agent);
    } else {
      const scope = activityScope(activity);
      const copies = activityOnly.get(scope) ?? []; copies.push(activity); activityOnly.set(scope, copies);
    }
  }

  const result: AgentDockItem[] = [];
  for (const owner of assignments) {
    const { agent, round, rounds } = owner;
    const identity = { scope: owner.scope, producerTask: owner.producerTask, workerTask: String(agent.executionTaskId || '') };
    // Render only this execution, preserving evidence from canonical duplicates
    // and its unambiguous live activity without mutating a historical tool row.
    // The local rendering view also fences the panel's agent-ID merger from
    // sibling workers in the original launch/snapshot. This is never persisted.
    const source: Row = { ...round, _swarmAgents: [agent],
      _swarmSnapshot: { ...round._swarmSnapshot, agents: [agent] } };
    const childId = agent.conversationId || agent.conversation_id;
    result.push({
      key: JSON.stringify([identity.scope, identity.producerTask, identity.workerTask]), identity,
      name: agent.name || agent.id || agent.role || 'general',
      status: agent.status, statusLabel: statusLabel(agent, ports.translate), objective: agent.objective,
      agentId: agent.id, swarmRunId: swarmRoundExplicitRunId(round) || undefined,
      revision: JSON.stringify([source, agent]),
      ...(childId ? { openConversation: () => ports.openConversation(childId) } : {}),
      async render() {
        const fragment = document.createElement('template');
        fragment.innerHTML = ports.renderPanel(source, rounds);
        const card = Array.from(fragment.content.querySelectorAll<HTMLElement>('.sw-agent'))
          .find(node => node.dataset.agentId === String(agent.id));
        const body = card?.querySelector('.sw-a-body');
        const note = ports.translate('agentDock.boundedEvidence', 'Recent execution records. Omitted calls and truncated previews are disclosed below; this is not a full transcript.');
        const empty = ports.translate('agentDock.noRecords', 'Execution records are not yet available in this conversation.');
        return `<p class="agent-dock-evidence-note">${escapeHtml(note)}</p>${body?.outerHTML || `<p>${escapeHtml(empty)}</p>`}`;
      },
    });
  }
  for (const [scope, copies] of activityOnly) {
    const producerTasks = copies.flatMap(copy => activityTaskEvidence(copy).producerTasks);
    for (const [producerTask, siblings] of partitionByTask(copies, copy => swarmFrameExecutionTaskId(copy.frame), producerTasks)) {
      const workerTasks = siblings.flatMap(copy => activityTaskEvidence(copy, producerTask).workerTasks);
      for (const [workerTask, duplicates] of partitionByTask(siblings, copy => String(copy.agent.executionTaskId || ''), workerTasks)) {
        const agent = duplicates.slice(1).reduce((base, copy) => mergeSwarmAgent(base, copy.agent), duplicates[0].agent);
        const identity = { scope, producerTask, workerTask };
        const frame = duplicates[0].frame;
        result.push({
          key: JSON.stringify([scope, producerTask, workerTask]), identity,
          name: agent.name || agent.id || agent.role || 'general',
          status: agent.status, statusLabel: statusLabel(agent, ports.translate), objective: agent.objective,
          agentId: agent.id, swarmRunId: String(frame.swarm_id || frame.swarm_run_id || '') || undefined,
          revision: JSON.stringify(agent),
          async render() {
            const note = ports.translate('agentDock.noRecords', 'Execution records are not yet available in this conversation.');
            const activity = ports.renderActivity(agent);
            // Serialized HTML cannot retain factory listeners. Keep evidence
            // readable without exposing inert copy controls.
            for (const button of activity.querySelectorAll('button')) {
              const code = activity.ownerDocument.createElement('code');
              code.textContent = button.textContent;
              button.replaceWith(code);
            }
            return `${activity.outerHTML}<p>${escapeHtml(note)}</p>`;
          },
        });
      }
    }
  }
  return result;
}
