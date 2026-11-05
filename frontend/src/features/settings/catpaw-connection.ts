/** Catpaw connection intent and asynchronous lifetime. The Settings adapter
 * owns DOM/API wiring; Provider mutations own writes and authority adoption.
 * Closing retires reads/login/poll callbacks, while accepted writes settle.
 */
import type {LifecycleScope} from '../../lifecycle';
import type {CatpawLocalImportStatusResponse, CatpawLocalImportLoginResponse} from '../../api/api-v1-model-routing.generated';
import type {ModelRoutingMutationResult} from './model-routing-authority';

// Existing CLI-login liveness contract: FRONTEND_ARCHITECTURE.md,
// “Catpaw connection lifetime”. These are preserved policy, not tuning knobs.
const CATPAW_LOGIN_STATUS_CHECKS = 60;
const CATPAW_LOGIN_STATUS_INTERVAL_MS = 3_000;

export type CatpawStatus = Pick<CatpawLocalImportStatusResponse, 'available' | 'connected'> &
  Partial<Pick<CatpawLocalImportStatusResponse, 'installed' | 'model_count'>>;
export interface CatpawView {status: CatpawStatus | null; busy: boolean; waiting: boolean}
interface ConnectionPorts {
  scope(): LifecycleScope;
  status(): Promise<CatpawStatus>;
  login(): Promise<CatpawLocalImportLoginResponse>;
  import(): Promise<ModelRoutingMutationResult & {status: CatpawStatus}>;
  prepareDisconnect(): Promise<(() => Promise<ModelRoutingMutationResult>) | null>;
  confirmDisconnect(): Promise<boolean>;
  openLogin(url: string): void;
  render(view: CatpawView): void;
  failed(operation: 'connect' | 'disconnect', error: unknown): void;
  savedWithoutRefresh(): void;
}

class CatpawConnection {
  private scope: LifecycleScope | null = null;
  private generation = 0;
  private autoImportAllowed = true;
  private autoAttempted = false;
  private status: CatpawStatus | null = null;
  private loading = false;
  private waiting = false;
  private pollTimer: number | null = null;
  private pollsRemaining = 0;
  private statusRead: {generation: number; promise: Promise<CatpawStatus>} | null = null;
  private pending: {kind: 'connect' | 'disconnect'; promise: Promise<void>; writing: boolean} | null = null;

  constructor(private readonly ports: ConnectionPorts) {}

  open(): Promise<void> {
    this.close();
    this.scope = this.ports.scope();
    this.status = null;
    return this.refresh();
  }

  close(): void {
    this.generation++;
    this.scope?.destroy();
    this.scope = null;
    this.pollTimer = null;
    this.statusRead = null;
    this.status = null;
    this.loading = false;
    this.waiting = false;
    // An old confirmation/status/login wait cannot hold a reopened panel.
    // Accepted routing writes retain their promise until authority settles.
    if (!this.pending?.writing) this.pending = null;
  }

  async refresh(): Promise<void> {
    const generation = this.generation;
    if (!this.current(generation)) return;
    this.loading = true; this.publish();
    if (this.pending) await this.pending.promise;
    if (!this.current(generation)) return;
    try {
      const status = await this.readStatus(generation);
      if (!this.current(generation)) return;
      this.status = status;
      if (status.connected) { this.autoAttempted = true; this.waiting = false; this.stopPoll(); }
      if (status.available && !status.connected && this.autoImportAllowed && !this.autoAttempted) {
        await this.connectWithStatus(true, status);
      }
    } catch { if (this.current(generation)) this.status = {available: false, connected: false}; }
    finally { if (this.current(generation)) { this.loading = false; this.publish(); } }
  }

  connect(): Promise<void> { return this.connectWithStatus(false); }

  /** A confirmed deletion from the ordinary Provider view supersedes pending
   * onboarding reads too, even before the first status response has arrived.
   * Its actual write still belongs to the common Provider mutation command.
   */
  async providerRemoval<T>(remove: () => Promise<T>): Promise<T> {
    this.generation++;
    this.autoImportAllowed = false;
    this.autoAttempted = true;
    this.loading = false;
    this.waiting = false;
    this.stopPoll();
    this.statusRead = null;
    if (!this.pending?.writing) this.pending = null;
    this.publish();
    try { return await remove(); }
    finally { void this.refresh(); }
  }

  disconnect(): Promise<void> {
    return this.command('disconnect', async generation => {
      const remove = await this.ports.prepareDisconnect();
      if (!this.current(generation) || !await this.ports.confirmDisconnect() || !this.current(generation)) return;
      this.autoImportAllowed = false;
      this.autoAttempted = true;
      this.waiting = false;
      if (remove && this.pending) this.pending.writing = true;
      const result = remove ? await remove() : {refreshed: true};
      if (!this.current(generation)) return;
      this.status = {...this.status, available: !!this.status?.available, connected: false};
      if (!result.refreshed) this.ports.savedWithoutRefresh();
      // This read cannot import: the explicit disconnect intent is retained
      // across panel close/reopen until the next manual connect command.
      try {
        const status = await this.readStatus(generation);
        if (this.current(generation)) this.status = status;
      } catch { /* The acknowledged deletion remains successful. */ }
    });
  }

  private current(generation: number): boolean { return !!this.scope && this.generation === generation; }

  private publish(): void {
    if (this.scope) this.ports.render({status: this.status, busy: this.loading || !!this.pending, waiting: this.waiting});
  }

  private readStatus(generation: number): Promise<CatpawStatus> {
    if (this.statusRead?.generation === generation) return this.statusRead.promise;
    const promise = Promise.resolve().then(() => this.ports.status()).finally(() => {
      if (this.statusRead?.promise === promise) this.statusRead = null;
    });
    this.statusRead = {generation, promise};
    return promise;
  }

  private connectWithStatus(silent: boolean, status?: CatpawStatus): Promise<void> {
    return this.command('connect', async generation => {
      if (!silent) this.autoImportAllowed = true;
      const current = status || await this.readStatus(generation);
      if (!this.current(generation)) return;
      this.status = current;
      this.waiting = false;
      if (current.connected) { this.autoAttempted = true; return; }
      if (current.available) {
        this.autoAttempted = true;
        if (this.pending) this.pending.writing = true;
        const result = await this.ports.import();
        if (!this.current(generation)) return;
        this.status = result.status;
        if (!result.refreshed) this.ports.savedWithoutRefresh();
      } else {
        const login = await this.ports.login();
        if (!this.current(generation)) return;
        if (login.login_url) {
          this.ports.openLogin(login.login_url);
          this.waiting = true; this.pollsRemaining = CATPAW_LOGIN_STATUS_CHECKS;
        } else this.status = {available: false, connected: false, installed: login.installed};
      }
    }, silent);
  }

  private command(kind: 'connect' | 'disconnect', work: (generation: number) => Promise<void>, silent = false): Promise<void> {
    if (!this.scope) return Promise.resolve();
    if (this.pending) {
      if (this.pending.kind === kind) return this.pending.promise;
      const scope = this.scope;
      return this.pending.promise.then(() => this.scope === scope ? this.command(kind, work, silent) : undefined);
    }
    const generation = ++this.generation;
    this.loading = false; this.stopPoll();
    const promise = Promise.resolve().then(() => {
      if (this.current(generation)) return work(generation);
    }).catch(error => {
      if (this.current(generation) && !silent) this.ports.failed(kind, error);
    }).finally(() => {
      if (this.pending?.promise === promise) this.pending = null;
      if (this.current(generation)) { this.publish(); this.schedulePoll(generation); }
    });
    this.pending = {kind, promise, writing: false};
    this.publish();
    return promise;
  }

  private stopPoll(): void {
    if (this.pollTimer !== null) this.scope?.cancelTimeout(this.pollTimer);
    this.pollTimer = null;
  }

  private schedulePoll(generation: number): void {
    if (!this.current(generation) || !this.waiting || this.pollTimer !== null) return;
    this.pollTimer = this.scope!.timeout(() => {
      if (!this.current(generation)) return;
      this.pollTimer = null;
      if (this.pollsRemaining-- <= 0) {
        this.waiting = false; this.loading = false;
        this.generation++; this.statusRead = null;
        this.publish(); return;
      }
      // Keep the fixed polling window running during a slow read, but never
      // overlap requests or attach another continuation to the same read.
      if (this.statusRead?.generation !== generation) void this.poll(generation);
      this.schedulePoll(generation);
    }, CATPAW_LOGIN_STATUS_INTERVAL_MS);
  }

  private async poll(generation: number): Promise<void> {
    try {
      const status = await this.readStatus(generation);
      if (!this.current(generation)) return;
      this.status = status;
      if (status.connected) { this.autoAttempted = true; this.waiting = false; this.stopPoll(); }
      else if (status.available && this.autoImportAllowed) {
        this.waiting = false;
        await this.connectWithStatus(true, status);
        return;
      }
    } catch { /* A transient read failure consumes this bounded poll only. */ }
    if (this.current(generation)) this.publish();
  }
}

export function createCatpawConnection(ports: ConnectionPorts): CatpawConnection {
  return new CatpawConnection(ports);
}
