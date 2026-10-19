// @vitest-environment jsdom
import { afterEach, describe, expect, it } from 'vitest';
import type { Translator } from '../../i18n';
import { mountSwarmMessages, type SwarmMessageReceipt, type SwarmMessagesSnapshot } from './swarm-messages';
const translate = ((key: string) => key) as Translator;
const receipt = (id: string, extra: Partial<SwarmMessageReceipt> = {}): SwarmMessageReceipt => ({
  swarmRunId: 'run', agentId: 'worker', messageId: id, senderTaskId: 'run',
  submittedAt: Number(id) || 1, state: 'submitted', content: `body ${id}`, ...extra,
});
function setup(messages?: SwarmMessageReceipt[]) {
  const host = document.createElement('div'); document.body.append(host);
  const snapshot: SwarmMessagesSnapshot = { swarm_id: 'run', messageReceipts: messages, messageReceiptsAvailable: true,
    messageParticipants: [{ id: 'worker', taskId: 'task-worker', label: 'Worker' }, { id: 'other', label: 'Other' }] };
  const handle = mountSwarmMessages(host, snapshot, translate);
  return { host, snapshot, handle };
}
const visibleIds = (host: HTMLElement) => [...host.querySelectorAll<HTMLLIElement>('li:not([hidden])')].map(el => el.dataset.messageId);
afterEach(() => document.body.replaceChildren());
describe('run messages', () => {

  it('keeps preview and full text current without replacing the focused disclosure on update', () => {
    const { host, snapshot, handle } = setup([receipt('1')]);
    const details = host.querySelector<HTMLDetailsElement>('.sw-messages-content')!;
    const summary = details.querySelector('summary')!;
    details.open = true; summary.focus();
    const content = '<script>not markup</script>\n' + 'updated message '.repeat(20);
    const localized = ((key: string) => key === 'toolCmd.collapse' ? 'Collapse message' : key) as Translator;
    handle.update({ ...snapshot, messageReceipts: [receipt('1', { content })] }, localized);
    expect(host.querySelector('.sw-messages-content')).toBe(details);
    expect(details.open).toBe(true); expect(document.activeElement).toBe(summary);
    expect(details.querySelector('.sw-messages-preview')?.textContent).toBe(`${content.slice(0, 160)}…`);
    expect(details.querySelector('.sw-messages-collapse')?.textContent).toBe('Collapse message');
    expect(details.querySelector('.sw-messages-body')?.textContent).toBe(content);
    expect(details.querySelector('script')).toBeNull();
    details.open = false;
    handle.update({ ...snapshot, messageReceipts: [receipt('1', { content: 'short replacement' })] });
    expect(details.open).toBe(false); expect(document.activeElement).toBe(summary);
    expect(details.querySelector('.sw-messages-preview')?.textContent).toBe('short replacement');
    expect(details.querySelector('.sw-messages-body')?.textContent).toBe('short replacement');
  });

  it('folds technical identifiers separately from the complete body and retains diagnostics focus', () => {
    const { host, snapshot, handle } = setup([receipt('1')]);
    const body = host.querySelector<HTMLDetailsElement>('.sw-messages-row > details')!;
    const diagnostics = host.querySelector<HTMLDetailsElement>('.sw-messages-diagnostics')!;
    body.open = true; expect(diagnostics.open).toBe(false);
    expect(host.querySelector('.sw-messages-body')!.textContent).toBe('body 1');
    diagnostics.open = true; const summary = diagnostics.querySelector('summary')!; summary.focus();
    handle.update({ ...snapshot, messageReceipts: [receipt('1', { inputConfirmedAt: 100, state: 'input_confirmed' })] });
    expect(diagnostics.open).toBe(true); expect(document.activeElement).toBe(summary);
    expect(body.open).toBe(true);
  });

  it('renders mailbox epoch milliseconds without converting them from seconds', () => {
    const submittedAt = Date.parse('2026-10-15T12:00:00.123Z');
    const { host } = setup([receipt('1', { submittedAt: String(submittedAt),
      inputInjectedAt: submittedAt + 1000, inputConfirmedAt: submittedAt + 2000 })]);
    expect(host.querySelector('.sw-messages-time')?.textContent).toBe('2026-10-15T12:00:00.123Z');
    expect(host.querySelector('dl')?.textContent).toContain('2026-10-15T12:00:01.123Z');
    expect(host.querySelector('dl')?.textContent).toContain('2026-10-15T12:00:02.123Z');
  });
  it('renders explicit directions and the whole body as safe text', () => {
    const body = '<img src=x onerror=alert(1)>' + 'long text '.repeat(50);
    const { host } = setup([receipt('1', { content: body }), receipt('2', { senderTaskId: 'task-worker', agentId: 'run' })]);
    expect(host.querySelector('img,script')).toBeNull();
    expect(host.querySelector('.sw-messages-body')?.textContent).toBe(body);
    expect(host.querySelector('.sw-messages-preview')!.textContent!.length).toBe(161);
    expect(host.querySelector('.sw-messages-direction')?.textContent).toBe('swarm.messagesParent → Worker (worker)');
    expect(host.querySelectorAll('.sw-messages-direction')[1].textContent).toBe('Worker (worker) → swarm.messagesParent');
  });
  it('labels legacy root metadata and exact sender aliases as the main agent, including filters', () => {
    const { host, handle, snapshot } = setup([
      receipt('1', { senderTaskId: 'parent-task' }),
      receipt('2', { senderTaskId: 'task-worker', agentId: 'run' }),
    ]);
    handle.update({ ...snapshot, messageParticipants: [...snapshot.messageParticipants!,
      { id: 'run', taskId: 'parent-task', label: 'Coordinator' }] });
    expect([...host.querySelectorAll('.sw-messages-direction')].map(node => node.textContent)).toEqual([
      'swarm.messagesParent → Worker (worker)', 'Worker (worker) → swarm.messagesParent',
    ]);
    const filter = host.querySelector('select')!;
    expect([...filter.options].find(option => option.value === 'run')?.textContent).toBe('swarm.messagesParent');
    filter.value = 'run'; filter.dispatchEvent(new Event('change'));
    expect(visibleIds(host)).toEqual(['1', '2']);
    expect(host.textContent).not.toContain('Coordinator');
  });
  it('deduplicates only exact tuples and never regresses confirmed receipts', () => {
    const confirmed = receipt('1', { state: 'input_confirmed', content: 'confirmed body' });
    const { host, handle, snapshot } = setup([confirmed, receipt('1'), receipt('1', { agentId: 'other' }), receipt('1', { swarmRunId: 'foreign' })]);
    expect(host.querySelectorAll('li')).toHaveLength(2);
    const workerRow = [...host.querySelectorAll('li')].find(row => row.querySelector('.sw-messages-direction')?.textContent?.endsWith('Worker (worker)'))!;
    expect(workerRow.querySelector('.sw-messages-status')?.textContent).toBe('tool.agentMessageInputConfirmed');
    handle.update({ ...snapshot, messageReceipts: [receipt('1')] });
    expect(host.querySelector('.sw-messages-status')?.textContent).toBe('tool.agentMessageInputConfirmed');
    expect(host.querySelector('.sw-messages-body')?.textContent).toBe('body 1');
  });
  it('shows the recent three in submission order with expandable earlier messages', () => {
    const { host } = setup([receipt('4'), receipt('1'), receipt('3'), receipt('2')]);
    expect(visibleIds(host)).toEqual(['2', '3', '4']);
    const button = host.querySelector('button')!;
    button.click(); expect(visibleIds(host)).toEqual(['1', '2', '3', '4']);
    expect(button.getAttribute('aria-expanded')).toBe('true');
    button.click(); expect(visibleIds(host)).toEqual(['2', '3', '4']);
  });
  it('preserves open bodies, earlier expansion and focused summaries across reordered updates', () => {
    const { host, snapshot, handle } = setup([receipt('1'), receipt('2'), receipt('3'), receipt('4')]);
    host.querySelector('button')!.click();
    const details = host.querySelector('details')!; details.open = true;
    const summary = details.querySelector('summary')!; summary.focus();
    handle.update({ ...snapshot, messageReceipts: [receipt('0', { submittedAt: 0.5 }), ...snapshot.messageReceipts!] });
    expect(document.activeElement).toBe(summary); expect(details.open).toBe(true);
    expect(visibleIds(host)).toEqual(['0', '1', '2', '3', '4']);
    expect(host.querySelectorAll('.sw-messages-row > details')[1]).toBe(details);
  });
  it('does not hide the focused row when a new recent message arrives', () => {
    const { host, snapshot, handle } = setup([receipt('1'), receipt('2'), receipt('3')]);
    const summary = host.querySelector('summary')!; summary.focus();
    handle.update({ ...snapshot, messageReceipts: [...snapshot.messageReceipts!, receipt('4')] });
    expect(document.activeElement).toBe(summary);
    expect(summary.closest('li')!.hidden).toBe(false);
  });
  it('filters either endpoint, preserves select focus/value and uses the field kit', () => {
    const { host, snapshot, handle } = setup([receipt('1'), receipt('2', { senderTaskId: 'task-worker', agentId: 'other' }), receipt('3', { agentId: 'other' })]);
    const filter = host.querySelector('select')!;
    expect(filter.parentElement?.className).toBe('ui-select');
    filter.value = 'worker'; filter.dispatchEvent(new Event('change')); filter.focus();
    expect(visibleIds(host)).toEqual(['1', '2']);
    handle.update({ ...snapshot, messageParticipants: [...snapshot.messageParticipants!, { id: 'new', label: 'New' }] });
    expect(document.activeElement).toBe(filter); expect(filter.value).toBe('worker');
    expect(visibleIds(host)).toEqual(['1', '2']);
  });
  it('distinguishes unavailable, empty and missing evidence without guessing sender or dates', () => {
    const { host, handle, snapshot } = setup();
    expect(host.querySelector('p')?.textContent).toBe('swarm.messagesUnavailable');
    handle.update({ ...snapshot, messageReceipts: [] });
    expect(host.querySelector('p')?.textContent).toBe('swarm.messagesEmpty');
    handle.update({ ...snapshot, messageReceipts: [receipt('1', { senderTaskId: 'unmapped-task', submittedAt: undefined, content: undefined, state: undefined })] });
    expect(host.querySelector('.sw-messages-direction')?.textContent).toBe('swarm.messagesUnknownSender (unmapped-task) → Worker (worker)');
    expect(host.querySelector('.sw-messages-time')?.textContent).toBe('swarm.messagesTimeUnavailable');
    expect(host.querySelector('.sw-messages-body')?.textContent).toBe('tool.agentMessageUnavailable');

    expect(host.querySelector('.sw-messages-status')?.textContent).toBe('tool.agentMessageUnconfirmed');
    handle.update({ ...snapshot, messageReceiptsAvailable: false, messageReceipts: [] });
    expect(host.querySelector('p')?.textContent).toBe('swarm.messagesUnavailable');
  });
  it('orders unknown submission times deterministically without using injection timestamps', () => {
    const { host } = setup([receipt('z', { submittedAt: undefined, inputInjectedAt: 1 }), receipt('a', { submittedAt: undefined, inputInjectedAt: 999 }), receipt('2')]);
    expect(visibleIds(host)).toEqual(['2', 'a', 'z']);
  });
  it('bounds mounted history and retains open bodies outside the recent window', () => {
    const messages = Array.from({ length: 60 }, (_, i) => receipt(String(i + 1)));
    const { host, snapshot, handle } = setup(messages);
    expect(host.querySelectorAll('li')).toHaveLength(3);
    const details = host.querySelector('details')!; details.open = true;
    handle.update({ ...snapshot, messageReceipts: [...messages, receipt('61')] });
    expect(details.isConnected).toBe(true); expect(details.open).toBe(true);
    expect(host.querySelectorAll('li')).toHaveLength(4);
    host.querySelector('button')!.click();
    expect(host.querySelectorAll('li')).toHaveLength(23);
    host.querySelector('button')!.click();
    expect(host.querySelectorAll('li')).toHaveLength(43);
  });
  it('shows exact receipt evidence and a non-error delivery state', () => {
    const { host } = setup([receipt('1', { inputInjectedAt: 2000, inputConfirmedAt: 3000, state: 'input_confirmed' })]);
    const evidence = host.querySelector('dl')!;
    expect(evidence.textContent).toContain('swarm.messagesRunIdrun');
    expect(evidence.textContent).toContain('swarm.messagesMessageId1');
    expect(evidence.textContent).toContain('1970-01-01T00:00:02.000Z');
    expect(evidence.textContent).toContain('1970-01-01T00:00:03.000Z');
    expect(host.querySelector<HTMLElement>('.sw-messages-status')!.dataset.state).toBe('input_confirmed');
  });
  it('treats absent completeness as unavailable and resolves resumed sender aliases exactly', () => {
    const { host, handle, snapshot } = setup([]);
    handle.update({ ...snapshot, messageReceiptsAvailable: undefined });
    expect(host.querySelector('p')!.textContent).toBe('swarm.messagesUnavailable');
    handle.update({ ...snapshot, messageReceipts: [receipt('1', { senderTaskId: 'task-worker' })],
      messageParticipants: [...snapshot.messageParticipants!, { id: 'worker', taskId: 'resumed-task', label: 'Worker' }] });
    expect(host.querySelector('.sw-messages-direction')!.textContent).toBe('Worker (worker) → Worker (worker)');
  });
  it('does not guess ambiguous sender task mappings and resets run-local reader state', () => {
    const { host, handle, snapshot } = setup([receipt('1', { senderTaskId: 'task-worker' })]);
    handle.update({ ...snapshot, messageParticipants: [...snapshot.messageParticipants!, { id: 'duplicate', taskId: 'task-worker', label: 'Duplicate' }] });
    expect(host.querySelector('.sw-messages-direction')?.textContent).toContain('swarm.messagesUnknownSender (task-worker)');
    handle.update({ swarm_id: 'next', messageReceipts: [] });
    expect(host.querySelectorAll('li')).toHaveLength(0);
    handle.dispose(); expect(host.children).toHaveLength(0);
    handle.update(snapshot); expect(host.children).toHaveLength(0);
  });
});
