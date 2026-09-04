/**
 * Fail-closed ownership policy for server-push frames.
 *
 * Responsibility: compare an explicitly supplied authenticated local owner
 * with a frame owner. Entry point: `frameBelongsToOwner`. Dependencies: none;
 * identity lookup and transport wiring stay at the composition boundary.
 */

function ownerIdentityKey(value: unknown): string | null {
  if (typeof value === 'string') return value.length > 0 ? value : null;
  if (typeof value === 'number' && Number.isFinite(value)) return String(value);
  if (typeof value === 'bigint') return String(value);
  return null;
}

/** Accept only an explicitly scoped frame for the authenticated owner. */
export function frameBelongsToOwner(
  localOwnerId: unknown,
  frameOwnerId: unknown,
): boolean {
  const localKey = ownerIdentityKey(localOwnerId);
  const frameKey = ownerIdentityKey(frameOwnerId);
  return localKey !== null && frameKey !== null && localKey === frameKey;
}
