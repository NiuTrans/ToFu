/**
 * Bounded projection text and record helpers plus stable block identity claims. Does
 * not
 * construct a second transcript or retain state.
 * Entry: asArray, claimedBlockId, recordValue, boundedActivityText.
 * Dependencies: types. */
import {
  type UnknownRecord,
  type BlockIdentitySource,
  type ConversationViewModelDiagnostics,
} from './types';

export function asArray<Item>(value: ReadonlyArray<Item> | undefined): ReadonlyArray<Item> {
  return value ?? [];
}

export function claimedBlockId(
  turnId: string,
  proposed: string,
  identitySource: BlockIdentitySource,
  claims: Map<string, number>,
  diagnostics: ConversationViewModelDiagnostics,
): string {
  const count = (claims.get(proposed) ?? 0) + 1;
  claims.set(proposed, count);
  const blockId = count === 1 ? proposed : `${proposed}~${count}`;
  if (identitySource === 'compatibility') {
    diagnostics.onCompatibilityIdentity?.(turnId, blockId);
  }
  return blockId;
}

export function recordValue(value: unknown): UnknownRecord | null {
  if (typeof value === 'string') {
    try {
      const parsed: unknown = JSON.parse(value);
      return parsed && typeof parsed === 'object' && !Array.isArray(parsed)
        ? parsed as UnknownRecord : null;
    } catch {
      return null;
    }
  }
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as UnknownRecord : null;
}

export function boundedActivityText(value: unknown, maxChars: number): string {
  return typeof value === 'string'
    ? value.replace(/\s+/g, ' ').trim().slice(0, maxChars) : '';
}
