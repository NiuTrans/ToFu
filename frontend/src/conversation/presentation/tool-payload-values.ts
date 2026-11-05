/**
 * Shared untrusted tool-payload reads for presentation and card actions.
 * Entry points: record, field, scalarText and nonNegativeInteger.
 * No DOM, coercion hooks, input mutation or application state dependencies.
 * Family-specific text coercion and preview retention remain with the caller.
 */
export type UnknownRecord = Readonly<Record<string, unknown>>;

export type BoundedText = Readonly<{
  value: string;
  truncated: boolean;
}>;

const EMPTY_RECORD: UnknownRecord = Object.freeze({});

export function record(value: unknown): UnknownRecord {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as UnknownRecord
    : EMPTY_RECORD;
}

/** A throwing getter is unavailable metadata; inherited fields remain valid. */
export function field(value: unknown, name: string): unknown {
  try {
    return record(value)[name];
  } catch {
    return undefined;
  }
}

export function scalarText(value: unknown): string {
  if (typeof value === 'string') return value;
  if (
    typeof value === 'number'
    || typeof value === 'boolean'
    || typeof value === 'bigint'
  ) return String(value);
  return '';
}

export function nonNegativeInteger(value: unknown, fallback: number): number {
  return typeof value === 'number'
    && Number.isSafeInteger(value)
    && value >= 0
    ? value
    : fallback;
}
