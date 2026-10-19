// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { ConversationTurnViewModel } from '../../presentation/conversation-view-model';
import type { TextBlockViewModel } from '../../presentation/view-model/types';
import type { ClassicConversationRendererPorts } from './ports';
import { renderTextBlock } from './text-blocks';

afterEach(() => document.body.replaceChildren());
function fixture(overrides: Partial<TextBlockViewModel> = {}) {
  const node = document.createElement('div');
  document.body.appendChild(node);
  const block: TextBlockViewModel = { kind: 'text', blockId: 'answer', source: {}, identitySource: 'contract',
    markdown: 'Original body', translatedMarkdown: 'Translated body', displayMarkdown: 'Original body',
    displayMode: 'original', translationModel: 'gpt-6-luna', terminal: true, deliverable: true,
    resumable: false, ...overrides };
  const ports: ClassicConversationRendererPorts = { renderSafeMarkdownHtml: text => {
    const safe = document.createElement('p'); safe.textContent = text; return safe.outerHTML;
  } };
  renderTextBlock(node, block, ports);
  return { node, block, ports };
}

describe('message translation attribution', () => {
  it('places the actual model beside the Translation disclosure without opening it', () => {
    const { node } = fixture();
    const details = node.querySelector('details')!;
    expect(details.open).toBe(false);
    expect(details.querySelector('summary')?.textContent).toContain('Translation');
    expect(details.querySelector('.bilingual-model')?.textContent).toBe('Translated by gpt-6-luna');
    expect(node.querySelector('.bilingual-attribution')).toBeNull();
  });

  it('attributes the visible translated answer once while retaining the Original fold, reader state and focus', () => {
    const { node, block, ports } = fixture();
    const details = node.querySelector('details')!;
    const summary = details.querySelector('summary')!;
    summary.tabIndex = 0; summary.focus(); details.open = true;
    renderTextBlock(node, { ...block, displayMode: 'translated', displayMarkdown: 'Translated body' }, ports);
    expect(node.querySelector('.md-content')?.textContent).toBe('Translated body');
    expect(node.querySelector('.bilingual-attribution')?.textContent).toBe('Translated by gpt-6-luna');
    expect(summary.textContent).toContain('Original');
    expect(summary.textContent).not.toContain('gpt-6-luna');
    expect(node.querySelectorAll('.bilingual-model')).toHaveLength(1);
    expect(node.querySelector('details')).toBe(details);
    expect(details.open).toBe(true);
    expect(document.activeElement).toBe(summary);
    renderTextBlock(node, block, ports);
    expect(details.open).toBe(true);
    expect(document.activeElement).toBe(summary);
    expect(node.querySelector('.bilingual-attribution')).toBeNull();
  });

  it('shows a canonical receipt without manufacturing an alternate-body panel', () => {
    const { node } = fixture({ translatedMarkdown: undefined, inputTranslationCompleted: true });
    expect(node.querySelector('details')).toBeNull();
    expect(node.querySelector('.bilingual-attribution')?.textContent).toBe('Translated by gpt-6-luna');
    expect(node.querySelector('.md-content')?.textContent).toBe('Original body');
  });

  it('uses localization and safe plain text for arbitrary model IDs, then removes stale attribution', () => {
    const model = '<img src=x onerror=alert(1)>/provider:model';
    const { node, block, ports } = fixture({ translationModel: model, displayMode: 'translated' });
    const localizedText = vi.fn((key: string, fallback: string, values?: Readonly<Record<string, string | number>>) =>
      key === 'chat.translationModel' ? `翻译模型：${values?.model}` : fallback);
    renderTextBlock(node, block, { ...ports, localizedText });
    expect(localizedText).toHaveBeenCalledWith('chat.translationModel', 'Translated by {model}', { model });
    expect(node.querySelector('.bilingual-attribution')?.textContent).toBe(`翻译模型：${model}`);
    expect(node.querySelector('.bilingual-attribution')?.getAttribute('title')).toBe(`翻译模型：${model}`);
    expect(node.querySelector('summary .bilingual-model')).toBeNull();
    expect(node.querySelector('img')).toBeNull();
    renderTextBlock(node, { ...block, translationModel: undefined }, ports);
    expect(node.querySelector('.bilingual-attribution')?.textContent).toBe('Translation model not reported');
  });

  it('makes missing attribution explicit for legacy translations without guessing a model', () => {
    const { node } = fixture({ translationModel: undefined });
    expect(node.querySelector('summary')?.textContent).toContain('Translation model not reported');
  });

  it('does not label original-only content from model metadata alone', () => {
    const { node } = fixture({ translatedMarkdown: undefined });
    expect(node.querySelector('.bilingual-model')).toBeNull();
    expect(node.querySelector('details')).toBeNull();
  });
});

it('retains focused evidence through answer replay and an appended paragraph', () => {
  const node = document.createElement('section');
  document.body.append(node);
  const turn = { actor: 'assistant', role: 'assistant', source: { projection: {} } } as ConversationTurnViewModel;
  const block = { kind: 'text', blockId: 'answer', markdown: 'original', terminal: true,
    deliverable: true } as TextBlockViewModel;
  const ports = { renderSafeMarkdownHtml: (text: string) =>
    '<p>Answer</p><pre><button type="button">Copy code</button><code>const value = 1;</code></pre>'
      + (text === 'extended' ? '<p>Further explanation.</p>' : '') };
  try {
    renderTextBlock(node, block, ports, turn);
    const control = node.querySelector<HTMLButtonElement>('pre button')!;
    control.focus();
    for (const markdown of ['original', 'extended']) {
      renderTextBlock(node, { ...block, markdown }, ports, turn);
      expect(document.activeElement).toBe(control);
      expect(node.querySelector('pre button')).toBe(control);
      expect(node.querySelectorAll('.conversation-answer-label')).toHaveLength(1);
    }
    renderTextBlock(node, { ...block, deliverable: false }, ports, turn);
    expect(node.querySelector('.conversation-answer-label')).toBeNull();
  } finally { node.remove(); }
});
