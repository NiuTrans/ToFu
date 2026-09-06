/**
 * Fail-closed ownership policy for server-push frames.
 *
 * Responsibility: compare an explicitly supplied authenticated local owner
 * with a frame owner, and narrow unknown push payloads to the declared
 * contract (`ContractedPushFrame`, generated from the PushFrameSpec registry
 * in lib/agent_core/events.py). Entry points: `frameBelongsToOwner`,
 * `isContractedPushFrame`, `narrowConvCatalogFrame`,
 * `narrowFoldersChangedFrame`. Dependencies: the generated event contract
 * only; identity lookup and transport wiring stay at the composition
 * boundary.
 */

import {
  CONTRACTED_PUSH_FRAME_TYPES,
  type ContractedPushFrame,
  type ConvChangedPushFrame,
  type ConvDeletedPushFrame,
  type FoldersChangedPushFrame,
} from '../api/event-contract.generated';

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

const PUSH_FRAME_TYPE_SET: ReadonlySet<string> = new Set(
  CONTRACTED_PUSH_FRAME_TYPES,
);

/**
 * Tag-narrow an unknown push payload to a declared frame.  Field shapes are
 * the backend's construction-gate guarantee; consumers that cannot trust a
 * best-effort frame still normalize the fields they act on.
 */
export function isContractedPushFrame(
  frame: unknown,
): frame is ContractedPushFrame {
  const tag = (frame as { type?: unknown } | null)?.type;
  return typeof tag === 'string' && PUSH_FRAME_TYPE_SET.has(tag);
}

/** Narrow to a conv_changed/conv_deleted wake hint carrying a usable id. */
export function narrowConvCatalogFrame(
  frame: unknown,
): ConvChangedPushFrame | ConvDeletedPushFrame | null {
  if (!isContractedPushFrame(frame)) return null;
  if (frame.type !== 'conv_changed' && frame.type !== 'conv_deleted') {
    return null;
  }
  if (typeof frame.convId !== 'string' || frame.convId.length === 0) {
    return null;
  }
  return frame;
}

/** Narrow to a folders_changed wake hint. */
export function narrowFoldersChangedFrame(
  frame: unknown,
): FoldersChangedPushFrame | null {
  if (!isContractedPushFrame(frame) || frame.type !== 'folders_changed') {
    return null;
  }
  return frame;
}
