import { describe, expect, it } from 'vitest';
import en from '../../i18n/locales/en.json';
import zh from '../../i18n/locales/zh.json';
import { liveAttemptPresentation } from './view-model/live-turns';
import {
  presentConversationRateLimit,
  presentLivePhaseLabel,
  type LivePhaseText,
} from './live-phase';

const model = 'gpt-6-astra';
const locales = [
  { language: 'en', messages: en, connecting: 'Connecting to', waiting: 'Waiting for' },
  { language: 'zh', messages: zh, connecting: '正在连接', waiting: '等待' },
] as const;

const unsupportedClaims = /\bsent\b|\baccepted\b|已发送|已送达|已受理|\battempt\s*\d+|第\s*\d+\s*次|切换|\bswitch(?:ing)?\b/i;

function localizedText(messages: Readonly<Record<string, string>>): LivePhaseText {
  return (key, fallback, values) => {
    const args = { ...values };
    if (typeof args.reasonKey === 'string' && messages[args.reasonKey]) {
      args.reason = messages[args.reasonKey];
    }
    return (messages[key] || fallback).replace(/\{(\w+)\}/g,
      (match, name) => String(args[name] ?? match));
  };
}

describe.each(locales)('network recovery presentation ($language)', ({ messages, connecting, waiting }) => {
  const text = localizedText(messages);

  it('localizes a network wait without a rate-limit, attempt or switch claim', () => {
    const phase = {
      phase: 'retrying', attempt: 0,
      detail: `Retrying… Waiting for network recovery (${model}, attempt 0)`,
      detailKey: 'stream.phase.retryCooldownWait', detailArgs: {
        reason: 'Waiting for network recovery',
        reasonKey: 'stream.retryReason.networkRecovery', model,
      },
    };
    expect(presentConversationRateLimit(phase)).toBeNull();
    const value = liveAttemptPresentation(phase, 'running');
    const label = presentLivePhaseLabel(value, text);
    expect(label).toContain(messages['stream.retryReason.networkRecovery']);
    expect(label).toContain(model);
    expect(label).not.toMatch(unsupportedClaims);
  });

  it('clears the egress wait on recovery without claiming a model request was sent', () => {
    // Recovery is a pre-POST route notification, not provider acceptance.
    const phase = {
      phase: 'retrying', attempt: 0,
      detail: `Retrying… Network recovered (${model}, attempt 0)`,
      detailKey: 'stream.phase.connectingModel', detailArgs: { model },
    };
    expect(presentConversationRateLimit(phase)).toBeNull();
    const value = liveAttemptPresentation(phase, 'running');
    expect(value.detailArgs).toEqual({ model });
    const label = presentLivePhaseLabel(value, text);
    expect(label).toContain(connecting);
    expect(label).toContain(model);
    expect(label).not.toContain(messages['stream.retryReason.networkRecovery']);
    expect(label).not.toMatch(/network route|网络出口|Network recovered/);
    expect(label).not.toMatch(unsupportedClaims);

    const generating = liveAttemptPresentation({
      phase: 'llm_thinking', detailKey: 'stream.phase.generatingResponse',
    }, 'running');
    expect(presentLivePhaseLabel(generating, text)).toBe(messages['stream.phase.generatingResponse']);
    expect(presentLivePhaseLabel(generating, text)).not.toContain(model);
  });

  it('keeps the logical model-start label neutral before dispatch', () => {
    const value = liveAttemptPresentation({
      phase: 'waiting_model',
      detail: `Sent to ${model}, waiting for it to start replying…`,
      detailKey: 'stream.phase.waitingForModel', detailArgs: { model },
    }, 'running');
    const label = presentLivePhaseLabel(value, text);
    expect(label).toContain(waiting);
    expect(label).toContain(model);
    expect(label).not.toMatch(unsupportedClaims);
  });

  it('does not infer dispatch from a bare waiting_model phase either', () => {
    const value = liveAttemptPresentation({ phase: 'waiting_model' }, 'running');
    const label = presentLivePhaseLabel(value, text);
    expect(label).toBe(messages['stream.phase.waitingModel']);
    expect(label).toContain(waiting);
    expect(label).not.toMatch(unsupportedClaims);
  });
});
