// @vitest-environment jsdom
/** Composer controls repaint on every projected Turn change; an unchanged state writes nothing. */
import { describe, expect, it } from 'vitest';
import { updateComposerSendControls, type ComposerSendControlsInput } from './composer-send-controls';

const input = (turnBusy: boolean, stopping = false): ComposerSendControlsInput => ({
  document, translating: false, startupConnecting: false, turnBusy, stopping,
  hasAttachmentDraft: false, queueCount: 0,
  labels: { send: 'Send', sendTitle: 'Send (Enter)', stop: 'Stop generating', stopping: 'Stopping…' },
  onSend: () => undefined, onStop: () => undefined, requestRefresh: () => undefined,
});

describe('composer send controls', () => {
  it.each([
    ['idle', false, false],
    ['busy', true, false],
    ['stopping', true, true],
  ])('repaints an unchanged %s state without touching the DOM', (_state, busy, stopping) => {
    document.body.innerHTML = '<div class="input-actions-right"><button id="sendBtn"></button></div>'
      + '<textarea id="userInput"></textarea>';
    updateComposerSendControls(input(busy, stopping));
    const observer = new MutationObserver(() => undefined);
    observer.observe(document.body, { subtree: true, attributes: true, childList: true, characterData: true });
    updateComposerSendControls(input(busy, stopping));
    updateComposerSendControls(input(busy, stopping));
    const records = observer.takeRecords();
    observer.disconnect();
    expect(records.map(record => `${record.type}:${record.attributeName ?? ''}`)).toEqual([]);
  });

  it('still projects a real state change', () => {
    document.body.innerHTML = '<div class="input-actions-right"><button id="sendBtn"></button></div>'
      + '<textarea id="userInput"></textarea>';
    updateComposerSendControls(input(true));
    updateComposerSendControls(input(false));
    const button = document.getElementById('sendBtn')!;
    expect([button.className, button.title, button.getAttribute('aria-label'),
      button.hasAttribute('aria-busy')]).toEqual(['send-btn', 'Send (Enter)', 'Send', false]);
  });
});
