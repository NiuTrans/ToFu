/** Unions canonical assignment evidence for the dock; never writes overlays.
 * Retained recovery/render ports are deleted when canonical swarm cards migrate.
 * Activity without a unique owner cannot borrow another assignment's tools.
 */
import type { AgentDockItem } from './agent-sidebar-dock';
import { mergeSwarmAgent, swarmStatusAgents, normalizeSwarmAgent, swarmRoundExplicitRunId, swarmAgentWaitLabel, swarmFrameMatchesExecution, swarmFrameExecutionTaskId } from './conversation/presentation/swarm-agent-presentation';
type Row = Record<string, any>;
interface Ports {
  turns: Row[];
  swarms: Row[];
  recover(round: Row, rounds: Row[]): Row[];
  renderPanel(round: Row, rounds: Row[]): string;
  renderActivity(agent: Row): HTMLElement;
  openConversation(id: string): void;
  /** Resolves true once the stop signal is accepted, false when nothing was left to stop. */
  stopAgent(swarmId: string, agentId: string): Promise<boolean>;
  translate(key: string, fallback: string, vars?: Record<string, string | number>): string;
}
interface Assignment {
  scope: string;
  runId: string;
  turnId: string;
  producerTask: string;
  agent: Row;
  round: Row;
  rounds: Row[];
  ambiguous?: boolean;
  // Durable settlement outranks a provisional end marker, which outranks live state.
  authority: number;
  live?: Activity;
}
interface Activity { frame: Row; agent: Row; scope: string; runId: string; producerTask: string }
/** Stop needs the live registry's word: its exact run lists the agent running now.
 * Canonical Turn evidence alone never grants it, and a settled verdict withholds it. */
function liveStop(agent: Row, live: Activity | undefined, ports: Ports): (() => Promise<boolean>) | undefined {
  const run = String(live?.frame.swarm_id || '');
  if (!run || !agent.id || agent.status !== 'running' || live!.agent.status !== 'running') return undefined;
  return () => ports.stopAgent(run, String(agent.id));
}
function statusLabel(agent: Row, tt: Ports['translate']): string {
  const phase = ({ pending: 'queued', done: 'complete', completed: 'complete', unknown: 'noResult', canceled: 'aborted', cancelled: 'aborted' } as Record<string, string>)[agent.status] || agent.status;
  // A state word only; the round count is its own row field.
  return swarmAgentWaitLabel(agent, tt) || tt('swarm.phase.' + phase, phase);
}
/** Live progress, status polls and durable snapshots all carry rounds as `round`. */
function roundCount(agent: Row): number | undefined {
  const rounds = Number(agent.round);
  return rounds > 0 ? rounds : undefined;
}
function assignmentScope(turn: Row, round: Row, index: number, agent: Row, run: string): string {
  // Array position is not assignment identity. Without run evidence retain
  // launch occurrence scope: a reused agent/tool ID alone cannot prove a union.
  const scope = run ? ['run', run] : ['launch', round.taskId, round.toolCallId,
    round.roundNum != null ? ['round', round.roundNum] : ['index', index]];
  return JSON.stringify([turn.turnId, round.attemptId, scope, agent.id]);
}
function activityScope(frame: Row, index: number, agent: Row, run: string): string {
  const scope = run ? ['run', run] : frame.swarm_key
    ? ['session', frame.swarm_key, frame.created_at] : ['unbound', index];
  return JSON.stringify(['activity', frame.turnId, frame.attemptId, scope, agent.id]);
}
function appendIndex<T>(index: Map<string, T[]>, key: string, value: T): void {
  const entries = index.get(key);
  if (entries) entries.push(value); else index.set(key, [value]);
}
const agentScopeKey = (agent: Row, scope: unknown): string => JSON.stringify([agent.id, scope]);
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
function matchesActivityScope({ frame, agent, runId, producerTask }: Activity, candidate: Assignment, allowMissingProducer = false): boolean {
  return candidate.agent.id === agent.id && (!frame.turnId || frame.turnId === candidate.turnId)
    && swarmFrameMatchesExecution(frame, { ...candidate.round,
      taskId: candidate.producerTask || (allowMissingProducer ? producerTask : '') })
    && (runId ? candidate.runId === runId
      : Boolean(frame.swarm_key) && candidate.round._swarmKey === frame.swarm_key && !candidate.authority
        && !['done', 'completed', 'failed', 'error', 'aborted', 'cancelled', 'canceled', 'interrupted'].includes(candidate.agent.status));
}
function resolveAgentEvidence(ports: Pick<Ports, 'turns' | 'swarms' | 'recover'>) {
  // These indexes expire with this projection. They narrow candidates only;
  // execution/attempt stamps and ambiguity remain owned by the predicates below.
  const observedRuns = new Map<string, Activity[]>(), observedSessions = new Map<string, Activity[]>();
  const activityPeers = new Map<string, Activity[]>();
  const observations: Activity[] = [];
  for (const [index, frame] of ports.swarms.entries()) {
    const runId = String(frame.swarm_id || frame.swarm_run_id || '');
    const producerTask = swarmFrameExecutionTaskId(frame);
    for (const raw of frame.agents || []) {
      const agent = normalizeSwarmAgent(raw);
      const scope = activityScope(frame, index, agent, runId);
      const activity = { frame, agent, scope, runId, producerTask };
      observations.push(activity);
      appendIndex(activityPeers, scope, activity);
      if (runId) appendIndex(observedRuns, agentScopeKey(agent, runId), activity);
      else if (frame.swarm_key) appendIndex(observedSessions, agentScopeKey(agent, frame.swarm_key), activity);
    }
  }
  const evidence = new Map<string, Assignment[]>();
  for (const turn of ports.turns) {
    const rounds: Row[] = turn.projection?.toolRounds || [];
    rounds.forEach((round, index) => {
      if (!round || !(round._swarm || round.toolName === 'spawn_agents' || round._swarmSnapshot)) return;
      // Launch-card merging keys by agent ID. Preserve raw worker stamps until
      // partitioning so reused IDs cannot inherit another execution's evidence.
      const agents = swarmStatusAgents(round, [...ports.recover(round, rounds),
        ...(round._swarmSnapshot?.settled ? [] : round._swarmAgents || [])]);
      const runId = swarmRoundExplicitRunId(round);
      const authority = round._swarmSnapshot?.settled ? 2 : round._swarmEndTime ? 1 : 0;
      for (const recorded of agents) {
        const agent = normalizeSwarmAgent(recorded);
        const scope = assignmentScope(turn, round, index, agent, runId);
        const copies = evidence.get(scope) || [];
        copies.push({ scope, runId, turnId: turn.turnId, producerTask: String(round.taskId || ''), agent, round, rounds, authority });
        evidence.set(scope, copies);
      }
    });
  }

  const assignments: Assignment[] = [];
  for (const copies of evidence.values()) {
    // A missing producer/worker stamp joins one known execution, never chooses
    // between two. Keys always include known stamps, even when only one exists.
    const related = new Set<Activity>();
    for (const copy of copies) {
      for (const item of observedRuns.get(agentScopeKey(copy.agent, copy.runId)) || []) related.add(item);
      for (const item of observedSessions.get(agentScopeKey(copy.agent, copy.round._swarmKey)) || []) related.add(item);
    }
    // Set order only contributes uniqueness evidence. Merging below still walks
    // the original observations, so run/session bucket order cannot pick a winner.
    const activity = [...related].filter(item => copies.some(copy => matchesActivityScope(item, copy, true)));
    const producerTasks = activity.map(item => item.producerTask);
    const producerCount = new Set([...copies.map(copy => copy.producerTask), ...producerTasks].filter(Boolean)).size;
    for (const [producerTask, siblings] of partitionByTask(copies, copy => copy.producerTask, producerTasks,
      Boolean(copies[0].runId))) {
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

  const assignedRuns = new Map<string, Assignment[]>(), assignedSessions = new Map<string, Assignment[]>();
  for (const owner of assignments) {
    if (owner.runId) appendIndex(assignedRuns, agentScopeKey(owner.agent, owner.runId), owner);
    if (owner.round._swarmKey) appendIndex(assignedSessions, agentScopeKey(owner.agent, owner.round._swarmKey), owner);
  }

  // Missing stamps in activity are subject to the same ambiguity rule as
  // canonical copies. Include evidence already owned by another row so that
  // consuming a stamped frame cannot make an ambiguous remainder look unique.
  function activityTaskEvidence(activity: Activity, producerTask = activity.producerTask) {
    const candidates = activity.runId
      ? assignedRuns.get(agentScopeKey(activity.agent, activity.runId))
      : assignedSessions.get(agentScopeKey(activity.agent, activity.frame.swarm_key));
    const canonical = (candidates || []).filter(candidate => matchesActivityScope(activity, candidate, true));
    const peers = activityPeers.get(activity.scope) || [];
    const producerTasks = [...canonical.map(candidate => candidate.producerTask),
      ...peers.map(other => other.producerTask)];
    const workerTasks = [
      ...canonical.filter(candidate => !producerTask || !candidate.producerTask || producerTask === candidate.producerTask)
        .map(candidate => String(candidate.agent.executionTaskId || '')),
      ...peers.filter(other => !producerTask || !other.producerTask
        || producerTask === other.producerTask).map(other => String(other.agent.executionTaskId || '')),
    ];
    return { canonical, producerTasks, workerTasks };
  }

  // Resolve the complete bipartite evidence set before hydrating any worker.
  // Greedily taking the first activity row lets array order choose which of
  // two executions receives an unstamped launch's objective and tool records.
  const bindings = observations.map(activity => {
    const { agent } = activity;
    const { canonical, producerTasks, workerTasks } = activityTaskEvidence(activity);
    const ambiguous = (!activity.producerTask && new Set(producerTasks.filter(Boolean)).size > 1)
      || (!agent.executionTaskId && new Set(workerTasks.filter(Boolean)).size > 1);
    let candidates = canonical.filter(candidate => !ambiguous && !candidate.ambiguous && matchesActivityScope(activity, candidate)
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
      owner.live = activity;
    } else {
      const scope = activity.scope;
      const copies = activityOnly.get(scope) ?? []; copies.push(activity); activityOnly.set(scope, copies);
    }
  }

  const unassigned: { identity: { scope: string; producerTask: string; workerTask: string }; activity: Activity }[] = [];
  for (const [scope, copies] of activityOnly) {
    const producerTasks = copies.flatMap(copy => activityTaskEvidence(copy).producerTasks);
    for (const [producerTask, siblings] of partitionByTask(copies, copy => copy.producerTask, producerTasks)) {
      const workerTasks = siblings.flatMap(copy => activityTaskEvidence(copy, producerTask).workerTasks);
      for (const [workerTask, duplicates] of partitionByTask(siblings, copy => String(copy.agent.executionTaskId || ''), workerTasks)) {
        const agent = duplicates.slice(1).reduce((base, copy) => mergeSwarmAgent(base, copy.agent), duplicates[0].agent);
        unassigned.push({ identity: { scope, producerTask, workerTask }, activity: { ...duplicates[0], agent } });
      }
    }
  }
  return { assignments, unassigned };
}

export function agentDockProjection(ports: Ports): AgentDockItem[] {
  // Deferred render closures cannot retain the resolver's temporary indexes.
  const { assignments, unassigned } = resolveAgentEvidence(ports);
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
    const stop = liveStop(agent, owner.live, ports);
    result.push({
      key: JSON.stringify([identity.scope, identity.producerTask, identity.workerTask]), identity,
      name: agent.name || agent.id || agent.role || 'general',
      status: agent.status, statusLabel: statusLabel(agent, ports.translate), roundCount: roundCount(agent),
      objective: agent.objective, agentId: agent.id, swarmRunId: owner.runId || undefined,
      revision: JSON.stringify([source, agent]),
      ...(childId ? { openConversation: () => ports.openConversation(childId) } : {}),
      ...(stop ? { stop } : {}),
      async render() {
        const fragment = document.createElement('template');
        fragment.innerHTML = ports.renderPanel(source, rounds);
        const card = Array.from(fragment.content.querySelectorAll<HTMLElement>('.sw-agent'))
          .find(node => node.dataset.agentId === String(agent.id));
        return card?.querySelector('.sw-a-body')?.outerHTML || '';
      },
    });
  }
  for (const { identity, activity: live } of unassigned) {
    const { agent } = live;
    const stop = liveStop(agent, live, ports);
    result.push({
      key: JSON.stringify([identity.scope, identity.producerTask, identity.workerTask]), identity,
      name: agent.name || agent.id || agent.role || 'general',
      status: agent.status, statusLabel: statusLabel(agent, ports.translate), roundCount: roundCount(agent),
      objective: agent.objective, agentId: agent.id,
      swarmRunId: live.runId || undefined,
      revision: JSON.stringify(agent),
      ...(stop ? { stop } : {}),
      async render() {
        const activity = ports.renderActivity(agent);
        // Serialized HTML cannot retain factory listeners. Keep evidence
        // readable without exposing inert copy controls.
        for (const button of activity.querySelectorAll('button')) {
          const code = activity.ownerDocument.createElement('code');
          code.textContent = button.textContent;
          button.replaceWith(code);
        }
        return activity.outerHTML;
      },
    });
  }
  return result;
}
