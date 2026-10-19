// @vitest-environment jsdom
import { expect, it, vi } from 'vitest';
import { projectCoordinationGraph } from './project-coordination-graph';
import { createCoordinationBoard } from './project-coordination-board';
import { createCoordinationWorkList, projectCoordinationList, COORDINATION_LIST_PAGE_SIZE } from './project-coordination-work-list';
import { captureCoordinationView, createCoordinationReference } from './project-coordination-reference';
vi.mock('../i18n', () => ({ t: (key: string) => key }));
const graph = () => projectCoordinationGraph({ active: [], recent: [] }, { coordinatorId: 'leader', assignments: [] });

it('shows zero-turn shells as not started, unknown catalogs as unknown and nonempty conversations as history', () => {
  const rows = projectCoordinationList(graph(), [
    { id: 'leader', title: 'Coordinator', turnCount: 0 },
    { id: 'empty', title: 'Created worker', turnCount: 0 },
    { id: 'unknown', title: 'Unloaded worker' },
    { id: 'history', title: 'Old conversation', turnCount: 5 },
  ]);
  expect(rows.map(row => [row.conversationId, row.status])).toEqual([
    ['empty', 'not_started'], ['unknown', 'unknown'], ['history', 'conversation'],
  ]);
  expect(rows.some(row => row.status === 'queued')).toBe(false);
});

it('does not silently truncate the Team at 100 conversations and keeps only a demanded page mounted', () => {
  const roster = Array.from({ length: 105 }, (_, index) => ({ id: `worker-${index}`, title: `Worker ${index}`, turnCount: 0 }));
  const navigate = vi.fn();
  const list = createCoordinationWorkList(graph(), navigate, { state: {}, conversations: roster, team: true, digest: () => '' });
  document.body.replaceChildren(list);
  for (let page = 0; page < 13; page++) {
    expect(list.querySelectorAll('article').length).toBeLessThanOrEqual(COORDINATION_LIST_PAGE_SIZE);
    list.querySelector<HTMLButtonElement>('[data-coordination-focus="team-list-more"]')!.click();
  }
  expect(list.textContent).toContain('Worker 104');
  expect(list.querySelectorAll('article')).toHaveLength(1);
  list.querySelector<HTMLButtonElement>('[data-coordination-focus="work-open:team:conversation:worker-104"]')!.click();
  expect(navigate).toHaveBeenCalledWith('worker-104');
});

it('defaults to work actions and mounts topology only on explicit disclosure', () => {
  const board = createCoordinationBoard({ active: [{ id: 'a', status: 'active', conversationId: 'worker' }], recent: [] }, vi.fn());
  expect(board.querySelector('.coordination-work-list')).not.toBeNull();
  expect(board.querySelector('.coordination-canvas')).toBeNull();
  expect(board.querySelector('[data-coordination-focus="copy:work:a"]')).not.toBeNull();
  const topology = board.querySelector<HTMLDetailsElement>('.coordination-topology')!;
  expect(topology.open).toBe(false);
  topology.open = true; topology.dispatchEvent(new Event('toggle'));
  expect(board.querySelectorAll('.coordination-canvas')).toHaveLength(1);
  topology.dispatchEvent(new Event('toggle'));
  expect(board.querySelectorAll('.coordination-canvas')).toHaveLength(1);
});

it('copies exact identifiers without debug mode and leaves a selectable fallback on clipboard failure', async () => {
  const copy = vi.fn(async () => {});
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: copy } });
  const reference = createCoordinationReference({ conversationId: 'worker', assignmentId: 'assignment', grantRevision: 3, resultRevision: 2 }, 'a');
  document.body.replaceChildren(reference);
  reference.querySelector<HTMLButtonElement>('button')!.click();
  await vi.waitFor(() => expect(copy).toHaveBeenCalledWith('conversationId: worker\nassignmentId: assignment\ngrantRevision: 3\nresultRevision: 2'));
  copy.mockRejectedValueOnce(new Error('denied'));
  reference.querySelector<HTMLButtonElement>('button')!.click();
  await vi.waitFor(() => expect(reference.querySelector('details')!.open).toBe(true));
  expect(reference.textContent).toContain('projectCoordination.referenceCopyFailed');
  expect(reference.querySelector('pre')?.textContent).toContain('assignmentId: assignment');
});

it('preserves native reference disclosures, focused actions and list scroll after a changed projection', () => {
  const host = document.createElement('div'); document.body.replaceChildren(host);
  const build = () => createCoordinationWorkList(graph(), vi.fn(), { state: {}, conversations: [{ id: 'worker', title: 'Worker', turnCount: 0 }], digest: () => '' });
  host.append(build());
  host.querySelector('details')!.open = true;
  host.querySelector<HTMLElement>('[data-coordination-scroll]')!.scrollTop = 120;
  host.querySelector<HTMLButtonElement>('[data-coordination-focus="copy:conversation:worker"]')!.focus();
  const restore = captureCoordinationView(host); host.replaceChildren(build()); restore();
  expect(host.querySelector('details')!.open).toBe(true);
  expect(host.querySelector<HTMLElement>('[data-coordination-scroll]')!.scrollTop).toBe(120);
  expect((document.activeElement as HTMLElement).dataset.coordinationFocus).toBe('copy:conversation:worker');
});

it('does not let a selected graph node overwrite the same task reference in the work list', () => {
  const host = document.createElement('div'); document.body.replaceChildren(host);
  const build = () => createCoordinationBoard({ active: [{ id: 'a', status: 'active', conversationId: 'worker' }], recent: [] }, vi.fn(), 0, {},
    { viewState: { graphOpen: true, selectedId: 'work:a' } });
  host.append(build());
  const listReference = () => host.querySelector<HTMLDetailsElement>('[data-coordination-disclosure="reference:work:a"]')!;
  const graphReference = () => host.querySelector<HTMLDetailsElement>('[data-coordination-disclosure="reference:graph:work:a"]')!;
  listReference().open = true;
  listReference().querySelector('summary')!.focus();
  expect(graphReference().open).toBe(false);
  const restore = captureCoordinationView(host); host.replaceChildren(build()); restore();
  expect(listReference().open).toBe(true); expect(graphReference().open).toBe(false);
  expect(listReference().contains(document.activeElement)).toBe(true);
});
