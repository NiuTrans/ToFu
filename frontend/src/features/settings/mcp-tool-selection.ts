/** Ordered per-server tool selection intents over the last acknowledged policy.
 * Entry: createMcpToolSelection. The retained presenter owns catalog/tool rows;
 * this owner retains only unsettled intents, then releases the server record.
 * API replacements are serialized per server, with optimistic display rebased
 * after each acknowledgement or refusal. Different servers remain independent.
 */
import type {I18nKey} from '../../i18n';
import type {McpCommandView} from './mcp-command-view';

export interface McpToolRow {name: string; enabled: boolean}
interface ToolResponse {ok: boolean; status?: number; json?(): Promise<{mcp_tool_summary?: unknown}>}
interface Ports {
  rows(server: string): McpToolRow[] | undefined;
  write(server: string, disabled: string[]): Promise<ToolResponse | null>;
  acknowledge(server: string, disabled: string[], summary: unknown): void;
  view(): McpCommandView | null;
  render(): void;
  alert(message: string): unknown;
  log(message: string, level: string): void;
  t: (key: I18nKey, values?: Record<string, unknown>) => string;
}
interface Intent {changes: Map<string, boolean>; view: McpCommandView; resolve(): void}
interface Selection {base: Map<string, boolean>; pending: Intent[]}

class McpToolSelection {
  private readonly selections = new Map<string, Selection>();
  constructor(private readonly ports: Ports) {}

  private feedback(publish: () => void): void {
    try { publish(); }
    catch (error) { this.ports.log('[MCP] Tool selection presentation failed: ' + String(error), 'error'); }
  }

  private desired(state: Selection, intents = state.pending): Map<string, boolean> {
    const desired = new Map(state.base);
    for (const intent of intents) for (const [name, enabled] of intent.changes) desired.set(name, enabled);
    return desired;
  }
  adopt(server: string, rows: McpToolRow[]): McpToolRow[] {
    const state = this.selections.get(server);
    if (state) {
      const desired = this.desired(state);
      for (const row of rows) if (desired.has(row.name)) row.enabled = desired.get(row.name)!;
    }
    return rows;
  }
  change(server: string, name: string | null, enabled: boolean): Promise<void> {
    const rows = this.ports.rows(server), view = this.ports.view();
    if (!rows || !view) return Promise.resolve();
    const changes = new Map(rows.filter(row => name === null || row.name === name).map(row => [row.name, enabled]));
    if (!changes.size) return Promise.resolve();
    let state = this.selections.get(server);
    const start = !state;
    if (!state) { state = {base: new Map(rows.map(row => [row.name, row.enabled])), pending: []}; this.selections.set(server, state); }
    const promise = new Promise<void>(resolve => { state.pending.push({changes, view, resolve}); });
    this.adopt(server, rows);
    this.ports.log('[MCP] ' + server + ': ' + (enabled ? 'enabled' : 'disabled') + ' ' + (name || 'ALL tools'), 'success');
    if (start) void this.drain(server, state);
    return promise;
  }
  private async drain(server: string, state: Selection): Promise<void> {
    while (state.pending.length) {
      const intent = state.pending[0], desired = this.desired(state, [intent]);
      const disabled = [...desired].filter(([, enabled]) => !enabled).map(([name]) => name);
      let summary: unknown, accepted = false;
      try {
        const response = await this.ports.write(server, disabled);
        if (!response?.ok) throw new Error('HTTP ' + (response?.status || 'no response'));
        const data: {mcp_tool_summary?: unknown} | undefined = await response.json?.().catch(() => ({}));
        summary = data?.mcp_tool_summary;
        accepted = true;
      } catch (error) {
        const detail = error instanceof Error ? error.message : String(error);
        this.ports.log('[MCP] Tool selection failed: ' + detail, 'error');
        if (intent.view.isCurrent()) this.feedback(() => { this.ports.alert(this.ports.t('mcp.toolsToggleFailed', {err: detail})); });
      }
      if (accepted) state.base = desired;
      state.pending.shift();
      const rows = this.ports.rows(server);
      if (rows) this.adopt(server, rows);
      // Drop the record before resolving callers, which may submit new work.
      if (!state.pending.length) this.selections.delete(server);
      try {
        if (accepted) this.feedback(() => this.ports.acknowledge(server, disabled, summary));
        this.feedback(() => this.ports.render());
      } finally { intent.resolve(); }
    }
  }
}

export function createMcpToolSelection(ports: Ports): McpToolSelection { return new McpToolSelection(ports); }
