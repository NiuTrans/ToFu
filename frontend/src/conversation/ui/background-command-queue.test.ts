// @vitest-environment jsdom
/** Background completion runs are presentation only; delivery identities stay flat. */
import { afterEach, describe, expect, it } from 'vitest';
import type { ConversationQueueItem } from '../../api/conversation-sync.generated';
import { createTurnState } from '../domain/turn-store';
import { selectConversationViewModel } from '../presentation/conversation-view-model';
import { createClassicConversationRenderers } from './classic-conversation-renderers';
import { createConversationSurface } from './conversation-surface';
import type { ConversationIntent, ConversationSurface } from './surface-contracts';

const owned: ConversationSurface[] = [];
afterEach(() => { owned.splice(0).forEach(surface => surface.dispose()); document.body.replaceChildren(); });

function completion(queueId: string, position: number, commandId = queueId): ConversationQueueItem {
  return { queueId, position, kind: 'workflow_step', priority: 80, timestamp: 1,
    text: 'RAW_BACKGROUND_ENVELOPE', backgroundCommand: {
      commandId, status: 'completed', command: 'echo <script>unsafe</script>',
      resultPreview: '<img src=x onerror=bad> ' + 'output '.repeat(400), resultTruncated: true,
    } };
}

function fixture() {
  const host = document.createElement('main'); document.body.append(host);
  const intents: ConversationIntent[] = [];
  const surface = createConversationSurface(host, {
    ...createClassicConversationRenderers({ renderSafeMarkdownHtml: value => value }),
    onIntent: intent => { intents.push(intent); },
  });
  owned.push(surface);
  const render = (queueItems: ConversationQueueItem[], conversationId = 'conversation') => {
    const state = { ...createTurnState(conversationId), queueItems };
    const before = JSON.stringify(state);
    const model = selectConversationViewModel(state);
    surface.render(model);
    expect(JSON.stringify(state)).toBe(before);
    expect(model.queue.map(item => item.queueId)).toEqual(queueItems.map(item => item.queueId));
  };
  const row = (id: string) => host.querySelector<HTMLElement>(`[data-queue-id="${id}"]`)!;
  return { host, surface, intents, render, row };
}

describe('queued background command groups', () => {
  it('shows one counted status with independent bounded, escaped result rows', () => {
    const { host, render, row } = fixture();
    render([completion('q1', 1), completion('q2', 2)]);
    const headings = host.querySelectorAll('.bgcmd-queue-header');
    expect(headings).toHaveLength(1);
    expect(headings[0].textContent).toBe('2 background commands finished · delivery unconfirmed');
    expect(row('q1').parentElement).toBe(row('q2').parentElement);
    expect(host.querySelectorAll('details.bgcmd-details')).toHaveLength(2);
    expect(host.querySelectorAll('[data-conversation-action="remove-queue"]')).toHaveLength(2);
    expect(host.querySelectorAll('script, img')).toHaveLength(0);
    expect(host.textContent).not.toContain('RAW_BACKGROUND_ENVELOPE');
    expect(row('q2').querySelector('.sw-card-raw-pre')!.textContent).toHaveLength(2000);
    expect(row('q2').querySelector('.bgcmd-note')!.textContent).toContain('truncated');
  });

  it('keeps ordinary messages in place and never deduplicates repeated command IDs', () => {
    const { host, render } = fixture();
    const ordinary: ConversationQueueItem = { queueId: 'human', position: 3, kind: 'real',
      text: 'Queued instruction', priority: 90, timestamp: 1, isPeerMessage: true,
      isPeerHuman: false, fromConv: 'peer-conversation' };
    render([completion('q1', 1, 'shared'), completion('q2', 2, 'shared'), ordinary, completion('q4', 4)]);
    expect([...host.querySelectorAll('[data-queue-id]')].map(node => (node as HTMLElement).dataset.queueId))
      .toEqual(['q1', 'q2', 'human', 'q4']);
    expect(host.querySelectorAll('.bgcmd-queue-header')).toHaveLength(2);
    expect(host.querySelectorAll('.bgcmd-id')).toHaveLength(3);
    expect(host.querySelector('[data-queue-id="human"] .queue-item-number')!.textContent).toBe('#3');
    expect(host.querySelector('[data-queue-id="human"] .queue-item-src')!.textContent).toContain('peer-conversation');
  });

  it('preserves disclosure, focus, controls and unchanged selected output as a group changes', () => {
    const { render, row } = fixture();
    const first = completion('q1', 1); const second = completion('q2', 2);
    render([first, second]);
    const details = row('q2').querySelector<HTMLDetailsElement>('details')!;
    const summary = details.querySelector('summary')!;
    const cancel = row('q2').querySelector<HTMLButtonElement>('[data-conversation-action="remove-queue"]')!;
    const output = details.querySelector('.sw-card-raw-pre')!;
    details.open = true; summary.focus();
    const selection = document.getSelection()!;
    const range = document.createRange(); range.setStart(output.firstChild!, 0); range.setEnd(output.firstChild!, 12);
    selection.removeAllRanges(); selection.addRange(range);
    const selected = selection.toString();
    render(JSON.parse(JSON.stringify([first, second])));
    render([{ ...second, position: 1 }]);
    expect(row('q2').querySelector('details')).toBe(details);
    expect(details.open).toBe(true);
    expect(document.activeElement).toBe(summary);
    expect(selection.toString()).toBe(selected);
    expect(row('q2').querySelector('[data-conversation-action="remove-queue"]')).toBe(cancel);
    expect(row('q2').querySelector('.bgcmd-queue-header')!.textContent)
      .toBe('Background command finished · delivery unconfirmed');
    render([{ ...second, position: 1 }, completion('q3', 2)]);
    expect(details.open).toBe(true); expect(document.activeElement).toBe(summary);
    expect(selection.toString()).toBe(selected);
    render([{ ...second, position: 1, backgroundCommand: { ...second.backgroundCommand!, resultPreview: 'Updated evidence' } }]);
    expect(row('q2').querySelector('details')).toBe(details);
    expect(details.querySelector('.sw-card-raw-pre')!.textContent).toBe('Updated evidence');
    expect(document.activeElement).toBe(summary);
  });

  it('replaces only mode-specific chrome when a row gains or loses completion evidence', () => {
    const { render, row, host } = fixture();
    const ordinary: ConversationQueueItem = { queueId: 'q1', position: 1, kind: 'real',
      text: 'Original instruction', priority: 90, timestamp: 1 };
    render([ordinary]);
    const article = row('q1');
    render([completion('q1', 1), completion('q2', 2)]);
    expect(row('q1')).toBe(article);
    expect(host.textContent).not.toContain('Original instruction');
    expect(host.querySelectorAll('[data-conversation-action="remove-queue"]')).toHaveLength(2);
    expect(host.querySelectorAll('.bgcmd-queue-header')).toHaveLength(1);
    render([ordinary]);
    expect(row('q1')).toBe(article);
    expect(host.querySelectorAll('.bgcmd-queue-header, .bgcmd-details')).toHaveLength(0);
    expect(host.textContent).toContain('Original instruction');
  });

  it('routes cancellation to the exact delivery intent and retires groups on conversation change', () => {
    const { render, row, host, intents, surface } = fixture();
    render([completion('q1', 1, 'same-command'), completion('q2', 2, 'same-command')]);
    const secondCancel = row('q2').querySelector<HTMLButtonElement>('[data-conversation-action="remove-queue"]')!;
    expect(secondCancel.getAttribute('aria-label')).toContain('same-command');
    secondCancel.click();
    expect(intents).toEqual([{ type: 'remove-queue', conversationId: 'conversation', queueId: 'q2' }]);
    expect(host.querySelectorAll('[data-queue-id]')).toHaveLength(2); // Click is not an acknowledgement.
    render([], 'other-conversation');
    expect(host.querySelectorAll('.bgcmd-queue-header, [data-queue-id]')).toHaveLength(0);
    surface.dispose(); secondCancel.click();
    expect(intents).toHaveLength(1);
  });
});
