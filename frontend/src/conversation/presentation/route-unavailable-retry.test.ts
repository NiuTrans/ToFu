import { describe, expect, it } from 'vitest';
import en from '../../i18n/locales/en.json';
import zh from '../../i18n/locales/zh.json';
import { liveAttemptPresentation } from './view-model/live-turns';
import {
  presentConversationRateLimit,
  presentLivePhaseLabel,
  type LivePhaseText,
} from './live-phase';

const gatewayMessage = '分组 default 下模型 wire-alpha 无可用渠道（distributor）';
const locales = [
  { language: 'en', messages: en, cause: 'No gateway channel' },
  { language: 'zh', messages: zh, cause: '暂无可用网关渠道' },
] as const;

function localizedText(messages: Readonly<Record<string, string>>): LivePhaseText {
  return (key, fallback, values) => (messages[key] || fallback).replace(/\{(\w+)\}/g,
    (match, name) => String(values?.[name] ?? match));
}

describe.each(locales)('gateway no-channel retry notice ($language)', ({ language, messages, cause }) => {
  const phase = liveAttemptPresentation({
    phase: 'retrying',
    statusCode: 503,
    detail: 'Retrying… Model route unavailable: legacy',
    detailKey: 'stream.phase.retryRouteUnavailable',
    detailArgs: { model: 'gpt-6.1-sol', attempt: 2, message: gatewayMessage },
  }, 'running');

  it('names the cause, model and gateway message in the UI language', () => {
    const label = presentLivePhaseLabel(phase, localizedText(messages));
    expect(label).toContain(cause);
    expect(label).toContain('gpt-6.1-sol');
    expect(label).toContain(gatewayMessage);
    expect(label).not.toMatch(/\{\w+\}/);
    if (language === 'zh') expect(label).not.toContain('Model route unavailable');
  });

  it('is a route hold, never the rate-limit chip', () => {
    expect(presentConversationRateLimit(phase)).toBeNull();
  });
});
