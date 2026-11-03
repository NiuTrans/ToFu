/** Conversation-first project navigation. Server binding alone confers the role.
 * The Coordinator chat shows only the Coordinator's task board (session-free:
 * no sessions, references, graphs or usage); questions are ask_human cards in
 * the chat. Opening the visible Coordinator chat reads its latest message,
 * exactly like a messaging app. No polling.
 */
import { createButton } from '../ui';
import { t } from '../i18n';
import { apiFailure, isTransientApiFailure } from '../api/errors';
import { coordinatorProgressHeadline, createCoordinatorProgress, type CoordinatorProgressView } from './coordinator-progress';
import type { CoordinatorProgressBoard, CoordinatorReportState } from '../api/project-coordination.generated';
import { coordinationText } from './project-coordination-board';
import { createRouterWorkerModel, type RouterWorkerModelControl } from './router-worker-model';
import type { ProjectCoordinationPorts } from './project-coordination';
import type { CoordinationBinding } from '../api/project-coordination.generated';
import '../styles/application/33-project-coordination.css';

/** Hub-with-satellites mark: the sidebar entry needed a recognizable identity,
 * not a bare text row that read as a stray label. */
function coordinationMark(): SVGSVGElement {
  const ns = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(ns, 'svg');
  svg.setAttribute('viewBox', '0 0 16 16');
  svg.setAttribute('aria-hidden', 'true'); svg.setAttribute('focusable', 'false');
  const node = (cx: string, cy: string, r: string) => {
    const circle = document.createElementNS(ns, 'circle');
    circle.setAttribute('cx', cx); circle.setAttribute('cy', cy); circle.setAttribute('r', r);
    circle.setAttribute('fill', 'currentColor'); return circle;
  };
  for (const [x, y] of [['8', '3'], ['3', '12'], ['13', '12']]) {
    const spoke = document.createElementNS(ns, 'line');
    spoke.setAttribute('x1', '8'); spoke.setAttribute('y1', '8'); spoke.setAttribute('x2', x); spoke.setAttribute('y2', y);
    spoke.setAttribute('stroke', 'currentColor'); spoke.setAttribute('stroke-width', '1');
    spoke.setAttribute('opacity', '0.45'); svg.append(spoke);
  }
  svg.append(node('8', '8', '2.6'), node('8', '3', '1.4'), node('3', '12', '1.4'), node('13', '12', '1.4'));
  return svg;
}

export function createProjectCoordinationSurface(ports: {
  sidebar: HTMLElement; canvas: HTMLElement;

  mode?: 'sidebar' | 'canvas';
  scope(): ProjectCoordinationPorts | null;
  sidebarPath(): string;
  details(scope: ProjectCoordinationPorts): void;
  create(scope: ProjectCoordinationPorts, binding: CoordinationBinding): Promise<void> | void;

  canCreate?(): boolean;

  changed?(): void;
}) {
  let alive = true, generation = 0, pending = false;
  let flight: AbortController | null = null;
  // Failed binding demand that recovery signals retry: either shown, or kept stale behind a confirmed binding.
  let bindingFailed = false, bindingStale = false;
  // The board starts folded to its one-line head: the sidebar already lists the
  // Coordinator's work, so the chat keeps its reading space until the owner opens it.
  let signature = '', projectSignature = '', boardOpen = false, finishedOpen = false;

  let rendered = '';
  let creating = false;
  /* Folder selection is local state outside the read key and the painted facts:
   * selecting the folder of a project the open conversation already showed
   * changes neither, so every refresh re-evaluates the painted Create control. */
  let createControl: { button: HTMLButtonElement; hint: HTMLElement } | undefined;
  const showCreateAvailability = (control: { button: HTMLButtonElement; hint: HTMLElement }) => {
    const allowed = ports.canCreate?.() !== false;
    control.button.disabled = creating || !allowed; control.hint.hidden = allowed;
  };
  let lastReport: CoordinatorReportState | undefined;
  // The exact report this mount acknowledged; a failed command clears it for retry.
  let acknowledgedTurnId = '';
  let unreadInView: (() => void) | null = null;
  /* The Coordinator's Worker model control survives repaints of the same
   * Coordinator conversation: it stays attached (an open choice is never torn
   * down by a push frame) and reads its value once per visit. */
  let workerModel: { key: string; control: RouterWorkerModelControl } | undefined;
  function dropWorkerModel() { workerModel?.control.destroy(); workerModel = undefined; }
  let unsubscribe: (() => void) | undefined;
  let subscribedPath = '';
  const norm = (path: string) => path.replace(/[/\\]+$/, '');
  /* Last resolved Coordinator per project. A folder switch can paint its row
   * from this cache before the binding read lands, so the conversation list
   * filters the same chat on the first frame and the row never pops in late. */
  const coordinatorByPath = new Map<string, string>();
  // Keep rejection handling attached even if navigation retires this demand.
  const read = <T>(request: () => Promise<T>): Promise<T> => {
    try { return request(); } catch (error) { return Promise.reject(error); }
  };
  /* Every read is a project-level fact, so one settled set remains a valid
   * first paint for any conversation of that project while fresh reads run. */
  const readProject = (scope: ProjectCoordinationPorts, binding: Promise<CoordinationBinding>, signal: AbortSignal) => Promise.allSettled([
    binding,
    scope.progress ? read(() => scope.progress!.read(scope.path, signal)) : Promise.resolve(null),
    scope.reports ? read(() => scope.reports!.read(scope.path, signal)) : Promise.resolve(undefined),
  ]);
  type ProjectReads = Awaited<ReturnType<typeof readProject>>;
  /* Newest read set of the mounted project whose binding resolved. A
   * conversation switch inside that project repaints the canvas from it at
   * once: blanking it until every fresh read landed (up to seconds) collapsed
   * the strip above the transcript, which then pushed the reading down again. */
  let settled: { path: string; reads: ProjectReads } | undefined;
  /* The task board belongs to the binding, not to one chat, so arriving at the
   * Coordinator paints it at once too; paint() shows it only in that chat. */
  const resumableReads = (scope: ProjectCoordinationPorts | null): ProjectReads | undefined =>
    ports.mode !== 'sidebar' && scope && settled?.path === scope.path ? settled.reads : undefined;
  function paintSidebarPending(path: string) {
    const row = document.createElement('section');
    row.className = 'coordination-leader';
    row.dataset.coordinatorId = coordinatorByPath.get(norm(path)) || '';
    row.setAttribute('aria-busy', 'true');
    const mark = document.createElement('span'); mark.className = 'coordination-leader-mark';
    mark.setAttribute('aria-hidden', 'true'); mark.append(coordinationMark());
    const body = document.createElement('span'); body.className = 'coordination-leader-body';
    const label = document.createElement('span'); label.className = 'coordination-leader-label';
    label.textContent = t('projectCoordination.coordinator'); body.append(label);
    const meta = document.createElement('span'); meta.className = 'coordination-leader-meta';
    meta.textContent = t('common.loading'); body.append(meta);
    row.append(mark, body);
    ports.sidebar.replaceChildren(row);
  }
  function invalidate() { pending = true; void refresh(true); }
  // Recovery signals retry failed demand, never ordinary list renders or a timer.
  function recoverBinding() {
    if (document.visibilityState === 'hidden') return;
    if (bindingFailed || bindingStale) invalidate();
    else unreadInView?.();
  }
  window.addEventListener('online', recoverBinding);
  document.addEventListener('visibilitychange', recoverBinding);
  async function refresh(force = false) {
    if (!alive) return;
    if (createControl && ports.sidebar.contains(createControl.button)) showCreateAvailability(createControl);
    const scope = ports.scope();
    const key = JSON.stringify([scope?.path, scope?.currentConversationId, ports.sidebarPath(), !!scope?.workerModel]);

    if (!force && key === signature) return;
    let resumed: ProjectReads | undefined;
    if (key !== signature) {
      /* Only a project/folder switch (or unmount) tears the surfaces down; a
       * conversation switch inside one project must leave the coordinator row
       * mounted so the sidebar does not jump. */
      const projectKey = JSON.stringify([scope?.path, ports.sidebarPath()]);
      const projectChanged = projectKey !== projectSignature;
      projectSignature = projectKey;
      signature = key; generation++;
      flight?.abort(); flight = null; pending = false;
      dropWorkerModel();
      if (projectChanged) {
        bindingFailed = false; bindingStale = false;
        /* Paint the row shell synchronously on a folder switch. Clearing the
         * container here left the sidebar empty until the binding read
         * resolved, then the row pushed the conversation list down. */
        if (ports.mode !== 'canvas' && scope && norm(ports.sidebarPath()) === norm(scope.path)) paintSidebarPending(scope.path);
        else ports.sidebar.replaceChildren();
        ports.canvas.replaceChildren(); finishedOpen = false; rendered = '';
        lastReport = undefined; acknowledgedTurnId = ''; settled = undefined;
      } else {
        // The previous conversation's canvas must not remain while the new
        // scope reads are in flight: it is repainted from the settled project
        // reads below, or cleared. Sidebar geometry stays stable.
        resumed = resumableReads(scope);
        if (!resumed) ports.canvas.replaceChildren();
      }
      unreadInView = null;
    }
    if (!scope) { unsubscribe?.(); unsubscribe = undefined; subscribedPath = ''; return; }
    if (subscribedPath !== scope.path) {
      unsubscribe?.(); subscribedPath = scope.path;
      unsubscribe = scope.subscribe?.(invalidate);
    }
    if (flight) { pending = true; return; }
    const controller = new AbortController();
    flight = controller; pending = false;
    const own = generation;
    const valid = () => alive && own === generation && norm(scope.currentPath()) === norm(scope.path)
      && (ports.mode === 'sidebar' || !scope.currentConversation || scope.currentConversation() === scope.currentConversationId);
    const bindingRequest = read(() => scope.binding(scope.path, controller.signal));
    const allReads = readProject(scope, bindingRequest, controller.signal);
    /* `resumed` repaints from settled reads inside a conversation switch; the
     * conversation list it would refresh has not changed. */
    const paint = async (results: ProjectReads, detailsPending = false, resumed = false) => {
      const [bindingRead, progressRead, reportRead] = results;
      if (!valid()) return;
      if (bindingRead.status === 'rejected' && (ports.canvas.childElementCount || ports.mode === 'sidebar' && ports.sidebar.childElementCount)) {
        const failure = apiFailure(bindingRead.reason);
        console.warn(`[ProjectCoordination] Binding read failed: code=${failure.code ?? 'unknown'} status=${failure.status} requestId=${failure.requestId ?? 'unknown'}`);
        /* A read the server never answered, or asked to repeat, is no evidence
         * against the binding this mount already painted from a successful read:
         * keep it usable and unflagged until a recovery or project signal re-reads. */
        if (rendered !== '' && !bindingFailed && isTransientApiFailure(bindingRead.reason)) {
          bindingStale = true; return;
        }
        bindingFailed = true;
        const pendingRow = ports.mode === 'sidebar'
          ? ports.sidebar.querySelector<HTMLElement>('.coordination-leader[aria-busy="true"]') : null;
        if (pendingRow) {
          pendingRow.removeAttribute('aria-busy');
          pendingRow.dataset.coordinatorId = '';
          const meta = pendingRow.querySelector('.coordination-leader-meta');
          if (meta) meta.textContent = t('projectCoordination.error');
          /* The cached identity no longer applies, so the list must stop hiding
           * the conversation it claimed. The re-render's own refresh returns at
           * the unchanged signature; it does not queue another read. */
          ports.changed?.();
        }
        const host = ports.mode === 'sidebar' ? ports.sidebar : ports.canvas;
        host.querySelector('.coordination-read-error')?.remove();
        const warning = document.createElement('div'); warning.className = 'coordination-read-error'; warning.setAttribute('role', 'status');
        const cause = failure.code === 'timeout' ? t('err.k.timeout.chip')
          : failure.code === 'network' ? t('err.k.network.chip') : failure.message;
        warning.append(coordinationText(`${t('projectCoordination.error')} · ${cause}`, 'small'),
          createButton({ label: t('projectCoordination.retry'), size: 'sm', variant: 'ghost', onClick: invalidate }));
        host.append(warning); return;
      }
      bindingFailed = bindingRead.status === 'rejected'; bindingStale = false;
      ports.canvas.querySelector('.coordination-read-error')?.remove();
      ports.sidebar.querySelector('.coordination-read-error')?.remove();
      const binding: CoordinationBinding | null = bindingRead.status === 'fulfilled' ? bindingRead.value : null;
      const progressUnavailable = progressRead.status === 'rejected';
      const progress = progressRead.status === 'fulfilled' ? progressRead.value : null;
      // A board belongs to exactly one binding; a replaced Coordinator starts empty.
      const board: CoordinatorProgressBoard | null = progress && binding
        && progress.conversationId === binding.conversationId && progress.bindingRevision === binding.revision
        ? progress.board : null;

      if (!detailsPending && reportRead.status === 'fulfilled') lastReport = reportRead.value;
      const report = lastReport?.conversationId === binding?.conversationId
        && lastReport?.bindingRevision === binding?.revision ? lastReport : undefined;
      const facts = JSON.stringify([key, detailsPending, binding, board, progressUnavailable, report]);
      if (facts === rendered) return;
      rendered = facts;

      const boardScroll = ports.canvas.querySelector('.coordinator-progress-body')?.scrollTop ?? 0;
      const focused = document.activeElement;
      const focusHost = focused && ports.sidebar.contains(focused) ? ports.sidebar
        : focused && ports.canvas.contains(focused) ? ports.canvas : null;
      const focusLabel = focusHost ? focused?.textContent : null;

      const focusKey = focusHost && focused instanceof HTMLElement ? focused.dataset.coordinationFocus : undefined;
      const details = () => { if (valid()) ports.details(scope); };
      const navigate = (id: string) => {
        const sidebarCurrent = ports.mode === 'sidebar' && alive && norm(ports.sidebarPath()) === norm(scope.path);
        if (sidebarCurrent || valid()) scope.navigate(id);
      };

      let keptControl: HTMLElement | null = null;
      if (ports.mode !== 'sidebar' && scope.workerModel && binding?.availability === 'available'
        && binding.conversationId === scope.currentConversationId) {
        const controlKey = JSON.stringify([scope.path, binding.conversationId]);
        if (workerModel?.key !== controlKey) {
          dropWorkerModel();
          workerModel = { key: controlKey, control: createRouterWorkerModel(scope.workerModel, scope.path) };
        }
        keptControl = workerModel.control.el;
      } else dropWorkerModel();
      ports.sidebar.replaceChildren();
      for (const child of [...ports.canvas.childNodes]) if (child !== keptControl) child.remove();
      if (keptControl && keptControl.parentNode !== ports.canvas) ports.canvas.prepend(keptControl);
      if (ports.mode !== 'canvas' && norm(ports.sidebarPath()) === norm(scope.path)) {
        const available = binding?.availability === 'available';
        if (available) coordinatorByPath.set(norm(scope.path), binding!.conversationId);
        else coordinatorByPath.delete(norm(scope.path));
        const row = document.createElement('section'); row.className = 'coordination-leader';
        row.dataset.coordinatorId = available ? binding!.conversationId : '';
        if (available) {
          row.tabIndex = 0; row.setAttribute('role', 'button');
          row.title = t('projectCoordination.openCoordinator');
          const open = () => navigate(binding!.conversationId);
          row.addEventListener('click', open);
          row.addEventListener('keydown', event => {
            if (event.key !== 'Enter' && event.key !== ' ') return;
            event.preventDefault(); open();
          });
          if (binding!.conversationId === scope.currentConversationId) row.setAttribute('aria-current', 'page');
        }
        // The mark and the work tally explain what this fixed row is; a bare
        // name read as a stray label above the date groups.
        const mark = document.createElement('span'); mark.className = 'coordination-leader-mark';
        mark.setAttribute('aria-hidden', 'true'); mark.append(coordinationMark());
        const body = document.createElement('span'); body.className = 'coordination-leader-body';
        const leader = document.createElement('span'); leader.className = 'coordination-leader-label';
        leader.textContent = t('projectCoordination.coordinator'); body.append(leader);
        const meta = document.createElement('span'); meta.className = 'coordination-leader-meta';
        if (available) {
          meta.textContent = detailsPending ? t('common.loading')
            : progressUnavailable ? t('projectCoordination.boardUnavailable')
            : coordinatorProgressHeadline(board) ?? t('projectCoordination.sidebarIdle');
          if (board) row.title = `${t('projectCoordination.openCoordinator')} · ${board.summary}`;
        } else {
          meta.textContent = t(!binding ? 'projectCoordination.error' : binding.availability === 'unbound'
            ? 'projectCoordination.unbound' : 'projectCoordination.unavailable');
        }
        if (available && report?.unread && report.latest) {
          meta.textContent = report.latest.title;
          const badge = document.createElement('span'); badge.className = 'coordination-leader-unread';
          badge.textContent = '•';
          badge.setAttribute('aria-label', t('projectCoordination.report.unread'));
          leader.append(badge); row.dataset.unread = 'true';
          row.title = `${t('projectCoordination.report.unread')} · ${report.latest.title}`;
        }
        body.append(meta); row.append(mark, body);
        if (available) {
          const go = document.createElement('span'); go.className = 'coordination-leader-go';
          go.setAttribute('aria-hidden', 'true'); go.textContent = '›'; row.append(go);
        } else {
          const actions = document.createElement('span'); actions.className = 'coordination-leader-actions';
          if (binding?.availability === 'unbound') {
            const create = createButton({ label: t('projectCoordination.create'), size: 'sm', onClick: async () => {
              if (!valid() || creating || ports.canCreate?.() === false) return;
              creating = true; create.disabled = true;
              const feedback = document.createElement('p'); feedback.setAttribute('role', 'alert');
              actions.querySelector('[role="alert"]')?.remove();
              try { await ports.create(scope, binding); }
              catch (error) {
                if (valid()) { feedback.textContent = error instanceof Error ? error.message : String(error); actions.append(feedback); }
                return;
              } finally { creating = false; showCreateAvailability(control); }
              if (valid()) await refresh(true);
            } });
            const control = { button: create, hint: coordinationText(t('projectCoordination.selectFolder'), 'small') };
            showCreateAvailability(control); createControl = control;
            actions.append(create, control.hint);
          }
          actions.append(createButton({ label: t('projectCoordination.setup'), size: 'sm', variant: 'ghost', onClick: details }));
          row.append(actions);
        }
        ports.sidebar.append(row);
      }
      if (ports.mode === 'sidebar') {
        // The sidebar owns navigation only, never the active conversation canvas.
      } else if (binding?.availability === 'available' && binding.conversationId === scope.currentConversationId) {
        // A Coordinator without a progress port (the Router Watchdog) has no board to show.
        if (scope.progress) {
          const view: CoordinatorProgressView = progressUnavailable ? { kind: 'unavailable' }
            : board ? { kind: 'board', board } : { kind: 'empty' };
          ports.canvas.append(createCoordinatorProgress(view, {
            open: boardOpen, finishedOpen,
            toggle: open => { boardOpen = open; }, toggleFinished: open => { finishedOpen = open; },
            manage: details, retry: invalidate,
          }));
          const body = ports.canvas.querySelector('.coordinator-progress-body');
          if (body) body.scrollTop = boardScroll;
        }
        const latest = report?.unread ? report.latest : null;
        if (latest && scope.reports) {
          // The open, visible Coordinator chat shows its latest message, so it
          // is read. The exact Turn ID keeps a stale view from reading a newer one.
          const acknowledge = () => {
            if (!valid() || document.visibilityState === 'hidden' || acknowledgedTurnId === latest.turnId) return;
            acknowledgedTurnId = latest.turnId; unreadInView = null;
            scope.reports!.markRead({ path: scope.path, conversationId: binding.conversationId,
              bindingRevision: binding.revision, turnId: latest.turnId })
              .then(state => { if (valid() && state) { lastReport = state; invalidate(); } })
              .catch(() => {
                // The next visibility/online signal retries; the message stays unread.
                if (acknowledgedTurnId === latest.turnId) { acknowledgedTurnId = ''; unreadInView = acknowledge; }
              });
          };
          unreadInView = acknowledge;
          acknowledge();
        }
      } else {
        // A Worker chat the Coordinator created keeps one way back to it.
        const related = scope.conversations?.().some(item => item.id === scope.currentConversationId
          && item.coordinatorId === binding?.conversationId);
        if (related && binding?.availability === 'available') ports.canvas.append(createButton({
          label: t('projectCoordination.returnCoordinator'), size: 'sm', variant: 'ghost', onClick: () => navigate(binding.conversationId),
        }));
      }
      if (!binding && ports.mode !== 'canvas') ports.sidebar.append(createButton({ label: t('projectCoordination.retry'), size: 'sm', onClick: invalidate }));

      if (focusHost) {
        const replacement = [...focusHost.querySelectorAll<HTMLElement>('button, summary, [data-coordination-focus]')]
          .find(node => focusKey ? node.dataset.coordinationFocus === focusKey : node.textContent === focusLabel);
        replacement?.focus({ preventScroll: true });
      }
      if (!resumed) ports.changed?.();
    };
    try {
      // A resumed canvas paints synchronously: no await precedes its DOM write.
      if (resumed) await paint(resumed, false, true);
      if (ports.mode === 'sidebar') {
        // Binding establishes navigation authority. Optional work/report reads
        // enrich the row later and must never hold its click target hostage.
        const [bindingRead] = await Promise.allSettled([bindingRequest]);
        const row = ports.sidebar.querySelector<HTMLElement>('.coordination-leader');
        const needsBindingPaint = !row || row.hasAttribute('aria-busy') || bindingFailed || bindingRead.status === 'rejected'
          || row.dataset.coordinatorId !== (bindingRead.value.availability === 'available' ? bindingRead.value.conversationId : '');
        if (needsBindingPaint) await paint([
          bindingRead,
          { status: 'fulfilled', value: null },
          { status: 'fulfilled', value: undefined },
        ], true);
        if (!valid() || bindingRead.status === 'rejected') { controller.abort(); return; }
      }
      const reads = await allReads;
      if (own === generation && reads[0].status === 'fulfilled') settled = { path: scope.path, reads };
      await paint(reads);
    } finally {
      if (flight === controller) {
        flight = null;
        if (alive && pending) void refresh(true);
        else if (alive && !valid()) void refresh();
      }
    }
  }
  /** The shown project's bound Coordinator from its newest settled binding read ('' when none). */
  const coordinatorId = () => {
    const read = settled?.reads[0];
    return read?.status === 'fulfilled' && read.value.availability === 'available' ? read.value.conversationId : '';
  };
  return { refresh, coordinatorId, destroy() {
    alive = false; generation++; flight?.abort(); flight = null; pending = false; unreadInView = null;
    dropWorkerModel();
    window.removeEventListener('online', recoverBinding);
    document.removeEventListener('visibilitychange', recoverBinding);
    unsubscribe?.(); coordinatorByPath.clear(); settled = undefined;
    ports.sidebar.replaceChildren(); ports.canvas.replaceChildren();
  } };
}
