/** Settings presentation lifetime. A new opening or close retires only reads
 * and view resources; routing writes retain their existing serialized owner.
 * Entry points: beginSettingsReadSession, currentSettingsReadSession, retireSettingsReads.
 */
import {createLifecycleScope, type LifecycleScope} from '../../lifecycle';

export interface SettingsReadSession {
  readonly scope: LifecycleScope;
  readonly ready: Promise<boolean>;
  completeLoading(ready: boolean): void;
  isCurrent(): boolean;
}

let current: SettingsReadSession | null = null;

export function currentSettingsReadSession(): SettingsReadSession | null { return current; }

export function retireSettingsReads(): void {
  const retired = current;
  current = null;
  retired?.completeLoading(false);
  retired?.scope.destroy();
}

export function beginSettingsReadSession(panels: readonly HTMLElement[] = []): SettingsReadSession {
  retireSettingsReads();
  const modal = document.getElementById('settingsModal');
  const scope = createLifecycleScope(window);
  const controls = createLifecycleScope(window);
  scope.add(() => controls.destroy());
  for (const panel of panels) {
    const inert = panel.getAttribute('inert');
    const busy = panel.getAttribute('aria-busy');
    panel.setAttribute('inert', '');
    panel.setAttribute('aria-busy', 'true');
    scope.add(() => {
      if (busy === null) panel.removeAttribute('aria-busy');
      else panel.setAttribute('aria-busy', busy);
    });
    controls.add(() => {
      if (inert === null) panel.removeAttribute('inert');
      else panel.setAttribute('inert', inert);
    });
  }
  let resolve!: (ready: boolean) => void;
  const ready = new Promise<boolean>(done => { resolve = done; });
  let settled = false;
  const session: SettingsReadSession = {
    scope, ready,
    completeLoading(success) {
      if (settled) return;
      settled = true;
      for (const panel of panels) panel.setAttribute('aria-busy', 'false');
      if (success) controls.destroy();
      resolve(success);
    },
    isCurrent: () => current === session && !scope.signal.aborted
      && document.getElementById('settingsModal') === modal,
  };
  current = session;
  scope.listen(window, 'pagehide', retireSettingsReads);
  return session;
}
