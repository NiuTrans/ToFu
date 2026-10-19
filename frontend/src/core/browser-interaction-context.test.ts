// @vitest-environment jsdom
import { describe, expect, it } from 'vitest';
import { createBrowserInteractionContext } from './browser-interaction-context';

describe('content-free interaction evidence', () => {
  it('captures destination before handlers and excludes links, text and typing', () => {
    document.body.innerHTML = '<div class="conv-item" data-conv-id="destination"><button>Private title</button></div>'
      + '<a href="https://example.test/private?token=secret">Secret link</a><input value="secret">';
    let now = 100;
    const owner = createBrowserInteractionContext(document, () => now);
    const button = document.querySelector('button')!;
    button.addEventListener('click', () => {
      expect(owner.context(100)).toMatchObject({ interaction: 'conversation-switch', interactionConversationId: 'destination' });
    });
    button.click();
    expect(owner.context(90)).toEqual({});
    expect(owner.context(90, 110)).toMatchObject({ interaction: 'conversation-switch', interactionAgeMs: -10 });
    expect(owner.context(80, 99)).toEqual({});
    document.querySelector('input')!.dispatchEvent(new KeyboardEvent('keydown', { key: 's', bubbles: true }));
    expect(owner.context(110).interaction).toBe('conversation-switch');
    now = 200;
    document.querySelector('a')!.addEventListener('click', event => event.preventDefault());
    document.querySelector('a')!.click();
    expect(owner.context(210)).toEqual({ interaction: 'link', interactionAgeMs: 10 });
    expect(JSON.stringify(owner.context(210))).not.toMatch(/private|secret|token|https/i);
    expect(owner.context(30201)).toEqual({});
    owner.dispose(); owner.dispose(); document.querySelector('a')!.click();
    expect(owner.context(31000)).toEqual({});
  });
});
