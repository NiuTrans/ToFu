/** Background chip/sidebar presentation: snapshot counts and safe, copyable agent rows.
 * No transport, timers, durable state or canonical agent-address rewriting.
 */
import { normalizeSwarmAgent, swarmAgentLogId, swarmAgentWaitLabel } from './conversation/presentation/swarm-agent-presentation';

import { swarmOutcomeCounts } from './conversation/presentation/swarm-failure-presentation';
type Row = Record<string, any>;
type Translate = (key: string, fallback: string, vars?: Record<string, string | number>) => string;

function swarmCounts(swarm: Row) {
  return swarmOutcomeCounts(Array.isArray(swarm.agents) ? swarm.agents : []);
}

export function backgroundActivityCounts(swarms: readonly Row[]) {
  const counts = swarms.map(swarmCounts);
  const agentsRunning = counts.reduce((sum, swarm) => sum + swarm.running, 0);
  const agentsQueued = counts.reduce((sum, swarm) => sum + swarm.pending, 0);
  return { agentsRunning, agentsQueued, agentsActive: agentsRunning + agentsQueued };
}

export interface BackgroundActivitySnapshot {
  swarms?: Row[];
  backgroundCommands?: Row[];
}

export function summarizeBackgroundActivity(data: BackgroundActivitySnapshot, conversationId?: string | null) {
  const belongs = (item: Row) => conversationId === undefined
    || (!!conversationId && item.conv_id === conversationId);
  const swarms = (Array.isArray(data?.swarms) ? data.swarms : []).filter(belongs);
  const commands = (Array.isArray(data?.backgroundCommands) ? data.backgroundCommands : []).filter(belongs);
  return { swarms, commands, ...backgroundActivityCounts(swarms), commandsActive: commands.length };
}
type ActivitySummary = ReturnType<typeof summarizeBackgroundActivity>;

/** A derived index of this snapshot only, never a historical conversation registry. */
export function backgroundActivityByConversation(data: BackgroundActivitySnapshot): Map<string, ActivitySummary> {
  const groups = new Map<string, BackgroundActivitySnapshot>();
  for (const field of ['swarms', 'backgroundCommands'] as const) {
    for (const item of (Array.isArray(data?.[field]) ? data[field]! : [])) {
      if (typeof item.conv_id !== 'string' || !item.conv_id) continue;
      let group = groups.get(item.conv_id);
      if (!group) { group = { swarms: [], backgroundCommands: [] }; groups.set(item.conv_id, group); }
      group[field]!.push(item);
    }
  }
  const index = new Map<string, ActivitySummary>();
  for (const [id, group] of groups) {
    const summary = summarizeBackgroundActivity(group);
    if (summary.agentsActive || summary.commandsActive) index.set(id, summary);
  }
  return index;
}

function activitySignature(summary?: ActivitySummary | null): string {
  return summary ? `${summary.agentsRunning}/${summary.agentsQueued}/${summary.commandsActive}` : '';
}

export function changedBackgroundActivityConversations(
  previous: ReadonlyMap<string, ActivitySummary>, next: ReadonlyMap<string, ActivitySummary>,
): string[] {
  return [...new Set([...previous.keys(), ...next.keys()])]
    .filter(id => activitySignature(previous.get(id)) !== activitySignature(next.get(id)));
}

/** Shared detailed label for the composer chip and sidebar tooltip. */
export function backgroundActivityLabel(summary: ActivitySummary, tt: Translate): string {
  const parts: string[] = [];
  const labels = [
    [summary.agentsRunning, 'bgActivity.chip.agentOne', 'bgActivity.chip.agentMany', '{count} sub-agent running', '{count} sub-agents running'],
    [summary.agentsQueued, 'bgActivity.chip.agentQueuedOne', 'bgActivity.chip.agentQueuedMany', '{count} sub-agent waiting', '{count} sub-agents waiting'],
    [summary.commandsActive, 'bgActivity.chip.cmdOne', 'bgActivity.chip.cmdMany', '{count} background command running', '{count} background commands running'],
  ] as const;
  for (const [count, one, many, fallbackOne, fallbackMany] of labels) {
    if (count > 0) parts.push(tt(count === 1 ? one : many, count === 1 ? fallbackOne : fallbackMany, { count }));
  }
  return parts.join(' · ');
}

export function sidebarBackgroundActivity(summary: ActivitySummary | null | undefined, tt: Translate) {
  if (!summary || !(summary.agentsActive + summary.commandsActive)) return null;
  const queuedOnly = summary.agentsRunning + summary.commandsActive === 0;
  return {
    signature: activitySignature(summary), queuedOnly,
    label: queuedOnly
      ? tt('sidebar.backgroundWaitingTag', 'Background queued · {count}', { count: summary.agentsActive })
      : tt('sidebar.backgroundRunningTag', 'Background · {count}', { count: summary.agentsActive + summary.commandsActive }),
    title: backgroundActivityLabel(summary, tt),
  };
}

/** Headers and rows project the same roster, including older mixed snapshots. */
export function backgroundActivitySummary(swarm: Row, tt: Translate): string {
  const counts = swarmCounts(swarm);
  const labels = [
    ['running', 'bgActivity.summaryRunning', '{count} running'],
    ['pending', 'bgActivity.summaryWaiting', '{count} waiting'],
    ['succeeded', 'swarm.summary.done', '{count} succeeded'],
    ['failed', 'swarm.summary.failed', '{count} failed'],
    ['cancelled', 'swarm.summary.aborted', '{count} cancelled'],

    ['interrupted', 'swarm.summary.interrupted', '{count} interrupted'],
  ] as const;
  return labels.filter(([status]) => counts[status] > 0)
    .map(([status, key, fallback]) => tt(key, fallback, { count: counts[status] }))
    .join(' · ');
}

export function renderBackgroundActivityAgent(document: Document, input: Row, tt: Translate): HTMLElement {
  const agent = normalizeSwarmAgent(input);
  const row = document.createElement('div');
  row.className = 'bg-act-agent';
  row.dataset.status = agent.status || 'pending';
  row.dataset.agentId = agent.id;
  const role = document.createElement('span');
  role.className = 'bg-act-agent-role';
  role.textContent = agent.role || 'general';
  row.appendChild(role);
  const status = document.createElement('span');
  status.className = 'bg-act-agent-status';
  const phase = ({ pending: 'queued', waiting: 'queued', done: 'complete', completed: 'complete',
    unknown: 'noResult', canceled: 'aborted', cancelled: 'aborted',
  } as Record<string, string>)[agent.status] || agent.status || 'noResult';
  status.textContent = swarmAgentWaitLabel(agent, tt) || tt('swarm.phase.' + phase, phase);
  if (agent.status === 'pending' && Array.isArray(agent.dependsOn) && agent.dependsOn.length) {
    status.title = agent.dependsOn.join(', ');
  }
  if (agent.status === 'running' && Number(agent.round) > 0) {
    status.textContent += ' · ' + tt('bgActivity.agentRound', 'round {round}', { round: agent.round });
  }
  row.appendChild(status);
  if (agent.id) {
    const id = document.createElement('button');
    id.type = 'button';
    id.className = 'bg-act-agent-id';
    const logId = swarmAgentLogId(agent);
    id.textContent = logId;
    id.title = tt('bgActivity.copyLogId', 'Copy log ID: {id}', { id: logId });
    id.setAttribute('aria-label', id.title);
    id.addEventListener('click', async event => {
      event.stopPropagation();
      try {
        const clipboard = document.defaultView?.navigator.clipboard;
        if (!clipboard) throw new Error('Clipboard unavailable');
        await clipboard.writeText(logId);
        if (!id.isConnected) return;
        id.title = tt('timerBlock.idCopied', 'Copied!');
        id.setAttribute('aria-label', id.title);
      } catch {
        if (!id.isConnected) return;
        id.title = tt('convLifecycle.copyFailed', 'Copy failed');
        id.setAttribute('aria-label', id.title);
      }
    });
    row.appendChild(id);
  }
  const objective = document.createElement('span');
  objective.className = 'bg-act-agent-objective';
  objective.textContent = agent.objective || agent.id || '';
  row.appendChild(objective);
  return row;
}
