// @vitest-environment jsdom
import { expect, it, vi } from 'vitest';
import { createCoordinatorReport } from './project-coordinator-report';
import type { CoordinatorReportState } from '../api/project-coordination.generated';
vi.mock('../i18n', () => ({ t: (key: string) => key }));

const state: CoordinatorReportState = { projectPath: '/p', conversationId: 'c', bindingRevision: 2, unread: true,
  latest: { turnId: 'report-A', ordinal: 7, title: 'Choose migration time', kind: 'decision',
    content: 'Choose migration time\n\nWe need your decision. <img src=x>', createdAt: 100 } };

it('does not acknowledge on render, disclosure, or a background update; ack captures the displayed report', async () => {
  const markRead = vi.fn(async () => ({ ...state, latest: { ...state.latest!, turnId: 'report-B', ordinal: 8 } }));
  const changed = vi.fn();
  const report = createCoordinatorReport(state, { expanded: true, toggle: vi.fn(), valid: () => true, markRead, changed });
  document.body.replaceChildren(report);
  expect(markRead).not.toHaveBeenCalled(); expect(report.querySelector('img')).toBeNull();
  report.querySelector('button')!.click();
  await vi.waitFor(() => expect(changed).toHaveBeenCalledOnce());
  expect(markRead).toHaveBeenCalledWith({ path: '/p', conversationId: 'c', bindingRevision: 2, turnId: 'report-A' });
  expect(report.dataset.unread).toBe('true'); // Server refresh alone may clear it.
});

it('retains the unread message and retry action after failed acknowledgement', async () => {
  const changed = vi.fn(), markRead = vi.fn().mockRejectedValue(new Error('Connection lost'));
  const report = createCoordinatorReport(state, { expanded: true, toggle: vi.fn(), valid: () => true, markRead, changed });
  report.querySelector('button')!.click();
  await vi.waitFor(() => expect(report.querySelector('[role="alert"]')?.textContent).toBe('Connection lost'));
  expect(changed).not.toHaveBeenCalled(); expect(report.querySelector('button')!.disabled).toBe(false);
  expect(report.dataset.unread).toBe('true');
});

it('fences a disposed or navigated report before its write and after a late response', async () => {
  let valid = false; const changed = vi.fn(); let resolve!: (value: CoordinatorReportState) => void;
  const markRead = vi.fn(() => new Promise<CoordinatorReportState>(done => { resolve = done; }));
  const report = createCoordinatorReport(state, { expanded: false, toggle: vi.fn(), valid: () => valid, markRead, changed });
  report.querySelector('button')!.click(); expect(markRead).not.toHaveBeenCalled();
  valid = true; report.querySelector('button')!.click(); valid = false;
  resolve({ ...state, unread: false }); await Promise.resolve(); await Promise.resolve();
  expect(changed).not.toHaveBeenCalled();
});
