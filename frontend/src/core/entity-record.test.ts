/** Persistent index contract: record compatibility, immutable forks, topology
 * ordering, reflection and revoked writers; no wall-clock assertions. */
import { describe, expect, it } from 'vitest';
import { editEntityRecord, entityRecordEntries, entityRecordKeys, entityRecordValues } from './entity-record';

function verify(record: Record<string, unknown>, expected: Record<string, unknown>): void {
  expect(Object.keys(record)).toEqual(Object.keys(expected));
  expect(Object.values(record)).toEqual(Object.values(expected));
  expect(Object.entries(record)).toEqual(Object.entries(expected));
  expect(entityRecordKeys(record)).toEqual(Object.keys(expected));
  expect(entityRecordValues(record)).toEqual(Object.values(expected));
  expect(entityRecordEntries(record)).toEqual(Object.entries(expected));
  expect(JSON.stringify(record)).toBe(JSON.stringify(expected));
  expect({ ...record }).toEqual({ ...expected });
  for (const key of Object.keys(expected)) {
    expect(key in record).toBe(true);
    expect(Object.hasOwn(record, key)).toBe(true);
    expect(record[key]).toBe(expected[key]);
  }
}

describe('immutable entity records', () => {
  it('agrees with plain records across deterministic insert/update/delete sequences and old snapshots', () => {
    let expected = Object.create(null) as Record<string, unknown>;
    let record = editEntityRecord(expected).finish();
    const names = ['__proto__', 'constructor', 'toString', '10', '1', '0', '01', '4294967295', '',
      ...Array.from({ length: 96 }, (_, i) => `turn-${i}`)];
    let seed = 719;
    for (let batch = 0; batch < 120; batch++) {
      const previous = record, before = JSON.stringify(previous), draft = editEntityRecord(record);
      expected = Object.assign(Object.create(null), expected);
      for (let i = 0; i < 8; i++) {
        seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0;
        const key = names[seed % names.length];
        if (seed % 5 === 0) { delete draft.record[key]; delete expected[key]; }
        else draft.record[key] = expected[key] = seed % 11 === 0 ? undefined : { value: seed };
        verify(draft.record, expected); // Bulk reads must include uncommitted topology too.
      }
      record = draft.finish(); verify(record, expected);
      expect(JSON.stringify(previous)).toBe(before);
      expect(() => { draft.record.illegal = 1; }).toThrow(TypeError);
    }
  });

  it('forks and freezes independently, including collisions and repeated freeze', () => {
    const source = Object.fromEntries(Array.from({ length: 1024 }, (_, i) => [`t${i}`, { value: i }]));
    const base = editEntityRecord(source).finish();
    Object.values(base).forEach(Object.freeze); Object.freeze(base); Object.freeze(base);
    const left = editEntityRecord(base), right = editEntityRecord(base);
    left.record.t0 = { value: -1 }; delete left.record.t1;
    right.record.t1 = { value: -2 }; right.record.new = { value: 1024 };
    const a = left.finish(), b = right.finish();
    expect(a.t0.value).toBe(-1); expect(b.t0.value).toBe(0);
    expect(a.t1).toBeUndefined(); expect(b.t1.value).toBe(-2); expect(base.t1.value).toBe(1);
    expect(a.t99).toBe(base.t99); expect(b.t99).toBe(base.t99);
    Object.seal(a); Object.freeze(a); expect(Object.isFrozen(a)).toBe(true);
    expect(() => { delete a.t0; }).toThrow(TypeError);
    expect(() => Object.defineProperty(a, 't0', { value: null })).toThrow(TypeError);
    expect(() => Object.setPrototypeOf(a, { injected: true })).toThrow(TypeError);
    expect('injected' in a).toBe(false);
    verify(base, source);
  });

  it('materializes a wire/clone boundary explicitly and does not expose mutable key arrays', () => {
    const record = editEntityRecord({ a: { value: 1 } }).finish();
    const keys = entityRecordKeys(record); keys.push('fake');
    expect(entityRecordKeys(record)).toEqual(['a']);
    const clone = structuredClone({ ...record }); clone.a.value = 9;
    expect(record.a.value).toBe(1);
    expect(entityRecordValues({ a: 1, b: 2 })).toEqual([1, 2]);
    expect(() => Object.defineProperty(record, 'b', { value: 2 })).toThrow(TypeError);
  });
});

it('splits large indexes, keeps earlier roots isolated and removes empty paths', () => {
  const plain = Object.fromEntries(Array.from({ length: 18000 }, (_, i) => [`long-turn-${i}`, i]));
  const base = editEntityRecord(plain).finish();
  const delta = editEntityRecord(base);
  for (let i = 0; i < 18000; i += 3) delta.record[`long-turn-${i}`] = -i;
  const updated = delta.finish();
  for (let i = 0; i < 18000; i++) {
    expect(base[`long-turn-${i}`]).toBe(i);
    expect(updated[`long-turn-${i}`]).toBe(i % 3 ? i : -i);
  }
  const removed = editEntityRecord(updated);
  for (let i = 17999; i >= 0; i--) delete removed.record[`long-turn-${i}`];
  removed.record.after = 42;
  verify(removed.finish(), { after: 42 });
  expect(entityRecordKeys(base)).toHaveLength(18000);
});
