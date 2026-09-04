/**
 * Responsibility: retain an insertion-ordered, size-bounded LRU map.
 * Entry point: BoundedMap. Dependencies: the JavaScript Map primitive only.
 */

export class BoundedMap<Key, Value> implements Iterable<[Key, Value]> {
  readonly capacity: number;
  private readonly values = new Map<Key, Value>();

  constructor(capacity: number) {
    if (!Number.isSafeInteger(capacity) || capacity < 1) {
      throw new RangeError('BoundedMap capacity must be a positive safe integer');
    }
    this.capacity = capacity;
  }

  get size(): number {
    return this.values.size;
  }

  get(key: Key): Value | undefined {
    if (!this.values.has(key)) return undefined;
    const value = this.values.get(key) as Value;
    this.values.delete(key);
    this.values.set(key, value);
    return value;
  }

  set(key: Key, value: Value): this {
    // Map.set does not refresh insertion order for an existing key. Delete
    // first so both reads and writes express recency through one invariant.
    this.values.delete(key);
    this.values.set(key, value);
    while (this.values.size > this.capacity) {
      const oldest = this.values.keys().next();
      if (oldest.done) break;
      this.values.delete(oldest.value);
    }
    return this;
  }

  delete(key: Key): boolean {
    return this.values.delete(key);
  }

  clear(): void {
    this.values.clear();
  }

  entries(): IterableIterator<[Key, Value]> {
    return this.values.entries();
  }

  [Symbol.iterator](): IterableIterator<[Key, Value]> {
    return this.entries();
  }
}
