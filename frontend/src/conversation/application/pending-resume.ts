/**
 * Responsibility: after a server restart, offer one-shot batch resume for the
 * conversations the boot task/turn recovery settled as 'interrupted'.
 * Entry point: createPendingResumePrompter. Dependencies: injected transport,
 * choice dialog, resume executor, and copy — this module owns no DOM or
 * module-global state, and never becomes a second authority over turn status
 * (the v3 snapshot stays authoritative; the server payload is a boot hint).
 */
import type { TurnRecord } from '../../api/conversation-sync.generated';
import { resumeTurnOptions } from '../presentation/turn-finish';

export interface PendingResumeConversation {
  readonly id: string;
  readonly title?: string;
  readonly updatedAt?: number;
}

export interface PendingResumePayload {
  readonly reason?: string;
  readonly recordedAt?: number;
  readonly conversations?: ReadonlyArray<
    PendingResumeConversation | null | undefined
  >;
}

export interface PendingResumeChoiceOption {
  readonly value: string;
  readonly label: string;
  readonly subtitle?: string;
  readonly icon?: string;
  readonly accent?: boolean;
}

export interface PendingResumeCopy {
  title(): string;
  message(count: number): string;
  resumeAll(count: number): string;
  later(): string;
  resumedToast(count: number): string;
  conversationLabel(conversation: PendingResumeConversation): string;
}

export interface PendingResumePrompterPorts {
  loadPending(): Promise<PendingResumePayload | null>;
  dismiss(conversationIds?: string[]): unknown;
  showChoice(config: {
    title: string;
    message?: string;
    options: PendingResumeChoiceOption[];
    dismissValue: string;
  }): Promise<string | null>;
  resumeConversation(conversationId: string): Promise<boolean>;
  openConversation?(conversationId: string): void;
  notify?(message: string): void;
  copy: PendingResumeCopy;
  warn?(message: string, error: unknown): void;
}

export interface PendingResumePrompter {
  maybeShow(): Promise<void>;
}

export interface PendingResumeTurnPick {
  readonly turnId: string;
  readonly operation: string;
}

export const PENDING_RESUME_ALL_VALUE = '__all__';
export const PENDING_RESUME_LATER_VALUE = '__later__';

const RESUMABLE_ACTORS = new Set(['assistant', 'planner']);
const RESUMABLE_STATUSES = new Set(['interrupted', 'truncated']);

/**
 * Pick the turn a manual "继续" click would target: the NEWEST assistant/
 * planner turn, resumed only when it settled interrupted/truncated with
 * server-provided resumeOptions. A newer completed turn (or one without
 * options) means the conversation no longer counts as unfinished.
 */
export function pickPendingResumeTurn(
  orderedTurns: ReadonlyArray<TurnRecord | null | undefined>,
): PendingResumeTurnPick | null {
  for (let index = orderedTurns.length - 1; index >= 0; index -= 1) {
    const turn = orderedTurns[index];
    if (!turn || !RESUMABLE_ACTORS.has(String(turn.actor || ''))) continue;
    if (!RESUMABLE_STATUSES.has(String(turn.status || ''))) return null;
    const operation = resumeTurnOptions(turn)[0]?.operation;
    const turnId = String(turn.turnId || '');
    if (!operation || !turnId) return null;
    return { turnId, operation };
  }
  return null;
}

/** Normalize the boot hint: keep server order, dedupe, require stable ids. */
export function normalizePendingResumeConversations(
  payload: PendingResumePayload | null | undefined,
): PendingResumeConversation[] {
  const seen = new Set<string>();
  const out: PendingResumeConversation[] = [];
  for (const item of payload?.conversations || []) {
    const id = String(item?.id || '').trim();
    if (!id || seen.has(id)) continue;
    seen.add(id);
    out.push({ id, title: item?.title, updatedAt: item?.updatedAt });
  }
  return out;
}

export function createPendingResumePrompter(
  ports: PendingResumePrompterPorts,
): PendingResumePrompter {
  const warn = (message: string, error: unknown) => {
    if (ports.warn) ports.warn(message, error);
  };
  const dismissQuietly = (conversationIds?: string[]) => {
    try {
      void Promise.resolve(ports.dismiss(conversationIds)).catch((error: unknown) => {
        warn('[PendingResume] dismiss failed:', error);
      });
    } catch (error) {
      warn('[PendingResume] dismiss failed:', error);
    }
  };

  /* One prompt per page load: the record is only cleared by an explicit user
   * choice, so a re-opened page would ask again — but this page never nags
   * twice in a row. */
  let prompted = false;

  const resumeAll = async (conversations: PendingResumeConversation[]) => {
    let resumed = 0;
    for (const conversation of conversations) {
      try {
        if (await ports.resumeConversation(conversation.id)) resumed += 1;
      } catch (error) {
        warn('[PendingResume] resume failed:', error);
      }
    }
    dismissQuietly(conversations.map((conversation) => conversation.id));
    if (resumed > 0 && ports.notify) {
      ports.notify(ports.copy.resumedToast(resumed));
    }
  };

  const maybeShow = async (): Promise<void> => {
    if (prompted) return;
    prompted = true;
    let payload: PendingResumePayload | null = null;
    try {
      payload = await ports.loadPending();
    } catch (error) {
      warn('[PendingResume] load failed:', error);
      return;
    }
    const conversations = normalizePendingResumeConversations(payload);
    if (conversations.length === 0) return;

    const options: PendingResumeChoiceOption[] = [];
    if (conversations.length > 1) {
      options.push({
        value: PENDING_RESUME_ALL_VALUE,
        label: ports.copy.resumeAll(conversations.length),
        accent: true,
      });
    }
    for (const conversation of conversations) {
      options.push({
        value: conversation.id,
        label: ports.copy.conversationLabel(conversation),
      });
    }
    options.push({ value: PENDING_RESUME_LATER_VALUE, label: ports.copy.later() });

    const choice = await ports.showChoice({
      title: ports.copy.title(),
      message: ports.copy.message(conversations.length),
      options,
      dismissValue: PENDING_RESUME_LATER_VALUE,
    });
    if (!choice || choice === PENDING_RESUME_LATER_VALUE) {
      /* "Not now" consumes the boot hint: the sidebar's incomplete dot still
       * marks every interrupted conversation for a manual resume, so nothing
       * is lost by not asking again on the next page load. */
      dismissQuietly();
      return;
    }
    if (choice === PENDING_RESUME_ALL_VALUE) {
      await resumeAll(conversations);
      return;
    }
    const selected = conversations.find(
      (conversation) => conversation.id === choice,
    );
    if (!selected) return;
    try {
      const resumed = await ports.resumeConversation(selected.id);
      if (resumed && ports.openConversation) ports.openConversation(selected.id);
    } catch (error) {
      warn('[PendingResume] resume failed:', error);
    }
    dismissQuietly([selected.id]);
  };

  return Object.freeze({ maybeShow });
}
