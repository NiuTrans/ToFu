/**
 * Pure serving-route projection for one assistant Turn.
 *
 * The generated `lastRoundUsage` contract is authoritative for current data.
 * Bounded `apiRounds` inspection exists only to render projections written
 * before that contract carried resolved route fields. Auxiliary accounting
 * rows can never become the displayed response model.
 */
import type {
  TurnLastRoundUsage,
} from '../../api/conversation-sync.generated';

type UnknownRecord = Readonly<Record<string, unknown>>;

const MAX_LEGACY_API_ROUNDS_TO_SCAN = 512;
const AGENT_API_ROUND_KINDS = new Set(['agent', 'main']);

export interface TurnServingRoute {
  /** Dispatcher-resolved upstream model, falling back to the logical model. */
  model: string;
  /** Logical orchestration/fallback model before alias-to-slot resolution. */
  logicalModel: string;
  providerId: string;
  keyName: string;
  keyTail: string;
  source: 'route-snapshot' | 'last-round' | 'legacy-agent-round' | 'projection';
}

function record(value: unknown): UnknownRecord | undefined {
  return value != null && typeof value === 'object' && !Array.isArray(value)
    ? value as UnknownRecord : undefined;
}

function text(value: unknown): string {
  return typeof value === 'string' ? value : '';
}

function dispatchFromUsage(value: unknown): UnknownRecord | undefined {
  return record(record(value)?._dispatch);
}

/** True only for a response-authoring agent row, including legacy untyped rows. */
export function isAgentApiRound(value: unknown): boolean {
  const round = record(value);
  if (!round || round.responseAuthoring === false) return false;
  const kind = text(round.kind).trim().toLowerCase();
  if (kind && !AGENT_API_ROUND_KINDS.has(kind)) return false;
  // Historical billed retry rows predate `responseAuthoring`; their stable
  // diagnostic tag is the only available distinction.
  return !/(?:^|[-_])(?:discarded|compaction)(?:$|[-_])/i.test(
    text(round.tag),
  );
}

/** Latest response-authoring row from a bounded historical breakdown. */
export function latestAgentApiRound(
  value: unknown,
): UnknownRecord | undefined {
  if (!Array.isArray(value)) return undefined;
  const start = Math.max(0, value.length - MAX_LEGACY_API_ROUNDS_TO_SCAN);
  for (let index = value.length - 1; index >= start; index -= 1) {
    if (!isAgentApiRound(value[index])) continue;
    return record(value[index]);
  }
  return undefined;
}

/** Latest response-authoring usage for legacy prompt-size consumers. */
export function latestAgentApiRoundUsage(
  value: unknown,
): UnknownRecord | undefined {
  return record(latestAgentApiRound(value)?.usage);
}

/** Exact agent-round divisor, or zero when the historical input exceeds budget. */
export function agentApiRoundCount(value: unknown): number {
  if (!Array.isArray(value)
      || value.length > MAX_LEGACY_API_ROUNDS_TO_SCAN) return 0;
  return value.reduce(
    (count, round) => count + (isAgentApiRound(round) ? 1 : 0), 0,
  );
}

/**
 * Resolve the route that authored the assistant response.
 *
 * Current projections read the explicit `lastRoundUsage` route. Historical
 * projections may fill missing route pieces from the latest bounded agent
 * API row, while compaction and discarded billing rows are excluded.
 */
export function resolveTurnServingRoute(value: unknown): TurnServingRoute {
  const turn = record(value) ?? {};
  const snapshot = record(turn.routeSnapshot);
  const selectedModel = record(snapshot?.selected_model);
  const actualModel = record(snapshot?.actual_model);
  const snapshotCredential = record(snapshot?.credential);
  const lastRound = record(turn.lastRoundUsage) as
    | (UnknownRecord & Partial<TurnLastRoundUsage>)
    | undefined;
  const legacyRound = latestAgentApiRound(turn.apiRounds);
  const legacyUsage = record(legacyRound?.usage);
  const legacyDispatch = dispatchFromUsage(legacyUsage);
  // Very old message documents had only aggregate usage. It is safe as a
  // final compatibility source only when no classified API round exists.
  const aggregateDispatch = legacyRound
    ? undefined : dispatchFromUsage(turn.usage);
  const dispatch = legacyDispatch ?? aggregateDispatch ?? {};

  const logicalModel = text(selectedModel?.model_id)
    || text(lastRound?.model)
    || text(legacyRound?.model)
    || text(turn.fallbackModel)
    || text(turn.model)
    || text(turn.preset);
  const model = text(snapshot?.wire_model_id)
    || text(actualModel?.model_id)
    || text(lastRound?.resolvedModel)
    || text(dispatch.model)
    || logicalModel;
  const providerId = text(snapshot?.provider_id)
    || text(lastRound?.providerId)
    || text(dispatch.provider_id)
    || text(dispatch.providerId)
    || text(turn.providerId)
    || text(turn.provider_id);
  const keyName = text(snapshotCredential?.credential_id)
    || text(lastRound?.keyName) || text(dispatch.key);
  const keyTail = text(lastRound?.keyTail) || text(dispatch.key_tail);
  const hasRouteSnapshot = Boolean(snapshot);
  const hasLastRound = Boolean(lastRound);
  const hasLegacyAgentRound = Boolean(legacyRound || aggregateDispatch);

  return {
    model,
    logicalModel,
    providerId,
    keyName,
    keyTail,
    source: hasRouteSnapshot
      ? 'route-snapshot'
      : hasLastRound
      ? 'last-round'
      : hasLegacyAgentRound ? 'legacy-agent-round' : 'projection',
  };
}
