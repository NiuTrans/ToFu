// @vitest-environment jsdom
/** Human authorization and delivery uncertainty on one terminal failure card. */
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
    onAccepted: vi.fn(), ...overrides,
  };
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

describe('explicit budget authorization', () => {
  it('makes checking read-only and confirms exact numeric budget only on the second explicit action', async () => {
    const pending = deferred<TurnCommandResponse>();
    const recover = vi.fn<UsageRecoveryPorts['recover']>().mockReturnValue(pending.promise);
    const { node, api, render } = setup({ recover });
    expect(api.inspect).not.toHaveBeenCalled();
    expect(recover).not.toHaveBeenCalled();
    button(node, 'Check and continue').click();
    await vi.waitFor(() => expect(phase(node)).toBe('ready'));
    expect(api.inspect).toHaveBeenCalledWith(binding.conversationId, binding.turnId, expect.any(AbortSignal));
    expect(node.querySelector('[data-usage-recovery-budget]')?.textContent).toContain('12345 tokens, 9 tool calls and 321 seconds');
    expect(node.textContent).toContain('additional model and tool charges');
    expect(node.textContent).toContain('does not fill in or settle');
    expect(node.textContent).toContain('does not resend the earlier request or resume the earlier Goal or Flow');
    expect(recover).not.toHaveBeenCalled();
    expect(api.createCommandId).not.toHaveBeenCalled();
    const confirm = button(node, 'Confirm and continue');
    confirm.focus(); confirm.click(); confirm.click(); render();
    expect(recover).toHaveBeenCalledTimes(1);
    expect(confirm.getAttribute('aria-disabled')).toBe('true');
    expect(document.activeElement).toBe(confirm);
    expect(recover).toHaveBeenCalledWith(binding.conversationId, binding.turnId, {
      commandId: 'one-confirmed-command', expectedProjectionRevision: 7, confirmed: true, budget: ready().budget,
    });
    pending.resolve(accepted());
    await vi.waitFor(() => expect(phase(node)).toBe('accepted'));
    expect(api.onAccepted).toHaveBeenCalledWith(binding.conversationId);
    button(node, 'Continuation accepted').click();
    expect(recover).toHaveBeenCalledTimes(1);
  });

  it('deduplicates a busy read, allows cancellation and fences the cancelled late ready result', async () => {
    const first = deferred<ConversationUsageRecovery>();
    const inspect = vi.fn<UsageRecoveryPorts['inspect']>().mockReturnValueOnce(first.promise).mockResolvedValue(ready());
    const { node, api, render } = setup({ inspect });
    const check = button(node, 'Check and continue');
    check.focus(); check.click(); check.click(); render();
    expect(inspect).toHaveBeenCalledTimes(1);
    expect(document.activeElement).toBe(check);
    const signal = inspect.mock.calls[0][2];
    button(node, 'Cancel').click();
    expect(signal.aborted).toBe(true);
    button(node, 'Check and continue').click();
    first.resolve({ ...ready(), budget: { maxTokens: 99999, maxToolCalls: 0, durationSeconds: 1 } });
    await vi.waitFor(() => expect(phase(node)).toBe('ready'));
    expect(node.textContent).not.toContain('99999');
    button(node, 'Cancel').click();
    expect(phase(node)).toBe('idle');
    expect(api.recover).not.toHaveBeenCalled();
    expect(api.createCommandId).not.toHaveBeenCalled();
  });

  it.each(['not_applicable', 'operation_unconfirmed', 'configuration_unavailable'] as const)(
    'never offers authorization when the check is blocked by %s', async reason => {
      const { node, api } = setup({ inspect: vi.fn().mockResolvedValue({ ...ready(), state: 'blocked', reason }) });
      button(node, 'Check and continue').click();
      await vi.waitFor(() => expect(phase(node)).toBe('blocked'));
      expect(node.textContent).not.toContain('Confirm and continue');
      expect(node.querySelector('[data-usage-recovery-budget]')).toBeNull();
      expect(api.recover).not.toHaveBeenCalled();
    });

  it.each([
    { conversationId: 'foreign' }, { sourceTurnId: 'foreign' }, { sourceProjectionRevision: 8 },
  ])('rejects a mismatched check %j rather than authorizing it', async mismatch => {
    const { node, api } = setup({ inspect: vi.fn().mockResolvedValue({ ...ready(), ...mismatch }) });
    button(node, 'Check and continue').click();
    await vi.waitFor(() => expect(['check-failed', 'changed']).toContain(phase(node)));
    expect(node.textContent).not.toContain('Confirm and continue');
    expect(api.recover).not.toHaveBeenCalled();
  });

  it('invalidates an unconfirmed review when the source revision changes, without new reads or writes', async () => {
    const { node, api, render } = setup();
    button(node, 'Check and continue').click();
    await vi.waitFor(() => expect(phase(node)).toBe('ready'));
    const oldConfirm = button(node, 'Confirm and continue');
    render({ ...binding, projectionRevision: 8 });
    oldConfirm.click();
    expect(phase(node)).toBe('idle');
    expect(api.inspect).toHaveBeenCalledTimes(1);
    expect(api.recover).not.toHaveBeenCalled();
  });

  it('allows explicit rechecking after a failed read without exposing raw server markup', async () => {
    const inspect = vi.fn<UsageRecoveryPorts['inspect']>().mockRejectedValueOnce(new Error('<script>private</script>')).mockResolvedValue(ready());
    const { node, api } = setup({ inspect });
    button(node, 'Check and continue').click();
    await vi.waitFor(() => expect(phase(node)).toBe('check-failed'));
    expect(node.textContent).not.toContain('private');
    expect(node.querySelector('script')).toBeNull();
    expect(api.recover).not.toHaveBeenCalled();
    button(node, 'Check and continue').click();
    await vi.waitFor(() => expect(phase(node)).toBe('ready'));
    expect(api.recover).not.toHaveBeenCalled();
  });
});

describe('mutation delivery uncertainty and lifecycle', () => {
  it('reuses a frozen command and body after lost ACK, even across a source repaint', async () => {
    const recover = vi.fn<UsageRecoveryPorts['recover']>().mockRejectedValueOnce(new Error('connection lost')).mockResolvedValue(accepted());
    const response = ready();
    const { node, api, render } = setup({ recover, inspect: vi.fn().mockResolvedValue(response) });
    button(node, 'Check and continue').click();
    await vi.waitFor(() => expect(phase(node)).toBe('ready'));
    button(node, 'Confirm and continue').click();
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

  it('keeps keyboard focus through check, review, confirmation and refusal', async () => {
    const pending = deferred<ConversationUsageRecovery>();
    const { node } = setup({ inspect: vi.fn().mockReturnValue(pending.promise),
      recover: vi.fn().mockRejectedValue({ status: 409 }) });
    const check = button(node, 'Check and continue');
    check.focus(); check.click();
    expect(document.activeElement).toBe(check);
    pending.resolve(ready());
    await vi.waitFor(() => expect(phase(node)).toBe('ready'));
    const confirm = button(node, 'Confirm and continue');
    expect(document.activeElement).toBe(confirm);
    confirm.click();
    await vi.waitFor(() => expect(phase(node)).toBe('refused'));
    expect(document.activeElement).toBe(button(node, 'Check and continue'));
  });

  it('requires a fresh read and confirmation after a definitive refusal', async () => {
    const recover = vi.fn<UsageRecoveryPorts['recover']>().mockRejectedValueOnce({ status: 409 }).mockResolvedValue(accepted());
    const { node, api } = setup({ recover });
    button(node, 'Check and continue').click();
    await vi.waitFor(() => expect(phase(node)).toBe('ready'));
    button(node, 'Confirm and continue').click();
    await vi.waitFor(() => expect(phase(node)).toBe('refused'));
    expect(node.textContent).not.toContain('Retry the same request');
    expect(api.inspect).toHaveBeenCalledTimes(1);
    button(node, 'Check and continue').click();
    await vi.waitFor(() => expect(phase(node)).toBe('ready'));
    expect(recover).toHaveBeenCalledTimes(1);
  });

  it.each(['dispose', 'identity-change'] as const)('aborts GET and ignores its late result after %s', async change => {
    const pending = deferred<ConversationUsageRecovery>();
    const inspect = vi.fn<UsageRecoveryPorts['inspect']>().mockReturnValue(pending.promise);
    const { node, api, render } = setup({ inspect });
    button(node, 'Check and continue').click();
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
    button(node, 'Check and continue').click();
    await vi.waitFor(() => expect(phase(node)).toBe('ready'));
    button(node, 'Confirm and continue').click();
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
    const { node, api, ports } = setup();
    ports.localizedText = (key, fallback, values) => ((labels as Record<string, string>)[key] ?? fallback)
      .replace(/\{(\w+)\}/g, (token, key: string) => String(values?.[key] ?? token));
    const block = terminalBlock(); const turn = sourceTurn();
    renderActivityEventBlock(node, block, ports, turn);
    expect(node.querySelector('.activity-event__copy-diagnostics')).not.toBeNull();
    const check = button(node, labels['usageRecovery.check']);
    check.click();
    await vi.waitFor(() => expect(phase(node)).toBe('ready'));
    for (const key of ['usageRecovery.cost', 'usageRecovery.preserve', 'usageRecovery.scope'] as const) {
      expect(node.textContent).toContain(labels[key]);
    }
    const confirm = button(node, labels['usageRecovery.confirm']);
    confirm.focus();
    renderActivityEventBlock(node, block, ports, turn);
    expect(button(node, labels['usageRecovery.confirm'])).toBe(confirm);
    expect(document.activeElement).toBe(confirm);
    button(node, labels['usageRecovery.cancel']).click();
    expect(api.recover).not.toHaveBeenCalled();
    expect(labels['err.k.execution_token_usage_unknown.hint']).not.toMatch(/maintainer|维护者|核对供应商/);
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
    button(node, 'Check and continue').click();
    renderers.disposeBlock!(node);
    expect(inspect.mock.calls[0][2].aborted).toBe(true);
    pending.resolve(ready());
    await Promise.resolve(); await Promise.resolve();
    expect(phase(node)).toBeUndefined();
    expect(api.recover).not.toHaveBeenCalled();
  });
});
