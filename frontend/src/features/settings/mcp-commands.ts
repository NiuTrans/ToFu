/** MCP command owner: captured inputs, pending projection and accepted settlement.
 * Entry: createMcpCommands. Existing API/polling policies remain explicit ports;
 * Settings retirement fences feedback, never acknowledged server work.
 */
import type {I18nKey} from '../../i18n';
import {McpCommandViews, ownMcpBusyButton, type McpCommandView, type McpDialogView} from './mcp-command-view';

type Result = Record<string, unknown> & {ok?: boolean; status?: string; error?: string};
interface StatusResponse {json(): Promise<Result | null>}
export interface McpCommandPorts {
  api: {
    catalogInstall(id: string, env: Record<string, string>): Promise<Result | null>;
    catalogInstallStatus(id: string): Promise<StatusResponse | null>;
    catalogUninstall(id: string, purge: boolean): Promise<Result | null>;
    connectOne(id: string): Promise<Result | null>;
    connectAll(): Promise<Result | null>;
    updateApply(id: string): Promise<Result | null>;
    serverCreate(payload: Record<string, unknown>): Promise<Result | null>;
  };
  catalog(): readonly {id: string; name: string}[];
  t: (key: I18nKey, values?: Record<string, unknown>) => string;
  detail(error: unknown): string;
  alert(message: string): unknown;
  confirm(message: string, options?: {danger: boolean}): Promise<boolean>;
  log(message: string, level: string): void;
  render(): void;
  refresh(): Promise<void>;
  openInstall(id: string): void;
}
const element = <T extends HTMLElement>(id: string) => document.getElementById(id) as T | null;
const value = (id: string) => element<HTMLInputElement>(id)?.value || '';

class McpCommands {
  readonly views = new McpCommandViews();
  readonly pending: Record<string, string> = Object.create(null);
  private readonly operations = new Map<string, Map<object, string>>();
  private readonly flights = new WeakMap<object, Map<string, Promise<void>>>();
  constructor(private readonly ports: McpCommandPorts) {}

  private run(owner: object, key: string, work: () => Promise<void>): Promise<void> {
    let flights = this.flights.get(owner);
    if (!flights) { flights = new Map(); this.flights.set(owner, flights); }
    const pending = flights.get(key);
    if (pending) return pending;
    let resolve!: () => void, reject!: (error: unknown) => void;
    const promise = new Promise<void>((yes, no) => { resolve = yes; reject = no; });
    flights.set(key, promise);
    const finish = () => { if (flights.get(key) === promise) flights.delete(key); };
    // Invoke synchronously so the click frame displays pending feedback.
    try { void work().then(() => { finish(); resolve(); }, error => { finish(); reject(error); }); }
    catch (error) { finish(); reject(error); }
    return promise;
  }
  private markPending(server: string, kind: string): (skipRender?: boolean) => void {
    let active = this.operations.get(server);
    if (!active) { active = new Map(); this.operations.set(server, active); }
    const token = {};
    active.set(token, kind);
    this.pending[server] = kind;
    this.ports.render();
    return (skipRender = false) => {
      if (!active.delete(token)) return;
      const remaining = [...active.values()];
      if (remaining.length) this.pending[server] = remaining[remaining.length - 1];
      else { this.operations.delete(server); delete this.pending[server]; }
      if (!skipRender) this.ports.render();
    };
  }
  private requireSuccess(data: Result | null, fallback: I18nKey): Result {
    if (data?.ok && data.status !== 'error') return data;
    throw Object.assign(new Error(data?.error || this.ports.t(fallback)), {body: data});
  }
  private failed(view: McpCommandView, key: I18nKey, error: unknown): void {
    const detail = this.ports.detail(error);
    this.ports.log('[MCP] ' + detail, 'error');
    if (view.isCurrent()) this.ports.alert(this.ports.t(key, {err: detail}));
  }
  private async committed(): Promise<void> {
    // Projection failure cannot turn an acknowledged write into a failed write.
    try { await this.ports.refresh(); }
    catch (error) { this.ports.log('[MCP] Catalog refresh failed: ' + this.ports.detail(error), 'error'); }
  }
  private async card(
    server: string, kind: string, view: McpCommandView, action: () => Promise<Result | null>,
    failure: I18nKey, success: (data: Result) => void, fallback?: () => void,
  ): Promise<void> {
    const finish = this.markPending(server, kind);
    let accepted = false;
    try {
      const data = this.requireSuccess(await action(), 'mcp.unknownError');
      accepted = true;
      success(data);
    } catch (error) {
      this.failed(view, failure, error);
      if (view.isCurrent()) fallback?.();
    } finally { finish(accepted); }
    if (accepted) await this.committed();
  }
  reconnect(server: string): Promise<void> {
    const view = this.views.capture();
    if (!view) return Promise.resolve();
    return this.run(this, 'connect:' + server, () => this.card(server, 'connecting', view,
      () => this.ports.api.connectOne(server), 'mcp.connectFailed', data =>
        this.ports.log('[MCP] Reconnected ' + server + ': ' + (data.tools_count || 0) + ' tools', 'success')));
  }
  update(server: string): Promise<void> {
    const view = this.views.capture();
    if (!view) return Promise.resolve();
    return this.run(this, 'update:' + server, () => this.card(server, 'updating', view,
      () => this.ports.api.updateApply(server), 'mcp.updateFailed', data => {
        const message = !data.updated && data.already_latest
          ? '[MCP] ' + server + ' already on latest v' + (data.version || '?')
          : '[MCP] Updated ' + server + ' to v' + (data.version || '?')
            + (data.reconnected ? ' — reconnected with ' + (data.tools_count || 0) + ' tools' : '');
        this.ports.log(message, !data.updated && data.already_latest ? 'info' : 'success');
      }));
  }
  remove(server: string, purge: boolean): Promise<void> {
    const view = this.views.capture();
    if (!view) return Promise.resolve();
    const name = this.ports.catalog().find(entry => entry.id === server)?.name || server;
    return this.run(view, 'remove:' + server + ':' + purge, async () => {
      const confirmed = await this.ports.confirm(this.ports.t(purge ? 'mcp.purgeConfirm' : 'mcp.uninstallConfirm', {name}),
        purge ? {danger: true} : undefined);
      if (!confirmed || !view.isCurrent()) return;
      await this.card(server, 'uninstalling', view, () => this.ports.api.catalogUninstall(server, purge),
        purge ? 'mcp.purgeFailed' : 'mcp.uninstallFailed', data => {
          this.ports.log('[MCP] ' + (purge ? 'Purged ' : 'Uninstalled ') + server
            + (purge ? '' : data.purged ? ' (purged)' : ' (soft, env kept)'), 'info');
        });
    });
  }
  quickInstall(server: string): Promise<void> {
    const view = this.views.capture();
    if (!view || !this.ports.catalog().some(entry => entry.id === server)) return Promise.resolve();
    return this.run(this, 'quick-install:' + server, () => this.card(server, 'installing', view, async () => {
      this.ports.log('[MCP] Quick-installing ' + server + ' (no required env)…', 'info');
      const data = await this.ports.api.catalogInstall(server, {});
      return data?.ok && data.status === 'installing' ? this.pollInstall(server) : data;
    }, 'mcp.quickInstallFailed', data => {
      this.ports.log('[MCP] Installed ' + server + ': ' + (data.tools_count || 0) + ' tools', 'success');
    }, () => this.ports.openInstall(server)));
  }
  async pollInstall(server: string, progress?: (seconds: number) => void): Promise<Result> {
    const started = Date.now();
    // Existing install observation policy: 2.5 seconds, six minutes total.
    while (Date.now() - started < 6 * 60 * 1000) {
      await new Promise<void>(done => window.setTimeout(done, 2500));
      progress?.(Math.round((Date.now() - started) / 1000));
      const response = await this.ports.api.catalogInstallStatus(server);
      if (!response) continue;
      const body = await response.json().catch(() => null);
      if (body && body.status !== 'installing') return body;
    }
    return {ok: false, status: 'error', error: this.ports.t('mcp.installTimeout')};
  }
  connectAll(): Promise<void> {
    const view = this.views.capture();
    if (!view) return Promise.resolve();
    return this.run(this, 'connect-all', async () => {
      const button = element<HTMLButtonElement>('mcpConnectAllBtn');
      const scope = ownMcpBusyButton(view, button, () => {
        if (button) { button.disabled = false; button.textContent = this.ports.t('mcp.connectAll'); }
      });
      if (button) { button.disabled = true; button.textContent = this.ports.t('mcp.connecting'); }
      let accepted = false;
      try {
        const data = this.requireSuccess(await this.ports.api.connectAll(), 'mcp.unknownError');
        accepted = true;
        this.ports.log('[MCP] Connected all: ' + Object.keys(data.servers || {}).length
          + ' server(s), ' + (data.total_tools || 0) + ' tools', 'success');
      } catch (error) { this.failed(view, 'mcp.connectFailed', error); }
      finally { scope.destroy(); }
      if (accepted) await this.committed();
    });
  }
  private dialogStatus(view: McpDialogView, kind: string, text: string): void {
    if (!view.isCurrent()) return;
    const status = element(view.kind === 'install' ? 'mcpInstallStatus' : 'mcpAddStatus');
    if (status) { status.style.display = 'block'; status.className = 'mcp-install-status ' + kind; status.textContent = text; }
  }
  install(): Promise<void> {
    const view = this.views.current('install');
    if (!view?.target) return Promise.resolve();
    return this.run(view, 'install', async () => {
      view.beginSubmission();
      const {id, name} = view.target!, version = view.version, env: Record<string, string> = {};
      document.querySelectorAll<HTMLInputElement>('#mcpInstallFields .mcp-env-input').forEach(input => {
        const key = input.getAttribute('data-key'), content = input.value.trim();
        if (key && content) env[key] = content;
      });
      const button = element<HTMLButtonElement>('mcpInstallBtn');
      const idleLabel = button?.textContent || this.ports.t('mcp.installConnect');
      if (button) { button.disabled = true; button.textContent = this.ports.t('mcp.installing'); }
      this.dialogStatus(view, 'info', this.ports.t('mcp.startingFirstInstall', {name}));
      let accepted = false;
      try {
        let data = await this.ports.api.catalogInstall(id, env);
        if (data?.ok && data.status === 'installing') {
          this.dialogStatus(view, 'info', this.ports.t('mcp.installingDepsName', {name}));
          data = await this.pollInstall(id, n => this.dialogStatus(view, 'info', this.ports.t('mcp.installingDeps', {n})));
        }
        const result = this.requireSuccess(data, 'mcp.installFailedNoConn');
        accepted = true;
        this.dialogStatus(view, 'success', this.ports.t('mcp.installedSuccess', {name, n: result.tools_count || 0}));
        this.ports.log('[MCP] Installed ' + id + ': ' + (result.tools_count || 0) + ' tools', 'success');
        view.finishSuccess(button, idleLabel, version, 1200);
      } catch (error) {
        this.ports.log('[MCP] Install ' + id + ': ' + this.ports.detail(error), 'error');
        this.dialogStatus(view, 'error', '✕ ' + this.ports.detail(error));
      } finally {
        if (!accepted && view.isCurrent() && button) { button.disabled = false; button.textContent = this.ports.t('mcp.retry'); }
      }
      if (accepted) await this.committed();
    });
  }
  private serverPayload(): Record<string, unknown> {
    const name = value('mcpNewName').trim(), transport = value('mcpNewTransport') || 'stdio';
    if (!name) throw new Error(this.ports.t('mcp.needName'));
    const payload: Record<string, unknown> = {name, transport, enabled: true};
    if (transport === 'stdio') {
      payload.command = value('mcpNewCommand');
      payload.args = value('mcpNewArgs').split('\n').map(line => line.trim()).filter(Boolean);
      if (!payload.command) throw new Error(this.ports.t('mcp.needCommand'));
    } else {
      payload.url = value('mcpNewUrl');
      if (!payload.url) throw new Error(this.ports.t('mcp.needUrl'));
    }
    const envText = value('mcpNewEnv');
    if (envText.trim()) {
      const env: Record<string, string> = {};
      for (const line of envText.split('\n')) {
        const equals = line.indexOf('=');
        if (equals > 0) env[line.slice(0, equals).trim()] = line.slice(equals + 1).trim();
      }
      payload.env = env;
    }
    const description = value('mcpNewDesc').trim();
    if (description) payload.description = description;
    return payload;
  }
  saveServer(): Promise<void> {
    const view = this.views.current('add');
    if (!view) return Promise.resolve();
    return this.run(view, 'save', async () => {
      view.beginSubmission();
      const version = view.version, button = element<HTMLButtonElement>('mcpAddSaveBtn');
      let created = false, connected = false;
      try {
        const payload = this.serverPayload(), name = String(payload.name);
        if (button) { button.disabled = true; button.textContent = this.ports.t('mcp.saving'); }
        this.dialogStatus(view, 'info', this.ports.t('mcp.startingName', {name}));
        this.requireSuccess(await this.ports.api.serverCreate(payload), 'mcp.saveFailedNoConn');
        created = true;
        this.requireSuccess(await this.ports.api.connectOne(name), 'mcp.unknownErrorNoConn');
        connected = true;
        this.ports.log('[MCP] Server "' + name + '" saved & connected', 'success');
        this.dialogStatus(view, 'success', this.ports.t('mcp.savedConnected', {name}));
        view.finishSuccess(button, this.ports.t('mcp.saveConnect'), version, 900);
      } catch (error) {
        const detail = this.ports.detail(error);
        this.ports.log('[MCP] Custom server: ' + detail, 'error');
        this.dialogStatus(view, 'error', '✕ ' + (created ? this.ports.t('mcp.connectFailed', {err: detail}) : detail));
      } finally {
        if (!connected && view.isCurrent() && button) { button.disabled = false; button.textContent = this.ports.t('mcp.saveConnect'); }
      }
      if (created) await this.committed();
    });
  }
}

export function createMcpCommands(ports: McpCommandPorts): McpCommands { return new McpCommands(ports); }
