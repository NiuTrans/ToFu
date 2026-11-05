/** Router Watchdog (docs/ROUTER.md R7), shown to the owner as health checks
 * (故障巡检). It owns a compact settings disclosure that the main
 * coordinator's header hosts — turn health checks on (ensures the Watchdog),
 * the periodic review switch and its cron cadence, and a way into the Watchdog
 * conversation — and a one-line header on that conversation with a way back.
 * Other surfaces call the same `ensure` before opening the Watchdog
 * conversation (e.g. the studio's compute-stall dialog). The Watchdog has no
 * sidebar entry: the owner hears from it through the main coordinator. The
 * server binding and the scheduler task are the only authorities; this module
 * keeps the last read state for presentation, starts no polling or timers and
 * performs no work before the user acts beyond one passive read. Static labels
 * carry data-i18n attributes, so the i18n runtime translates them once the
 * catalog is ready and on every language switch.
 */
import type { RouterWatchdogReviewRequest, RouterWatchdogState } from '../api/router.generated';
import { createButton, createChoice, createField, setFieldError } from '../ui';
import { t } from '../i18n';

export interface RouterWatchdogPorts {
  /** GET /api/v4/router/watchdog: passive, never creates the Watchdog. */
  read(signal?: AbortSignal): Promise<RouterWatchdogState>;
  /** POST /api/v4/router/watchdog: ensures the Watchdog conversation. */
  ensure(): Promise<RouterWatchdogState>;
  /** POST /api/v4/router/watchdog/review: the owner's review choice. */
  configureReview(request: RouterWatchdogReviewRequest): Promise<RouterWatchdogState>;
  open(conversationId: string): void;
  /** Returns to the main coordinator conversation. */
  back(): void;
  activeConversationId(): string;
  notifyError(message: string): void;
  /** The Watchdog path or its conversation became known or changed. */
  changed?(): void;
}

export interface RouterWatchdogHosts {
  /** Receives the compact header of the Watchdog conversation. */
  header: HTMLElement;
}

// Placeholder-free labels only: the runtime re-applies data-i18n keys without parameters.
type StaticKey = 'routerWatchdog.summaryOff' | 'routerWatchdog.summaryOn' | 'routerWatchdog.summaryPeriodic'
  | 'routerWatchdog.enableHint' | 'routerWatchdog.enable' | 'routerWatchdog.review' | 'routerWatchdog.schedule'
  | 'routerWatchdog.save' | 'routerWatchdog.open' | 'routerWatchdog.headerSubtitle' | 'routerWatchdog.back';

/** Current text now; the i18n runtime re-applies the key on ready() and on switches. */
function localized<E extends HTMLElement>(element: E, key: StaticKey): E {
  element.dataset.i18n = key;
  element.textContent = t(key);
  return element;
}

export function createRouterWatchdogEntry(ports: RouterWatchdogPorts, hosts: RouterWatchdogHosts) {
  let alive = true, pending = false;
  let state: RouterWatchdogState | null = null;
  // The Watchdog path, known from any read (also while unbound).
  let location = '';
  const reading = new AbortController();

  const settings = document.createElement('details');
  settings.className = 'router-watchdog';
  const summary = localized(document.createElement('summary'), 'routerWatchdog.summaryOff');
  const off = document.createElement('div');
  off.className = 'router-watchdog-off';
  const hint = localized(document.createElement('p'), 'routerWatchdog.enableHint');
  const enable = localized(createButton({ label: '', size: 'sm', variant: 'secondary' }), 'routerWatchdog.enable');
  off.append(hint, enable);
  const on = document.createElement('div');
  on.className = 'router-watchdog-on';
  on.hidden = true;
  const review = createChoice({ type: 'switch', label: '' });
  review.el.classList.add('router-watchdog-review');
  localized(review.el.querySelector('span')!, 'routerWatchdog.review');
  const schedule = createField({ label: '' });
  schedule.el.classList.add('router-watchdog-schedule');
  localized(schedule.el.querySelector('label')!, 'routerWatchdog.schedule');
  const save = localized(createButton({ label: '', size: 'sm', variant: 'secondary' }), 'routerWatchdog.save');
  const open = localized(createButton({ label: '', size: 'sm', variant: 'ghost' }), 'routerWatchdog.open');
  on.append(review.el, schedule.el, save, open);
  settings.append(summary, off, on);
  const cron = schedule.control as HTMLInputElement;

  const header = document.createElement('header');
  header.className = 'router-watchdog-header';
  header.hidden = true;
  const subtitle = localized(document.createElement('span'), 'routerWatchdog.headerSubtitle');
  subtitle.className = 'router-watchdog-header-subtitle';
  const back = localized(createButton({ label: '', size: 'sm', variant: 'ghost' }), 'routerWatchdog.back');
  header.append(subtitle, back);
  hosts.header.append(header);

  function adopt(next: RouterWatchdogState) {
    if (!alive) return;
    const identityChanged = next.projectPath !== location
      || (next.availability === 'available' ? next.conversationId : '') !== (state?.conversationId ?? '');
    state = next.availability === 'available' ? next : null;
    location = next.projectPath;
    localized(summary, !state ? 'routerWatchdog.summaryOff'
      : state.review.enabled ? 'routerWatchdog.summaryPeriodic' : 'routerWatchdog.summaryOn');
    off.hidden = !!state;
    on.hidden = !state;
    if (state) {
      review.input.checked = state.review.enabled;
      cron.value = state.review.schedule;
      setFieldError(schedule, null);
    }
    refresh();
    if (identityChanged) ports.changed?.();
  }

  function refresh() {
    if (!alive) return;
    header.hidden = !state || ports.activeConversationId() !== state.conversationId;
  }

  async function busy(control: HTMLButtonElement | HTMLInputElement, work: () => Promise<void>) {
    if (pending) return;
    pending = true; control.disabled = true; control.setAttribute('aria-busy', 'true');
    try {
      await work();
    } finally {
      pending = false;
      if (alive) { control.disabled = false; control.removeAttribute('aria-busy'); }
    }
  }

  const failure = (error: unknown) => (error instanceof Error ? error.message : String(error));

  /** Ensures the Watchdog conversation and adopts its state; resolves its ID or
   * rejects with the localized reason it is unavailable. */
  async function ensure(): Promise<string> {
    let ensured: RouterWatchdogState;
    try {
      ensured = await ports.ensure();
    } catch (error) {
      throw new Error(t('routerWatchdog.unavailable', { error: failure(error) }));
    }
    if (alive) adopt(ensured);
    if (ensured.availability !== 'available') throw new Error(t('routerWatchdog.unavailable', { error: ensured.availability }));
    return ensured.conversationId;
  }

  // Turning health checks on provisions the Watchdog; the owner stays where they are.
  enable.addEventListener('click', () => busy(enable, async () => {
    try {
      await ensure();
    } catch (error) {
      if (alive) ports.notifyError(failure(error));
    }
  }));

  async function configure(control: HTMLButtonElement | HTMLInputElement) {
    if (!state) return;
    if (pending) {
      // The switch shows only server state: a change made while a request runs is not sent.
      review.input.checked = state.review.enabled;
      return;
    }
    const wanted = review.input.checked;
    const cadence = cron.value.trim();
    await busy(control, async () => {
      try {
        const next = await ports.configureReview({ enabled: wanted, ...(cadence ? { schedule: cadence } : {}) });
        if (alive) adopt(next);
      } catch (error) {
        if (!alive) return;
        if (state) review.input.checked = state.review.enabled;
        setFieldError(schedule, t('routerWatchdog.reviewFailed', { error: failure(error) }));
      }
    });
  }
  review.input.addEventListener('change', () => { void configure(review.input); });
  save.addEventListener('click', () => { void configure(save); });
  open.addEventListener('click', () => { if (state) ports.open(state.conversationId); });
  back.addEventListener('click', () => ports.back());

  // One passive read so the settings and header reflect an existing Watchdog;
  // failure only means health checks show as off until the owner turns them on.
  ports.read(reading.signal).then(adopt, () => undefined);

  return {
    /** The disclosure the main coordinator's header hosts. */
    settings,
    refresh,
    ensure,
    /** The Watchdog path ('' until a read answers); a coordinator there is the Watchdog. */
    location: () => location,
    /** The bound Watchdog conversation the header marks ('' unless available). */
    conversationId: () => state?.conversationId ?? '',
    destroy() {
      alive = false; reading.abort(); settings.remove(); header.remove();
    },
  };
}
