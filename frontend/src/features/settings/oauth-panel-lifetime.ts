/** OAuth presentation resources for one Settings opening.
 * Entry: captureOAuthPanel. Status/egress/reset/device reads share one flight;
 * timers and read cancellation reuse the Settings scope. Auth commands and
 * callback validation remain with their existing owners and outlive the view.
 */
import {createLifecycleScope, type LifecycleScope} from '../../lifecycle';
import {currentSettingsReadSession, type SettingsReadSession} from './panel-read-session';

interface ProviderStatus {
  status?: string;
  authenticated?: boolean;
  device?: unknown;
  egress?: {state?: string};
  reset_offer?: {refreshing?: boolean};
}
type Status = Record<string, ProviderStatus | undefined>;
interface StatusPorts {
  read(options: {signal: AbortSignal}): Promise<Status | null>;
  publish(status: Status): void;
}
interface StatusRead { scope: LifecycleScope; promise: Promise<void>; }

export class OAuthPanelLifetime {
  readonly scope: LifecycleScope;
  private ports: StatusPorts | null = null;
  private pending: StatusRead | null = null;
  private repollTimer = 0;
  private repollAttempts = 0;
  private deviceTimer = 0;
  private device: {provider: string; connected(status: ProviderStatus): void} | null = null;

  constructor(readonly session: SettingsReadSession) {
    this.scope = session.scope;
  }
  isCurrent(): boolean { return this.session.isCurrent(); }

  invalidateStatus(): void {
    this.pending?.scope.destroy();
    this.pending = null;
  }

  loadStatus(ports: StatusPorts, fromRepoll = false): Promise<void> {
    if (!this.isCurrent()) return Promise.resolve();
    this.ports = ports;
    if (!fromRepoll) this.repollAttempts = 0;
    if (this.pending) return this.pending.promise;
    const scope = createLifecycleScope(window);
    scope.listen(this.scope.signal, 'abort', () => scope.destroy(), {once: true});
    const read: StatusRead = {scope, promise: Promise.resolve().then(async () => {
      try {
        const status = await ports.read({signal: scope.signal});
        if (!status || !this.isCurrent() || this.pending !== read || scope.signal.aborted) return;
        this.publishStatus(status);
      } catch (error) {
        if (this.isCurrent() && !scope.signal.aborted) console.warn('[OAuth] Failed to load status:', error);
      } finally {
        if (this.pending === read) this.pending = null;
        scope.destroy();
      }
    })};
    this.pending = read;
    return read.promise;
  }

  private publishStatus(status: Status): void {
    // The renderer may itself stop device polling or close/reopen Settings.
    const device = this.device;
    this.ports?.publish(status);
    if (!this.isCurrent()) return;
    if (device) {
      const result = status[device.provider];
      if (!result || result.authenticated || !result.device
          || ['error', 'timeout', 'not_started'].includes(result.status || '')) {
        this.stopDevicePoll();
        if (result?.authenticated) device.connected(result);
      }
    }
    if (!this.isCurrent()) return;
    const warming = [status.claude, status.codex].some(provider => provider?.egress?.state === 'unknown')
      || !!status.codex?.reset_offer?.refreshing;
    if (warming) this.scheduleRepoll();
    else {
      this.scope.cancelTimeout(this.repollTimer);
      this.repollTimer = this.repollAttempts = 0;
    }
  }

  scheduleRepoll(): void {
    // Existing transient-status policy: 2 seconds, at most 8 re-reads.
    if (!this.isCurrent() || this.repollTimer || this.repollAttempts >= 8) return;
    this.repollAttempts++;
    this.repollTimer = this.scope.timeout(() => {
      this.repollTimer = 0;
      if (this.ports) void this.loadStatus(this.ports, true);
    }, 2000);
  }

  startDevicePoll(provider: string, refresh: () => void, connected: (status: ProviderStatus) => void): void {
    if (this.device?.provider === provider && this.deviceTimer) return;
    this.stopDevicePoll();
    if (!this.isCurrent()) return;
    this.device = {provider, connected};
    this.deviceTimer = this.scope.interval(refresh, 3000);
  }

  stopDevicePoll(): void {
    this.scope.cancelInterval(this.deviceTimer);
    this.deviceTimer = 0;
    this.device = null;
  }
}

const panels = new WeakMap<SettingsReadSession, OAuthPanelLifetime>();
export function captureOAuthPanel(): OAuthPanelLifetime | null {
  const session = currentSettingsReadSession();
  if (!session?.isCurrent()) return null;
  let panel = panels.get(session);
  if (!panel) { panel = new OAuthPanelLifetime(session); panels.set(session, panel); }
  return panel;
}
