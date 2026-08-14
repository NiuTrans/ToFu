import { orchestrationRegistry } from './registry';
import type { OrchestrationNode } from './node-summary';

export interface NodeCatalogueDefinition extends Record<string, unknown> {
  role?: unknown;
  kind?: unknown;
  glyph?: unknown;
  accent?: unknown;
}

export interface OrchestrationNodeCatalogueOptions {
  roles?: NodeCatalogueDefinition[] | (() => unknown);
  controls?: NodeCatalogueDefinition[] | (() => unknown);
  nodeDefaults?: unknown | (() => unknown);
  nodeRuntimeDefaults?: unknown | (() => unknown);
}

export interface OrchestrationNodeCatalogue {
  role(roleName: unknown): NodeCatalogueDefinition | null;
  control(kind: unknown): NodeCatalogueDefinition | null;
  nodeParam(node: OrchestrationNode, key: string): unknown;
  runtimeParam(node: OrchestrationNode, key: string): unknown;
  controlGlyph(node: OrchestrationNode): unknown;
  accent(node: OrchestrationNode, controlFallback?: unknown): unknown;
}

type NodeCatalogueWindow = Window & {
  createOrchestrationNodeCatalogue?: typeof createOrchestrationNodeCatalogue;
};

const record = (value: unknown): Record<string, unknown> | null => value
  && typeof value === 'object' && !Array.isArray(value)
  ? value as Record<string, unknown> : null;

export function createOrchestrationNodeCatalogue(
  options: OrchestrationNodeCatalogueOptions = {},
): Readonly<OrchestrationNodeCatalogue> {
  const values = (source: unknown): NodeCatalogueDefinition[] => {
    const result = typeof source === 'function'
      ? (source as () => unknown)() : source;
    return Array.isArray(result) ? result as NodeCatalogueDefinition[] : [];
  };
  const role = (roleName: unknown): NodeCatalogueDefinition | null =>
    values(options.roles).find((item) => item.role === roleName) ?? null;
  const control = (kind: unknown): NodeCatalogueDefinition | null =>
    values(options.controls).find((item) => item.kind === kind) ?? null;
  const resolvedDefaults = (source: unknown): Record<string, unknown> | null =>
    record(typeof source === 'function' ? (source as () => unknown)() : source);
  const defaultParams = (node: OrchestrationNode): Record<string, unknown> | null => {
    const defaults = resolvedDefaults(options.nodeDefaults);
    if (!defaults) return null;
    if (node.type === 'role') {
      return record(record(defaults.roles)?.[String(node.role || '')])
        ?? record(defaults.genericRole);
    }
    if (node.type === 'subflow') return record(defaults.subflow);
    return record(record(defaults.controls)?.[String(node.kind || '')]);
  };
  const nodeParam = (nodeValue: OrchestrationNode, key: string): unknown => {
    const node = nodeValue ?? {};
    const params = record(node.params);
    if (params && Object.prototype.hasOwnProperty.call(params, key)) {
      return params[key];
    }
    const defaults = defaultParams(node);
    return defaults && Object.prototype.hasOwnProperty.call(defaults, key)
      ? defaults[key] : null;
  };
  const runtimeDefaultParams = (
    node: OrchestrationNode,
  ): Record<string, unknown> | null => {
    const defaults = resolvedDefaults(options.nodeRuntimeDefaults);
    if (!defaults) return null;
    if (node.type === 'role') return record(defaults.role);
    if (node.type === 'subflow') return record(defaults.subflow);
    return record(record(defaults.controls)?.[String(node.kind || '')]);
  };
  const runtimeParam = (nodeValue: OrchestrationNode, key: string): unknown => {
    const node = nodeValue ?? {};
    const value = record(node.params)?.[key];
    if (value != null && value !== '') return value;
    const defaults = runtimeDefaultParams(node);
    return defaults && Object.prototype.hasOwnProperty.call(defaults, key)
      ? defaults[key] : null;
  };
  const controlGlyph = (node: OrchestrationNode): unknown => {
    const definition = control(node?.kind);
    return definition?.glyph || node?.kind || '';
  };
  const accent = (node: OrchestrationNode, fallback?: unknown): unknown => {
    if (node?.type === 'role') return '#6e56cf';
    if (node?.type === 'subflow') return '#8b5cf6';
    return control(node?.kind)?.accent || fallback || 'var(--text-tertiary)';
  };
  return Object.freeze({
    accent, control, controlGlyph, nodeParam, role, runtimeParam,
  });
}

(orchestrationRegistry as unknown as NodeCatalogueWindow).createOrchestrationNodeCatalogue =
  createOrchestrationNodeCatalogue;
