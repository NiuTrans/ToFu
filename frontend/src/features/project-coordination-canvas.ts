/** Interactive project topology, over the existing bounded graph projection.
 * Owns only selection, history disclosure and zoom. No fetches, timers, observers
 * or execution state; the embedding surface retains preferences across pushes.
 */
import { createButton } from '../ui';
import { t } from '../i18n';
import { layoutCoordinationGraph, COORDINATION_GRAPH_GEOMETRY,
  type CoordinationGraph, type CoordinationGraphNode } from './project-coordination-graph';

export interface CoordinationCanvasState { selectedId?: string; showHistory?: boolean; zoom?: number; }
const archived = (node: CoordinationGraphNode) => node.status === 'completed' || node.status === 'cancelled';
const text = (value: string, tag = 'span', className = '') => {
  const el = document.createElement(tag); el.textContent = value; el.className = className; return el;
};
const titleOf = (node: CoordinationGraphNode) => node.kind === 'coordinator' ? t('projectCoordination.coordinator') : node.title;
const statusOf = (node: CoordinationGraphNode) => node.kind === 'coordinator' ? t('projectCoordination.graph.coordinatorRole')
  : node.kind === 'reference' ? t('projectCoordination.graph.reference') : t(`projectCoordination.graph.status.${node.status || 'unknown'}`);

/** Keep ancestors of current work, even completed ones: hiding them would erase
 * the dependency that explains a blocked node. Never manufacture dispatch edges
 * for historical Board records whose source is not recorded. */
export function visibleCoordinationGraph(graph: CoordinationGraph, state: CoordinationCanvasState): CoordinationGraph {
  if (state.showHistory) return graph;
  const visible = new Set(graph.nodes.filter(node => node.kind === 'coordinator' || node.id === state.selectedId || !archived(node)).map(node => node.id));
  for (let pass = 0; pass < graph.nodes.length; pass++) {
    let changed = false;
    for (const edge of graph.edges) {
      if (visible.has(edge.to) && !visible.has(edge.from)) { visible.add(edge.from); changed = true; }
    }
    if (!changed) break;
  }
  return { ...graph, nodes: graph.nodes.filter(node => visible.has(node.id)),
    edges: graph.edges.filter(edge => visible.has(edge.from) && visible.has(edge.to)) };
}

export function createCoordinationCanvas(graph: CoordinationGraph, navigate: (id: string) => void, ports: {
  state: CoordinationCanvasState; digest(node: CoordinationGraphNode): string;
}): HTMLElement {
  const { state, digest } = ports;
  const root = document.createElement('div'); root.className = 'coordination-canvas';
  const toolbar = document.createElement('div'); toolbar.className = 'coordination-canvas-toolbar';
  toolbar.setAttribute('role', 'group'); toolbar.setAttribute('aria-label', t('projectCoordination.graph.controls'));
  const historyCount = graph.nodes.filter(archived).length;
  const history = createButton({ label: '', size: 'sm', variant: 'ghost', onClick: () => {
    state.showHistory = !state.showHistory;
    if (!state.showHistory && graph.nodes.some(node => node.id === state.selectedId && archived(node))) state.selectedId = undefined;
    render();
  } });
  history.dataset.coordinationFocus = 'graph-history';
  const zoomLabel = text('', 'span', 'coordination-zoom-label');
  const setZoom = (value: number) => {
    state.zoom = Math.max(0.5, Math.min(1.5, value));
    root.style.setProperty('--coordination-zoom', String(state.zoom));
    zoomLabel.textContent = `${Math.round(state.zoom * 100)}%`;
    zoomOut.disabled = state.zoom <= 0.5; zoomIn.disabled = state.zoom >= 1.5;
  };
  const zoomOut = createButton({ label: '−', size: 'sm', variant: 'ghost', onClick: () => setZoom((state.zoom ?? 1) - 0.25) });
  zoomOut.setAttribute('aria-label', t('projectCoordination.graph.zoomOut')); zoomOut.dataset.coordinationFocus = 'graph-zoom-out';
  const zoomIn = createButton({ label: '+', size: 'sm', variant: 'ghost', onClick: () => setZoom((state.zoom ?? 1) + 0.25) });
  zoomIn.setAttribute('aria-label', t('projectCoordination.graph.zoomIn')); zoomIn.dataset.coordinationFocus = 'graph-zoom-in';
  const reset = createButton({ label: t('projectCoordination.graph.resetView'), size: 'sm', variant: 'ghost', onClick: () => {
    setZoom(1); viewport.scrollTo?.({ left: 0, top: 0 });
  } }); reset.dataset.coordinationFocus = 'graph-reset';
  toolbar.append(history, zoomOut, zoomLabel, zoomIn, reset);
  const viewport = document.createElement('div'); viewport.className = 'coordination-graph-viewport';
  viewport.tabIndex = 0; viewport.setAttribute('role', 'region'); viewport.setAttribute('aria-label', t('projectCoordination.graph.viewport'));
  viewport.dataset.coordinationFocus = 'graph-viewport';
  const inspector = document.createElement('section'); inspector.className = 'coordination-node-inspector'; inspector.hidden = true;
  inspector.setAttribute('aria-label', t('projectCoordination.graph.details')); inspector.setAttribute('aria-live', 'polite');
  const legend = document.createElement('div'); legend.className = 'coordination-edge-legend';
  for (const kind of ['dispatch', 'dependency', 'rework'] as const) {
    if (!graph.edges.some(edge => edge.kind === kind)) continue;
    const label = text(t(`projectCoordination.graph.edge.${kind}`)); label.dataset.edgeLegend = kind; legend.append(label);
  }
  root.append(toolbar, viewport, inspector, legend);
  setZoom(state.zoom ?? 1);

  function select(id: string, toggle = false) {
    state.selectedId = toggle && state.selectedId === id ? undefined : id;
    for (const card of viewport.querySelectorAll<HTMLElement>('[data-node-id]')) {
      const selected = card.dataset.nodeId === state.selectedId;
      card.dataset.selected = String(selected);
      card.querySelector('button')?.setAttribute('aria-pressed', String(selected));
    }
    inspector.replaceChildren();
    const node = graph.nodes.find(item => item.id === state.selectedId);
    inspector.hidden = !node;
    if (!node) return;
    const heading = text(titleOf(node), 'strong');
    inspector.append(heading, text(statusOf(node), 'small'));
    if (node.summary) inspector.append(text(digest(node) || node.summary, 'p'));
    if (node.revoked) inspector.append(text(t('projectCoordination.graph.revoked'), 'small'));
    if (node.delivered !== undefined) inspector.append(text(t(node.delivered
      ? 'projectCoordination.graph.deliveryConfirmed' : 'projectCoordination.graph.deliveryUnconfirmed'), 'small'));
    if (node.dependencyUnknown) inspector.append(text(t('projectCoordination.graph.dependencyUnknown'), 'small'));
    for (const edge of graph.edges.filter(edge => edge.to === node.id && edge.kind !== 'dispatch')) {
      const source = graph.nodes.find(item => item.id === edge.from);
      const label = t(edge.kind === 'rework' ? 'projectCoordination.graph.reworkOf' : 'projectCoordination.graph.dependsOn',
        { task: source ? titleOf(source) : edge.reference?.assignmentId || '' });
      inspector.append(text(label + (edge.status ? ` · ${t(`projectCoordination.graph.dependency.${edge.status}`)}` : ''), 'small'));
    }
    if (node.conversationId) {
      const open = createButton({ label: t('projectCoordination.graph.openConversation', { task: titleOf(node) }),
        size: 'sm', onClick: () => navigate(node.conversationId!) });
      open.dataset.coordinationFocus = `graph-open:${node.id}`; inspector.append(open);
    } else if (node.kind === 'task') inspector.append(text(t('projectCoordination.graph.missingConversation'), 'small'));
  }

  function render() {
    history.textContent = t(state.showHistory ? 'projectCoordination.graph.hideHistory' : 'projectCoordination.graph.showHistory', { count: historyCount });
    history.setAttribute('aria-pressed', String(!!state.showHistory)); history.hidden = historyCount === 0;
    const visible = visibleCoordinationGraph(graph, state);
    if (!visible.nodes.some(node => node.id === state.selectedId)) state.selectedId = undefined;
    viewport.replaceChildren();
    const sheet = document.createElement('div'); sheet.className = 'coordination-graph-sheet';
    const stage = document.createElement('div'); stage.className = 'coordination-graph-stage';
    sheet.append(stage); viewport.append(sheet);
    const { width, height } = COORDINATION_GRAPH_GEOMETRY;
    const cards = new Map<string, HTMLElement>();
    for (const node of visible.nodes) {
      const card = document.createElement('article'); card.className = 'coordination-graph-node';
      card.dataset.nodeId = node.id; card.dataset.nodeKind = node.kind;
      if (node.status) card.dataset.status = node.status;
      const selectButton = createButton({ label: titleOf(node), size: 'sm', variant: 'ghost', wrap: true, onClick: () => select(node.id, true) });
      selectButton.setAttribute('aria-label', `${titleOf(node)} · ${statusOf(node)}`);
      selectButton.dataset.coordinationFocus = `graph-node:${node.id}`;
      const dot = text(node.kind === 'coordinator' ? '◎' : node.kind === 'reference' ? '?' : node.status === 'completed' ? '✓'
        : node.status === 'failed' ? '!' : node.status === 'cancelled' ? '−' : node.status === 'blocked' ? 'Ⅱ'
        : node.status === 'running' ? '◉' : node.status === 'queued' ? '◌' : '◇', 'span', 'coordination-node-signal');
      dot.setAttribute('aria-hidden', 'true'); dot.title = statusOf(node);
      card.append(dot, selectButton);
      const summary = node.kind === 'coordinator' ? t('projectCoordination.graph.coordinatorRole') : digest(node);
      if (summary) card.append(text(summary, 'p', 'coordination-node-digest'));
      stage.append(card); cards.set(node.id, card);
    }
    // CSS container queries select identical geometry for both cards and wires.
    // This adapts to split panes without a resident ResizeObserver per graph.
    for (const columns of [1, 2, 3]) {
      const layout = layoutCoordinationGraph(visible, columns);
      sheet.style.setProperty(`--graph-width-${columns}`, String(layout.width));
      sheet.style.setProperty(`--graph-height-${columns}`, String(layout.height));
      const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
      svg.classList.add('coordination-graph-edges'); svg.dataset.columns = String(columns);
      svg.setAttribute('viewBox', `0 0 ${layout.width} ${layout.height}`); svg.setAttribute('aria-hidden', 'true');
      for (const edge of visible.edges) {
        const a = layout.positions.get(edge.from), b = layout.positions.get(edge.to);
        if (!a || !b) continue;
        const x1 = a.x + width / 2, y1 = a.y + height, x2 = b.x + width / 2, y2 = b.y;
        const path = document.createElementNS(svg.namespaceURI, 'path');
        // Backward/cyclic evidence remains visible; it does not acquire a fake rank.
        const bend = y2 > y1 ? (y1 + y2) / 2 : y1 + 1;
        path.setAttribute('d', `M ${x1} ${y1} C ${x1} ${bend}, ${x2} ${bend}, ${x2} ${y2}`);
        path.setAttribute('data-edge-kind', edge.kind); path.setAttribute('data-edge-from', edge.from); path.setAttribute('data-edge-to', edge.to);
        svg.append(path);
      }
      stage.prepend(svg);
      for (const [id, position] of layout.positions) {
        cards.get(id)!.style.setProperty(`--graph-x-${columns}`, String(position.x));
        cards.get(id)!.style.setProperty(`--graph-y-${columns}`, String(position.y));
      }
    }
    if (!visible.nodes.some(node => node.kind === 'task')) viewport.append(text(t(graph.total
      ? 'projectCoordination.graph.noActiveWork' : 'projectCoordination.graph.empty'), 'p', 'coordination-canvas-empty'));
    select(state.selectedId || '');
  }
  render();
  return root;
}
