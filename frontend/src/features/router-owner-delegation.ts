/** The owner's delegation to the main coordinator (docs/ROUTER.md R9), shown
 * as "Act for me" (代我操作): a compact disclosure the main coordinator's
 * header hosts with the grant switch, its state and the last audited use.
 * The lifecycle approval gate on the server is the only authority: it grants
 * only for the owner's own browser session (this request) and revokes at
 * once. This module keeps the last read state for presentation, starts no
 * polling or timers, reads once when created and again when the owner opens
 * it. Static labels carry data-i18n keys; the parameterized last-use line
 * repaints on `tofu:language-change`.
 */
import type { RouterOwnerDelegationState } from '../api/router.generated';
import { createChoice } from '../ui';
import { t } from '../i18n';

export interface RouterOwnerDelegationPorts {
  /** GET /api/v4/router/owner-delegation: passive. */
  read(signal?: AbortSignal): Promise<RouterOwnerDelegationState>;
  /** POST /api/v4/router/owner-delegation: the owner's grant or revocation. */
  set(granted: boolean): Promise<RouterOwnerDelegationState>;
}

type Action = RouterOwnerDelegationState['actions'][number];
// Placeholder-free labels only: the runtime re-applies data-i18n keys without parameters.
type StaticKey = 'routerDelegation.summaryOff' | 'routerDelegation.summaryOn' | 'routerDelegation.hint'
  | 'routerDelegation.switch' | 'routerDelegation.advisory' | 'routerDelegation.noUse';

/** Current text now; the i18n runtime re-applies the key on ready() and on switches. */
function localized<E extends HTMLElement>(element: E, key: StaticKey): E {
  element.dataset.i18n = key;
  element.textContent = t(key);
  return element;
}

const failure = (error: unknown) => (error instanceof Error ? error.message : String(error));

export function createRouterOwnerDelegation(ports: RouterOwnerDelegationPorts) {
  let alive = true, pending = false;
  let state: RouterOwnerDelegationState | null = null;
  let reading: AbortController | null = null;

  const settings = document.createElement('details');
  settings.className = 'router-owner-delegation';
  const summary = localized(document.createElement('summary'), 'routerDelegation.summaryOff');
  const body = document.createElement('div');
  body.className = 'router-owner-delegation-body';
  const hint = localized(document.createElement('p'), 'routerDelegation.hint');
  const toggle = createChoice({ type: 'switch', label: '' });
  toggle.el.classList.add('router-owner-delegation-switch');
  localized(toggle.el.querySelector('span')!, 'routerDelegation.switch');
  const advisory = localized(document.createElement('p'), 'routerDelegation.advisory');
  advisory.className = 'router-owner-delegation-advisory';
  advisory.hidden = true;
  const lastUse = document.createElement('p');
  lastUse.className = 'router-owner-delegation-last';
  const error = document.createElement('p');
  error.className = 'router-owner-delegation-error';
  error.setAttribute('role', 'alert');
  error.hidden = true;
  body.append(hint, toggle.el, advisory, lastUse, error);
  settings.append(summary, body);

  function renderLastUse() {
    if (!alive) return;
    const action: Action | undefined = state?.actions[0];
    if (!action) {
      localized(lastUse, 'routerDelegation.noUse');
      lastUse.removeAttribute('title');
      return;
    }
    delete lastUse.dataset.i18n;
    lastUse.textContent = t('routerDelegation.lastUse', {
      time: new Date(action.requestedAt * 1000).toLocaleString(),
      operation: t(`routerDelegation.op.${action.scope}`),
      status: t(`routerDelegation.status.${action.status}`),
      reason: action.reason,
    });
    const detail = action.detail || action.code || '';
    if (detail) lastUse.title = detail; else lastUse.removeAttribute('title');
  }

  function adopt(next: RouterOwnerDelegationState) {
    if (!alive) return;
    state = next;
    toggle.input.checked = next.active;
    localized(summary, next.active ? 'routerDelegation.summaryOn' : 'routerDelegation.summaryOff');
    advisory.hidden = !(next.active && next.grant.assurance === 'advisory');
    renderLastUse();
  }

  function read() {
    reading?.abort();
    const current = reading = new AbortController();
    ports.read(current.signal).then((next) => { if (reading === current) adopt(next); }, () => undefined);
  }

  toggle.input.addEventListener('change', () => {
    if (pending) {
      // The switch shows only server state: a change made while a request runs is not sent.
      toggle.input.checked = !!state?.active;
      return;
    }
    const wanted = toggle.input.checked;
    pending = true;
    toggle.input.disabled = true;
    error.hidden = true;
    reading?.abort();
    ports.set(wanted).then(adopt, (reason) => {
      if (!alive) return;
      toggle.input.checked = !!state?.active;
      error.textContent = t('routerDelegation.failed', { error: failure(reason) });
      error.hidden = false;
    }).finally(() => {
      pending = false;
      if (alive) toggle.input.disabled = false;
    });
  });
  // Opening it shows the latest audited use without polling.
  settings.addEventListener('toggle', () => { if (settings.open && !pending) read(); });
  window.addEventListener('tofu:language-change', renderLastUse);
  renderLastUse();
  read();

  return {
    /** The disclosure the main coordinator's header hosts. */
    settings,
    destroy() {
      alive = false;
      reading?.abort();
      window.removeEventListener('tofu:language-change', renderLastUse);
      settings.remove();
    },
  };
}
