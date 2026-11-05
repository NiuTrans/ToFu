/** Tool presenters preserve their untrusted-payload and text-coercion policies. */
import { describe, expect, it, vi } from 'vitest';
import type { Translator } from '../../i18n';
import { createToolApprovalPresentation } from './tool-approval-presentation';
import { createToolHumanGuidancePresentation } from './tool-human-guidance-presentation';
import { createToolInjectionPresentation } from './tool-injection-presentation';

const translate: Translator = (key, ..._args) => key;
const approval = createToolApprovalPresentation({ translate });
const guidance = createToolHumanGuidancePresentation({ translate, renderMarkdown: text => text });
const injection = createToolInjectionPresentation({
  translate, renderMarkdown: text => text, iconHtml: () => '', resolveConversationTitle: () => '',
});
const header = { iconHtml: '', queryHtml: '' };
const cases = [
  {
    name: 'approval',
    round: { status: 'pending_approval', approvalId: 'id' },
    render: (round: unknown) => approval.renderApprovalHtml(round, header),
    text: (value: unknown) => approval.renderApprovalHtml({
      status: 'pending_approval', approvalId: 'id', approvalMeta: { command: value },
    }, header),
  },
  {
    name: 'guidance',
    round: { status: 'awaiting_human', guidanceId: 'id' },
    render: (round: unknown) => guidance.renderGuidanceHtml(round, header),
    text: (value: unknown) => guidance.renderGuidanceHtml({
      status: 'awaiting_human', guidanceId: 'id', guidanceQuestion: value,
    }, header),
  },
  {
    name: 'injection',
    round: { _userSteerInject: true },
    render: (round: unknown) => injection.renderInjectionHtml(round),
    text: (value: unknown) => injection.renderInjectionHtml({
      _userSteerInject: true, steerPreviews: [{ text: value }],
    }),
  },
];

describe.each(cases)('$name payload boundary', ({ round, render, text }) => {
  it('rejects arrays even when they carry recognizable named properties', () => {
    expect(render(Object.assign([], round))).toBe('');
    expect(render(null)).toBe('');
  });

  it('reads inherited fields without changing the payload', () => {
    const inherited = Object.freeze(Object.create(round));
    expect(render(inherited)).toBe(render(round));
    expect(Object.keys(inherited)).toEqual([]);
  });

  it('treats throwing field access as absent metadata', () => {
    const unavailable = new Proxy({}, { get() { throw new Error('unavailable'); } });
    expect(render(unavailable)).toBe('');
  });

  it.each([0, false, 123n])('retains scalar text %s', value => {
    expect(text(value)).toBe(text(String(value)));
  });
});

it('keeps approval object coercion separate from guidance and injection scalar text', () => {
  const stringify = vi.fn(() => 'coerced-value');
  const value = { toString: stringify };
  expect(cases[0].text(value)).toBe(cases[0].text('coerced-value'));
  expect(stringify).toHaveBeenCalledTimes(1);
  stringify.mockClear();
  for (const owner of cases.slice(1)) {
    expect(owner.text(value)).toBe(owner.text(''));
  }
  expect(stringify).not.toHaveBeenCalled();
});

it('approval tolerates a throwing text conversion', () => {
  const value = { toString() { throw new Error('unavailable'); } };
  expect(cases[0].text(value)).toBe(cases[0].text(''));
});
