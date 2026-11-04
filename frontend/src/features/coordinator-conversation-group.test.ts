// @vitest-environment jsdom
import { expect, it, vi } from 'vitest';
import { createCoordinatorConversationGroup, createCoordinatorConversationGroups, coordinatorConversationIds, type CoordinatorConversationRow } from './coordinator-conversation-group';
import type { CoordinatorConversationPage } from '../api/project-coordination.generated';

vi.mock('../i18n', () => ({ t: (key: string, values?: Record<string, number>) => `${key}:${values?.n ?? ''}` }));

function fixture() {
  const host = document.createElement('div');
  document.body.replaceChildren(host);
  const readPage = vi.fn(async (cursor?: string) => ({
    conversations: [{ conversationId: 'worker-old', title: 'Older work', turnCount: 1, coordinatorId: 'coordinator-1', createdAt: 1, updatedAt: 5 }],
    nextCursor: null, coverage: 'complete' as const,
  }));
  const rows: CoordinatorConversationRow[] = [
    { id: 'coordinator-1', title: 'Project coordinator', _coordinatorSource: null },
    { id: 'worker-new', title: 'New work', _coordinatorSource: 'coordinator-1', _serverTurnCount: 1, updatedAt: 20 },
    { id: 'worker-empty', title: 'Empty shell', _coordinatorSource: 'coordinator-1', _serverRev: 1, updatedAt: 10 },
    { id: 'ordinary', title: 'Owner conversation', _serverTurnCount: 1 },
    { id: 'owner-worker-like', title: 'worker-title only', _serverTurnCount: 1 },
  ];
  const renderRow = vi.fn((conversation: { id: string }) => `<div class="conv-item" data-conv-id="${conversation.id}">${conversation.id === 'worker-empty' ? '<span class="conv-review-dot"></span>' : ''}${conversation.id}<div class="conv-actions"><button class="conv-copy-id" data-conv-id="${conversation.id}">copy</button><button class="conv-rename" data-conv-id="${conversation.id}">rename</button></div></div>`);
  const open = vi.fn();
  const refresh = vi.fn();
  const group = createCoordinatorConversationGroup({
    host, rows: () => rows, readPage, adoptPage: page => { for (const row of page.conversations) rows.push({ id: row.conversationId, title: row.title, _coordinatorSource: row.coordinatorId, updatedAt: row.updatedAt }); }, open, renderRow,
    activeConversationId: () => 'worker-new',
    isRunning: conversation => conversation.id === 'worker-new',
    readExpanded: vi.fn(() => null),
    writeExpanded: vi.fn(),
    refresh,
  });
  const adoptPage = (page: Awaited<ReturnType<typeof readPage>>) => { for (const row of page.conversations) rows.push({ id: row.conversationId, title: row.title, _coordinatorSource: row.coordinatorId, updatedAt: row.updatedAt }); };
  return { host, rows, group, readPage, adoptPage, open, renderRow, refresh };
}

it('groups every authoritative child while leaving the owner catalog and counts intact', () => {
  expect(coordinatorConversationIds([
    { id: 'coordinator-1' }, { id: 'worker-1', _coordinatorSource: 'coordinator-1' },
    { id: 'owner', title: 'worker-title only' }, { id: 'swarm-child' },
  ])).toEqual(new Set(['worker-1']));
});

it('is collapsed by default, preserves coordinator provenance, and opens all children including empty shells', async () => {
  const f = fixture();
  f.group.update('coordinator-1');
  const button = f.host.querySelector<HTMLButtonElement>('.coordinator-conversations-toggle')!;
  expect(button.getAttribute('aria-expanded')).toBe('false');
  expect(f.host.textContent).toContain('projectCoordination.conversations:');
  await vi.waitFor(() => expect(f.host.querySelector('.coordinator-conversations-count')?.textContent).toBe('3'));
  // A reply awaiting review is marked on its own row only, never on the folded header.
  expect(button.querySelector('.conv-review-dot')).toBeNull();
  expect(f.host.textContent).toContain('projectCoordination.running:1');
  expect(f.host.querySelectorAll('.coordination-read-error')).toHaveLength(0);
  button.click();
  await vi.waitFor(() => expect(button.getAttribute('aria-expanded')).toBe('true'));
  const children = [...f.host.querySelectorAll('.conv-item')];
  expect(children.map(row => row.getAttribute('data-conv-id'))).toEqual(['worker-new', 'worker-empty', 'worker-old']);
  expect(children.map(row => !!row.querySelector('.conv-review-dot'))).toEqual([false, true, false]);
  (children[1] as HTMLElement).click();
  expect(f.open).toHaveBeenCalledWith('worker-empty');
  const copy = children[1].querySelector<HTMLButtonElement>('.conv-copy-id')!;
  expect(copy.dataset.convId).toBe('worker-empty');
  expect(children[1].querySelector('.conv-rename')).toBeNull();
  copy.click();
  expect(f.open).toHaveBeenCalledTimes(1);
  f.group.destroy();
});

it('shows no group or count until it has a member to list', async () => {
  const host = document.createElement('div');
  document.body.replaceChildren(host);
  const rows: CoordinatorConversationRow[] = [];
  let settle: (page: CoordinatorConversationPage) => void = () => undefined;
  const readPage = vi.fn(() => new Promise<CoordinatorConversationPage>(resolve => { settle = resolve; }));
  const group = createCoordinatorConversationGroup({
    host, rows: () => rows, readPage,
    adoptPage: page => { for (const item of page.conversations) rows.push({ id: item.conversationId, title: item.title, _coordinatorSource: item.coordinatorId }); },
    open: vi.fn(), renderRow: () => '', activeConversationId: () => '',
    readExpanded: () => true, writeExpanded: vi.fn(),
  });
  group.update('coordinator-1');
  const root = host.querySelector<HTMLElement>('.coordinator-conversations')!;
  expect(readPage).toHaveBeenCalledOnce();
  expect(root.hidden).toBe(true);
  settle({ conversations: [{ conversationId: 'worker-1', title: 'Work', turnCount: 1, coordinatorId: 'coordinator-1' }],
    nextCursor: null, coverage: 'complete' });
  await vi.waitFor(() => expect(root.hidden).toBe(false));
  expect(root.querySelector('.coordinator-conversations-count')?.textContent).toBe('1');
  group.destroy();
});

it('keeps expanded rows (hover and focus) across unchanged sidebar refreshes', async () => {
  const f = fixture();
  f.group.update('coordinator-1');
  f.host.querySelector<HTMLButtonElement>('.coordinator-conversations-toggle')!.click();
  await vi.waitFor(() => expect(f.host.querySelectorAll('.conv-item')).toHaveLength(3));
  const first = f.host.querySelector<HTMLElement>('.conv-item[data-conv-id="worker-empty"]')!;
  first.focus();
  f.group.refresh(); f.group.revalidate(); f.group.update('coordinator-1');
  expect(f.host.querySelector('.conv-item[data-conv-id="worker-empty"]')).toBe(first);
  f.renderRow.mockImplementation((c: { id: string }) => `<div class="conv-item" data-conv-id="${c.id}">${c.id}!</div>`);
  f.group.refresh();
  const replaced = f.host.querySelector<HTMLElement>('.conv-item[data-conv-id="worker-empty"]')!;
  expect(replaced).not.toBe(first);
  expect(document.activeElement).toBe(replaced);
  f.group.destroy();
});

it('refreshes catalog rows after adopting server inventory', async () => {
  const f = fixture();
  f.group.update('coordinator-1');
  await vi.waitFor(() => expect(f.readPage).toHaveBeenCalledOnce());
  expect(f.refresh).toHaveBeenCalledOnce();
  f.group.destroy();
});

it('discovers legacy members from the roster when the catalog has no provenance yet', async () => {
  const host = document.createElement('div');
  document.body.replaceChildren(host);
  const rows: CoordinatorConversationRow[] = [];
  const readPage = vi.fn(async () => ({
    conversations: [{ conversationId: 'legacy-worker', title: 'Legacy work', turnCount: 1,
      coordinatorId: 'coordinator-1', createdAt: 1, updatedAt: 2 }],
    nextCursor: null, coverage: 'complete' as const,
  }));
  const groups = createCoordinatorConversationGroups({
    pinned: () => [{ id: 'coordinator-1', path: '/project', host }], historySettled: () => true, projectPath: () => '',
    listHost: document.createElement('div'), rows: () => rows, readPage,
    adoptPage: page => {
      for (const item of page.conversations) rows.push({
        id: item.conversationId, title: item.title,
        _coordinatorSource: item.coordinatorId, updatedAt: item.updatedAt,
      });
    },
    adoptCoordinator: (id, path) => {
      rows.push({ id, projectPath: path });
    },
    open: vi.fn(), renderRow: () => '', activeConversationId: () => '',
  });
  groups.refresh();
  await vi.waitFor(() => expect(rows.map(row => row.id)).toEqual(['coordinator-1', 'legacy-worker']));
  expect(document.querySelector('.coordinator-conversations-count')?.textContent).toBe('1');
  groups.destroy();
});

it('retries a failed roster discovery on the next catalog refresh', async () => {
  const host = document.createElement('div');
  document.body.replaceChildren(host);
  const rows: CoordinatorConversationRow[] = [];
  const readPage = vi.fn()
    .mockRejectedValueOnce(new Error('temporary history contention'))
    .mockResolvedValue({
      conversations: [{ conversationId: 'legacy-worker', title: 'Legacy work', turnCount: 1,
        coordinatorId: 'coordinator-1', createdAt: 1, updatedAt: 2 }],
      nextCursor: null, coverage: 'complete' as const,
    });
  const groups = createCoordinatorConversationGroups({
    pinned: () => [{ id: 'coordinator-1', path: '/project', host }], historySettled: () => true, projectPath: () => '',
    listHost: document.createElement('div'), rows: () => rows, readPage,
    adoptPage: page => {
      for (const item of page.conversations) rows.push({
        id: item.conversationId, title: item.title,
        _coordinatorSource: item.coordinatorId, updatedAt: item.updatedAt,
      });
    },
    open: vi.fn(), renderRow: () => '', activeConversationId: () => '',
  });
  groups.refresh();
  await vi.waitFor(() => expect(readPage).toHaveBeenCalledTimes(1));
  groups.refresh();
  await vi.waitFor(() => expect(readPage).toHaveBeenCalledTimes(2));
  expect(rows.map(row => row.id)).toEqual(['legacy-worker']);
  groups.destroy();
});

it('loads a known Coordinator group before any Worker is in the local catalog', async () => {
  const host = document.createElement('div');
  const coordinator = document.createElement('div');
  coordinator.id = 'projectCoordinatorEntry';
  document.body.replaceChildren(host, coordinator);
  const rows: CoordinatorConversationRow[] = [];
  const groups = createCoordinatorConversationGroups({
    pinned: () => [{ id: 'coordinator-1', path: '/project', host }], historySettled: () => true, projectPath: () => '',
    listHost: document.createElement('div'),
    rows: () => rows,
    readPage: vi.fn(async () => ({ conversations: [{ conversationId: 'worker-cold', title: 'Cold worker', turnCount: 1, coordinatorId: 'coordinator-1' }], nextCursor: null, coverage: 'complete' as const })),
    adoptPage: page => { for (const row of page.conversations) rows.push({ id: row.conversationId, title: row.title, _coordinatorSource: row.coordinatorId }); },
    open: vi.fn(), renderRow: row => `<div class="conv-item" data-conv-id="${row.id}">${row.title}</div>`, activeConversationId: () => '',
  });
  groups.refresh([]);
  expect(document.querySelector('.coordinator-conversations')).not.toBeNull();
  await vi.waitFor(() => expect(document.querySelector('.coordinator-conversations-count')?.textContent).toBe('1'));
  expect(document.querySelector('.coordinator-conversations-toggle')?.getAttribute('aria-expanded')).toBe('false');
  groups.destroy();
});

it('does not fabricate a row for a Coordinator that is not in the shown view', () => {
  const listHost = document.createElement('div');
  document.body.replaceChildren(listHost);
  const worker = { id: 'worker', title: 'Historical Worker', _coordinatorSource: 'missing-coordinator', _serverRev: 1 };
  const renderRow = vi.fn((row: CoordinatorConversationRow) => `<div class="conv-item" data-conv-id="${row.id}">${row.title}</div>`);
  const groups = createCoordinatorConversationGroups({
    listHost, pinned: () => [], historySettled: () => true, projectPath: () => '/project',
    rows: () => [worker], readPage: vi.fn(), open: vi.fn(), renderRow, activeConversationId: () => '',
  });
  groups.refresh([worker]);
  expect(document.querySelector('.coordinator-history-entry')).toBeNull();
  expect(document.querySelector('.coordinator-conversations')).toBeNull();
  expect(renderRow).not.toHaveBeenCalled();
  groups.destroy();
});

function sidebar(initial: { pinned?: string; settled?: boolean; view?: string[] } = {}) {
  const column = document.createElement('div');
  const leader = document.createElement('div'); leader.id = 'leader';
  const listHost = document.createElement('div'); listHost.id = 'list';
  column.append(leader, listHost);
  document.body.replaceChildren(column);
  const rows: CoordinatorConversationRow[] = [
    { id: 'current', title: 'Project coordinator', _serverRev: 3, updatedAt: 30 },
    { id: 'replaced', title: 'Project coordinator', _serverRev: 2, updatedAt: 20 },
    { id: 'other-project', title: 'Project coordinator', _serverRev: 1, updatedAt: 10 },
    { id: 'current-worker', title: 'Current work', _coordinatorSource: 'current', _serverRev: 1 },
    { id: 'replaced-worker', title: 'Earlier work', _coordinatorSource: 'replaced', _serverRev: 1 },
    { id: 'other-worker', title: 'Other work', _coordinatorSource: 'other-project', _serverRev: 1 },
    { id: 'router', title: 'Global Router', _serverRev: 1 },
    { id: 'router-worker', title: 'Routed work', _coordinatorSource: 'router', _serverRev: 1 },
  ];
  const state = { pinned: initial.pinned ?? '', settled: initial.settled ?? true, view: initial.view ?? ['current', 'replaced', 'router'] };
  const open = vi.fn();
  let active = '';
  const groups = createCoordinatorConversationGroups({
    listHost,
    pinned: () => [...(state.pinned ? [{ id: state.pinned, path: '/alpha', host: leader }] : []), { id: 'router', path: '', host: leader }],
    historySettled: () => state.settled,
    projectPath: () => '/alpha',
    rows: () => rows,
    readPage: vi.fn(async () => ({ conversations: [], nextCursor: null, coverage: 'complete' as const })),
    open, activeConversationId: () => active,
    renderRow: row => `<div class="conv-item" data-conv-id="${row.id}"><span class="conv-title">${row.title}</span><div class="conv-actions"><button class="conv-copy-id" data-conv-id="${row.id}">copy</button><button class="conv-rename" data-conv-id="${row.id}">rename</button></div></div>`,
  });
  const refresh = () => groups.refresh(rows.filter(row => state.view.includes(row.id)));
  const historyRows = () => [...document.querySelectorAll<HTMLElement>('.coordinator-history-entry .conv-item')].map(row => row.dataset.convId);
  return { leader, listHost, rows, state, open, groups, refresh, historyRows, setActive: (id: string) => { active = id; } };
}

it('waits for the binding before calling any conversation a replaced Coordinator', () => {
  const f = sidebar({ settled: false });
  f.refresh();
  expect(f.historyRows()).toEqual([]);
  expect(document.querySelectorAll('.coordinator-conversations-host')).toHaveLength(0);
  f.state.pinned = 'current';
  f.refresh();
  expect(f.historyRows()).toEqual([]);
  expect(f.leader.nextElementSibling?.className).toBe('coordinator-conversations-host');
  f.groups.destroy();
});

it('moves a Coordinator classified before its binding under the pinned entry without leaving a second row', () => {
  // A failed binding read leaves nothing pinned: the bound Coordinator shows as a row of its own.
  const f = sidebar();
  f.refresh();
  expect(f.historyRows()).toEqual(['current', 'replaced']);
  expect(f.groups.hasHistoryRow('current')).toBe(true);
  f.state.pinned = 'current';
  f.refresh();
  expect(f.historyRows()).toEqual(['replaced']);
  expect(f.groups.hasHistoryRow('current')).toBe(false);
  expect(f.leader.nextElementSibling?.className).toBe('coordinator-conversations-host');
  expect(document.querySelectorAll('.coordinator-conversations')).toHaveLength(2);
  expect(document.querySelectorAll('.coordinator-conversations-host')).toHaveLength(2);
  f.groups.destroy();
  expect(document.body.querySelectorAll('.coordinator-history-entry, .coordinator-conversations-host')).toHaveLength(0);
});

it('removes every node it painted when a Coordinator leaves the view and paints one row on return', () => {
  const f = sidebar({ pinned: 'current' });
  f.refresh();
  expect(f.historyRows()).toEqual(['replaced']);
  f.state.view = ['other-project'];
  f.state.pinned = '';
  f.refresh();
  expect(f.historyRows()).toEqual(['other-project']);
  expect(document.querySelectorAll('.coordinator-conversations-host')).toHaveLength(1);
  f.state.view = ['current', 'replaced'];
  f.state.pinned = 'current';
  f.refresh();
  f.refresh();
  expect(f.historyRows()).toEqual(['replaced']);
  expect(document.querySelectorAll('.coordinator-conversations')).toHaveLength(2);
  // A presence-only refresh (a pinned entry changed) reconciles against the last shown view.
  f.groups.refresh();
  expect(f.historyRows()).toEqual(['replaced']);
  expect(document.querySelectorAll('.coordinator-conversations')).toHaveLength(2);
  f.groups.destroy();
});

it('reserves pinned ids, keeps a replaced Coordinator row current, and opens it', () => {
  const f = sidebar({ pinned: 'current' });
  f.refresh();
  expect(f.historyRows()).toEqual(['replaced']);
  const row = () => document.querySelector<HTMLElement>('.coordinator-history-entry .conv-item')!;
  expect(row().querySelector<HTMLButtonElement>('.conv-copy-id')!.dataset.convId).toBe('replaced');
  expect(row().querySelector('.conv-rename')).toBeNull();
  expect(row().getAttribute('role')).toBe('button');
  f.rows.find(item => item.id === 'replaced')!.title = 'Renamed coordinator';
  f.setActive('replaced');
  f.refresh();
  expect(row().querySelector('.conv-title')!.textContent).toBe('Renamed coordinator');
  expect(row().classList.contains('active')).toBe(true);
  row().querySelector<HTMLElement>('.conv-title')!.click();
  expect(f.open).toHaveBeenCalledWith('replaced');
  const copy = row().querySelector<HTMLButtonElement>('.conv-copy-id')!;
  copy.click();
  copy.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
  expect(f.open).toHaveBeenCalledTimes(1);
  row().dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
  expect(f.open).toHaveBeenCalledTimes(2);
  f.groups.destroy();
});

it('folds with the date groups\' down chevron and leaves review marks to the rows', async () => {
  const f = fixture();
  f.group.update('coordinator-1');
  const button = f.host.querySelector<HTMLButtonElement>('.coordinator-conversations-toggle')!;
  const chevron = button.querySelector('svg.coordinator-conversations-chevron')!;
  expect(chevron.getAttribute('aria-hidden')).toBe('true');
  expect(chevron.querySelector('polyline')?.getAttribute('points')).toBe('6 9 12 15 18 9');
  expect(button.classList.contains('is-expanded')).toBe(false);
  await vi.waitFor(() => expect(button.getAttribute('aria-label')).toBe(
    'projectCoordination.conversations: (3), projectCoordination.running:1'));
  expect(button.querySelector('.conv-review-dot')).toBeNull();
  button.click();
  expect(button.classList.contains('is-expanded')).toBe(true);
  f.group.destroy();
});

it('refreshes a loaded inventory when a later Worker changes the catalog', async () => {
  const f = fixture();
  f.group.update('coordinator-1');
  await vi.waitFor(() => expect(f.refresh).toHaveBeenCalledOnce());
  // Neither adopted members nor edits to known members re-read the inventory.
  f.group.update('coordinator-1');
  const known = f.rows.find(row => row.id === 'worker-new')!;
  Object.assign(known, { _serverRev: 9, _serverTurnCount: 4, updatedAt: 99 });
  f.group.update('coordinator-1');
  await Promise.resolve();
  expect(f.readPage).toHaveBeenCalledOnce();
  f.rows.push({ id: 'worker-later', title: 'Later work', _coordinatorSource: 'coordinator-1', updatedAt: 30 });
  f.group.update('coordinator-1');
  await vi.waitFor(() => expect(f.readPage).toHaveBeenCalledTimes(2));
  f.group.destroy();
});

it('remembers each coordinator independently and keeps the group beneath its own entry', async () => {
  const f = fixture();
  const readExpanded = vi.fn((id: string) => id === 'coordinator-1');
  const writeExpanded = vi.fn();
  const host = document.createElement('div');
  const second = createCoordinatorConversationGroup({
    host, rows: () => [{ id: 'worker-empty', _coordinatorSource: 'coordinator-1', _serverRev: 1 }],
    readPage: f.readPage, open: f.open, renderRow: f.renderRow,
    activeConversationId: () => '', readExpanded, writeExpanded,
  });
  second.update('coordinator-1');
  expect(host.querySelector('.coordinator-conversations')?.getAttribute('data-coordinator-id')).toBe('coordinator-1');
  expect(host.querySelector('button')!.getAttribute('aria-expanded')).toBe('true');
  host.querySelector<HTMLButtonElement>('button')!.click();
  expect(writeExpanded).toHaveBeenCalledWith('coordinator-1', false);
  expect(second.isCoordinatorConversation('worker-empty')).toBe(true);
  expect(second.isCoordinatorConversation('owner-worker-like')).toBe(false);
  second.destroy();
});
