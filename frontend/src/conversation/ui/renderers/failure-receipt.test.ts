/** Failure diagnostics retain their own document, evidence and cleanup on copy failure. */
import { describe, expect, it, vi } from 'vitest';
import type { ErrorEnvelope } from '../../../api/errors';
import { buildFailureReceipt } from './failure-receipt';
import type { ConversationTurnViewModel } from '../../presentation/conversation-view-model';

// The repo's jsdom dependency has no declaration package. Name only the
// browser capabilities this fixture needs, without widening production types.
type FixtureWindow = Pick<Window, 'document' | 'navigator' | 'close'>;
const { JSDOM } = await vi.importActual<{
  JSDOM: new (markup: string) => { window: FixtureWindow };
}>('jsdom');

const failure = {
  kind: 'internal', message: 'Synthetic failure', retryable: false,
} as ErrorEnvelope;

function fixture() {
  const dom = new JSDOM('<!doctype html><body><button id="reader">Reader</button></body>');
  const document = dom.window.document;
  const receipt = buildFailureReceipt(document, failure, undefined, {
    renderSafeMarkdownHtml: () => { throw new Error('Diagnostics must remain plain text'); },
  });
  document.body.appendChild(receipt);
  const button = receipt.querySelector('button')!;
  const focus = document.querySelector<HTMLButtonElement>('#reader')!;
  focus.focus();
  return { dom, document, button, focus };
}

describe('failure receipt clipboard lifecycle', () => {
  it('uses the receipt document capability without borrowing a page global', async () => {
    const first = fixture();
    const second = fixture();
    try {
      const firstWrite = vi.fn(async (_text: string) => {});
      const secondWrite = vi.fn(async (_text: string) => {});
      Object.defineProperty(first.dom.window.navigator, 'clipboard', { value: { writeText: firstWrite } });
      Object.defineProperty(second.dom.window.navigator, 'clipboard', { value: { writeText: secondWrite } });
      first.button.click();
      await vi.waitFor(() => expect(first.button.textContent).toBe('Copied'));
      expect(firstWrite).toHaveBeenCalledOnce();
      expect(JSON.parse(firstWrite.mock.calls[0][0]).error).toEqual(failure);
      expect(secondWrite).not.toHaveBeenCalled();
      expect(first.document.querySelector('textarea')).toBeNull();
      expect(first.document.activeElement).toBe(first.focus);
    } finally {
      first.dom.window.close();
      second.dom.window.close();
    }
  });

  it('falls back after denied native access and restores focus after copying', async () => {
    const { dom, document, button, focus } = fixture();
    try {
      Object.defineProperty(dom.window.navigator, 'clipboard', {
        value: { writeText: async () => { throw new Error('Synthetic permission denial'); } },
      });
      let copied = '';
      Object.defineProperty(document, 'execCommand', { value: () => {
        const area = document.querySelector('textarea')!;
        area.focus();
        copied = area.value;
        return true;
      } });
      button.click();
      await vi.waitFor(() => expect(button.textContent).toBe('Copied'));
      expect(JSON.parse(copied).error).toEqual(failure);
      expect(document.querySelector('textarea')).toBeNull();
      expect(document.activeElement).toBe(focus);
    } finally { dom.window.close(); }
  });

  it.each(['throws', 'returns false'] as const)('cleans up when compatibility copy %s and permits retry', async (mode) => {
    const { dom, document, button, focus } = fixture();
    try {
      let attempts = 0;
      Object.defineProperty(document, 'execCommand', { value: () => {
        attempts += 1;
        document.querySelector('textarea')!.focus();
        if (mode === 'throws') throw new Error('Synthetic unsupported copy');
        return false;
      } });
      button.click();
      await vi.waitFor(() => expect(attempts).toBe(1));
      expect(button.textContent).toBe('Copy diagnostics');
      expect(document.querySelector('textarea')).toBeNull();
      expect(document.activeElement).toBe(focus);
      button.click();
      await vi.waitFor(() => expect(attempts).toBe(2));
      expect(document.querySelector('textarea')).toBeNull();
      expect(button.disabled).toBe(false);
    } finally { dom.window.close(); }
  });
});


it('copies failure facts without duplicate errors or private replay payloads', async () => {
  const dom = new JSDOM('<!doctype html><body></body>');
  try {
    const writeText = vi.fn(async (_text: string) => {});
    Object.defineProperty(dom.window.navigator, 'clipboard', {value: {writeText}});
    const error = {...failure, detail: 'loader failure', raw: 'loader failure',
      hint: 'Retry', hintKey: 'internal.hint', titleKey: 'internal.title',
      failureAttribution: {phase: 'task-start'}};
    const turn = {turnId: 'turn', attemptId: 'attempt', taskId: 'task', status: 'failed',
      source: {settlement: {outcome: 'failed', cause: 'generation_error', error,
        streamState: null, resumeOptions: [{anchor: {content: 'PRIVATE PROMPT',
          thinking: 'PRIVATE THINKING', retainedToolRoundPositions: [0, 1]}}]}}
    } as unknown as ConversationTurnViewModel;
    const receipt = buildFailureReceipt(dom.window.document, error, turn, {renderSafeMarkdownHtml: () => ''});
    receipt.querySelector('button')!.click();
    await vi.waitFor(() => expect(writeText).toHaveBeenCalledOnce());
    const copied = JSON.parse(writeText.mock.calls[0][0]);
    expect(copied.turnId).toBe('turn');
    expect(copied.error.failureAttribution).toEqual({phase: 'task-start'});
    expect(copied.error.detail).toBe('loader failure');
    expect(copied.error.raw).toBeUndefined();
    expect(copied.error.hintKey).toBeUndefined();
    expect(copied.error.hint).toBeUndefined();
    expect(copied.settlement).toEqual({outcome: 'failed', cause: 'generation_error'});
    expect(writeText.mock.calls[0][0]).not.toContain('PRIVATE');
  } finally { dom.window.close(); }
});
