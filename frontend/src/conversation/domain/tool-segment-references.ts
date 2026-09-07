/**
 * Materialize snapshot-only references into the local Turn shape.
 *
 * Conversation Sync may omit duplicate input/result bytes from completed tool
 * segments, repeated large tool-round documents, and exact projection content
 * or round thinking
 * when the generated browser requests `segmentPayload=refs`. This pure boundary
 * restores every reference before TurnStore, then discards request-local maps.
 */
import type {
  ConversationSyncSnapshot,
  ConversationTurnPage,
  TurnRecord,
  TurnToolResult,
  TurnToolRound,
  TurnToolUseSegment,
} from '../../api/conversation-sync.generated';

type UnknownRecord = Record<string, unknown>;
type ReferenceEnvelope = ConversationSyncSnapshot | ConversationTurnPage;
type ReferenceBudget = { count: number };

const SNAPSHOT_DOCUMENT_KEY = /^sha256:[0-9a-f]{64}$/;
const MAX_SHARED_TOOL_DOCUMENTS = 256;
const MAX_SHARED_TOOL_DOCUMENT_REFERENCES = 4096;
const MAX_SNAPSHOT_PROJECTION_REFERENCES = 4096;
const SHARED_TOOL_DOCUMENT_FIELDS = new Set(['toolContent', 'results']);
const SNAPSHOT_PROJECTION_REFERENCE_FIELDS = new Set([
  'content', 'roundThinking',
]);
const TERMINAL_TURN_STATUSES = new Set([
  'completed', 'failed', 'interrupted', 'truncated',
]);

function owns(value: object, field: PropertyKey): boolean {
  return Object.prototype.hasOwnProperty.call(value, field);
}

function record(value: unknown): UnknownRecord | null {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as UnknownRecord : null;
}

function resultFromRound(round: TurnToolRound): TurnToolResult {
  const roundResult = record(round.result) as TurnToolResult | null;
  const hasContent = roundResult !== null
    && Object.prototype.hasOwnProperty.call(roundResult, 'content');
  const hasStatus = roundResult !== null
    && Object.prototype.hasOwnProperty.call(roundResult, 'status');
  if ((hasContent || round.toolContent === undefined)
      && (hasStatus || round.status === undefined)) {
    return roundResult ?? {};
  }
  return {
    ...(roundResult ?? {}),
    ...(!hasContent && round.toolContent !== undefined
      ? { content: round.toolContent } : {}),
    ...(!hasStatus && round.status !== undefined
      ? { status: round.status } : {}),
  };
}

function uniqueRoundsByCallId(
  rounds: ReadonlyArray<TurnToolRound>,
): Map<string, TurnToolRound | null> {
  const indexed = new Map<string, TurnToolRound | null>();
  rounds.forEach((round) => {
    const callId = typeof round.toolCallId === 'string'
      ? round.toolCallId.trim() : '';
    if (!callId) return;
    indexed.set(callId, indexed.has(callId) ? null : round);
  });
  return indexed;
}

function uniqueSegmentTextsByBlockId(
  turn: TurnRecord,
  segmentType: 'text' | 'thinking',
): Map<string, string | null> {
  const indexed = new Map<string, string | null>();
  (turn.projection.segments ?? []).forEach((segment) => {
    if ((segment.type !== 'text' && segment.type !== 'thinking')
        || segment.type !== segmentType || !segment.blockId) return;
    indexed.set(
      segment.blockId,
      indexed.has(segment.blockId) ? null : segment.text,
    );
  });
  return indexed;
}

function materializeSnapshotProjectionReferences<T extends ReferenceEnvelope>(
  snapshot: T,
): T {
  if (!owns(snapshot, 'snapshotProjectionRefs')) return snapshot;
  const references = record(snapshot.snapshotProjectionRefs);
  const entries = references ? Object.entries(references) : [];
  if (entries.length < 1 || entries.length > MAX_SNAPSHOT_PROJECTION_REFERENCES) {
    throw new Error('Conversation snapshot has invalid projection references');
  }

  const turnsById = new Map<string, TurnRecord | null>();
  snapshot.turns.forEach((turn) => {
    const turnId = typeof turn.turnId === 'string' ? turn.turnId : '';
    if (!turnId) return;
    turnsById.set(turnId, turnsById.has(turnId) ? null : turn);
  });
  const replacements = new Map<string, TurnRecord>();
  let referenceCount = 0;
  entries.forEach(([turnId, rawTurnReferences]) => {
    const turn = turnsById.get(turnId);
    const turnReferences = record(rawTurnReferences);
    const fields = turnReferences ? Object.keys(turnReferences) : [];
    if (!turn || !turnReferences || fields.length < 1 || fields.length > 2
        || fields.some((field) => !SNAPSHOT_PROJECTION_REFERENCE_FIELDS.has(field))) {
      throw new Error('Conversation snapshot projection reference has no unique turn');
    }
    if (!TERMINAL_TURN_STATUSES.has(turn.status)) {
      throw new Error('Conversation snapshot references an active turn projection');
    }

    let projection = turn.projection;
    if (owns(turnReferences, 'content')) {
      referenceCount += 1;
      const blockId = turnReferences.content;
      if (referenceCount > MAX_SNAPSHOT_PROJECTION_REFERENCES
          || typeof blockId !== 'string' || !blockId) {
        throw new Error('Conversation snapshot has invalid projection references');
      }
      if (owns(projection, 'content')) {
        throw new Error('Conversation snapshot content reference conflicts with inline data');
      }
      const sourceText = uniqueSegmentTextsByBlockId(turn, 'text').get(blockId);
      if (typeof sourceText !== 'string') {
        throw new Error('Conversation snapshot content reference has no unique source');
      }
      projection = { ...projection, content: sourceText };
    }

    if (owns(turnReferences, 'roundThinking')) {
      const roundReferences = record(turnReferences.roundThinking);
      const roundEntries = roundReferences ? Object.entries(roundReferences) : [];
      referenceCount += roundEntries.length;
      if (roundEntries.length < 1
          || referenceCount > MAX_SNAPSHOT_PROJECTION_REFERENCES) {
        throw new Error('Conversation snapshot has invalid round thinking references');
      }
      const rounds = projection.toolRounds;
      if (!rounds?.length) {
        throw new Error('Conversation round thinking reference has no unique source');
      }
      const roundsById = uniqueRoundsByCallId(rounds);
      const thinkingByBlockId = uniqueSegmentTextsByBlockId(turn, 'thinking');
      const roundReplacements = new Map<string, TurnToolRound>();
      roundEntries.forEach(([callId, blockId]) => {
        const round = roundsById.get(callId);
        const sourceText = typeof blockId === 'string'
          ? thinkingByBlockId.get(blockId) : undefined;
        if (!callId || !round || typeof sourceText !== 'string') {
          throw new Error('Conversation round thinking reference has no unique source');
        }
        if (owns(round, 'thinking')) {
          throw new Error('Conversation round thinking reference conflicts with inline data');
        }
        roundReplacements.set(callId, { ...round, thinking: sourceText });
      });
      projection = {
        ...projection,
        toolRounds: rounds.map((round) => {
          const callId = typeof round.toolCallId === 'string'
            ? round.toolCallId.trim() : '';
          return roundReplacements.get(callId) ?? round;
        }),
      };
    }

    replacements.set(turnId, { ...turn, projection });
  });

  const materialized = {
    ...snapshot,
    turns: snapshot.turns.map((turn) => replacements.get(turn.turnId) ?? turn),
  };
  delete materialized.snapshotProjectionRefs;
  return materialized as T;
}

function materializeTurnSharedDocuments(
  turn: TurnRecord,
  documents: UnknownRecord | null,
  budget: ReferenceBudget,
): TurnRecord {
  const rounds = turn.projection.toolRounds;
  if (!rounds?.some((round) => owns(round, '_snapshotDocumentRefs'))) {
    return turn;
  }
  if (!documents) {
    throw new Error('Conversation snapshot has tool document references without documents');
  }

  let changed = false;
  const materializedRounds = rounds.map((round) => {
    if (!owns(round, '_snapshotDocumentRefs')) return round;
    const refs = record(round._snapshotDocumentRefs);
    const entries = refs ? Object.entries(refs) : [];
    if (entries.length < 1 || entries.length > 2) {
      throw new Error('Conversation tool round has invalid document references');
    }
    budget.count += entries.length;
    if (budget.count > MAX_SHARED_TOOL_DOCUMENT_REFERENCES) {
      throw new Error('Conversation snapshot exceeds the tool document reference budget');
    }

    const materialized = { ...round } as UnknownRecord;
    delete materialized._snapshotDocumentRefs;
    entries.forEach(([field, documentKey]) => {
      if (!SHARED_TOOL_DOCUMENT_FIELDS.has(field)
          || typeof documentKey !== 'string'
          || !SNAPSHOT_DOCUMENT_KEY.test(documentKey)) {
        throw new Error('Conversation tool round has invalid document references');
      }
      if (owns(round, field)) {
        throw new Error('Conversation tool round document reference conflicts with inline data');
      }
      if (!owns(documents, documentKey)) {
        throw new Error('Conversation tool round references a missing shared document');
      }
      const document = documents[documentKey];
      if (field === 'results' && !Array.isArray(document)) {
        throw new Error('Conversation tool round results reference is not an array');
      }
      materialized[field] = document;
    });
    changed = true;
    return materialized as TurnToolRound;
  });
  if (!changed) return turn;
  return {
    ...turn,
    projection: { ...turn.projection, toolRounds: materializedRounds },
  };
}

function materializeTurn(turn: TurnRecord): TurnRecord {
  const segments = turn.projection.segments;
  const rounds = turn.projection.toolRounds;
  if (!segments?.some((segment) => (
    segment.type === 'tool_use' && segment.roundRef !== undefined
  )) || !rounds?.length) return turn;

  const roundsById = uniqueRoundsByCallId(rounds);
  let changed = false;
  const materialized = segments.map((segment) => {
    if (segment.type !== 'tool_use' || segment.roundRef === undefined) {
      return segment;
    }
    const roundRef = typeof segment.roundRef === 'string'
      ? segment.roundRef.trim() : '';
    const round = roundsById.get(roundRef);
    if (!roundRef || segment.id !== roundRef || !round) {
      throw new Error('Conversation tool segment has an invalid round reference');
    }
    if (round.toolName && segment.name && round.toolName !== segment.name) {
      throw new Error('Conversation tool segment round reference changes tool identity');
    }
    changed = true;
    return {
      ...segment,
      ...(round.toolArgs !== undefined ? { input: round.toolArgs } : {}),
      result: resultFromRound(round),
      _round: round,
    } satisfies TurnToolUseSegment;
  });
  if (!changed) return turn;
  return {
    ...turn,
    projection: { ...turn.projection, segments: materialized },
  };
}

export function materializeSnapshotReferences<T extends ReferenceEnvelope>(
  snapshot: T,
): T {
  const projectionRestored = materializeSnapshotProjectionReferences(snapshot);
  const hasSharedDocuments = owns(projectionRestored, 'sharedToolDocuments');
  const documents = hasSharedDocuments
    ? record(projectionRestored.sharedToolDocuments) : null;
  if (hasSharedDocuments && !documents) {
    throw new Error('Conversation snapshot has an invalid shared document dictionary');
  }
  if (documents) {
    const documentKeys = Object.keys(documents);
    if (documentKeys.length < 1
        || documentKeys.length > MAX_SHARED_TOOL_DOCUMENTS
        || documentKeys.some((key) => !SNAPSHOT_DOCUMENT_KEY.test(key))) {
      throw new Error('Conversation snapshot has an invalid shared document dictionary');
    }
  }

  let changed = projectionRestored !== snapshot || hasSharedDocuments;
  const budget: ReferenceBudget = { count: 0 };
  const turns = projectionRestored.turns.map((turn) => {
    const restored = materializeTurnSharedDocuments(turn, documents, budget);
    const materialized = materializeTurn(restored);
    if (materialized !== turn) changed = true;
    return materialized;
  });
  if (!changed) return snapshot;
  const materialized = { ...projectionRestored, turns };
  delete materialized.sharedToolDocuments;
  return materialized as T;
}
