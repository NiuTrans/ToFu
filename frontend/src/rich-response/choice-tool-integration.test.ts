// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest';
import { renderToolBlock, disposeToolBody } from '../conversation/ui/renderers/tool-blocks';
import type { ChoicePorts } from './choices';
const flush = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };
describe('native ask_human tool boundary', () => {
  it('uses mounted turn scope, preserves compatible focus and refreshes accepted answer state', async () => {
    let state: 'pending' | 'answered' = 'pending';
    const choices: ChoicePorts = { commandId: () => 'cmd',
      load: vi.fn(async () => ({ interactionId: 'hg_owned', question: 'Choose scope', options: ['Client', 'Server'], state,
        ...(state === 'answered' ? { response: 'Server' } : {}) })),
      submit: vi.fn(async () => { throw Error('not invoked'); }),
    };
    const bind = vi.fn(() => choices);
    const ports: any = { choiceInteractions: bind, renderSafeMarkdownHtml: (s: string) => s };
    const turn: any = { source: { conversationId: 'owned' }, turnId: 'turn', status: 'running' };
    const block: any = { kind: 'tool', blockId: 'tool', round: { toolName: 'ask_human', guidanceType: 'choice', guidanceId: 'hg_owned', status: 'awaiting_human' } };
    const host = document.createElement('div'); document.body.append(host);
    renderToolBlock(host, block, turn, ports); await flush();
    expect(bind).toHaveBeenCalledWith('owned', 'turn');
    expect(host.dataset.rendererMode).toBe('native-choice');
    const input = host.querySelector<HTMLInputElement>('input')!; input.focus();
    renderToolBlock(host, block, turn, ports); await flush();
    expect(host.querySelector('input')).toBe(input); expect(document.activeElement).toBe(input);
    expect(choices.load).toHaveBeenCalledTimes(1);
    state = 'answered'; block.round.guidanceResponse = 'Server'; block.round.status = 'completed';
    renderToolBlock(host, block, turn, ports); await flush();
    expect(host.textContent).toContain('Answer saved: Server');
    expect(host.querySelector('fieldset')?.disabled).toBe(true);
    expect(choices.submit).not.toHaveBeenCalled(); disposeToolBody(host); host.remove();
  });
  it('aborts an owner-scoped question load on teardown with no late DOM mutation', async () => {
    let signal!: AbortSignal; let resolve!: (v: any) => void;
    const choices: ChoicePorts = { commandId: () => 'cmd', load: (_id, s) => { signal = s; return new Promise(r => resolve = r); }, submit: async () => { throw Error('no'); } };
    const host = document.createElement('div');
    renderToolBlock(host, { kind: 'tool', round: { toolName: 'ask_human', guidanceType: 'choice', guidanceId: 'hg_owned' } } as any,
      { source: { conversationId: 'owned' }, turnId: 'turn' } as any, { choiceInteractions: () => choices } as any);
    const before = host.textContent; disposeToolBody(host); expect(signal.aborted).toBe(true);
    resolve({ interactionId: 'hg_owned', question: 'Late', options: ['One', 'Two'], state: 'pending' }); await flush();
    expect(host.textContent).toBe(before);
  });
});
