import { describe, expect, it } from 'vitest';
import type { TurnProjection, TurnRecord } from '../../../api/conversation-sync.generated';
import { selectTurnBlocks } from './turn-blocks';
import { translatedPlanPreview } from './translation-blocks';

function turn(actor: TurnRecord['actor'], projection: TurnProjection): TurnRecord {
  return { turnId: 'translated', conversationId: 'c1', laneId: 'main', actor,
    status: 'completed', projectionRevision: 1, projection } as TurnRecord;
}

const translation = { status: 'completed', model: 'gpt-6-luna' } as const;
describe('recorded message translation attribution', () => {
  it('keeps the canonical model on translated human input, including cold reload', () => {
    const record = turn('human', { content: 'English input', originalContent: '中文输入', translation });
    for (const source of [record, JSON.parse(JSON.stringify(record)) as TurnRecord]) {
      const block = selectTurnBlocks(source)[0];
      expect(block).toMatchObject({ kind: 'text', markdown: '中文输入',
        translatedMarkdown: 'English input', translationModel: 'gpt-6-luna', displayMode: 'original' });
    }
  });

  it('retains the recorded model even when no alternate body survives reload', () => {
    const block = selectTurnBlocks(turn('human', { content: 'English input', translation }))[0];
    expect(block).toMatchObject({ kind: 'text', displayMarkdown: 'English input', translationModel: 'gpt-6-luna' });
    expect(block).not.toHaveProperty('translatedMarkdown');
    expect(block).toMatchObject({ inputTranslationCompleted: true });
  });

  it('carries the model on the answer, not unrelated narration', () => {
    const blocks = selectTurnBlocks(turn('assistant', { content: 'Answer', translatedContent: '译文', translation,
      segments: [{ type: 'text', blockId: 'narration', text: 'Narration' },
        { type: 'text', blockId: 'answer', text: 'Answer', terminal: true, deliverable: true }] }));
    expect(blocks[0]).not.toHaveProperty('translationModel');
    expect(blocks[1]).toMatchObject({ kind: 'text', displayMode: 'translated', displayMarkdown: '译文',
      translationModel: 'gpt-6-luna' });
  });

  it.each([undefined, { status: 'completed' }, { status: 'completed', model: '  ' }] as const)(
    'never guesses a translation model from author or route metadata (%j)', translation => {
      const record = turn('assistant', { content: 'Answer', translatedContent: '译文',
        model: 'author-model', translation });
      expect(selectTurnBlocks(record)[0]).not.toHaveProperty('translationModel');
    });

  it('requires canonical completed human-input translation for a no-alternate receipt', () => {
    const projection = { content: 'Body', translation };
    expect(selectTurnBlocks(turn('assistant', projection))[0]).not.toHaveProperty('inputTranslationCompleted');
    expect(selectTurnBlocks(turn('human', { ...projection, translation: { status: 'pending', model: 'reported' } }))[0])
      .not.toHaveProperty('inputTranslationCompleted');
    expect(selectTurnBlocks(turn('human', { content: 'Body', translation: { status: 'completed' } }))[0])
      .toMatchObject({ inputTranslationCompleted: true });
  });

  const planOf = (text: string) => ({ blockId: 'proposed-plan', planId: 'p1',
    revision: 1, format: 'markdown', text }) as const;
  const tagged = (body: string) => `<proposed_plan>\n${body}\n</proposed_plan>`;

  it('shows the finished plan translation instead of an in-flight preview', () => {
    const proposedPlan = planOf('Do the thing.');
    const projection = { content: tagged('Do the thing.'),
      translatedContent: tagged('\u505a\u8fd9\u4ef6\u4e8b\u3002'), proposedPlan };
    const preview = translatedPlanPreview(projection, proposedPlan, {
      status: 'pending', partial: tagged('\u505a'), partialByRound: {},
    });
    expect(preview).toEqual({ markdown: '\u505a\u8fd9\u4ef6\u4e8b\u3002',
      pending: true, streaming: false });
  });

  it('streams the preview only while no translation is finished yet', () => {
    const proposedPlan = planOf('Do the thing.');
    const preview = translatedPlanPreview(
      { content: tagged('Do the thing.'), proposedPlan }, proposedPlan,
      { status: 'pending', partial: tagged('\u505a'), partialByRound: {} });
    expect(preview).toEqual({ markdown: '\u505a', pending: true, streaming: true });
  });
  it('refreshes the attribution with a new authoritative projection', () => {
    const record = turn('assistant', { content: 'Answer', translatedContent: '译文', translation });
    expect(selectTurnBlocks(record)[0]).toMatchObject({ translationModel: 'gpt-6-luna' });
    const updated = { ...record, projectionRevision: 2,
      projection: { ...record.projection, translation: { ...translation, model: 'actual-retry-model' } } };
    expect(selectTurnBlocks(updated)[0]).toMatchObject({ translationModel: 'actual-retry-model' });
    expect(record.projection.translation?.model).toBe('gpt-6-luna');
  });
});
