// @vitest-environment jsdom
/** One-gesture continuation, human consequences and delivery uncertainty. */
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { ConversationUsageRecovery, TurnCommandResponse } from '../../../api/conversation-sync.generated';
import type { ConversationBlockViewModel, ConversationTurnViewModel } from '../../presentation/conversation-view-model';
import { createClassicConversationRenderers } from '../classic-conversation-renderers';
import { renderActivityEventBlock } from './activity-block';
import { disposeUsageRecoveryNotice, renderUsageRecoveryNotice } from './usage-recovery-notice';
import type { ClassicConversationRendererPorts, UsageRecoveryPorts } from './ports';
import en from '../../../i18n/locales/en.json';
import zh from '../../../i18n/locales/zh.json';

const binding = { conversationId: 'conversation-source', turnId: 'turn-source', projectionRevision: 7 };
const ready = (): ConversationUsageRecovery => ({ ok: true, conversationId: binding.conversationId,
  sourceTurnId: binding.turnId, sourceProjectionRevision: 7, state: 'ready', reason: 'ready',
  budget: { maxTokens: 12345, maxToolCalls: 9, durationSeconds: 321 } });
const accepted = (): TurnCommandResponse => ({ ok: true,
  conversationId: binding.conversationId, conversationRevision: 10 });
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
function setup(overrides: Partial<UsageRecoveryPorts> = {}) {
  const node = document.createElement('div');
  document.body.append(node);
  const api = {
    inspect: vi.fn<UsageRecoveryPorts['inspect']>().mockResolvedValue(ready()),
    recover: vi.fn<UsageRecoveryPorts['recover']>().mockResolvedValue(accepted()),
    createCommandId: vi.fn(() => 'one-confirmed-command'),
    onAccepted: vi.fn(),
  };
  // Assign rather than spread: a spread of the port type would widen these
  // properties and hide their mock API from the assertions below.
  Object.assign(api, overrides);
  const ports: ClassicConversationRendererPorts = { usageRecovery: api, renderSafeMarkdownHtml: text => text };
  const render = (next = binding) => renderUsageRecoveryNotice(node, node, next, ports);
  render();
  return { node, api, ports, render };
}
function button(node: HTMLElement, label: string): HTMLButtonElement {
  const found = Array.from(node.querySelectorAll('button')).find(candidate => candidate.textContent === label);
  expect(found, `Missing button ${label}`).toBeDefined();
  return found!;
}
const phase = (node: HTMLElement) => node.querySelector<HTMLElement>('[data-usage-recovery]')?.dataset.usageRecovery;
afterEach(() => { document.body.replaceChildren(); });

describe('one-gesture continuation', () => {
  it('continues generation from a single explicit action without asking for numbers', async () => {
    const pending = deferred<TurnCommandResponse>();
    const recover = vi.fn<UsageRecoveryPorts['recover']>().mockReturnValue(pending.promise);
    const { node, api } = setup({ recover });
    expect(api.inspect).not.toHaveBeenCalled();
    expect(recover).not.toHaveBeenCalled();
    const control = button(node, 'Continue generating');
    expect(node.textContent).toContain('additional model and tool charges');
    expect(node.textContent).toContain('exactly as they are');
    expect(node.textContent).toContain('does not resend the earlier request');
    control.focus(); control.click(); control.click();
    await vi.waitFor(() => expect(api.recover).toHaveBeenCalledTimes(1));
    expect(api.inspect).toHaveBeenCalledWith(binding.conversationId, binding.turnId, expect.any(AbortSignal));
    // The click is the authorization; the exact server-proposed budget is
    // still sent verbatim, and its numbers are never a field to fill in.
    expect(recover).toHaveBeenCalledWith(binding.conversationId, binding.turnId, {
      commandId: 'one-confirmed-command', expectedProjectionRevision: 7,
      confirmed: true, budget: ready().budget,
    });
    const details = node.querySelector<HTMLDetailsElement>('[data-usage-recovery-budget]')!;
    expect(details.open).toBe(false);
    expect(details.querySelector('summary')?.textContent).toBe('Limits chosen automatically');
    expect(details.textContent).toContain('12345 tokens, 9 tool calls and 321 seconds');
    expect(phase(node)).toBe('submitting');
    pending.resolve(accepted());
    await vi.waitFor(() => expect(phase(node)).toBe('accepted'));
    expect(api.onAccepted).toHaveBeenCalledWith(binding.conversationId);
    const done = button(node, 'Continuation started');
    done.click();
    expect(recover).toHaveBeenCalledTimes(1);
  });

  it('deduplicates a busy read, allows cancellation and never submits a cancelled check', async () => {
    const first = deferred<ConversationUsageRecovery>();
    const inspect = vi.fn<UsageRecoveryPorts['inspect']>().mockReturnValueOnce(first.promise).mockResolvedValue(ready());
    const { node, api } = setup({ inspect });
    const control = button(node, 'Continue generating');
    control.focus(); control.click(); control.click();
    expect(inspect).toHaveBeenCalledTimes(1);
    expect(document.activeElement).toBe(control);
    const signal = inspect.mock.calls[0][2];
    button(node, 'Cancel').click();
    expect(signal.aborted).toBe(true);
    expect(phase(node)).toBe('idle');
    control.click();
    // The aborted read's late answer must not authorize anything: only the
    // live read may supply the budget this click confirms.
    first.resolve({ ...ready(), budget: { maxTokens: 99999, maxToolCalls: 0, durationSeconds: 1 } });
    await vi.waitFor(() => expect(api.recover).toHaveBeenCalledTimes(1));
    expect(inspect).toHaveBeenCalledTimes(2);
    expect(api.recover.mock.calls[0][2].budget).toEqual(ready().budget);
    expect(api.createCommandId).toHaveBeenCalledTimes(1);
  });

  it.each(['not_applicable', 'operation_unconfirmed', 'configuration_unavailable'] as const)(
    'never starts a continuation when the read is blocked by %s', async reason => {
      const { node, api } = setup({ inspect: vi.fn().mockResolvedValue({ ...ready(), state: 'blocked', reason }) });
      button(node, 'Continue generating').click();
      await vi.waitFor(() => expect(phase(node)).toBe('blocked'));
      expect(node.querySelector('[data-usage-recovery-budget]')).toBeNull();
      expect(api.recover).not.toHaveBeenCalled();
      expect(api.createCommandId).not.toHaveBeenCalled();
      // Re-reading cannot lift a permanent block, so the control is not
      // offered again; only a fixable configuration keeps it.
      expect(node.querySelectorAll('button')).toHaveLength(
        reason === 'configuration_unavailable' ? 1 : 0);
    });

  it.each([
    { conversationId: 'foreign' }, { sourceTurnId: 'foreign' }, { sourceProjectionRevision: 8 },
  ])('rejects a mismatched read %j rather than authorizing it', async mismatch => {
    const { node, api } = setup({ inspect: vi.fn().mockResolvedValue({ ...ready(), ...mismatch }) });
    button(node, 'Continue generating').click();
    await vi.waitFor(() => expect(['check-failed', 'changed']).toContain(phase(node)));
    expect(api.recover).not.toHaveBeenCalled();
  });

  it('re-reads the source instead of continuing a projection that already changed', async () => {
    const inspect = vi.fn<UsageRecoveryPorts['inspect']>()
      .mockResolvedValueOnce({ ...ready(), sourceProjectionRevision: 8 })
      .mockResolvedValue({ ...ready(), sourceProjectionRevision: 8 });
    const { node, api, render } = setup({ inspect });
    button(node, 'Continue generating').click();
    await vi.waitFor(() => expect(phase(node)).toBe('changed'));
    expect(api.recover).not.toHaveBeenCalled();
    render({ ...binding, projectionRevision: 8 });
    button(node, 'Continue generating').click();
    await vi.waitFor(() => expect(api.recover).toHaveBeenCalledTimes(1));
    expect(api.recover.mock.calls[0][2].expectedProjectionRevision).toBe(8);
  });

  it('allows trying again after a failed read without exposing raw server markup', async () => {
    const inspect = vi.fn<UsageRecoveryPorts['inspect']>().mockRejectedValueOnce(new Error('<script>private</script>')).mockResolvedValue(ready());
    const { node, api } = setup({ inspect });
    button(node, 'Continue generating').click();
    await vi.waitFor(() => expect(phase(node)).toBe('check-failed'));
    expect(node.textContent).not.toContain('private');
    expect(node.querySelector('script')).toBeNull();
    expect(api.recover).not.toHaveBeenCalled();
    button(node, 'Continue generating').click();
    await vi.waitFor(() => expect(api.recover).toHaveBeenCalledTimes(1));
  });
});

describe('mutation delivery uncertainty and lifecycle', () => {
  it('reuses a frozen command and body after lost ACK, even across a source repaint', async () => {
    const recover = vi.fn<UsageRecoveryPorts['recover']>().mockRejectedValueOnce(new Error('connection lost')).mockResolvedValue(accepted());
    const response = ready();
    const { node, api, render } = setup({ recover, inspect: vi.fn().mockResolvedValue(response) });
    button(node, 'Continue generating').click();
    await vi.waitFor(() => expect(phase(node)).toBe('unknown'));
    const sent = recover.mock.calls[0][2];
    expect(Object.isFrozen(sent)).toBe(true);
    expect(Object.isFrozen(sent.budget)).toBe(true);
    response.budget.maxTokens = 999;
    render({ ...binding, projectionRevision: 8 });
    expect(recover).toHaveBeenCalledTimes(1);
    expect(api.inspect).toHaveBeenCalledTimes(1);
    expect(api.onAccepted).not.toHaveBeenCalled();
    button(node, 'Retry the same request').click();
    await vi.waitFor(() => expect(phase(node)).toBe('accepted'));
    expect(recover.mock.calls[1][2]).toBe(sent);
    expect(sent.budget.maxTokens).toBe(12345);
    expect(sent.expectedProjectionRevision).toBe(7);
    expect(api.createCommandId).toHaveBeenCalledTimes(1);
  });

  it('keeps keyboard focus through the single gesture and a refusal', async () => {
    const pending = deferred<ConversationUsageRecovery>();
    const { node } = setup({ inspect: vi.fn().mockReturnValue(pending.promise),
      recover: vi.fn().mockRejectedValue({ status: 409 }) });
    const control = button(node, 'Continue generating');
    control.focus(); control.click();
    pending.resolve(ready());
    await vi.waitFor(() => expect(phase(node)).toBe('refused'));
    expect(document.activeElement).toBe(button(node, 'Continue generating'));
  });

  it('requires a fresh read after a definitive refusal', async () => {
    const recover = vi.fn<UsageRecoveryPorts['recover']>().mockRejectedValueOnce({ status: 409 }).mockResolvedValue(accepted());
    const { node, api } = setup({ recover });
    button(node, 'Continue generating').click();
    await vi.waitFor(() => expect(phase(node)).toBe('refused'));
    expect(node.textContent).not.toContain('Retry the same request');
    expect(api.inspect).toHaveBeenCalledTimes(1);
    button(node, 'Continue generating').click();
    await vi.waitFor(() => expect(api.recover).toHaveBeenCalledTimes(2));
    expect(api.inspect).toHaveBeenCalledTimes(2);
  });

  it.each(['dispose', 'identity-change'] as const)('aborts the read and ignores its late result after %s', async change => {
    const pending = deferred<ConversationUsageRecovery>();
    const inspect = vi.fn<UsageRecoveryPorts['inspect']>().mockReturnValue(pending.promise);
    const { node, api, render } = setup({ inspect });
    button(node, 'Continue generating').click();
    const signal = inspect.mock.calls[0][2];
    if (change === 'dispose') { disposeUsageRecoveryNotice(node); node.textContent = 'Different conversation'; }
    else render({ ...binding, turnId: 'new-turn' });
    expect(signal.aborted).toBe(true);
    const before = node.textContent;
    pending.resolve(ready());
    await Promise.resolve(); await Promise.resolve();
    expect(node.textContent).toBe(before);
    expect(api.recover).not.toHaveBeenCalled();
    expect(api.onAccepted).not.toHaveBeenCalled();
  });

  it('does not cancel POST or publish a late acceptance after disposal', async () => {
    const pending = deferred<TurnCommandResponse>();
    const recover = vi.fn<UsageRecoveryPorts['recover']>().mockReturnValue(pending.promise);
    const { node, api } = setup({ recover });
    button(node, 'Continue generating').click();
    await vi.waitFor(() => expect(phase(node)).toBe('submitting'));
    disposeUsageRecoveryNotice(node); node.textContent = 'Different conversation';
    pending.resolve(accepted());
    await Promise.resolve(); await Promise.resolve();
    expect(node.textContent).toBe('Different conversation');
    expect(api.onAccepted).not.toHaveBeenCalled();
    expect(recover).toHaveBeenCalledTimes(1);
  });
});

function terminalBlock(): Extract<ConversationBlockViewModel, { kind: 'activity-event' }> {
  return { kind: 'activity-event', blockId: 'terminal', identitySource: 'contract', source: {},
    terminalError: { kind: 'execution_token_usage_unknown', message: 'Usage unconfirmed', severity: 'warning',
      retryable: false, hint: '', detail: 'original evidence', model: '', context: '', source: '', raw: '' },
    value: { id: 'terminal', spanId: 'terminal', seq: 1, occurredAt: 1000, kind: 'error',
      status: 'failed', severity: 'warning', summary: 'Turn stopped', count: 1 } };
}
function sourceTurn(): ConversationTurnViewModel {
  return { turnId: binding.turnId, projectionRevision: 7, status: 'failed',
    source: { conversationId: binding.conversationId } } as ConversationTurnViewModel;
}

describe('concrete terminal receipt integration', () => {
  it.each([['en', en], ['zh', zh]] as const)('keeps diagnostics and all consent consequences in %s', async (_language, labels) => {
    const { node, ports } = setup();
    ports.localizedText = (key, fallback, values) => ((labels as Record<string, string>)[key] ?? fallback)
      .replace(/\{(\w+)\}/g, (token, key: string) => String(values?.[key] ?? token));
    const block = terminalBlock(); const turn = sourceTurn();
    renderActivityEventBlock(node, block, ports, turn);
    expect(node.querySelector('.activity-event__copy-diagnostics')).not.toBeNull();
    const control = button(node, labels['usageRecovery.check']);
    for (const key of ['usageRecovery.cost', 'usageRecovery.preserve', 'usageRecovery.scope'] as const) {
      expect(node.textContent).toContain(labels[key]);
    }
    control.focus();
    renderActivityEventBlock(node, block, ports, turn);
    expect(button(node, labels['usageRecovery.check'])).toBe(control);
    expect(document.activeElement).toBe(control);
    expect(labels['err.k.execution_token_usage_unknown.hint']).not.toMatch(/maintainer|维护者|核对供应商/);
    expect(labels['usageRecovery.check']).not.toMatch(/[0-9]/);
  });

  it('never advertises recovery on unrelated errors, live turns or receipts without source identity', () => {
    const { node, ports, api } = setup();
    for (const status of ['pending', 'running'] as const) {
      renderActivityEventBlock(node, terminalBlock(), ports, { ...sourceTurn(), status });
      expect(phase(node)).toBeUndefined();
    }
    const unrelated = terminalBlock(); unrelated.terminalError!.kind = 'generic';
    renderActivityEventBlock(node, unrelated, ports, sourceTurn());
    expect(phase(node)).toBeUndefined();
    renderActivityEventBlock(node, terminalBlock(), ports);
    expect(phase(node)).toBeUndefined();
    expect(api.inspect).not.toHaveBeenCalled();
  });

  it('classic renderer disposal reaches the pending read and retires its controls', async () => {
    const pending = deferred<ConversationUsageRecovery>();
    const inspect = vi.fn<UsageRecoveryPorts['inspect']>().mockReturnValue(pending.promise);
    const { node, api, ports } = setup({ inspect });
    const renderers = createClassicConversationRenderers(ports);
    renderers.renderBlock!(node, terminalBlock(), { turn: sourceTurn() } as never);
    button(node, 'Continue generating').click();
    renderers.disposeBlock!(node);
    expect(inspect.mock.calls[0][2].aborted).toBe(true);
    pending.resolve(ready());
    await Promise.resolve(); await Promise.resolve();
    expect(phase(node)).toBeUndefined();
    expect(api.recover).not.toHaveBeenCalled();
  });
});
