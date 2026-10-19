/** Copy-on-write ID records for immutable normalized browser state.
 * Entry: editEntityRecord (one synchronous writer), entityRecordValues/Entries/Keys.
 * A fixed radix fanout is an algorithmic layout, not a capacity limit. Updates
 * copy hash paths and bounded leaves; key topology is shared until changed.
 * The read-only Proxy preserves existing record reads/reflection/JSON. Bulk
 * readers use the explicit helpers to avoid per-key reflection traps. This is
 * not a wire DTO: serialize with JSON or materialize with object spread before
 * structuredClone/postMessage. Weak backing ownership retains no prior root.
 * Dependencies: none. Values remain caller-owned immutable DTOs.
 */
type IdRecord<T> = Record<string, T>;
interface Leaf<T> { kind: 'leaf'; values: IdRecord<T>; size: number }
interface Branch<T> { kind: 'branch'; children: Array<Node<T> | undefined> }
type Node<T> = Leaf<T> | Branch<T>;
interface Backing<T> {
  root: Branch<T>;
  keys: readonly string[];
  refreshKeys?(): void;
}
export interface EntityRecordDraft<T> {
  readonly record: IdRecord<T>;
  finish(): IdRecord<T>;
}
// Six bits per level; split leaves instead of growing per-event copy cost.
// Exact 32-bit hash collisions retain a collision leaf after the final level.
const BITS = 6, FANOUT = 1 << BITS, LEAF_SIZE = 64;
const backings = new WeakMap<object, Backing<unknown>>();
function keyHash(key: string): number {
  let hash = 2166136261;
  for (let i = 0; i < key.length; i++) hash = Math.imul(hash ^ key.charCodeAt(i), 16777619);
  return (hash ^ (hash >>> 16)) >>> 0;
}
function backingFor<T>(value: IdRecord<T>): Backing<T> | undefined {
  return backings.get(value) as Backing<T> | undefined;
}
function leafAt<T>(backing: Backing<T>, key: string): Leaf<T> | undefined {
  const hash = keyHash(key);
  let branch = backing.root;
  for (let shift = 0; shift < 32; shift += BITS) {
    const child = branch.children[(hash >>> shift) & (FANOUT - 1)];
    if (!child || child.kind === 'leaf') return child;
    branch = child;
  }
  throw new Error('Entity index exceeds hash depth');
}
function valueAt<T>(backing: Backing<T>, key: string): T {
  return leafAt(backing, key)?.values[key] as T;
}
function orderedKeys(keys: Iterable<string>): string[] {
  const integers: string[] = [], strings: string[] = [];
  for (const key of keys) {
    const value = Number(key);
    if (Number.isInteger(value) && value >= 0 && value < 4294967295 && String(value) === key) integers.push(key);
    else strings.push(key);
  }
  integers.sort((a, b) => Number(a) - Number(b));
  return [...integers, ...strings];
}

/** Create a private writable draft; finish revokes writes without freezing DTOs. */
export function editEntityRecord<T>(source: IdRecord<T>): EntityRecordDraft<T> {
  const previous = backingFor(source);
  const backing: Backing<T> = {
    root: { kind: 'branch', children: previous ? previous.root.children.slice() : new Array(FANOUT) },
    keys: previous?.keys ?? [],
  };
  const detached = new WeakSet<object>([backing.root]);
  let mutable = true;
  let keySet: Set<string> | undefined;
  const target = Object.create(null) as IdRecord<T>;
  function copyNode(node: Node<T>): Node<T> {
    if (detached.has(node)) return node;
    const copy: Node<T> = node.kind === 'branch'
      ? { kind: 'branch', children: node.children.slice() }
      : { kind: 'leaf', values: Object.assign(Object.create(null), node.values), size: node.size };
    detached.add(copy);
    return copy;
  }
  function split(leaf: Leaf<T>, shift: number): Branch<T> {
    const branch: Branch<T> = { kind: 'branch', children: new Array(FANOUT) };
    detached.add(branch);
    for (const key of Object.keys(leaf.values)) {
      const slot = (keyHash(key) >>> shift) & (FANOUT - 1);
      let child = branch.children[slot] as Leaf<T> | undefined;
      if (!child) {
        child = { kind: 'leaf', values: Object.create(null) as IdRecord<T>, size: 0 };
        branch.children[slot] = child; detached.add(child);
      }
      child.values[key] = leaf.values[key]; child.size++;
    }
    if (shift + BITS < 32) branch.children.forEach((child, slot) => {
      if (child?.kind === 'leaf' && child.size > LEAF_SIZE) branch.children[slot] = split(child, shift + BITS);
    });
    return branch;
  }
  function write(key: string, value: T, remove = false): void {
    if (!mutable) throw new TypeError('Published entity records are immutable');
    const hash = keyHash(key), ancestors: Array<{ branch: Branch<T>; slot: number }> = [];
    let branch = backing.root;
    for (let shift = 0; shift < 32; shift += BITS) {
      const slot = (hash >>> shift) & (FANOUT - 1);
      let child = branch.children[slot];
      if (!child) {
        if (remove) return;
        child = { kind: 'leaf', values: Object.create(null) as IdRecord<T>, size: 0 };
        detached.add(child);
      } else child = copyNode(child);
      branch.children[slot] = child;
      ancestors.push({ branch, slot });
      if (child.kind === 'branch') { branch = child; continue; }
      const exists = Object.hasOwn(child.values, key);
      if (remove) {
        if (!exists) return;
        (keySet ??= new Set(backing.keys)).delete(key);
        delete child.values[key]; child.size--;
        // Empty paths do not accumulate across create/delete cycles.
        if (!child.size) for (let i = ancestors.length - 1; i >= 0; i--) {
          const parent = ancestors[i]; delete parent.branch.children[parent.slot];
          if (parent.branch.children.some(Boolean)) break;
        }
      } else {
        if (!exists) { (keySet ??= new Set(backing.keys)).add(key); child.size++; }
        child.values[key] = value;
        if (child.size > LEAF_SIZE && shift + BITS < 32) branch.children[slot] = split(child, shift + BITS);
      }
      return;
    }
    throw new Error('Entity index exceeds hash depth');
  }
  if (!previous) for (const key of Object.keys(source)) write(key, source[key]);
  function keys(): readonly string[] {
    if (keySet) { backing.keys = orderedKeys(keySet); keySet = undefined; }
    return backing.keys;
  }
  const record = new Proxy(target, {
    get(_target, key) {
      return typeof key === 'string' ? valueAt(backing, key) : undefined;
    },
    has(_target, key) {
      return typeof key === 'string' && Object.hasOwn(leafAt(backing, key)?.values ?? {}, key);
    },
    ownKeys() { return [...keys()]; },
    getOwnPropertyDescriptor(_target, key) {
      if (!Reflect.isExtensible(target)) return Reflect.getOwnPropertyDescriptor(target, key);
      if (typeof key !== 'string' || !Object.hasOwn(leafAt(backing, key)?.values ?? {}, key)) return undefined;
      return { value: valueAt(backing, key), enumerable: true, configurable: true, writable: true };
    },
    set(_target, key, value: T) {
      if (typeof key !== 'string') throw new TypeError('Entity IDs must be strings');
      write(key, value);
      return true;
    },
    deleteProperty(_target, key) {
      if (typeof key !== 'string') throw new TypeError('Entity IDs must be strings');
      write(key, undefined as T, true);
      return true;
    },
    // Freeze/seal are diagnostic operations: materialize descriptors once so
    // ECMAScript's non-extensible Proxy invariants remain valid.
    preventExtensions() {
      if (!Reflect.isExtensible(target)) return true;
      if (mutable) throw new TypeError('Finish an entity record before freezing it');
      for (const key of keys()) Object.defineProperty(target, key, {
        value: valueAt(backing, key), enumerable: true, configurable: true, writable: true,
      });
      return Reflect.preventExtensions(target);
    },
    defineProperty(_target, key, descriptor) {
      if (Reflect.isExtensible(target)) throw new TypeError('Entity records accept only ID assignments');
      const current = Reflect.getOwnPropertyDescriptor(target, key);
      if (!current || 'get' in descriptor || 'set' in descriptor
          || ('value' in descriptor && descriptor.value !== current.value)
          || descriptor.configurable === true || descriptor.writable === true
          || descriptor.enumerable === false) return false;
      return Reflect.defineProperty(target, key, descriptor);
    },
    setPrototypeOf() { return false; },
  });
  backing.refreshKeys = () => { keys(); };
  backings.set(record, backing);
  return { record, finish() { keys(); mutable = false; delete backing.refreshKeys; return record; } };
}

export function entityRecordKeys<T>(record: IdRecord<T>): string[] {
  const backing = backingFor(record);
  backing?.refreshKeys?.();
  return backing ? [...backing.keys] : Object.keys(record);
}
export function entityRecordValues<T>(record: IdRecord<T>): T[] {
  const backing = backingFor(record);
  backing?.refreshKeys?.();
  return backing ? backing.keys.map(key => valueAt(backing, key)) : Object.values(record);
}
export function entityRecordEntries<T>(record: IdRecord<T>): Array<[string, T]> {
  const backing = backingFor(record);
  backing?.refreshKeys?.();
  return backing ? backing.keys.map(key => [key, valueAt(backing, key)]) : Object.entries(record);
}
