/** Mount the production Settings scope in isolated classic presenter fixtures.
 * Minimal legacy windows supply their clock globally; event ownership uses a
 * real EventTarget when that fixture does not provide a browser window.
 */
import {beginSettingsReadSession} from '../../frontend/src/features/settings/panel-read-session';

export function mountSettingsPresenterFixture(): void {
  window.setInterval ??= globalThis.setInterval.bind(globalThis);
  window.clearInterval ??= globalThis.clearInterval.bind(globalThis);
  window.setTimeout ??= globalThis.setTimeout.bind(globalThis);
  window.clearTimeout ??= globalThis.clearTimeout.bind(globalThis);
  if (typeof window.addEventListener !== 'function') {
    const events = new EventTarget();
    Object.assign(window, {
      addEventListener: events.addEventListener.bind(events),
      removeEventListener: events.removeEventListener.bind(events),
      dispatchEvent: events.dispatchEvent.bind(events),
    });
  }
  beginSettingsReadSession().completeLoading(true);
}
