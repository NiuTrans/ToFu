/** ask_human -> native choices adapter. Conversation/turn identity is supplied by
 * the trusted mounted surface. Model content supplies only an opaque guidance ID.
 * Only the trusted fixed turn command port can submit; a waiter ACK is not proof.
 */
import { validGuidanceAnswer } from '../../rich-response/guidance-values';
import type { ChoiceInteraction, ChoicePorts } from '../../rich-response/choices';

import type { CreateAttemptRequest } from '../../api/conversation-sync.generated';

type RecordValue = Record<string, unknown>;
const record = (value: unknown): RecordValue => value !== null && typeof value === 'object' && !Array.isArray(value)
  ? value as RecordValue : {};
export interface ChoiceInteractionDependencies {
  conversationId: string;
  turnId: string;
  readTurn(conversationId: string, turnId: string, signal: AbortSignal): Promise<unknown>;
  createAttempt(conversationId: string, turnId: string, body: RecordValue, signal: AbortSignal): Promise<unknown>;
  commandId(): string;
  text?(key: string, fallback: string): string;
}

export function createChoiceInteractionPorts(dependencies: ChoiceInteractionDependencies): ChoicePorts {
  const { conversationId, turnId } = dependencies;

  let attempted: { interactionId: string; response: string; commandId: string; body: RecordValue } | null = null;
  const read = async (interactionId: string, signal: AbortSignal) => {
    if (!conversationId || !turnId || !interactionId) throw new Error('Unscoped interaction');
    const turn = record(await dependencies.readTurn(conversationId, turnId, signal));
    if (signal.aborted) throw new Error('Disposed interaction');
    // Queries are authenticated by the API owner; still deny accidental adapter
    // cross-binding before any question or answer is projected.
    if (turn.conversationId !== conversationId || turn.turnId !== turnId) throw new Error('Foreign turn');
    return turn;
  };
  const project = (turn: RecordValue, interactionId: string): ChoiceInteraction => {
    const projection = record(turn.projection);
    const rounds = Array.isArray(projection.toolRounds) ? projection.toolRounds : [];
    const matches = rounds.map(record).filter(round => round.guidanceId === interactionId);
    if (matches.length !== 1) throw new Error('Interaction not owned by this turn');
    const round = matches[0];
    if (round.toolName !== 'ask_human' || !['choice','free_text','batch'].includes(String(round.guidanceType))) throw new Error('Unsupported interaction');
    const mode=round.guidanceType as 'choice'|'free_text'|'batch';
    function declaredOptions(values:unknown,required=false):string[]{
      const options=Array.isArray(values)?values.map(option=>typeof option==='string'?option:record(option).label):[];
      if((required&&!options.length)||options.length>32||!options.every(option=>typeof option==='string'&&option.length>0&&option.length<=2000)||new Set(options).size!==options.length)throw new Error('Invalid declared choices');
      return options as string[];
    }
    const descriptions=(values:unknown)=>Array.isArray(values)?values.map(option=>typeof option==='string'?'':String(record(option).description??'')):[];
    const options=declaredOptions(round.guidanceOptions,mode==='choice');
    const questions=mode==='batch'&&Array.isArray(round.guidanceBatchQuestions)?round.guidanceBatchQuestions.map(value=>{
      const question=record(value);
      if(typeof question.question!=='string'||!question.question||question.question.length>32768||!['choice','free_text'].includes(String(question.response_type)))throw new Error('Invalid declared question');
      return {question:question.question,options:declaredOptions(question.options,question.response_type==='choice'),descriptions:descriptions(question.options)};
    }):undefined;
    if(mode==='batch'&&(!questions?.length||questions.length>8))throw new Error('Invalid question form');
    const metadata = [round.results, round.toolMeta].flatMap(value => Array.isArray(value) ? value.map(record) : []);
    const answer = typeof round.guidanceResponse === 'string' ? round.guidanceResponse
      : metadata.find(meta => meta.guidanceId === interactionId && typeof meta.userResponse === 'string')?.userResponse;
    const settlement = record(turn.settlement);
    const resumeOptions = Array.isArray(settlement.resumeOptions) ? settlement.resumeOptions.map(record) : [];
    const eligible = ((turn.status === 'running' || turn.status === 'pending') && (round.status === 'pending' || round.status === 'awaiting_human'))
      || resumeOptions.some(option => option.operation === 'answer_guidance'
        && record(option.anchor).guidanceId === interactionId);
    return { interactionId, mode, questions, question: String(round.guidanceQuestion ?? ''), options,
      descriptions: descriptions(round.guidanceOptions),
      state: typeof answer === 'string' ? 'answered' : eligible ? 'pending' : 'stale',
      ...(typeof answer === 'string' ? { response: answer } : {}) };
  };
  return {
    commandId: dependencies.commandId,
    text: dependencies.text,
    load: async (interactionId, signal) => project(await read(interactionId, signal), interactionId),
    submit: async (interactionId, response, commandId, signal) => {
      const turn = await read(interactionId, signal);
      const current = project(turn, interactionId);
      if (current.state === 'answered') return current; // Durable replay, no second command.
      const retry = attempted?.interactionId === interactionId && attempted.response === response && attempted.commandId === commandId;
      if ((!retry && current.state !== 'pending') || !validGuidanceAnswer(current.mode,current.options,current.questions,response)) throw new Error('Question is stale or choice is undeclared');
      if (!retry) attempted = { interactionId, response, commandId, body: {
        commandId, operation: 'answer_guidance', expectedProjectionRevision: turn.projectionRevision,
        humanResponse: response, config: {},
        ...((turn.status === 'running' || turn.status === 'pending') ? { guidanceId: interactionId } : {}),
      } };
      const result = record(await dependencies.createAttempt(conversationId, turnId, attempted!.body, signal));
      if (signal.aborted) throw new Error('Disposed interaction');
      const attempt = record(result.attempt), acceptedTurn = record(result.turn);
      const scopedLive = typeof attempted!.body.guidanceId === 'string';
      if ((!scopedLive && (attempt.commandId !== commandId || attempt.operation !== 'answer_guidance'))
        || attempt.turnId !== turnId || acceptedTurn.turnId !== turnId || acceptedTurn.conversationId !== conversationId) {
        throw new Error('Answer command not durably accepted');
      }
      const accepted = project(acceptedTurn, interactionId);

      const acceptedRounds = record(acceptedTurn.projection).toolRounds;
      const receipt = Array.isArray(acceptedRounds)
        ? acceptedRounds.map(record).find(round => round.guidanceId === interactionId) : undefined;
      if (scopedLive && (accepted.state !== 'answered' || accepted.response !== response
        || receipt?.guidanceResponse !== response || typeof receipt.guidanceAnswerCommandId !== 'string'
        || !receipt.guidanceAnswerCommandId)) {
        throw new Error('Answer command not durably accepted');
      }
      // The accepted-attempt reply proves durability even before executor output
      // folds the answer into toolMeta. Reload obtains the server projection.
      return { ...accepted, state: 'answered', response: accepted.response ?? response };
    },
  };
}

/** Production port: endpoint selection belongs only to the generated API contract. */
export function createAuthenticatedChoiceInteractionPorts(
  context: Omit<ChoiceInteractionDependencies, 'readTurn' | 'createAttempt'>,
): ChoicePorts {
  return createChoiceInteractionPorts({
    ...context,
    readTurn: async (conversationId, turnId, signal) => {
      const { conversationSyncApi } = await import('../../api/conversation-sync.generated');
      return (await conversationSyncApi.turn(conversationId, turnId, { signal })).turn;
    },
    createAttempt: async (conversationId, turnId, body, signal) => {
      const { conversationSyncApi } = await import('../../api/conversation-sync.generated');
      return conversationSyncApi.createAttempt(conversationId, turnId, body as unknown as CreateAttemptRequest, { signal });
    },
  });
}
