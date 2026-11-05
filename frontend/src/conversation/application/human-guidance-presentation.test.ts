import { describe, expect, it } from 'vitest';

import { createHumanGuidancePresentationStore } from './human-guidance-presentation';

describe('human guidance presentation store', () => {
  it('turns an answered blocking question into the submitted read model', () => {
    const store = createHumanGuidancePresentationStore();
    store.patch('conv', 'blocking', { submittedResponse: 'yes' });
    const round = { guidanceId: 'blocking', status: 'awaiting_human', toolName: 'ask_human' };

    expect(store.decorate('conv', round)).toEqual({
      ...round, status: 'submitted', _hgUserResponse: 'yes',
    });
  });

  it('marks a non-blocking question replied in chat without changing its status', () => {
    const store = createHumanGuidancePresentationStore();
    store.patch('conv', 'chat', { submittedResponse: 'Choice B' });
    const round = {
      guidanceId: 'chat', status: 'done', toolName: 'ask_human', guidanceBlocking: false,
    };

    const decorated = store.decorate('conv', round);

    expect(decorated).toEqual({ ...round, _hgRepliedInChat: 'Choice B' });
    expect(decorated).not.toHaveProperty('_hgUserResponse');
    expect(round).not.toHaveProperty('_hgRepliedInChat');
  });

  it('leaves settled blocking, unanswered and foreign rounds untouched', () => {
    const store = createHumanGuidancePresentationStore();
    store.patch('conv', 'settled', { submittedResponse: 'late' });
    const settled = { guidanceId: 'settled', status: 'done', toolName: 'ask_human' };
    const unanswered = {
      guidanceId: 'open', status: 'done', toolName: 'ask_human', guidanceBlocking: false,
    };
    const foreign = { ...settled, guidanceBlocking: false };

    expect(store.decorate('conv', settled)).toBe(settled);
    expect(store.decorate('conv', unanswered)).toBe(unanswered);
    expect(store.decorate('other-conv', foreign)).toBe(foreign);
  });
});
