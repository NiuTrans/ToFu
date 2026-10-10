import { describe, expect, it } from 'vitest';
import type { TurnRecord } from '../../api/conversation-sync.generated';
import {
  PENDING_RESUME_ALL_VALUE,
  PENDING_RESUME_LATER_VALUE,
  createPendingResumePrompter,
  normalizePendingResumeConversations,
  pickPendingResumeTurn,
} from './pending-resume';

function turn(partial: Record<string, unknown>): TurnRecord {
  return { actor: 'assistant', ...partial } as unknown as TurnRecord;
}

describe('pickPendingResumeTurn', () => {
  it('picks the newest interrupted assistant turn with its first resume option', () => {
    const pick = pickPendingResumeTurn([
      turn({ turnId: 't1', status: 'completed' }),
      turn({
        turnId: 't2',
        status: 'interrupted',
        settlement: { resumeOptions: [{ operation: 'continue' }, { operation: 'regenerate' }] },
      }),
    ]);
    expect(pick).toEqual({ turnId: 't2', operation: 'continue' });
  });

  it('skips trailing human turns when finding the newest assistant turn', () => {
    const pick = pickPendingResumeTurn([
      turn({
        turnId: 't1',
        status: 'truncated',
        settlement: { resumeOptions: ['continue'] },
      }),
      turn({ turnId: 'h1', actor: 'human', status: 'completed' }),
    ]);
    expect(pick).toEqual({ turnId: 't1', operation: 'continue' });
  });

  it('returns null when the newest assistant turn completed normally', () => {
    expect(pickPendingResumeTurn([
      turn({
        turnId: 't1',
        status: 'interrupted',
        settlement: { resumeOptions: [{ operation: 'continue' }] },
      }),
      turn({ turnId: 't2', status: 'completed' }),
    ])).toBeNull();
  });

  it('returns null when the interrupted turn offers no resume options', () => {
    expect(pickPendingResumeTurn([
      turn({ turnId: 't1', status: 'interrupted', settlement: {} }),
    ])).toBeNull();
  });

  it('returns null for empty or non-assistant transcripts', () => {
    expect(pickPendingResumeTurn([])).toBeNull();
    expect(pickPendingResumeTurn([
      turn({ turnId: 'h1', actor: 'human', status: 'completed' }),
    ])).toBeNull();
  });
});

describe('normalizePendingResumeConversations', () => {
  it('dedupes, drops id-less rows, and keeps server order', () => {
    expect(normalizePendingResumeConversations({
      conversations: [
        { id: 'a', title: 'A' },
        { id: 'a', title: 'A again' },
        { id: '  ' },
        null,
        { id: 'b' },
      ],
    })).toEqual([{ id: 'a', title: 'A', updatedAt: undefined }, { id: 'b', title: undefined, updatedAt: undefined }]);
  });

  it('tolerates a missing payload', () => {
    expect(normalizePendingResumeConversations(null)).toEqual([]);
    expect(normalizePendingResumeConversations({})).toEqual([]);
  });
});

interface PrompterHarnessOptions {
  conversations?: Array<{ id: string; title?: string }> | null;
  choice: string | null;
  resumeResults?: Record<string, boolean>;
}

function createHarness(options: PrompterHarnessOptions) {
  const dismissed: Array<string[] | undefined> = [];
  const resumed: string[] = [];
  const opened: string[] = [];
  const notified: string[] = [];
  const choices: Array<{
    title: string;
    message?: string;
    options: Array<{ value: string; label: string }>;
    dismissValue: string;
  }> = [];
  const prompter = createPendingResumePrompter({
    loadPending: async () => (options.conversations == null
      ? null
      : { reason: 'process_killed', conversations: options.conversations }),
    dismiss: (ids) => { dismissed.push(ids ? [...ids] : undefined); },
    showChoice: async (config) => {
      choices.push(config);
      return options.choice;
    },
    resumeConversation: async (id) => {
      resumed.push(id);
      return options.resumeResults?.[id] ?? true;
    },
    openConversation: (id) => { opened.push(id); },
    notify: (message) => { notified.push(message); },
    copy: {
      title: () => 'title',
      message: (count) => `message ${count}`,
      resumeAll: (count) => `all ${count}`,
      later: () => 'later',
      resumedToast: (count) => `resumed ${count}`,
      conversationLabel: (conversation) => conversation.title || conversation.id,
    },
  });
  return { prompter, dismissed, resumed, opened, notified, choices };
}

describe('createPendingResumePrompter', () => {
  it('does not open the dialog when nothing is pending', async () => {
    const harness = createHarness({ conversations: [], choice: null });
    await harness.prompter.maybeShow();
    expect(harness.choices).toHaveLength(0);
    expect(harness.dismissed).toHaveLength(0);
  });

  it('dismisses the whole hint when the user picks "not now"', async () => {
    const harness = createHarness({
      conversations: [{ id: 'a' }, { id: 'b' }],
      choice: PENDING_RESUME_LATER_VALUE,
    });
    await harness.prompter.maybeShow();
    expect(harness.resumed).toHaveLength(0);
    expect(harness.dismissed).toEqual([undefined]);
  });

  it('treats dialog dismissal (null) as "not now"', async () => {
    const harness = createHarness({
      conversations: [{ id: 'a' }],
      choice: null,
    });
    await harness.prompter.maybeShow();
    expect(harness.resumed).toHaveLength(0);
    expect(harness.dismissed).toEqual([undefined]);
  });

  it('resumes every conversation on "resume all" and dismisses exactly those ids', async () => {
    const harness = createHarness({
      conversations: [{ id: 'a' }, { id: 'b' }, { id: 'c' }],
      choice: PENDING_RESUME_ALL_VALUE,
      resumeResults: { a: true, b: false, c: true },
    });
    await harness.prompter.maybeShow();
    expect(harness.resumed).toEqual(['a', 'b', 'c']);
    expect(harness.dismissed).toEqual([['a', 'b', 'c']]);
    expect(harness.notified).toEqual(['resumed 2']);
    expect(harness.opened).toHaveLength(0);
  });

  it('does not offer "resume all" for a single conversation', async () => {
    const harness = createHarness({
      conversations: [{ id: 'a', title: 'One' }],
      choice: 'a',
    });
    await harness.prompter.maybeShow();
    expect(harness.choices[0]?.options.map((option) => option.value)).toEqual([
      'a',
      PENDING_RESUME_LATER_VALUE,
    ]);
    expect(harness.resumed).toEqual(['a']);
    expect(harness.opened).toEqual(['a']);
    expect(harness.dismissed).toEqual([['a']]);
  });

  it('prompts at most once per page load', async () => {
    const harness = createHarness({
      conversations: [{ id: 'a' }],
      choice: PENDING_RESUME_LATER_VALUE,
    });
    await harness.prompter.maybeShow();
    await harness.prompter.maybeShow();
    expect(harness.choices).toHaveLength(1);
  });
});
