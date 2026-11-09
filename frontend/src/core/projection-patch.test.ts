/** Ordered projection edits stay atomic and copy each shared history once. */
import { describe, expect, it, vi } from 'vitest';
import { applyProjectionPatch, type ProjectionPatchOperation } from './projection-patch';

function freeze<T>(value: T): T {
  if (value && typeof value === 'object' && !Object.isFrozen(value)) {
    Object.values(value).forEach(freeze);
    Object.freeze(value);
  }
  return value;
}

const patch = (operations: ProjectionPatchOperation[]) => freeze({ version: 1, operations });

describe('projection patch copy ownership', () => {
  it('copies a long shared history once for a batch of sibling field updates', () => {
    const toolRounds = Array.from({ length: 128 }, (_, index) => ({ text: `round ${index}`, status: 'running' }));
    const source = freeze({ content: 'answer', toolRounds, untouched: { label: 'stable' } });
    const operations: ProjectionPatchOperation[] = [];
    for (let index = 120; index < 128; index += 1) operations.push(
      { op: 'append_text', path: ['toolRounds', index, 'text'], value: ' complete' },
      { op: 'set', path: ['toolRounds', index, 'status'], value: 'completed' },
    );
    const edits = patch(operations);
    const originalSlice = Array.prototype.slice;
    let copiedHistories = 0;
    const copy = vi.spyOn(Array.prototype, 'slice').mockImplementation(function (this: unknown[], start?: number, end?: number) {
      if (this.length === toolRounds.length && this[0] === toolRounds[0]) copiedHistories += 1;
      return originalSlice.call(this, start, end);
    });
    let result: ReturnType<typeof applyProjectionPatch>;
    try { result = applyProjectionPatch(source, edits); }
    finally { copy.mockRestore(); }
    const rounds = result!.toolRounds as typeof toolRounds;
    expect(rounds[127]).toEqual({ text: 'round 127 complete', status: 'completed' });
    expect(source.toolRounds[127].status).toBe('running');
    expect(rounds[0]).toBe(source.toolRounds[0]);
    expect(result!.untouched).toBe(source.untouched);
    expect(copiedHistories).toBe(1);
  });

  it('retains sequential index meaning across remove, append, truncate and nested edits', () => {
    const source = freeze({ rows: [{ text: 'a' }, { text: 'b' }, { text: 'c' }] });
    const addition = freeze({ text: 'd' });
    const edits = patch([
      { op: 'append_text', path: ['rows', 2, 'text'], value: '!' },
      { op: 'remove', path: ['rows', 0] },
      { op: 'append_text', path: ['rows', 0, 'text'], value: '?' },
      { op: 'append', path: ['rows'], value: [addition] },
      { op: 'append_text', path: ['rows', 2, 'text'], value: '+' },
      { op: 'truncate', path: ['rows'], length: 2 },
      { op: 'set', path: ['rows', 1, 'text'], value: 'done' },
    ]);
    expect(applyProjectionPatch(source, edits)).toEqual({ rows: [{ text: 'b?' }, { text: 'done' }] });
    expect(source.rows.map(row => row.text)).toEqual(['a', 'b', 'c']);
    expect(addition.text).toBe('d');
  });

  it('isolates borrowed patch values and aliases across roots and nested histories', () => {
    const row = freeze({ text: 'original' });
    const payload = freeze({ rows: [row, row], sibling: { text: 'payload' } });
    const source = freeze({ left: [row, row], right: [row, row] });
    const result = applyProjectionPatch(source, patch([
      { op: 'append_text', path: ['left', 0, 'text'], value: ' left' },
      { op: 'append_text', path: ['right', 0, 'text'], value: ' right' },
      { op: 'append_text', path: ['left', 0, 'text'], value: ' again' },
      { op: 'set', path: [], value: payload },
      { op: 'append_text', path: ['rows', 0, 'text'], value: ' replacement' },
      { op: 'remove', path: ['sibling', 'text'] },
    ]));
    expect(result).toEqual({ rows: [{ text: 'original replacement' }, row], sibling: {} });
    expect((result!.rows as unknown[])[1]).toBe(row);
    expect(payload.rows[0]).toBe(row);
    expect(payload.sibling.text).toBe('payload');
    expect(source.left[0]).toBe(row);
    expect(source.right[0]).toBe(row);
  });

  it('never treats a shared source array as an owned draft for a different path', () => {
    const shared = freeze([{ text: 'unchanged' }]);
    const source = freeze({ left: shared, right: shared });
    expect(applyProjectionPatch(source, patch([
      { op: 'append_text', path: ['left', 0, 'text'], value: ' left' },
      { op: 'append_text', path: ['right', 0, 'text'], value: ' right' },
      { op: 'append_text', path: ['left', 0, 'text'], value: ' twice' },
    ]))).toEqual({ left: [{ text: 'unchanged left twice' }], right: [{ text: 'unchanged right' }] });
    expect(source.left).toBe(source.right);
  });

  it('starts fresh ownership when a previous result becomes the next patch input', () => {
    const source = freeze({ rows: [{ text: 'one' }, { text: 'stable' }] });
    const edits = patch([
      { op: 'append_text', path: ['rows', 0, 'text'], value: '+' },
      { op: 'append_text', path: ['rows', 0, 'text'], value: 'two' },
    ]);
    const first = freeze(applyProjectionPatch(source, edits));
    const second = applyProjectionPatch(first, edits);
    expect(first).toEqual({ rows: [{ text: 'one+two' }, { text: 'stable' }] });
    expect(second).toEqual({ rows: [{ text: 'one+two+two' }, { text: 'stable' }] });
    expect((second!.rows as unknown[])[1]).toBe(source.rows[1]);
  });

  it.each<ProjectionPatchOperation>([
    { op: 'append_text', path: ['rows', 0, 'text'], value: 42 },
    { op: 'set', path: ['rows', 9, 'text'], value: 'invalid' },
    { op: 'truncate', path: ['rows'], length: 9 },
    { op: 'remove', path: [] },
  ])('discards all earlier draft edits if a later operation fails: %j', invalid => {
    const source = freeze({ rows: [{ text: 'before' }, { text: 'stable' }] });
    expect(applyProjectionPatch(source, patch([
      { op: 'append_text', path: ['rows', 0, 'text'], value: ' modified' },
      { op: 'remove', path: ['rows', 1] },
      invalid,
    ]))).toBeNull();
    expect(source.rows).toEqual([{ text: 'before' }, { text: 'stable' }]);
  });
});
