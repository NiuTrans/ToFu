// @vitest-environment jsdom
import { describe, expect, it } from 'vitest';
import { createErrorEnvelopePresentation } from './error-presentation';
import type { Translator } from './i18n';

const translate = ((key: string) => key === 'err.k.internal.title'
  ? 'Internal error'
  : key === 'err.k.internal.hint' ? 'Check server logs.' : key) as Translator;
const presentation = createErrorEnvelopePresentation({ translate });

describe('error envelope text used by Toast callers', () => {
  it('keeps a public occurrence detail beside a generic localized title', () => {
    expect(presentation.errorEnvelopeMessage({
      kind: 'internal', message: 'Internal error',
      detail: 'ContractViolation: turn.status', source: 'conversation-sync',
    })).toBe('Internal error: ContractViolation: turn.status');
  });

  it('uses guidance when the backend has no occurrence detail', () => {
    expect(presentation.errorEnvelopeMessage({
      kind: 'internal', message: 'Internal error',
      hint: 'Reload the conversation.', source: 'conversation-sync',
    })).toBe('Internal error: Reload the conversation.');
  });

  it('gives a useful fallback without exposing JSON for a partial envelope', () => {
    expect(presentation.errorEnvelopeMessage({
      kind: 'internal', message: 'Internal error',
    })).toBe('Internal error: Check server logs.');
    expect(presentation.errorEnvelopeMessage({
      kind: 'internal', message: 'Internal error', detail: 'turn.status: required',
    })).toBe('Internal error: turn.status: required');
  });
});


describe('subscription sign-in recovery', () => {
  it.each(['oauth_reauth', 'internal'])('opens subscription settings for %s failures', kind => {
    const html = presentation.renderErrorEnvelope({kind, message: 'Internal error',
      detail: 'local request preparation failed: Codex subscription not logged in (no valid OAuth token)',
      titleKey: `err.k.${kind}.title`, hintKey: `err.k.${kind}.hint`});
    const host = document.createElement('div');
    host.innerHTML = html;
    const button = host.querySelector('button');
    expect(button?.textContent).toBe('err.subscription.signIn');
    expect(button?.getAttribute('data-tofu-action')).toBe("jumpToSetting('oauth')");
    expect(host.textContent).not.toContain('Check server logs.');
  });

  it('does not offer sign-in for unrelated internal errors', () => {
    const html = presentation.renderErrorEnvelope({kind: 'internal', message: 'Internal error',
      detail: 'TypeError: body conversion failed'});
    expect(html).not.toContain('error-block-signin-btn');
  });
});
