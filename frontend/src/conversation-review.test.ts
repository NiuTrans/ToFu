import { describe, expect, it } from 'vitest';
import type { TurnRecord } from './api/conversation-sync.generated';
import { createConversationReviewTracker } from './conversation-review';

const turn = (overrides: Partial<TurnRecord> = {}): TurnRecord => ({
  turnId: 'reply', presentationId: 'reply', conversationId: 'c', laneId: 'main',
  ordinal: 1, actor: 'assistant', status: 'completed', kind: 'flow_node', runId: '',
  currentAttemptId: 'attempt', projection: { content: 'Here is a plan.' },
  projectionRevision: 1, settlement: {}, createdAt: 1, updatedAt: 2, ...overrides,
});
function fixture() {
  const data = new Map<string, string>();
  const storage = { getItem: (key: string) => data.get(key) || null,
    setItem: (key: string, value: string) => { data.set(key, value); } };
  let owner: number | null = 1, visible = true;
  const host = { ownerId: () => owner, storage: () => storage, canView: () => visible };
  return { data, host, tracker: createConversationReviewTracker(host),
    owner: (value: number | null) => { owner = value; },
    visible: (value: boolean) => { visible = value; } };
}

describe('completed reply review receipts', () => {
  it('keeps a completed plan pending across reload until its reply is displayed', () => {
    const f = fixture();
    expect(f.tracker.observe('c', [turn({ status: 'running' })])).toBe(false);
    expect(f.tracker.observe('c', [turn()])).toBe(true);
    const reload = createConversationReviewTracker(f.host);
    expect(reload.observe('c', [])).toBe(true);
    expect(reload.viewed('c', [turn()], [])).toBe(false);
    f.visible(false);
    expect(reload.viewed('c', [turn()], ['reply'])).toBe(false);
    f.visible(true);
    expect(reload.viewed('c', [turn()], ['reply'])).toBe(true);
    expect(createConversationReviewTracker(f.host).observe('c', [turn()])).toBe(false);
  });
  it('rearms for regeneration and new turns, but never for metadata or stale history', () => {
    const { tracker } = fixture();
    tracker.viewed('c', [turn()], ['reply']);
    expect(tracker.observe('c', [turn({ projectionRevision: 9, updatedAt: 5 })])).toBe(false);
    const regenerated = turn({ currentAttemptId: 'retry', updatedAt: 10 });
    expect(tracker.observe('c', [regenerated])).toBe(true);
    tracker.viewed('c', [regenerated], ['reply']);
    expect(tracker.observe('c', [turn()])).toBe(false);
    expect(tracker.observe('c', [turn({ turnId: 'next', ordinal: 3, updatedAt: 20 })])).toBe(true);
  });
  it.each(['failed', 'interrupted', 'truncated', 'pending'] as const)(
    'does not turn %s into successful completion', status => {
      expect(fixture().tracker.observe('c', [turn({ status })])).toBe(false);
    });
  it('requires canonical, main-lane assistant/planner completion without error or waiting', () => {
    for (const overrides of [{ actor: 'human' }, { laneId: 'branch' }, { conversationId: 'other' },
      { settlement: { error: { message: 'failed' } } }, { projection: { waitingOn: {} } }]) {
      expect(fixture().tracker.observe('c', [turn(overrides as Partial<TurnRecord>)])).toBe(false);
    }
    expect(fixture().tracker.observe('c', [turn({ actor: 'planner' })])).toBe(true);
  });
  it('isolates identities and observes a second tab acknowledgement', () => {
    const f = fixture();
    f.tracker.observe('c', [turn()]);
    const second = createConversationReviewTracker(f.host);
    second.viewed('c', [turn()], ['reply']);
    expect(f.tracker.storageChanged([...f.data.keys()][0])).toBe(true);
    expect(f.tracker.observe('c', [turn()])).toBe(false);
    f.owner(2);
    expect(f.tracker.observe('c', [])).toBe(false);
    expect(f.tracker.observe('c', [turn()])).toBe(true);
    f.owner(null);
    expect(f.tracker.observe('c', [turn()])).toBe(false);
  });
  it('survives corrupt or unavailable browser storage', () => {
    const f = fixture();
    f.data.set('tofu:conversation-review:v1:1:c', '{');
    expect(f.tracker.observe('c', [turn()])).toBe(true);
    const tracker = createConversationReviewTracker({ ...f.host, storage: () => undefined });
    expect(tracker.observe('c', [turn()])).toBe(true);
    tracker.viewed('c', [turn()], ['reply']);
    expect(tracker.observe('c', [turn()])).toBe(false);
    tracker.viewed('c', [turn()], ['reply']);
    expect(tracker.observe('c', [turn()])).toBe(false);
  });
  it('keeps one receipt per conversation across repeated completions', () => {
    const f = fixture();
    for (let index = 1; index <= 1000; index += 1) {
      const reply = turn({ turnId: `reply-${index}`, ordinal: index, updatedAt: index });
      f.tracker.observe('c', [reply]);
      f.tracker.viewed('c', [reply], [reply.turnId]);
    }
    expect(f.data.size).toBe(1);
    const characters = [...f.data].reduce((total, [key, value]) => total + key.length + value.length, 0);
    expect(characters).toBeLessThan(200);
    console.info(`Review receipt after 1000 completions: ${characters} UTF-16 code units`);
  });
  it('does not acknowledge a newer completion from another tab with an old paint', () => {
    const f = fixture();
    f.tracker.observe('c', [turn()]);
    const second = createConversationReviewTracker(f.host);
    second.observe('c', [turn({ turnId: 'new', ordinal: 2, updatedAt: 10 })]);
    expect(f.tracker.viewed('c', [turn()], ['reply'])).toBe(false);
    expect(createConversationReviewTracker(f.host).observe('c', [])).toBe(true);
  });
});
