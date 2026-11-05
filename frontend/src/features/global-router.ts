/** Global Router (docs/ROUTER.md), shown to the owner as the main coordinator
 * (总负责人). It owns a leader card that sits in the same sidebar slot and
 * style as a project's coordinator card (presence shows it where the list has
 * no project) and ensures and opens the router conversation, plus a compact
 * header on that conversation: one sentence on what it does and its settings
 * (Worker model, standing instructions, then controls other owners contribute,
 * such as health checks). The server binding is the only authority; this
 * module keeps the last read state for presentation, starts no polling or
 * timers, and performs no work before the user acts beyond one passive read.
 * It is created before the locale catalog loads, so every static label carries
 * a data-i18n key and parameterized text repaints on `tofu:language-change`.
 */
import type { RouterState } from '../api/router.generated';
import { t } from '../i18n';
import { createRouterInstructions, type RouterInstructionPorts } from './router-instructions';
import { createRouterWorkerModel, type RouterWorkerModelControl, type RouterWorkerModelPorts } from './router-worker-model';
import '../styles/application/33-project-coordination.css';

export interface GlobalRouterPorts {
  /** GET /api/v4/router: passive, never creates the router. */
  read(signal?: AbortSignal): Promise<RouterState>;
  /** POST /api/v4/router: ensures the router home and conversation. */
  ensure(): Promise<RouterState>;
  open(conversationId: string): void;
  activeConversationId(): string;
  notifyError(message: string): void;
  /** The router home or its conversation became known or changed. */
  changed?(): void;
  workerModel: RouterWorkerModelPorts;
  instructions: RouterInstructionPorts;
}

export interface GlobalRouterHosts {
  /** Receives the leader card. */
  sidebar: HTMLElement;
  /** Receives the compact conversation header. */
  header: HTMLElement;
  /** Settings other owners contribute to the header, kept after the router's own. */
  controls?: readonly HTMLElement[];
}

type StaticLabel = 'globalRouter.entry' | 'globalRouter.entryMeta' | 'globalRouter.headerSubtitle';

/** A text node owner the locale catalog re-translates, whenever it was created. */
function staticLabel(key: StaticLabel, className: string): HTMLSpanElement {
  const label = document.createElement('span');
  label.className = className;
  label.dataset.i18n = key;
  label.textContent = t(key);
  return label;
}

function routerMark(): SVGSVGElement {
  const ns = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(ns, 'svg');
  svg.setAttribute('viewBox', '0 0 16 16');
  svg.setAttribute('aria-hidden', 'true'); svg.setAttribute('focusable', 'false');
  const ring = document.createElementNS(ns, 'circle');
  ring.setAttribute('cx', '8'); ring.setAttribute('cy', '8'); ring.setAttribute('r', '6');
  ring.setAttribute('fill', 'none'); ring.setAttribute('stroke', 'currentColor'); ring.setAttribute('stroke-width', '1.4');
  const hub = document.createElementNS(ns, 'circle');
  hub.setAttribute('cx', '8'); hub.setAttribute('cy', '8'); hub.setAttribute('r', '2'); hub.setAttribute('fill', 'currentColor');
  svg.append(ring, hub);
  return svg;
}

export function createGlobalRouterEntry(ports: GlobalRouterPorts, hosts: GlobalRouterHosts) {
  let alive = true, pending = false;
  let state: RouterState | null = null;
  // The router home path, known from any read (also while unbound).
  let home = '';
  let workerModel: RouterWorkerModelControl | null = null;
  let instructions: ReturnType<typeof createRouterInstructions> | null = null;
  const reading = new AbortController();

  // The project coordinator card's shape and classes: one kind of thing, two scopes.
  const card = document.createElement('section');
  card.className = 'coordination-leader global-router-card';
  card.tabIndex = 0;
  card.setAttribute('role', 'button');
  card.dataset.i18nTitle = 'globalRouter.entryTitle';
  card.title = t('globalRouter.entryTitle');
  const mark = document.createElement('span');
  mark.className = 'coordination-leader-mark';
  mark.setAttribute('aria-hidden', 'true');
  mark.append(routerMark());
  const body = document.createElement('span');
  body.className = 'coordination-leader-body';
  body.append(staticLabel('globalRouter.entry', 'coordination-leader-label'),
    staticLabel('globalRouter.entryMeta', 'coordination-leader-meta'));
  const go = document.createElement('span');
  go.className = 'coordination-leader-go';
  go.setAttribute('aria-hidden', 'true');
  go.textContent = '›';
  card.append(mark, body, go);
  hosts.sidebar.append(card);

  const header = document.createElement('header');
  header.className = 'global-router-header';
  header.hidden = true;
  header.append(staticLabel('globalRouter.headerSubtitle', 'global-router-header-subtitle'), ...(hosts.controls ?? []));
  hosts.header.append(header);
  const contributed = hosts.controls?.[0] ?? null;

  function adopt(next: RouterState) {
    if (!alive) return;
    const identityChanged = next.projectPath !== home
      || (next.availability === 'available' ? next.conversationId : '') !== (state?.conversationId ?? '');
    home = next.projectPath;
    state = next.availability === 'available' ? next : null;
    if (state && workerModel?.path === state.projectPath) workerModel.update(state);
    else {
      workerModel?.destroy(); workerModel = null;
      if (state) {
        workerModel = createRouterWorkerModel(ports.workerModel, state.projectPath, state);
        header.insertBefore(workerModel.el, instructions?.el ?? contributed);
      }
    }
    if (state && !instructions) {
      instructions = createRouterInstructions(ports.instructions);
      header.insertBefore(instructions.el, contributed);
    } else if (!state) { instructions?.destroy(); instructions = null; }
    refresh();
    if (identityChanged) ports.changed?.();
  }

  function refresh() {
    if (!alive) return;
    const active = !!state && ports.activeConversationId() === state.conversationId;
    header.hidden = !active;
    if (active) card.setAttribute('aria-current', 'page');
    else card.removeAttribute('aria-current');
  }

  async function activate() {
    if (pending) return;
    pending = true; card.setAttribute('aria-busy', 'true'); card.setAttribute('aria-disabled', 'true');
    try {
      const ensured = await ports.ensure();
      if (!alive) return;
      adopt(ensured);
      if (state) { ports.open(state.conversationId); refresh(); }
      else ports.notifyError(t('globalRouter.unavailable', { error: ensured.availability }));
    } catch (error) {
      if (alive) ports.notifyError(t('globalRouter.unavailable', {
        error: error instanceof Error ? error.message : String(error) }));
    } finally {
      pending = false;
      if (alive) { card.removeAttribute('aria-busy'); card.removeAttribute('aria-disabled'); }
    }
  }
  card.addEventListener('click', () => { void activate(); });
  card.addEventListener('keydown', (event) => {
    if (event.key !== 'Enter' && event.key !== ' ') return;
    event.preventDefault(); void activate();
  });

  // One passive read so the card and header can identify an existing router;
  // failure only means the header stays hidden until the card is used.
  ports.read(reading.signal).then(adopt, () => undefined);

  return {
    refresh,
    /** The router home path ('' until a read answers); a Project Router there is this router. */
    home: () => home,
    /** The bound router conversation the card and header mark ('' unless available). */
    conversationId: () => state?.conversationId ?? '',
    destroy() {
      alive = false; reading.abort();
      workerModel?.destroy(); instructions?.destroy();
      card.remove(); header.remove();
    },
  };
}
