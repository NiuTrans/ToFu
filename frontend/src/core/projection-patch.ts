/**
 * Apply ordered projection-patch operations without mutating prior reducer
 * state or borrowed patch values. Entry: applyProjectionPatch; dependencies:
 * standard JavaScript only. Producer: lib/turn_projection_patch.py.
 */

type UnknownRecord = Record<string, unknown>;
type PathPart = string | number;

export interface ProjectionPatchOperation extends UnknownRecord {
  op: 'set' | 'remove' | 'append' | 'truncate' | 'append_text';
  path: PathPart[];
  value?: unknown;
  length?: number;
}

export interface ProjectionPatch extends UnknownRecord {
  version: number;
  baseRevision: number;
  targetRevision: number;
  operations: ProjectionPatchOperation[];
}

function record(value: unknown): UnknownRecord | null {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as UnknownRecord : null;
}

/** Only arrays created by this invocation may be edited again in place.
 * Keep the latest draft directly: sibling fields usually share one history.
 * Additional histories use an exact identity set, discarded with this patch. */
type PatchArrayCopies = { latest?: unknown[]; earlier?: WeakSet<unknown[]> };

function copyArrayForPatch(value: unknown[], copies: PatchArrayCopies | undefined): unknown[] {
  if (!copies) return value.slice();
  if (value === copies.latest || copies.earlier?.has(value)) return value;
  const next = value.slice();
  if (copies.latest) (copies.earlier ??= new WeakSet<unknown[]>()).add(copies.latest);
  copies.latest = next;
  return next;
}

function updateAtPath(
  value: unknown,
  path: readonly PathPart[],
  update: (current: unknown) => unknown,
  copies: PatchArrayCopies | undefined,
  depth = 0,
): unknown {
  if (depth >= path.length) return update(value);
  const part = path[depth];
  if (Array.isArray(value)) {
    if (typeof part !== 'number' || part < 0 || part >= value.length) {
      throw new Error('Projection patch array path is out of bounds.');
    }
    const next = copyArrayForPatch(value, copies);
    next[part] = updateAtPath(next[part], path, update, copies, depth + 1);
    return next;
  }
  const source = record(value);
  if (!source || typeof part !== 'string') {
    throw new Error('Projection patch object path is invalid.');
  }
  const next: UnknownRecord = { ...source };
  next[part] = updateAtPath(next[part], path, update, copies, depth + 1);
  return next;
}

function removeAtPath(value: unknown, path: readonly PathPart[], copies: PatchArrayCopies | undefined): unknown {
  if (!path.length) throw new Error('Projection patch cannot remove its root.');
  const parent = path.slice(0, -1);
  const leaf = path[path.length - 1];
  return updateAtPath(value, parent, (container) => {
    if (Array.isArray(container)) {
      if (typeof leaf !== 'number' || leaf < 0 || leaf >= container.length) {
        throw new Error('Projection patch array removal is out of bounds.');
      }
      const next = copyArrayForPatch(container, copies);
      next.splice(leaf, 1);
      return next;
    }
    const source = record(container);
    if (!source || typeof leaf !== 'string') {
      throw new Error('Projection patch object removal is invalid.');
    }
    const next: UnknownRecord = { ...source };
    delete next[leaf];
    return next;
  }, copies);
}

/** Return null for malformed/unsupported patches so callers resync. */
export function applyProjectionPatch(
  projection: unknown,
  rawPatch: unknown,
): UnknownRecord | null {
  const patch = record(rawPatch);
  if (Number(patch?.version || 0) !== 1 || !Array.isArray(patch?.operations)) {
    return null;
  }
  // A single operation cannot reuse an earlier operation's history copy.
  const copies = patch.operations.length > 1 ? {} : undefined;
  let next: unknown = record(projection) ?? {};
  try {
    for (const rawOperation of patch.operations) {
      const operation = record(rawOperation) as ProjectionPatchOperation | null;
      if (!operation || !Array.isArray(operation.path)) return null;
      const path = operation.path;
      if (operation.op === 'set') {
        next = updateAtPath(next, path, () => operation.value, copies);
      } else if (operation.op === 'remove') {
        next = removeAtPath(next, path, copies);
      } else if (operation.op === 'append_text') {
        next = updateAtPath(next, path, (current) => {
          if (typeof current !== 'string' || typeof operation.value !== 'string') {
            throw new Error('Projection text append has incompatible values.');
          }
          return current + operation.value;
        }, copies);
      } else if (operation.op === 'append') {
        next = updateAtPath(next, path, (current) => {
          if (!Array.isArray(current) || !Array.isArray(operation.value)) {
            throw new Error('Projection list append has incompatible values.');
          }
          return current.concat(operation.value);
        }, copies);
      } else if (operation.op === 'truncate') {
        next = updateAtPath(next, path, (current) => {
          const length = Number(operation.length);
          if (!Array.isArray(current) || !Number.isInteger(length)
              || length < 0 || length > current.length) {
            throw new Error('Projection list truncation is invalid.');
          }
          return current.slice(0, length);
        }, copies);
      } else {
        return null;
      }
    }
  } catch {
    return null;
  }
  return record(next);
}
