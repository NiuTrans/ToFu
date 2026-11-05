import { describe, expect, it } from 'vitest';
import {
  cancelledCompactionNotice, createScheduledCompactions, readCancelledCompaction,
  readScheduledCompaction, scheduledCompactionNotice, scheduledCompactionState,
} from './scheduled-compaction';

const reply = { ok: true, scheduled: true, alreadyScheduled: false, requestId: 'r1',
  requestedAt: 1_700_000_000_000, protection: 'coordinator', appliesAt: 'next_reply' };
const busyReply = { ...reply, requestId: 'r2', protection: null, appliesAt: 'next_model_call' };
const fallback = (_key: string, value: string) => value;
const idle = (turns: number) => ({ busy: false, turns });
const busy = (turns: number) => ({ busy: true, turns });

describe('readScheduledCompaction', () => {
  it('accepts only the complete scheduled reply', () => {
    expect(readScheduledCompaction(reply)).toEqual({ requestId: 'r1', requestedAt: reply.requestedAt,
      alreadyScheduled: false, protection: 'coordinator', appliesAt: 'next_reply' });
    expect(readScheduledCompaction(busyReply)).toMatchObject({ protection: null, appliesAt: 'next_model_call' });
    for (const other of [null, [], { ok: true, archiveId: 'a1', tokensBefore: 9, tokensAfter: 3 },
      { ...reply, ok: false }, { ...reply, scheduled: 'yes' }, { ...reply, requestId: '' },
      { ...reply, requestedAt: Number.NaN }, { ...reply, protection: 'global' },
      { ...reply, protection: undefined }, { ...reply, alreadyScheduled: undefined },
      { ...reply, appliesAt: undefined }, { ...reply, appliesAt: 'later' }]) {
      expect(readScheduledCompaction(other)).toBeNull();
    }
  });

  it('distinguishes a new request from a pending one and names when it applies', () => {
    const created = readScheduledCompaction(reply)!;
    const pending = readScheduledCompaction({ ...reply, alreadyScheduled: true })!;
    const running = readScheduledCompaction(busyReply)!;
    expect(scheduledCompactionNotice(created, fallback)).toMatch(/^Compaction scheduled: the next reply/);
    expect(scheduledCompactionNotice(pending, fallback)).toMatch(/already scheduled for the next reply/);
    expect(scheduledCompactionNotice(running, fallback)).toMatch(/before its next model call/);
    expect(scheduledCompactionNotice({ ...running, alreadyScheduled: true }, fallback))
      .toMatch(/already scheduled for the next model call/);
    expect(scheduledCompactionState('next_model_call', fallback)).toBe('Compaction scheduled for the next model call');
    expect(scheduledCompactionState('next_reply', fallback)).toBe('Compaction scheduled for the next reply');
  });
});

describe('cancellation replies', () => {
  it('reads only the complete outcome and words each case', () => {
    expect(readCancelledCompaction({ ok: true, cancelled: true, applying: false }))
      .toEqual({ cancelled: true, applying: false });
    for (const other of [null, [], { ok: true, cancelled: true }, { ok: false, cancelled: true, applying: false },
      { ok: true, cancelled: 'yes', applying: false }]) {
      expect(readCancelledCompaction(other)).toBeNull();
    }
    expect(cancelledCompactionNotice({ cancelled: true, applying: false }, fallback))
      .toEqual({ level: 'success', msg: 'Scheduled compaction cancelled' });
    expect(cancelledCompactionNotice({ cancelled: true, applying: true }, fallback).msg).toMatch(/already started/);
    expect(cancelledCompactionNotice({ cancelled: false, applying: false }, fallback).msg)
      .toMatch(/No compaction was still scheduled/);
  });
});

describe('createScheduledCompactions', () => {
  it('a request from an idle page stays pending until the next run settles', () => {
    const state = createScheduledCompactions();
    expect(state.accept('c1', reply, idle(4))).not.toBeNull();
    expect(state.pending('c1', idle(4))).toBe(true);
    expect(state.current('c1')?.requestId).toBe('r1');
    expect(state.pending('c1', busy(6))).toBe(true);
    expect(state.pending('c1', idle(6))).toBe(false);
    expect(state.current('c1')).toBeNull();
  });

  it('a woken running reply retires the request when that reply settles', () => {
    const state = createScheduledCompactions();
    state.accept('c1', busyReply, busy(4));
    expect(state.pending('c1', busy(4))).toBe(true);
    expect(state.pending('c1', idle(4))).toBe(false);
  });

  it('a run that cannot apply the request is not mistaken for the one that does', () => {
    const state = createScheduledCompactions();
    state.accept('c1', reply, busy(4));
    expect(state.pending('c1', busy(5))).toBe(true);
    expect(state.pending('c1', idle(5))).toBe(true);
    expect(state.pending('c1', idle(5))).toBe(true);
    expect(state.pending('c1', busy(7))).toBe(true);
    expect(state.pending('c1', idle(7))).toBe(false);
  });

  it('retires after a run this page did not watch, and on explicit cancel', () => {
    const state = createScheduledCompactions();
    state.accept('c1', reply, idle(4));
    expect(state.pending('c2', idle(0))).toBe(false);
    expect(state.pending('c1', idle(6))).toBe(false);
    state.accept('c1', reply, idle(6));
    state.forget('c1');
    expect(state.pending('c1', idle(6))).toBe(false);
    expect(state.current('c1')).toBeNull();
  });

  it('records nothing for ordinary results or missing conversations', () => {
    const state = createScheduledCompactions();
    expect(state.accept('c1', { ok: true, archiveId: 'a1' }, idle(4))).toBeNull();
    expect(state.accept('', reply, idle(4))).not.toBeNull();
    expect(state.pending('c1', idle(4))).toBe(false);
    expect(state.pending(null, idle(4))).toBe(false);
    expect(state.current(null)).toBeNull();
  });
});
