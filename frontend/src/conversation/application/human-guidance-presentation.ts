/**
 * Lifecycle-local Human Guidance presentation state.
 *
 * Translation progress and optimistic submit feedback are UI facts, not Turn
 * facts. This store keys them by durable conversation/guidance identity and
 * decorates a disposable renderer read model without mutating projections.
 */

type UnknownRecord = Record<string, unknown>;

export interface HumanGuidanceOptionTranslation {
  label?: string;
  description?: string;
}

export interface HumanGuidancePresentation {
  translating?: boolean;
  translatedQuestion?: string;
  translatedOptions?: ReadonlyArray<HumanGuidanceOptionTranslation>;
  submittedResponse?: string;
}

export interface HumanGuidancePresentationStore {
  read(conversationId: string, guidanceId: string): HumanGuidancePresentation | undefined;
  patch(
    conversationId: string,
    guidanceId: string,
    patch: HumanGuidancePresentation,
  ): HumanGuidancePresentation;
  decorate<T extends UnknownRecord>(conversationId: string, round: T): T;
  clearConversation(conversationId: string): void;
}

function keyFor(conversationId: string, guidanceId: string): string {
  return `${conversationId}\u0000${guidanceId}`;
}

export function createHumanGuidancePresentationStore(): HumanGuidancePresentationStore {
  const states = new Map<string, HumanGuidancePresentation>();
  return {
    read(conversationId, guidanceId) {
      return states.get(keyFor(conversationId, guidanceId));
    },
    patch(conversationId, guidanceId, patch) {
      const key = keyFor(conversationId, guidanceId);
      const next = Object.freeze({ ...(states.get(key) ?? {}), ...patch });
      states.set(key, next);
      return next;
    },
    decorate<T extends UnknownRecord>(conversationId: string, round: T): T {
      const guidanceId = typeof round.guidanceId === 'string'
        ? round.guidanceId : '';
      if (!conversationId || !guidanceId) return round;
      const state = states.get(keyFor(conversationId, guidanceId));
      if (!state || round.status !== 'awaiting_human') return round;
      const options = Array.isArray(round.guidanceOptions)
        ? round.guidanceOptions as UnknownRecord[] : [];
      const translatedOptions = state.translatedOptions ?? [];
      return {
        ...round,
        ...(state.submittedResponse == null
          ? {} : { status: 'submitted', _hgUserResponse: state.submittedResponse }),
        ...(state.translating == null ? {} : { _hgTranslating: state.translating }),
        ...(state.translatedQuestion
          ? { _translatedQuestion: state.translatedQuestion } : {}),
        ...(translatedOptions.length ? {
          guidanceOptions: options.map((option, index) => ({
            ...option,
            ...(translatedOptions[index]?.label
              ? { _translatedLabel: translatedOptions[index]?.label } : {}),
            ...(translatedOptions[index]?.description
              ? { _translatedDescription: translatedOptions[index]?.description } : {}),
          })),
        } : {}),
      } as T;
    },
    clearConversation(conversationId) {
      const prefix = `${conversationId}\u0000`;
      for (const key of states.keys()) {
        if (key.startsWith(prefix)) states.delete(key);
      }
    },
  };
}
