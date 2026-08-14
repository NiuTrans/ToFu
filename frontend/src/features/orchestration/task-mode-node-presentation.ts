import { orchestrationRegistry } from './registry';
import {
  createOrchestrationNodeCatalogue,
  type NodeCatalogueDefinition,
  type OrchestrationNodeCatalogue,
  type OrchestrationNodeCatalogueOptions,
} from './node-catalogue';
import {
  projectOrchestrationControlSummary,
  projectOrchestrationRoleExecutionSummary,
  projectOrchestrationSubflowSummary,
  type OrchestrationNode,
  type OrchestrationTranslate,
} from './node-summary';

export interface TaskModeNodePresentationOptions
  extends OrchestrationNodeCatalogueOptions {
  catalogue?: OrchestrationNodeCatalogue;
  escape?: (value: unknown) => unknown;
  translate?: OrchestrationTranslate;
  icon?: (name: string) => unknown;
  glyphs?: unknown | (() => unknown);
  iconSrc?: (name: unknown) => unknown;
  definition?: unknown | (() => unknown);
}

type TaskModeNodePresentationWindow = Window & {
  createTaskModeNodePresentation?: typeof createTaskModeNodePresentation;
};

const record = (value: unknown): Record<string, unknown> | null => value
  && typeof value === 'object' && !Array.isArray(value)
  ? value as Record<string, unknown> : null;

export function createTaskModeNodePresentation(
  options: TaskModeNodePresentationOptions = {},
) {
  const catalogue = options.catalogue ?? createOrchestrationNodeCatalogue({
    controls: options.controls,
    nodeRuntimeDefaults: options.nodeRuntimeDefaults,
    roles: options.roles,
  });
  const escape = (value: unknown): string => String(
    options.escape ? options.escape(value) : value == null ? '' : value);
  const translate = (key: string, params?: Record<string, unknown>): unknown =>
    options.translate ? options.translate(key, params) : key;
  const icon = (name: string): string => String(
    options.icon ? options.icon(name) || '' : '');
  const glyphs = (): Record<string, unknown> => {
    const result = typeof options.glyphs === 'function'
      ? options.glyphs() : options.glyphs;
    return record(result) ?? {};
  };
  const roleDef = (role: unknown): NodeCatalogueDefinition | null =>
    catalogue.role(role);
  const controlDef = (kind: unknown): NodeCatalogueDefinition | null =>
    catalogue.control(kind);
  const accent = (node: OrchestrationNode): unknown =>
    catalogue.accent(node, 'var(--text-tertiary)');
  const iconHtml = (nodeValue: OrchestrationNode = {}): string => {
    const node = nodeValue ?? {};
    if (node.type === 'role') {
      const role = roleDef(node.role);
      if (role && typeof options.iconSrc === 'function') {
        return `<img src="${escape(options.iconSrc(role.icon))}" alt="" data-tm-avatar>`;
      }
      return icon('bot');
    }
    if (node.type === 'subflow') return String(glyphs().group || icon('bot'));
    return String(glyphs()[String(catalogue.controlGlyph(node))] || icon('bot'));
  };
  const bindImageFallbacks = (root: ParentNode | null): void => {
    if (!root || typeof root.querySelectorAll !== 'function') return;
    root.querySelectorAll<HTMLElement>('[data-tm-avatar]').forEach((avatar) => {
      avatar.addEventListener('error', () => {
        avatar.style.display = 'none';
      }, { once: true });
    });
  };
  const glyph = (nodeValue: OrchestrationNode = {}): string => {
    const node = nodeValue ?? {};
    if (node.type === 'role' || node.type === 'subflow') return icon('bot');
    return String(glyphs()[String(catalogue.controlGlyph(node))] || icon('bot'));
  };
  const label = (nodeValue: OrchestrationNode = {}): string => {
    const node = nodeValue ?? {};
    if (node.name) return String(node.name);
    if (node.type === 'role') {
      const role = roleDef(node.role);
      return String(role?.label || node.role || translate('tm.node.agent'));
    }
    const control = controlDef(node.kind);
    return String(control?.label || node.kind || node.id || '?');
  };
  const subtitle = (nodeValue: OrchestrationNode = {}): string => {
    const node = nodeValue ?? {};
    if (node.type === 'role') {
      return projectOrchestrationRoleExecutionSummary(
        node, translate, { nodeParam: catalogue.runtimeParam }).text;
    }
    if (node.type === 'subflow') {
      return projectOrchestrationSubflowSummary(
        node, translate, { nodeParam: catalogue.runtimeParam }).text;
    }
    const definitionValue = typeof options.definition === 'function'
      ? options.definition() : options.definition;
    const definition = record(definitionValue);
    const edges = Array.isArray(definition?.edges) ? definition.edges : [];
    return projectOrchestrationControlSummary(node, edges, translate, {
      profile: 'task', nodeParam: catalogue.runtimeParam,
    }).text;
  };
  return {
    roleDef, controlDef, accent, iconHtml, bindImageFallbacks,
    glyph, label, subtitle,
  };
}

(orchestrationRegistry as unknown as TaskModeNodePresentationWindow).createTaskModeNodePresentation =
  createTaskModeNodePresentation;
