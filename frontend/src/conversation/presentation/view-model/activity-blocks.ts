/**
 * Visible activity and route-snapshot evidence selection. Normalizes legacy validation
 * records without inventing chronology.
 * Entry: visibleActivityEntries, routeSnapshotActivityEntries.
 * Dependencies: conversation-sync.generated, activity-anchor, types, projection-values. */
import {
  type TurnActivityEntry,
  type TurnActivityTimeline,
  type RouteSnapshot,
  type TurnToolRound,
} from '../../../api/conversation-sync.generated';
import {
  activityEntryIsVisible,
  resolveActivityOccurrence,
  type AnchoredActivityEntry,
} from '../activity-anchor';
import {
  type ToolBlockViewModel,
  type ConversationBlockViewModel,
} from './types';
import {
  recordValue,
  boundedActivityText,
} from './projection-values';

function legacyGatewayValidationEntries(
  entry: TurnActivityEntry,
  toolRounds: ReadonlyArray<TurnToolRound>,
): ReadonlyArray<AnchoredActivityEntry> {
  // Gateways may have no content segment. Resolve their retained round with
  // the same occurrence rules, never the first recycled provider call ID.
  const candidates: ToolBlockViewModel[] = toolRounds.map((round, index) => ({
    kind: 'tool', blockId: `gateway-candidate:${index}`, identitySource: 'contract',
    source: round, round, toolCallId: round.toolCallId ?? '',
    name: round.toolName ?? '', input: round.toolArgs, result: round.result ?? {},
  }));
  const index = resolveActivityOccurrence(candidates, entry);
  const round = index == null ? undefined : toolRounds[index];
  if (round?.toolName !== 'execute_tools') return [entry];
  const envelope = recordValue(round?.toolContent);
  const items = Array.isArray(envelope?.items) ? envelope.items : [];
  const payload = recordValue(items[0]) ?? envelope;
  const errors = Array.isArray(payload?.errors) ? payload.errors.slice(0, 8) : [];
  const children = errors.flatMap((rawError, index) => {
    const error = recordValue(rawError);
    if (!error) return [];
    const toolName = boundedActivityText(
      error.name ?? error.attempted, 160,
    ) || 'tool request';
    const message = boundedActivityText(error.message, 260);
    const retryHint = boundedActivityText(
      error.retry_hint ?? error.next_action, 220,
    );
    const detail = boundedActivityText(
      [message, retryHint ? `Next: ${retryHint}` : '']
        .filter(Boolean).join(' '),
      400,
    );
    return [{
      ...entry,
      id: `${entry.id}:validation-${index}`,
      spanId: `${entry.spanId}:validation-${index}`,
      /* Synthesized rows are absent from the raw timeline, so the chronology
       * windows map has no key for them: inherit the parent gateway row's
       * window instead of degrading to an unbounded round scan. */
      anchorEntryId: entry.id,
      kind: 'tool',
      status: 'skipped',
      severity: 'warning',
      summary: `${toolName} not run: argument validation failed`,
      summaryKey: 'activity.tool.validationRejected',
      summaryArgs: { tool: toolName },
      ...(detail ? { detail } : {}),
      reasonCode: boundedActivityText(error.code, 160) || 'gateway_validation',
      toolName,
    } satisfies AnchoredActivityEntry];
  });
  // Missing/ambiguous legacy details must not erase the only failure record.
  return children.length ? children : [entry];
}

function toolRoundHasRejection(round: TurnToolRound): boolean {
  const record = recordValue(round) ?? {};
  if (record.rejection || record._rejected) return true;
  const result = recordValue(record.result);
  if (result && (result.rejection || result.rejected || result._rejected)) {
    return true;
  }
  const results = Array.isArray(record.results) ? record.results : [];
  const first = recordValue(results[0]);
  return Boolean(
    first && (first.rejection || first.rejected || first._rejected),
  );
}

export function visibleActivityEntries(
  timeline: TurnActivityTimeline | undefined,
  toolRounds: ReadonlyArray<TurnToolRound>,
  renderedBlocks: ReadonlyArray<ConversationBlockViewModel>,
): ReadonlyArray<AnchoredActivityEntry> {
  /* Routine info facts are already owned by inline tool blocks and live
   * status. A settled compaction receipt is the deliberate exception: it is a
   * durable context boundary with accounting + archive identity, not another
   * progress beat. Protocol adapters are excluded as a cold-replay defense. */
  /* A pre-execution block is already owned by its tool row (badge + full
   * reason inline); the timeline's blocked/unavailable row repeated the same
   * text right under it. Drop the duplicate only when the matching round
   * actually carries the rejection — a trimmed/absent round keeps the row as
   * the last place the block is visible. */
  // Resolve against actual content blocks, not a bag of retained raw IDs.
  // A trimmed occurrence may have the same call ID as a different attempt.
  const renderedRound = (entry: TurnActivityEntry): TurnToolRound | undefined => {
    const index = resolveActivityOccurrence(renderedBlocks, entry);
    const block = index == null ? undefined : renderedBlocks[index];
    if (block?.kind !== 'tool' || block.toolCallId !== entry.toolCallId
        || (entry.toolName && block.name !== entry.toolName)) return undefined;
    const round = block.round;
    // Anchoring trusts the segment, but an old rich-round pairing is not
    // proof that this rendered occurrence actually owns the diagnostic.
    if (!round || (entry.taskId && round.taskId && entry.taskId !== round.taskId)
        || (entry.attemptId && round.attemptId && entry.attemptId !== round.attemptId)
        || (entry.anchorToolRoundNum != null && round.roundNum !== entry.anchorToolRoundNum)) {
      return undefined;
    }
    return round;
  };
  /* A failed round likewise owns its terminal error inline (badge + the full
   * tool content), while the timeline's `failed` row repeats a log-policy-
   * bounded excerpt beneath it. Drop the duplicate only when the matching
   * round actually renders the error lane — a trimmed/absent round keeps the
   * row as the last visible record. execute_tools rows are exempt: they
   * replay as actionable child-validation rows below. */

  return (timeline?.entries ?? []).flatMap((entry) => {
    if (!activityEntryIsVisible(entry)) return [];

    const owner = renderedRound(entry);
    if ((entry.summaryKey === 'activity.tool.blocked'
        || entry.summaryKey === 'activity.tool.unavailable')
        && owner && toolRoundHasRejection(owner)) {
      return [];
    }
    if (entry.summaryKey === 'activity.tool.failed'
        && entry.toolName !== 'execute_tools'
        && owner?.status === 'error') {
      return [];
    }
    if (entry.toolName !== 'execute_tools') return [entry];
    return legacyGatewayValidationEntries(entry, toolRounds);
  });
}

/** Project v2 failover decisions into the same chronological diagnostic lane
 * as retries and model switches. The snapshot remains the durable authority;
 * these rows are presentation-only and contain no credential secret. */
export function routeSnapshotActivityEntries(
  snapshot: RouteSnapshot | undefined,
): ReadonlyArray<TurnActivityEntry> {
  if (!snapshot) return [];
  const selectedModel = snapshot.selected_model?.model_id
    ?? snapshot.provider_scoped_selection?.offering_id ?? '';
  const actualModel = snapshot.actual_model?.model_id
    ?? snapshot.offering_id ?? snapshot.wire_model_id;
  return snapshot.transitions.flatMap((transition, index) => {
    if (transition.kind === 'initial') return [];
    const fromProvider = transition.from?.provider_id ?? snapshot.preferred_provider_id;
    const from = transition.from;
    const isModelFallback = transition.kind === 'model_fallback';
    const keyHint = snapshot.credential?.key_hint;
    /* Map the bounded transition cause to a readable WHY. ``cause`` is an
     * exception class name when the previous slot failed, or a scheduler
     * reason (``route_rescore``) when nothing failed. Older snapshots have no
     * cause — a same-provider deployment switch with no failure is almost
     * always a scheduler re-pick, so default to that phrasing rather than a
     * vague "something changed". */
    const cause = typeof transition.cause === 'string' ? transition.cause : '';
    const isRescore = !cause || cause === 'route_rescore';
    const reasonKey = isRescore
      ? 'activity.route.reason.route_rescore'
      : 'activity.route.reason.failure';
    const reasonArgs = isRescore ? undefined : { kind: cause };
    /* The summary states the human outcome (what keeps serving the request).
     * Offering/connection/deployment are registry internals with no user-
     * visible surface, so the detail line keeps only facts the user can
     * verify (model, provider, the masked API key also shown in Settings →
     * Providers) plus the readable WHY. The transition reason enum and the
     * internal off_/conn_/dep_ IDs stay out of the rendered facts. */
    const summaryKey = isModelFallback
      ? 'activity.route.switchedModel'
      : from && from.provider_id === transition.to.provider_id
        ? 'activity.route.switchedWithinProvider'
        : 'activity.route.switchedProvider';
    const summary = isModelFallback
      ? `Model switched mid-turn — now served by ${actualModel}`
      : from && from.provider_id === transition.to.provider_id
        ? `Connection switched within ${transition.to.provider_id} — same model keeps running`
        : `Provider switched ${fromProvider || '?'} → ${transition.to.provider_id} — request continues`;
    const reasonFallback = isRescore
      ? 'The scheduler re-picked the route (the previous connection did not fail; normal load/capacity routing)'
      : `The previous connection failed (${cause}); switched to a healthy one automatically`;
    const detail = `Model ${actualModel || '—'}`
      + ` · provider ${snapshot.provider_id || '—'}`
      + ` · key ${keyHint || '—'}`
      + `\nWhy: ${reasonFallback}`;
    return [{
      id: `route-snapshot:${index}`,
      spanId: `route-snapshot:${index}`,
      seq: Number.MAX_SAFE_INTEGER - snapshot.transitions.length + index,
      occurredAt: Math.max(0, Math.round(snapshot.recorded_at * 1000)),
      kind: 'model',
      status: 'switched',
      severity: 'warning',
      count: 1,
      summary,
      summaryKey,
      summaryArgs: {
        provider: transition.to.provider_id,
        from: fromProvider || '?',
        to: transition.to.provider_id,
        model: actualModel || '—',
      },
      detail,
      detailKey: 'activity.route.detail',
      detailArgs: {
        model: actualModel || '—',
        provider: snapshot.provider_id || '—',
        key: keyHint || '—',
        reasonKey,
        ...(reasonArgs ? { reasonArgs } : {}),
      },
      model: actualModel,
      providerId: snapshot.provider_id,
      fromModel: isModelFallback ? selectedModel : fromProvider,
      toModel: isModelFallback ? actualModel : transition.to.provider_id,
    } satisfies TurnActivityEntry];
  });
}
