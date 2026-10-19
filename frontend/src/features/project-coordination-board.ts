/** Project execution graph and decision cards. The graph helper owns bounded
 * evidence projection/layout; this view adds accessible navigation, not execution.
 */
import { createButton } from '../ui';
import { createCoordinationWorkList, type CoordinationConversation } from './project-coordination-work-list';
import { projectCoordinationGraph,
  type CoordinationGraphEdge, type CoordinationGraphNode, type CoordinationGraphOptions, type CoordinationGraphStatus } from './project-coordination-graph';
import { t } from '../i18n';
import { createCoordinationCanvas, type CoordinationCanvasState } from './project-coordination-canvas';
import type { CoordinationWork } from './project-coordination';
import type { ProjectDispatchView } from '../api/project-dispatch.generated';

export interface CoordinationRenderHooks {
  /** Produces the one-line reading of a task node (e.g. an LLM-organized
   * digest). The default shows the recorded result summary instead. */
  summarizeTask?(node: CoordinationGraphNode): string;
  /** Attention items shown above the graph; rendered separately by the owner. */
  pendingDecisions?: number | null;
  /** The embedding surface already labels the section (e.g. a fold summary). */
  heading?: boolean;
  /** View-only preferences owned by the mounted project surface. */
  viewState?: CoordinationCanvasState;

  conversations?: readonly CoordinationConversation[];
  inspect?(id: string): void;
  manage?(): void;
}

interface CoordinationDecision { id: string; title: string; kind: 'dependency' | 'review' | 'delivery'; }

export function coordinationDecisions(assignments: readonly ProjectDispatchView[]) {
  return assignments.filter(item => !item.revoked && item.request.profile?.mode !== 'coordinator').flatMap<CoordinationDecision>(item => {
    const execution = item.execution;
    const reviewed = item.review && Number.isSafeInteger(execution?.resultRevision)
      && execution!.resultRevision! > 0 && item.review.resultRevision === execution?.resultRevision;
    const delivered = item.canonicalDelivery === true && item.delivery?.status === 'delivered';
    const kind = item.dependencyStates?.some(dependency => dependency.status !== 'ready') ? 'dependency'
      : execution?.status === 'completed' && (!reviewed || item.review?.decision === 'rework') ? 'review'
      : execution?.status === 'completed' && reviewed && item.review?.decision === 'accept'
        && (item.request.profile?.mode === 'files' || item.request.profile?.mode === 'project') && !delivered ? 'delivery' : null;
    return kind ? [{ id: item.assignmentId, title: item.request.brief, kind }] : [];
  }).slice(0, 128);
}

export function createCoordinationDecisions(decisions: readonly CoordinationDecision[], manage: () => void): HTMLElement {
  const section = document.createElement('details'); section.className = 'coordination-decision-list';
  section.dataset.coordinationDisclosure = 'decisions';
  if (!decisions.length) { section.hidden = true; return section; }
  section.append(coordinationText(`${t('projectCoordination.decisions')} · ${decisions.length}`, 'summary'));
  for (const item of decisions) {
    const card = document.createElement('article'); card.className = 'ui-card ui-stack ui-stack--tight';
    card.append(coordinationText(item.title, 'strong'), coordinationText(t(`projectCoordination.decisionAction.${item.kind}`)));
    const action = createButton({ label: t('projectCoordination.controls'), size: 'sm', onClick: manage });
    action.dataset.coordinationFocus = JSON.stringify(['decision', item.id]);
    card.append(action);
    section.append(card);
  }
  return section;
}

export const coordinationText = (content: string, tag = 'p'): HTMLElement => {
  const node = document.createElement(tag); node.textContent = content; return node;
};

export function projectCoordinationWorkGroups(active: readonly CoordinationWork[], recent: readonly CoordinationWork[]) {
  return {
    active: active.filter(item => item.status === 'active'),
    recent: recent.filter(item => ['completed', 'failed', 'cancelled'].includes(item.status)),
  };
}

type GraphLabel = 'title' | 'coordinatorRole' | 'reference' | 'revoked' | 'deliveryConfirmed' | 'deliveryUnconfirmed'
  | 'dependencyUnknown' | 'missingConversation' | 'progressUnknown' | 'assignmentCoverage' | 'workCoverage'
  | 'assignmentsUnavailable' | 'bounded' | 'empty' | 'viewport' | 'details'
  | `status.${CoordinationGraphStatus}` | `dependency.${NonNullable<CoordinationGraphEdge['status']>}`;
const graphText = (key: GraphLabel) => t(`projectCoordination.graph.${key}`);

/** Extract the human-readable line from a serialized result/error envelope
 * (`{'kind':'server_busy',...,'message':'...'}`) so the list shows the cause,
 * not the JSON envelope. Falls back to the raw text when it is not an envelope. */
function envelopeMessage(value: string): string {
  const text = (value || '').trim();
  if (!text.startsWith('{') || !text.includes('"message"')) return text;
  try {
    const parsed = JSON.parse(text) as { message?: unknown };
    if (parsed && typeof parsed.message === 'string' && parsed.message.trim()) return parsed.message.trim();
  } catch { /* not clean JSON; fall through to a truncation-tolerant pull */ }
  // The stored summary is often truncated, so strict JSON.parse fails. Pull the
  // message field directly; tolerate an unterminated tail from the excerpt cut.
  const match = /"message"\s*:\s*"((?:[^"\\]|\\.)*)/.exec(text);
  if (!match) return text;
  const raw = match[1].replace(/\\n/g, ' ').replace(/\\"/g, '"').replace(/\\\\/g, '\\');
  const message = raw.replace(/\s+/g, ' ').trim();
  return message || text;
}

/** Deterministic digest used until an LLM-backed summarizer is wired in: the
 * latest recorded result summary for tasks, otherwise the role/status line. */
export function coordinationTaskDigest(node: CoordinationGraphNode): string {
  if (node.kind !== 'task' || !node.summary) return '';
  return envelopeMessage(node.summary).replace(/\s+/g, ' ').trim();
}

/** Normal chat stays primary. A bounded work list provides direct actions;
 * topology is built only when its native disclosure is opened. */
export function createCoordinationBoard(
  groups: ReturnType<typeof projectCoordinationWorkGroups>, navigate: (id: string) => void, pendingDecisions: number | null = 0,
  options: CoordinationGraphOptions = {}, hooks: CoordinationRenderHooks = { pendingDecisions },
): HTMLElement {
  const graph = projectCoordinationGraph(groups, options);
  const root = document.createElement('section'); root.className = 'coordination-task-graph';
  root.setAttribute('aria-label', graphText('title')); root.dataset.graphSource = graph.source;
  const state = hooks.viewState ?? {};
  if (hooks.heading !== false) root.append(coordinationText(t('projectCoordination.workList'), 'h3'));
  if (graph.assignmentsUnavailable) root.append(coordinationText(graphText('assignmentsUnavailable'), 'small'));
  if (graph.truncated) root.append(coordinationText(graphText('bounded'), 'small'));
  root.append(createCoordinationWorkList(graph, navigate, { state, conversations: hooks.conversations,
    digest: hooks.summarizeTask || coordinationTaskDigest, inspect: hooks.inspect, manage: hooks.manage }));
  const topology = document.createElement('details'); topology.className = 'coordination-topology';
  topology.dataset.coordinationDisclosure = 'topology'; topology.open = !!state.graphOpen;
  const summary = coordinationText(t('projectCoordination.dependencyGraph'), 'summary');
  summary.dataset.coordinationFocus = 'topology'; topology.append(summary);
  let mounted = false;
  const mount = () => {
    state.graphOpen = topology.open;
    if (!topology.open || mounted) return;
    mounted = true;
    topology.append(createCoordinationCanvas(graph, navigate, {
      state, digest: hooks.summarizeTask || coordinationTaskDigest,
    }));
  };
  topology.addEventListener('toggle', mount); mount();
  root.append(topology);
  const coverage = document.createElement('details'); coverage.className = 'coordination-graph-coverage';
  const title = document.createElement('summary'); title.textContent = t('projectCoordination.graph.coverage');
  coverage.append(title, coordinationText(graphText(graph.source === 'assignments' ? 'assignmentCoverage' : 'workCoverage'), 'small'));
  if (graph.total !== null) coverage.append(coordinationText(t('projectCoordination.graph.progress', { completed: graph.completed, total: graph.total }), 'small'));
  if (graph.delivered !== null) coverage.append(coordinationText(t('projectCoordination.graph.delivery', { count: graph.delivered }), 'small'));
  root.append(coverage);
  return root;
}
