import { describe, expect, it } from 'vitest';

import { BoundedMap } from './bounded-map';

describe('BoundedMap', () => {
  it('rejects non-positive or fractional capacities', () => {
    expect(() => new BoundedMap(0)).toThrow(RangeError);
    expect(() => new BoundedMap(1.5)).toThrow(RangeError);
    expect(() => new BoundedMap(1)).not.toThrow();
  });

  it('evicts the oldest entry beyond capacity', () => {
    const map = new BoundedMap<string, number>(2);
    map.set('a', 1).set('b', 2).set('c', 3);
    expect(map.size).toBe(2);
    expect(map.get('a')).toBeUndefined();
    expect(map.get('b')).toBe(2);
    expect(map.get('c')).toBe(3);
  });

  it('a read refreshes recency and spares the entry from eviction', () => {
    const map = new BoundedMap<string, number>(2);
    map.set('a', 1).set('b', 2);
    expect(map.get('a')).toBe(1);
    map.set('c', 3);
    expect(map.get('a')).toBe(1);
    expect(map.get('b')).toBeUndefined();
  });

  it('rewriting an existing key refreshes recency without growing', () => {
    const map = new BoundedMap<string, number>(2);
    map.set('a', 1).set('b', 2);
    map.set('a', 10);
    map.set('c', 3);
    expect(map.size).toBe(2);
    expect(map.get('a')).toBe(10);
    expect(map.get('b')).toBeUndefined();
  });
});
