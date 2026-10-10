// @vitest-environment jsdom
import { beforeEach, expect, it, vi } from 'vitest';
vi.mock('../i18n', () => ({ t: (key: string) => key }));
import { createProjectMessageReceipts } from './project-message-receipts';
import type { ProjectMessagePage } from '../api/project-coordination.generated';

beforeEach(() => document.body.replaceChildren());
const page: ProjectMessagePage = { messages: [{ id: 'one', sender: 'worker', kind: 'report', state: 'input_confirmed',
  decision: 'accepted', revision: 3, content: '<unsafe>evidence</unsafe>', decisionNote: 'Not proof of delivery' }], next_before_id: 'one' };

it('reads only on disclosure and separates input from consideration', async () => {
  const read = vi.fn(async (_before: string) => page);
  const node = createProjectMessageReceipts(read, () => true); document.body.append(node);
  expect(read).not.toHaveBeenCalled();
  node.querySelector('button')!.click();
  await vi.waitFor(() => expect(node.textContent).toContain('projectCoordination.decision.accepted'));
  expect(node.textContent).toContain('projectCoordination.messageState.input_confirmed');
  expect(node.textContent).toContain('<unsafe>evidence</unsafe>');
  expect(node.querySelector('unsafe')).toBeNull();
  const older = [...node.querySelectorAll('button')].find(n => n.textContent === 'projectCoordination.olderMessages')!;
  older.click(); await vi.waitFor(() => expect(read).toHaveBeenLastCalledWith('one'));
});

it('drops a result after project/conversation invalidation', async () => {
  let valid = true; let resolve!: (value: ProjectMessagePage) => void;
  const node = createProjectMessageReceipts(() => new Promise(r => { resolve = r; }), () => valid);
  document.body.append(node); node.querySelector('button')!.click(); valid = false; resolve(page);
  await Promise.resolve(); await Promise.resolve();
  expect(node.textContent).not.toContain('worker');
});

it('shows an unavailable mailbox instead of fabricating zero messages', async () => {
  const node = createProjectMessageReceipts(async () => { throw new Error('Store unavailable'); }, () => true);
  document.body.append(node); node.querySelector('button')!.click();
  await vi.waitFor(() => expect(node.textContent).toContain('projectCoordination.messagesUnavailable'));
  expect(node.textContent).toContain('Store unavailable');
  expect(node.textContent).not.toContain('projectCoordination.empty');
});
