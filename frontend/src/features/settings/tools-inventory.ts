/**
 * Settings tool-inventory owner.
 *
 * Responsibility: fetch and render the process-wide tool catalogue exposed by
 * `Api.tools.inventory()`. Entry points are `populateToolsInventory` and
 * `searchToolsInventory`; runtime dependencies are read through the injected
 * feature registry so this module stays independently testable and lazy.
 */
import { featureRegistry } from '../../feature-registry';
import { escapeHtml as escape } from '../../html-safety';
import type { I18nKey } from '../../i18n';

export interface ToolInventoryItem {
  name: string;
  description?: string;
  required?: string[];
  write?: boolean;
  server?: string;
}

export interface ToolInventoryFamily {
  key: string;
  source?: string;
  plugin_name?: string;
  description?: string;
  tools?: ToolInventoryItem[];
  mcp_tools?: ToolInventoryItem[];
}

export interface ToolInventoryGroup {
  id: string;
  families?: ToolInventoryFamily[];
}

export interface ToolInventorySnapshot {
  totals?: { tools?: number };
  groups?: ToolInventoryGroup[];
}

interface ToolsInventoryApi {
  inventory(): Promise<ToolInventorySnapshot | null>;
}

type ToolsInventoryRuntime = Window & {
  Api?: { tools?: ToolsInventoryApi };
  t?: (key: string, values?: Record<string, unknown>) => string;
  debugLog?: (message: string, level?: string) => void;
  populateToolsInventory?: () => Promise<void>;
  searchToolsInventory?: (query: unknown) => void;
};

const GROUP_ORDER = [
  'search', 'project', 'browser', 'desktop', 'image', 'video',
  'conversation', 'human', 'memory', 'skills', 'knowledge', 'task',
  'scheduler', 'swarm', 'mcp', 'custom',
] as const;

let inventorySnapshot: ToolInventorySnapshot | null = null;
let inventoryQuery = '';
let inventoryRequestSequence = 0;

function runtime(): ToolsInventoryRuntime {
  return featureRegistry as unknown as ToolsInventoryRuntime;
}

function toolsApi(): ToolsInventoryApi {
  const api = runtime().Api?.tools;
  if (!api) throw new Error('Tools inventory API is not ready');
  return api;
}

function translate(key: I18nKey, values?: Record<string, unknown>): string {
  return runtime().t?.(key, values) || key;
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error || '?');
}

function localized(
  prefix: string,
  id: string,
  fallback = '',
): string {
  // Inventory IDs come from the backend/plugin registry. This is an optional
  // catalog lookup with an explicit server-provided fallback, not a static UI
  // label; ordinary product copy must go through the typed translate() path.
  const key = `${prefix}${id}`;
  const value = runtime().t?.(key) || key;
  return value === key ? fallback : value;
}

function familyDescription(family: ToolInventoryFamily): string {
  return localized(
    'toolsInv.family.', family.key, family.description || '',
  );
}

function toolDescription(tool: ToolInventoryItem): string {
  return localized(
    'toolsInv.tool.', tool.name, tool.description || '',
  );
}

export function toolMatches(tool: ToolInventoryItem, query: string): boolean {
  if (!query) return true;
  const haystack = `${tool.name} ${tool.description || ''} ${
    toolDescription(tool)}`.toLowerCase();
  return haystack.includes(query);
}

export function familyVisible(
  family: ToolInventoryFamily,
  query: string,
): boolean {
  if (!query) return true;
  const haystack = `${family.key} ${family.description || ''} ${
    familyDescription(family)}`.toLowerCase();
  if (haystack.includes(query)) return true;
  return [...(family.tools || []), ...(family.mcp_tools || [])]
    .some((tool) => toolMatches(tool, query));
}

export function renderToolRow(
  tool: ToolInventoryItem,
  isMcp: boolean,
): string {
  let html = '<div class="tools-inv-tool">';
  html += '<div class="tools-inv-tool-head">';
  html += `<code class="tools-inv-tool-name">${escape(tool.name)}</code>`;
  if (tool.write) {
    html += `<span class="tools-inv-badge is-write" title="${escape(
      translate('toolsInv.writeTitle'))}">${escape(
      translate('toolsInv.writeBadge'))}</span>`;
  }
  if (isMcp && tool.server) {
    html += `<span class="tools-inv-badge is-mcp">${escape(
      tool.server)}</span>`;
  }
  html += '</div>';
  const description = toolDescription(tool);
  if (description) {
    html += `<div class="tools-inv-tool-desc">${escape(description)}</div>`;
  }
  if (tool.required?.length) {
    const parameters = tool.required
      .map((name) => `<code>${escape(name)}</code>`).join(' ');
    html += `<div class="tools-inv-tool-req">${escape(
      translate('toolsInv.required'))} ${parameters}</div>`;
  }
  return `${html}</div>`;
}

export function renderFamily(
  family: ToolInventoryFamily,
  query: string,
): string {
  const tools = family.tools || [];
  const mcpTools = family.mcp_tools || [];
  const total = tools.length + mcpTools.length;
  let html = '<div class="tools-inv-family">';
  html += '<div class="tools-inv-family-head">';
  html += `<span class="tools-inv-family-name">${escape(family.key)}</span>`;
  if (family.source === 'plugin') {
    const pluginName = family.plugin_name
      ? ` · ${escape(family.plugin_name)}` : '';
    html += `<span class="tools-inv-badge is-plugin">${escape(
      translate('toolsInv.pluginBadge'))}${pluginName}</span>`;
  }
  html += `<span class="tools-inv-family-desc">${escape(
    familyDescription(family))}</span>`;
  html += `<span class="tools-inv-family-count">${escape(
    translate('toolsInv.familyCount', { n: total }))}</span>`;
  html += '</div>';

  const visibleTools = tools.filter((tool) => toolMatches(tool, query));
  const visibleMcpTools = mcpTools.filter((tool) => toolMatches(tool, query));
  if (visibleTools.length || visibleMcpTools.length) {
    html += '<div class="tools-inv-tools">';
    html += visibleTools.map((tool) => renderToolRow(tool, false)).join('');
    html += visibleMcpTools.map((tool) => renderToolRow(tool, true)).join('');
    html += '</div>';
  } else if (!total) {
    html += `<div class="tools-inv-empty">${escape(
      translate('toolsInv.familyEmpty'))}</div>`;
  }
  return `${html}</div>`;
}

export function groupTitle(groupId: string): string {
  return localized('toolsInv.group.', groupId, groupId);
}

export function orderedGroups(
  groups: ToolInventoryGroup[],
): ToolInventoryGroup[] {
  const byId = new Map(groups.map((group) => [group.id, group]));
  const known = GROUP_ORDER.filter((id) => byId.has(id));
  const extra = [...byId.keys()]
    .filter((id) => !GROUP_ORDER.includes(id as typeof GROUP_ORDER[number]))
    .sort();
  return [...known, ...extra].map((id) => byId.get(id) as ToolInventoryGroup);
}

export function renderToolsInventory(
  snapshot: ToolInventorySnapshot,
  query = '',
): void {
  const body = document.getElementById('toolsInvBody');
  if (!body) return;
  const total = document.getElementById('toolsInvTotalCount');
  if (total) {
    total.textContent = translate(
      'toolsInv.countTotal', { n: snapshot.totals?.tools || 0 },
    );
  }

  let html = '';
  for (const group of orderedGroups(snapshot.groups || [])) {
    const families = (group.families || [])
      .filter((family) => familyVisible(family, query));
    if (!families.length) continue;
    html += '<div class="tools-inv-group">';
    html += `<div class="tools-inv-group-title">${escape(
      groupTitle(group.id))}</div>`;
    html += families.map((family) => renderFamily(family, query)).join('');
    html += '</div>';
  }
  body.innerHTML = html || `<p class="stg-empty">${escape(
    translate('toolsInv.noMatch'))}</p>`;
}

function renderCurrentInventory(): void {
  if (inventorySnapshot) {
    renderToolsInventory(inventorySnapshot, inventoryQuery);
  }
}

export async function populateToolsInventory(): Promise<void> {
  const body = document.getElementById('toolsInvBody');
  const requestSequence = ++inventoryRequestSequence;
  body?.setAttribute('aria-busy', 'true');
  try {
    const snapshot = await toolsApi().inventory();
    if (!snapshot) throw new Error(translate('toolsInv.noResponse'));
    if (requestSequence !== inventoryRequestSequence) return;
    inventorySnapshot = snapshot;
    renderCurrentInventory();
  } catch (error: unknown) {
    if (requestSequence !== inventoryRequestSequence) return;
    const message = errorMessage(error);
    runtime().debugLog?.(
      `[ToolsPanel] Failed to load inventory: ${message}`, 'error',
    );
    if (body) {
      body.innerHTML = `<p class="stg-empty">${escape(translate(
        'toolsInv.loadFailed', { err: message }))}</p>`;
    }
  } finally {
    if (requestSequence === inventoryRequestSequence) {
      body?.removeAttribute('aria-busy');
    }
  }
}

export function searchToolsInventory(query: unknown): void {
  inventoryQuery = String(query || '').toLowerCase().trim();
  renderCurrentInventory();
}

const bridge = runtime();
bridge.populateToolsInventory = populateToolsInventory;
bridge.searchToolsInventory = searchToolsInventory;
