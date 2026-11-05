/** Coordinator-started conversations in the existing sidebar, never a session registry.
 * Server-issued _coordinatorSource is the only grouping evidence. Inventory
 * follows the same paged project-conversation endpoint as Manage work.
 */
import { t } from '../i18n';
import { createButton } from '../ui';
import type { CoordinatorConversationPage } from '../api/project-coordination.generated';
import { folderIcon } from './project/folder-icons';
import '../styles/application/33-project-coordination.css';

export interface CoordinatorConversationRow {
  id: string;
  title?: string;
  _coordinatorSource?: string | null;
  _serverTurnCount?: number;
  _serverRev?: number;
  projectPath?: string;
  updatedAt?: number;
  createdAt?: number;
}

export function coordinatorConversationIds(rows: readonly CoordinatorConversationRow[]): ReadonlySet<string> {
  return new Set(rows.filter(row => typeof row._coordinatorSource === 'string' && row._coordinatorSource.length > 0)
    .map(row => row.id));
}

export interface CoordinatorConversationGroupPorts {
  host: HTMLElement;
  rows(): readonly CoordinatorConversationRow[];
  readPage(cursor?: string, signal?: AbortSignal): Promise<CoordinatorConversationPage>;
  adoptPage?(page: CoordinatorConversationPage): void;
  adoptCoordinator?(id: string): void;
  open(id: string): void;
  renderRow(conversation: CoordinatorConversationRow): string;
  activeConversationId(): string;
  isRunning?(conversation: CoordinatorConversationRow): boolean;
  hasUnread?(conversation: CoordinatorConversationRow): boolean;
  status?(conversation: CoordinatorConversationRow): 'running' | 'done' | 'failed' | 'cancelled' | null;
  readExpanded(coordinatorId: string): boolean | null;
  writeExpanded(coordinatorId: string, expanded: boolean): void;
  refresh?(): void;
  refreshActive?(): void;
}

function relativeTime(timestamp: number | undefined): string {
  if (!timestamp) return '';
  const difference = timestamp - Date.now();
  const absolute = Math.abs(difference);
  const [unit, divisor]: [Intl.RelativeTimeFormatUnit, number] = absolute >= 31_536_000_000 ? ['year', 31_536_000_000]
    : absolute >= 2_592_000_000 ? ['month', 2_592_000_000]
      : absolute >= 86_400_000 ? ['day', 86_400_000]
        : absolute >= 3_600_000 ? ['hour', 3_600_000] : ['minute', 60_000];
  return new Intl.RelativeTimeFormat(document.documentElement.lang || undefined, { numeric: 'auto' })
    .format(Math.trunc(difference / divisor), unit);
}

export function createCoordinatorConversationGroup(ports: CoordinatorConversationGroupPorts) {
  let coordinatorId = '', expanded = false, loaded = false, failed = false, loading = false, disposed = false;
  let membership = '', bodyKey = '';
  let flight: AbortController | null = null;
  const root = document.createElement('section');
  root.className = 'coordinator-conversations';
  root.dataset.coordinatorId = '';
  root.hidden = true;
  ports.host.append(root);
  const button = createButton({ label: '', variant: 'ghost' });
  button.classList.add('coordinator-conversations-toggle');
  button.setAttribute('aria-expanded', 'false');
  // The date groups' down chevron: CSS turns it to point right while collapsed.
  const chevron = folderIcon('down');
  chevron.classList.add('coordinator-conversations-chevron');
  chevron.setAttribute('width', '12'); chevron.setAttribute('height', '12'); chevron.setAttribute('stroke-width', '2.5');
  const label = document.createElement('span');
  label.className = 'coordinator-conversations-label';
  label.dataset.i18n = 'projectCoordination.conversations';
  label.textContent = t('projectCoordination.conversations');
  const count = document.createElement('span'); count.className = 'coordinator-conversations-count';
  const live = document.createElement('span'); live.className = 'coordinator-conversations-live';
  // Same mark as a row's completed-reply-awaiting-review dot.
  const unread = document.createElement('span');
  unread.className = 'coordinator-conversations-unread conv-review-dot';
  unread.setAttribute('aria-hidden', 'true');
  button.append(chevron, label, count, live, unread);
  const body = document.createElement('div'); body.className = 'coordinator-conversations-body'; body.hidden = true;
  root.append(button, body);

  function members() {
    return ports.rows().filter(row => row._coordinatorSource === coordinatorId)
      .sort((a, b) => (b.updatedAt || b.createdAt || 0) - (a.updatedAt || a.createdAt || 0));
  }
  /* The inventory only adds members the local catalog lacks, so only a member
   * joining or leaving invalidates it; edits to known members are already local. */
  function membershipOf(id: string) {
    return ports.rows().filter(row => row._coordinatorSource === id).map(row => row.id).sort().join('|');
  }
  function paint() {
    const rows = members();
    // The count is always the rows the group lists: an empty or still-loading inventory shows nothing.
    root.hidden = !coordinatorId || rows.length === 0;
    if (root.hidden) { body.hidden = true; return; }
    count.textContent = String(rows.length);
    const running = rows.filter(row => ports.isRunning?.(row)).length;
    live.textContent = running ? t('projectCoordination.running', { n: running }) : '';
    unread.hidden = !rows.some(row => ports.hasUnread?.(row));
    unread.title = unread.hidden ? '' : t('sidebar.reviewState');
    button.setAttribute('aria-expanded', String(expanded));
    button.classList.toggle('is-expanded', expanded);
    button.setAttribute('aria-label', [`${t('projectCoordination.conversations')} (${rows.length})`,
      live.textContent, unread.title].filter(Boolean).join(', '));
    body.hidden = !expanded;
    if (!expanded) { bodyKey = ''; return; }
    const html = rows.map(ports.renderRow).join('');
    const activeId = ports.activeConversationId();
    const decorations = rows.map(row => [relativeTime(row.updatedAt || row.createdAt), ports.status?.(row) ?? '']);
    // Every sidebar refresh reaches here; replacing unchanged rows drops :hover and focus.
    const nextBodyKey = JSON.stringify([html, activeId, decorations]);
    if (nextBodyKey === bodyKey) return;
    bodyKey = nextBodyKey;
    const focusedId = body.contains(document.activeElement)
      ? document.activeElement!.closest<HTMLElement>('.conv-item[data-conv-id]')?.dataset.convId : undefined;
    body.innerHTML = html;
    body.querySelectorAll<HTMLElement>('.conv-item[data-conv-id]').forEach(row => {
      const conversation = rows.find(item => item.id === row.dataset.convId)!;
      row.tabIndex = 0; row.setAttribute('role', 'button');
      row.classList.toggle('active', row.dataset.convId === activeId);
      row.querySelector('.conv-actions')?.remove();
      row.querySelector('.conv-coordinator-badge')?.remove();
      const date = row.querySelector('.conv-date');
      if (date) {
        const time = document.createElement('span');
        time.className = 'coordinator-conversations-time';
        time.textContent = relativeTime(conversation.updatedAt || conversation.createdAt);
        const status = ports.status?.(conversation);
        const statusLabel = status ? document.createElement('span') : null;
        if (statusLabel && status) {
          statusLabel.className = `coordinator-conversations-status is-${status}`;
          statusLabel.textContent = t(`projectCoordination.status.${status}`);
        }
        date.replaceChildren(time, ...(statusLabel ? [statusLabel] : []));
      }
      const open = () => { ports.open(row.dataset.convId!); ports.refreshActive?.(); };
      row.addEventListener('click', open);
      row.addEventListener('keydown', event => {
        if (event.key !== 'Enter' && event.key !== ' ') return;
        event.preventDefault(); open();
      });
    });
    if (focusedId) [...body.querySelectorAll<HTMLElement>('.conv-item[data-conv-id]')]
      .find(row => row.dataset.convId === focusedId)?.focus({ preventScroll: true });
  }
  async function load() {
    if (loading || loaded || disposed || !coordinatorId) return;
    loading = true;
    failed = false;
    const controller = new AbortController(); flight = controller;
    try {
      let cursor: string | undefined;
      do {
        const page = await ports.readPage(cursor, controller.signal);
        if (disposed || controller.signal.aborted) return;
        ports.adoptCoordinator?.(coordinatorId);
        ports.adoptPage?.(page);
        cursor = page.nextCursor ?? undefined;
      } while (cursor);
      loaded = true;
      // Members this read adopted must not invalidate the read that found them.
      membership = membershipOf(coordinatorId);
      paint();
      ports.refresh?.();
    } catch (error) {
      if (controller.signal.aborted || disposed) return;
      failed = true;
      console.warn('[CoordinatorConversations] inventory read failed', error);
      paint();
    } finally { loading = false; if (flight === controller) flight = null; }
  }
  button.addEventListener('click', () => {
    expanded = !expanded;
    ports.writeExpanded(coordinatorId, expanded);
    paint(); void load();
  });
  return {
    update(id: string) {
      root.dataset.coordinatorId = id;
      const nextMembership = membershipOf(id);
      if (nextMembership !== membership) {
        membership = nextMembership; loaded = false;
      }
      if (id !== coordinatorId) {
        coordinatorId = id; expanded = ports.readExpanded(id) === true;
        loaded = false; failed = false; flight?.abort();
      }
      paint(); void load();
    },
    isCoordinatorConversation(id: string) {
      return Boolean(coordinatorId && ports.rows().some(row => row.id === id && row._coordinatorSource === coordinatorId));
    },
    refresh: paint,
    revalidate() {
      paint();
      if (failed) void load();
    },
    destroy() { disposed = true; flight?.abort(); root.remove(); },
  };
}

/** A Coordinator with its own pinned entry; its group follows `host`. */
export interface PinnedCoordinator { id: string; path: string; host: HTMLElement }

/**
 * One group per Coordinator in the shown sidebar view. A pinned Coordinator
 * (the shown project's binding, the Global Router, the Watchdog) groups beneath
 * its entry. Any other conversation of the view that started conversations is a
 * replaced Coordinator: it gets one clickable row directly above the list, and
 * the list omits it (`hasHistoryRow`). Each mounted record owns every node it
 * inserted and is rebuilt when its anchor changes, so a Coordinator classified
 * before its binding settled never leaves a second row behind.
 */
export function createCoordinatorConversationGroups(ports: {
  /** The conversation list; replaced Coordinators' rows sit directly above it. */
  listHost: HTMLElement;
  /** Pinned Coordinators; an id without a path reserves it without a group. */
  pinned(): ReadonlyArray<PinnedCoordinator>;
  /** False while the shown project's binding is unresolved: its current Coordinator cannot be told from a replaced one yet. */
  historySettled(): boolean;
  projectPath(conversation: CoordinatorConversationRow): string;
  rows(): readonly CoordinatorConversationRow[];
  readPage(path: string, cursor?: string, signal?: AbortSignal): Promise<CoordinatorConversationPage>;
  adoptPage?(page: CoordinatorConversationPage): void;
  adoptCoordinator?(id: string, path: string): void;
  open(id: string): void;
  renderRow(conversation: CoordinatorConversationRow): string;
  activeConversationId(): string;
  isRunning?(conversation: CoordinatorConversationRow): boolean;
  hasUnread?(conversation: CoordinatorConversationRow): boolean;
  status?(conversation: CoordinatorConversationRow): 'running' | 'done' | 'failed' | 'cancelled' | null;
  refresh?(): void;
  refreshActive?(): void;
}) {
  interface Wanted { anchor: HTMLElement | null; path: string; coordinator?: CoordinatorConversationRow }
  interface Mounted {
    anchor: HTMLElement | null;
    row: HTMLElement | null;
    rowHtml: string;
    host: HTMLElement;
    group: ReturnType<typeof createCoordinatorConversationGroup>;
  }
  const mounted = new Map<string, Mounted>();
  let shown: readonly CoordinatorConversationRow[] = [];
  function unmount(id: string) {
    const record = mounted.get(id);
    if (!record) return;
    record.group.destroy(); record.host.remove(); record.row?.remove();
    mounted.delete(id);
  }
  function open(id: string) { ports.open(id); ports.refreshActive?.(); }
  function mountRow(id: string): HTMLElement {
    const row = document.createElement('div');
    row.className = 'coordinator-history-entry';
    row.addEventListener('click', event => {
      if ((event.target as Element).closest('.conv-item')) open(id);
    });
    row.addEventListener('keydown', event => {
      if (event.key !== 'Enter' && event.key !== ' ') return;
      if (!(event.target as Element).closest('.conv-item')) return;
      event.preventDefault(); open(id);
    });
    ports.listHost.before(row);
    return row;
  }
  function paintRow(record: Mounted, coordinator: CoordinatorConversationRow) {
    const row = record.row!;
    const html = ports.renderRow(coordinator);
    if (html !== record.rowHtml) {
      const focused = row.contains(document.activeElement);
      row.innerHTML = html; record.rowHtml = html;
      const item = row.querySelector<HTMLElement>('.conv-item');
      if (item) {
        item.querySelector('.conv-actions')?.remove();
        item.querySelector('.conv-coordinator-badge')?.remove();
        item.tabIndex = 0; item.setAttribute('role', 'button');
        if (focused) item.focus({ preventScroll: true });
      }
    }
    row.querySelector('.conv-item')?.classList.toggle('active', coordinator.id === ports.activeConversationId());
  }
  return {
    /** Reconcile against the conversations of the shown sidebar view; a
     * presence-only refresh (a pinned entry changed) reuses the last view. */
    refresh(view: readonly CoordinatorConversationRow[] = shown) {
      shown = view;
      const rows = ports.rows();
      const sources = new Set(rows.map(row => row._coordinatorSource).filter((id): id is string => !!id));
      const pins = ports.pinned().filter(pin => pin.id);
      const pinned = new Set(pins.map(pin => pin.id));
      const wanted = new Map<string, Wanted>();
      // A pinned Coordinator mounts before any Worker is local: its paged inventory supplies cold history.
      for (const pin of pins) if (pin.path && !wanted.has(pin.id)) wanted.set(pin.id, { anchor: pin.host, path: pin.path });
      if (ports.historySettled()) {
        for (const row of view) {
          // A conversation another Coordinator started belongs to that group, never a row of its own.
          if (!sources.has(row.id) || row._coordinatorSource || pinned.has(row.id) || wanted.has(row.id)) continue;
          wanted.set(row.id, { anchor: null, path: ports.projectPath(row), coordinator: row });
        }
      }
      for (const [id, record] of mounted) {
        if (!wanted.has(id) || wanted.get(id)!.anchor !== record.anchor) unmount(id);
      }
      for (const [id, next] of wanted) {
        let record = mounted.get(id);
        if (!record) {
          const row = next.anchor ? null : mountRow(id);
          const host = document.createElement('div'); host.className = 'coordinator-conversations-host';
          (next.anchor ?? row!).after(host);
          const path = next.path;
          record = {
            anchor: next.anchor, row, rowHtml: '', host,
            group: createCoordinatorConversationGroup({
              host, rows: ports.rows,
              readPage: path ? (cursor, signal) => ports.readPage(path, cursor, signal)
                : async () => ({ conversations: [], nextCursor: null, coverage: 'complete' as const }),
              adoptPage: ports.adoptPage,
              adoptCoordinator: coordinator => ports.adoptCoordinator?.(coordinator, path),
              open: ports.open, renderRow: ports.renderRow,
              activeConversationId: ports.activeConversationId,
              isRunning: ports.isRunning, hasUnread: ports.hasUnread, status: ports.status,
              readExpanded: key => localStorage.getItem(`tofu.coordinator.conversations.expanded.${key}`) === 'true' ? true : null,
              writeExpanded: (key, value) => localStorage.setItem(`tofu.coordinator.conversations.expanded.${key}`, String(value)),
              refresh: ports.refresh, refreshActive: ports.refreshActive,
            }),
          };
          mounted.set(id, record);
        }
        if (record.row && next.coordinator) paintRow(record, next.coordinator);
        record.group.update(id);
        record.group.revalidate();
      }
    },
    /** True when a replaced Coordinator's row is painted above the list instead of in it. */
    hasHistoryRow(id: string) { return Boolean(mounted.get(id)?.row); },
    isCoordinatorConversation(id: string) {
      return [...mounted.values()].some(record => record.group.isCoordinatorConversation(id));
    },
    destroy() { for (const id of [...mounted.keys()]) unmount(id); },
  };
}
