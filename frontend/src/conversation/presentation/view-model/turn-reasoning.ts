/** Pure reasoning metadata: recorded request controls, provider metering and
 * readable summary availability never imply one another. No replay payload is
 * inspected or copied. Entry: reasoningMetadataFrom; dependencies: DTOs, existing
 * serving-route classification, token-usage and presentation types. */
import type { AttemptRecord, TurnRecord } from '../../../api/conversation-sync.generated';
import { normalizeTokenUsage } from '../../../token-usage';
import { latestAgentApiRound, normalizeRecordedReasoningEffort,
  reasoningReceiptMatchesScope, turnReasoningResolution } from '../turn-serving-route';
import type { ConversationBlockViewModel, TurnReasoningViewModel } from './types';

type RecordValue = Readonly<Record<string, unknown>>;
const record = (value: unknown): RecordValue => value != null
  && typeof value === 'object' && !Array.isArray(value) ? value as RecordValue : {};
const own = (value: RecordValue, key: string): unknown => Object.hasOwn(value, key) ? value[key] : undefined;
const text = (value: unknown): string => typeof value === 'string' ? value : '';

export function reasoningMetadataFrom(
  turn: TurnRecord,
  blocks: ReadonlyArray<ConversationBlockViewModel>,
  attempt?: Partial<AttemptRecord>,
): TurnReasoningViewModel | undefined {
  const projection = record(turn.projection);
  if (turn.actor === 'human' || own(projection, '_isCompactionSummary') === true
      || own(projection, 'compaction')) return undefined;
  const live = turn.status === 'pending' || turn.status === 'running';
  // Resume ACK itself increments projectionRevision, and unrelated later deltas
  // do too. Neither proves new reasoning. Until an existing receipt has a matching
  // invocation identity, inherited unscoped effort/usage stays unknown while live.
  const resumed = ['continue', 'checkpoint_resume', 'regenerate', 'answer_guidance'].includes(attempt?.operation ?? '');

  const latest = record(latestAgentApiRound(own(projection, 'apiRounds')));
  const latestUsage = record(own(latest, 'usage'));
  const expectedScope = { attemptId: turn.currentAttemptId, taskId: attempt?.taskId,
    requireExplicit: live && resumed };
  const compatibleScope = { ...expectedScope, requireExplicit: false };
  const effort = normalizeRecordedReasoningEffort(turnReasoningResolution(projection, expectedScope));

  const aggregateUsage = record(own(projection, 'usage'));
  const turnTokens = reasoningReceiptMatchesScope(aggregateUsage, expectedScope)
    ? normalizeTokenUsage(aggregateUsage).thinkingTokens : null;
  const lastTokens = reasoningReceiptMatchesScope(latest, compatibleScope)
      && reasoningReceiptMatchesScope(latestUsage, compatibleScope)
      && (!expectedScope.requireExplicit || reasoningReceiptMatchesScope(latest, expectedScope)
        || reasoningReceiptMatchesScope(latestUsage, expectedScope))
    ? normalizeTokenUsage(latestUsage).thinkingTokens : null;
  // Aggregate billing can include retries/earlier rounds: label it as the Turn's
  // reported count, never as the tokens for the displayed effort's round. With
  // no aggregate, one latest authoring round is a labelled subset, not a sum.
  const tokens = turnTokens ?? lastTokens;
  if (effort === null && (tokens === null || tokens === 0)) return undefined;
  const positiveTokens = tokens !== null && tokens > 0;
  const readable = blocks.some(block => block.kind === 'thinking'
    && [block.markdown, block.translatedMarkdown, block.displayMarkdown]
      .some(value => typeof value === 'string' && Boolean(value.trim())))
    || (typeof own(projection, 'thinking') === 'string' && Boolean(text(own(projection, 'thinking')).trim()));
  const reasoningEvidence = positiveTokens || (effort !== null && effort !== 'off');
  const settled = turn.status === 'completed' || turn.status === 'truncated';
  const stopped = turn.status === 'failed' || turn.status === 'interrupted';
  const summary = readable ? 'readable' : live && reasoningEvidence ? 'not-received-yet'
    : settled && positiveTokens ? 'unavailable' : stopped && reasoningEvidence ? 'not-received' : null;
  return { effort, tokens, tokensSource: tokens === null ? null : turnTokens !== null ? 'turn' : 'last-round',
    tokensLowerBound: own(projection, 'usageComplete') === false, summary };
}
