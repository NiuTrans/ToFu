import { orchestrationRegistry } from './registry';
import type { OrchestrationNode } from './node-summary';

export interface TaskModeGraphState extends Record<string, unknown> {
  definition?: unknown;
  activeNode?: unknown;
  doneNodes?: Record<string, unknown>;
  selectedNode?: unknown;
  trace?: Record<string, unknown>;
}

export interface TaskModeGraphProjectionOptions {
  nodeWidth?: number;
  nodeHeight?: number;
  markerId?: string;
  padding?: number;
  escape?: (value: unknown) => unknown;
  translate?: (key: string, params?: Record<string, unknown>) => unknown;
  nodeLabel?: (node: OrchestrationNode) => unknown;
  nodeSubtitle?: (node: OrchestrationNode) => unknown;
  nodeIconHtml?: (node: OrchestrationNode) => unknown;
}

export interface TaskModeGraphProjectionResult {
  html: string;
  nodes: OrchestrationNode[];
  nodeIds: unknown[];
  width: number;
  height: number;
}

type Position = { x: number; y: number };
type TaskModeGraphProjectionWindow = Window & {
  orchestrationNodePosition?: (
    node: OrchestrationNode,
    fallback: Position,
  ) => Position;
  createTaskModeGraphProjection?: typeof createTaskModeGraphProjection;
};

const record = (value: unknown): Record<string, unknown> | null => value
  && typeof value === 'object' && !Array.isArray(value)
  ? value as Record<string, unknown> : null;

export function createTaskModeGraphProjection(
  options: TaskModeGraphProjectionOptions = {},
) {
  const nodeWidth = options.nodeWidth || 168;
  const nodeHeight = options.nodeHeight || 56;
  const markerId = options.markerId || 'tmArrow';
  const padding = options.padding || 24;
  const escape = (value: unknown): string => String(
    options.escape ? options.escape(value) : value == null ? '' : value);
  const translate = (key: string, params?: Record<string, unknown>): unknown =>
    options.translate ? options.translate(key, params) : key;
  const position = (node: OrchestrationNode): Position => {
    const projected = (orchestrationRegistry as unknown as TaskModeGraphProjectionWindow)
      .orchestrationNodePosition?.(node, { x: 20, y: 20 });
    if (projected) return projected;
    const pos = record(node.pos);
    return {
      x: typeof pos?.x === 'number' && Number.isFinite(pos.x) ? pos.x : 20,
      y: typeof pos?.y === 'number' && Number.isFinite(pos.y) ? pos.y : 20,
    };
  };
  const project = (
    stateValue: TaskModeGraphState = {},
  ): TaskModeGraphProjectionResult | null => {
    const state = stateValue ?? {};
    const definition = record(state.definition) ?? {};
    const nodes = Array.isArray(definition.nodes)
      ? definition.nodes as OrchestrationNode[] : [];
    const edges = Array.isArray(definition.edges)
      ? definition.edges as Record<string, unknown>[] : [];
    if (!nodes.length) return null;
    let minX = Infinity;
    let minY = Infinity;
    let maxX = -Infinity;
    let maxY = -Infinity;
    nodes.forEach((node) => {
      const pos = position(node);
      minX = Math.min(minX, pos.x);
      minY = Math.min(minY, pos.y);
      maxX = Math.max(maxX, pos.x + nodeWidth);
      maxY = Math.max(maxY, pos.y + nodeHeight);
    });
    const width = Math.max(1, Math.round(maxX - minX + padding * 2));
    const height = Math.max(1, Math.round(maxY - minY + padding * 2));
    const screenX = (x: number): number => x - minX + padding;
    const screenY = (y: number): number => y - minY + padding;
    const byId: Record<string, OrchestrationNode> = Object.create(null) as
      Record<string, OrchestrationNode>;
    nodes.forEach((node) => { byId[String(node.id)] = node; });
    let parts = `<defs><marker id="${escape(markerId)
      }" viewBox="0 0 12 12" refX="9.5" refY="6" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path class="tm-edge-arrow" d="M1 1 L11 6 L1 11 L4 6 Z"></path></marker></defs>`;
    edges.forEach((edge) => {
      const source = byId[String(edge.from)];
      const target = byId[String(edge.to)];
      if (!source || !target) return;
      const sourcePos = position(source);
      const targetPos = position(target);
      const ax = screenX(sourcePos.x + nodeWidth / 2);
      const ay = screenY(sourcePos.y + nodeHeight);
      const bx = screenX(targetPos.x + nodeWidth / 2);
      const by = screenY(targetPos.y);
      const deltaY = by - ay;
      let path: string;
      if (deltaY >= 24) {
        const vertical = deltaY * 0.5;
        path = `M ${ax} ${ay} C ${ax} ${ay + vertical} ${bx} ${
          by - vertical} ${bx} ${by}`;
      } else {
        const deltaX = bx - ax;
        const side = deltaX >= 0 ? 1 : -1;
        const horizontal = Math.max(50, Math.abs(deltaX) * 0.5);
        const bend = Math.max(34, Math.abs(deltaY) * 0.5);
        path = `M ${ax} ${ay} C ${ax + side * horizontal} ${ay + bend} ${
          bx + side * horizontal} ${by - bend} ${bx} ${by}`;
      }
      parts += `<path class="tm-edge" marker-end="url(#${
        escape(markerId)})" d="${path}"></path>`;
    });
    nodes.forEach((node, index) => {
      const pos = position(node);
      const active = node.id === state.activeNode ? ' is-active' : '';
      const runStatus = state.doneNodes?.[String(node.id)] || '';
      const terminalClass = !active && runStatus === 'error'
        ? ' is-error' : !active && runStatus === 'done' ? ' is-done' : '';
      const selected = node.id === state.selectedNode ? ' is-selected' : '';
      const traced = state.trace?.[String(node.id)] ? ' tm-gnode-traced' : '';
      const typeClass = node.type === 'role' ? ' tm-gnode-role'
        : node.type === 'subflow' ? ' tm-gnode-sub' : ' tm-gnode-ctrl';
      const label = String(options.nodeLabel
        ? options.nodeLabel(node) : node.name || node.id || '');
      const subtitle = String(options.nodeSubtitle
        ? options.nodeSubtitle(node) : '');
      const icon = String(options.nodeIconHtml
        ? options.nodeIconHtml(node) : '');
      const ribbon = node.kind === 'start'
        ? `<span class="tm-gnode-ribbon">${escape(
          translate('tm.ribbon.input'))}</span>`
        : node.kind === 'stop'
          ? `<span class="tm-gnode-ribbon tm-gnode-ribbon-out">${escape(
            translate('tm.ribbon.result'))}</span>` : '';
      let accessible = label + (subtitle ? ` — ${subtitle}` : '');
      if (runStatus === 'done' || runStatus === 'error') {
        accessible += ` — ${String(translate(runStatus === 'error'
          ? 'orch.run.statusError' : 'orch.run.statusDone'))}`;
      }
      parts += `<foreignObject x="${screenX(pos.x)}" y="${
        screenY(pos.y)}" width="${nodeWidth}" height="${nodeHeight}"><div xmlns="http://www.w3.org/1999/xhtml" class="tm-gnode${
        typeClass}${active}${terminalClass}${selected}${traced}" data-tm-node-index="${
        index}" role="button" tabindex="0" aria-pressed="${
        node.id === state.selectedNode}"${node.id === state.activeNode
        ? ' aria-current="step"' : ''} aria-label="${escape(accessible)
        }" title="${escape(translate('tm.tip.inspectNode'))}">${ribbon
        }<span class="tm-gnode-ico">${icon}</span><span class="tm-gnode-text"><span class="tm-gnode-label">${
        escape(label)}</span><span class="tm-gnode-sub">${escape(subtitle)
        }</span></span></div></foreignObject>`;
    });
    return {
      html: `<svg class="tm-graph-svg" width="${width}" height="${height
        }" viewBox="0 0 ${width} ${height}">${parts}</svg>`,
      nodes,
      nodeIds: nodes.map((node) => node.id),
      width,
      height,
    };
  };
  return Object.freeze({ project });
}

(orchestrationRegistry as unknown as TaskModeGraphProjectionWindow).createTaskModeGraphProjection =
  createTaskModeGraphProjection;
