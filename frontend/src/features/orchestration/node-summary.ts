import { orchestrationRegistry } from './registry';
export type OrchestrationTranslate = (
  key: string,
  params?: Record<string, unknown>,
) => unknown;

export interface OrchestrationNode extends Record<string, unknown> {
  id?: unknown;
  type?: unknown;
  role?: unknown;
  kind?: unknown;
  params?: unknown;
}

export interface OrchestrationSummaryOptions {
  defaultEmits?: unknown | ((role: unknown) => unknown);
  nodeParam?: (node: OrchestrationNode, key: string) => unknown;
  profile?: string;
}

export interface RoleExecutionSummary {
  tier: string;
  isolation: string;
  emits: string;
  emitsValue: string;
  text: string;
}

export interface SubflowSummary {
  scope: string;
  scopeValue: string;
  nodeCount: number;
  text: string;
}

export interface ControlSummary {
  kind: string;
  outgoingCount: number;
  maxIterations: unknown;
  stopCondition: string;
  classifier: string;
  path: string;
  mode: string;
  seed: string;
  text: string;
}

type NodeSummaryWindow = Window & {
  orchestrationConnections?: (
    edges: unknown,
    nodeId: unknown,
  ) => { outgoing?: unknown[] };
  orchestrationExecutionOptionLabel?: typeof orchestrationExecutionOptionLabel;
  projectOrchestrationRoleExecutionSummary?:
    typeof projectOrchestrationRoleExecutionSummary;
  projectOrchestrationSubflowSummary?: typeof projectOrchestrationSubflowSummary;
  orchestrationSummaryNodeParam?: typeof orchestrationSummaryNodeParam;
  projectOrchestrationControlSummary?: typeof projectOrchestrationControlSummary;
};

const asNode = (value: unknown): OrchestrationNode => value
  && typeof value === 'object' && !Array.isArray(value)
  ? value as OrchestrationNode : {};
const asRecord = (value: unknown): Record<string, unknown> => value
  && typeof value === 'object' && !Array.isArray(value)
  ? value as Record<string, unknown> : {};
const translator = (translate?: OrchestrationTranslate): OrchestrationTranslate =>
  typeof translate === 'function' ? translate : (key) => key;

export function orchestrationExecutionOptionLabel(
  axisValue: unknown,
  value: unknown,
  translate?: OrchestrationTranslate,
  profile?: unknown,
): string {
  const axis = String(axisValue || '');
  const raw = String(value == null ? '' : value);
  const tr = translator(translate);
  const aliases: Record<string, Record<string, string>> = {
    isolation: { fresh: 'fresh-context', shared: 'shared-context' },
  };
  const canonical = aliases[axis]?.[raw] || raw;
  const keys: Record<string, Record<string, string>> = profile === 'editor' ? {
    tiers: {
      light: 'orch.tier.light', standard: 'orch.tier.standard',
      heavy: 'orch.tier.heavy',
    },
    isolation: {
      'fresh-context': 'orch.iso.fresh',
      'shared-context': 'orch.iso.shared',
    },
    scopes: { isolated: 'orch.scope.isolated', inline: 'orch.scope.inline' },
    emits: { assistant: 'orch.emits.assistant', user: 'orch.emits.user' },
  } : {
    tiers: {
      light: 'orch.node.tier.light', standard: 'orch.node.tier.standard',
      heavy: 'orch.node.tier.heavy',
    },
    isolation: {
      'fresh-context': 'orch.node.isolation.fresh',
      'shared-context': 'orch.node.isolation.shared',
    },
    scopes: {
      isolated: 'orch.node.scope.isolated', inline: 'orch.node.scope.inline',
    },
    emits: {
      assistant: 'orch.node.emits.assistant', user: 'orch.node.emits.user',
    },
  };
  const key = keys[axis]?.[canonical];
  if (!key) return raw;
  const translated = tr(key);
  return translated && translated !== key ? String(translated) : raw;
}

export function orchestrationSummaryNodeParam(
  nodeValue: unknown,
  key: string,
  options: OrchestrationSummaryOptions = {},
): unknown {
  const node = asNode(nodeValue);
  if (typeof options.nodeParam === 'function') {
    return options.nodeParam(node, key);
  }
  const params = asRecord(node.params);
  return Object.prototype.hasOwnProperty.call(params, key) ? params[key] : null;
}

export function projectOrchestrationRoleExecutionSummary(
  nodeValue: unknown,
  translate?: OrchestrationTranslate,
  optionsValue: OrchestrationSummaryOptions | string
    | ((role: unknown) => unknown) = {},
): Readonly<RoleExecutionSummary> {
  const node = asNode(nodeValue);
  const options: OrchestrationSummaryOptions =
    typeof optionsValue === 'function' || typeof optionsValue === 'string'
      ? { defaultEmits: optionsValue } : optionsValue || {};
  const tr = translator(translate);
  const tierParam = orchestrationSummaryNodeParam(node, 'tier', options);
  const isolationParam = orchestrationSummaryNodeParam(node, 'isolation', options);
  const tierValue = String(tierParam == null ? '—' : tierParam);
  const isolationValue = String(isolationParam == null ? '—' : isolationParam);
  let emitsValue = orchestrationSummaryNodeParam(node, 'emits', options);
  if (!emitsValue && typeof options.defaultEmits === 'function') {
    emitsValue = options.defaultEmits(node.role);
  } else if (!emitsValue && options.defaultEmits) {
    emitsValue = options.defaultEmits;
  }
  const emitsRaw = String(emitsValue || '');
  const tier = orchestrationExecutionOptionLabel('tiers', tierValue, tr, 'compact');
  const isolation = orchestrationExecutionOptionLabel(
    'isolation', isolationValue, tr, 'compact');
  const emits = orchestrationExecutionOptionLabel('emits', emitsRaw, tr, 'compact');
  return Object.freeze({
    tier, isolation, emits, emitsValue: emitsRaw,
    text: `${tier} · ${isolation}`,
  });
}

export function projectOrchestrationSubflowSummary(
  nodeValue: unknown,
  translate?: OrchestrationTranslate,
  options: OrchestrationSummaryOptions = {},
): Readonly<SubflowSummary> {
  const node = asNode(nodeValue);
  const params = asRecord(node.params);
  const tr = translator(translate);
  const scopeParam = orchestrationSummaryNodeParam(node, 'scope', options);
  const scopeValue = String(scopeParam == null ? '—' : scopeParam);
  const scope = orchestrationExecutionOptionLabel(
    'scopes', scopeValue, tr, 'compact');
  const definition = asRecord(params.definition);
  const nodeCount = Array.isArray(definition.nodes) ? definition.nodes.length : 0;
  const translated = tr('orch.sub.group', { scope, n: nodeCount });
  const text = !translated || translated === 'orch.sub.group'
    ? `${scope} · ${nodeCount}` : String(translated);
  return Object.freeze({ scope, scopeValue, nodeCount, text });
}

export function projectOrchestrationControlSummary(
  nodeValue: unknown,
  edges: unknown,
  translate?: OrchestrationTranslate,
  options: OrchestrationSummaryOptions = {},
): Readonly<ControlSummary> {
  const node = asNode(nodeValue);
  const tr = translator(translate);
  const registry = orchestrationRegistry as unknown as NodeSummaryWindow;
  const published = globalThis as unknown as NodeSummaryWindow;
  const connections = (registry.orchestrationConnections
    ?? published.orchestrationConnections)?.(edges, node.id)
    ?? { outgoing: [] };
  const maxIterations = orchestrationSummaryNodeParam(
    node, 'max_iterations', options);
  const summary: ControlSummary = {
    kind: String(node.kind || ''),
    outgoingCount: Array.isArray(connections.outgoing)
      ? connections.outgoing.length : 0,
    maxIterations,
    stopCondition: String(
      orchestrationSummaryNodeParam(node, 'stop_condition', options) || ''),
    classifier: String(
      orchestrationSummaryNodeParam(node, 'classifier', options) || ''),
    path: String(orchestrationSummaryNodeParam(node, 'path', options) || ''),
    mode: String(orchestrationSummaryNodeParam(node, 'mode', options) || ''),
    seed: String(orchestrationSummaryNodeParam(node, 'seed', options) || '')
      .trim().slice(0, 42),
    text: '',
  };
  const shownMax = maxIterations == null ? '—' : maxIterations;
  const humanStudio: Record<string, string> = {
    approve: 'orch.sub.approvalGate', input: 'orch.sub.collectInput',
    notify: 'orch.sub.notifyUser',
  };
  const humanTask: Record<string, string> = {
    approve: 'tm.sub.approvalGate', input: 'tm.sub.collectInput',
    notify: 'tm.sub.notify',
  };
  if (options.profile === 'studio') {
    if (summary.kind === 'loop') {
      summary.text = String(tr('orch.sub.loop', {
        n: shownMax, condition: summary.stopCondition,
      }));
    } else if (summary.kind === 'parallel') {
      summary.text = String(tr('orch.sub.parallel', { n: summary.outgoingCount }));
    } else if (summary.kind === 'branch') {
      summary.text = String(tr('orch.sub.routes', {
        n: summary.outgoingCount,
        classifier: summary.classifier || tr('orch.sub.firstEdge'),
      }));
    } else if (summary.kind === 'artifact') {
      summary.text = summary.path || String(tr('orch.sub.deliverable'));
    } else if (summary.kind === 'human') {
      summary.text = String(tr(humanStudio[summary.mode]
        || 'orch.sub.approvalGate'));
    } else if (summary.kind === 'start') {
      summary.text = summary.seed || String(tr('orch.sub.setInput'));
    } else if (summary.kind === 'stop') {
      summary.text = String(tr('orch.sub.returnResult'));
    }
  } else if (summary.kind === 'loop') {
    summary.text = `${String(tr('tm.sub.max'))} ${String(shownMax)}`;
  } else if (summary.kind === 'parallel') {
    summary.text = String(tr('tm.sub.fanoutBranches', {
      n: summary.outgoingCount,
    }));
  } else if (summary.kind === 'branch') {
    summary.text = String(tr('tm.sub.branchRoutes', { n: summary.outgoingCount }));
  } else if (summary.kind === 'artifact') {
    summary.text = summary.path || String(tr('tm.sub.deliverable'));
  } else if (summary.kind === 'human') {
    summary.text = String(tr(humanTask[summary.mode] || 'tm.sub.gate'));
  } else if (summary.kind === 'start') {
    summary.text = String(tr('tm.sub.startInput'));
  } else if (summary.kind === 'stop') {
    summary.text = String(tr('tm.sub.stopResult'));
  } else {
    summary.text = summary.kind;
  }
  return Object.freeze(summary);
}

Object.assign(orchestrationRegistry as unknown as NodeSummaryWindow, {
  orchestrationExecutionOptionLabel,
  projectOrchestrationRoleExecutionSummary,
  projectOrchestrationSubflowSummary,
  orchestrationSummaryNodeParam,
  projectOrchestrationControlSummary,
});
