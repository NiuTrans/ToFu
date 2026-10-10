// @vitest-environment jsdom

import { describe, expect, it } from 'vitest';

import {
  buildToolFallbackCard,
  toolFallbackStatusTone,
} from './tool-fallback-presentation';

describe('toolFallbackStatusTone', () => {
  it('maps statuses onto tones', () => {
    expect(toolFallbackStatusTone('done')).toBe('ok');
    expect(toolFallbackStatusTone('completed')).toBe('ok');
    expect(toolFallbackStatusTone('error')).toBe('err');
    expect(toolFallbackStatusTone('rejected')).toBe('err');
    expect(toolFallbackStatusTone('running')).toBe('active');
    expect(toolFallbackStatusTone('searching')).toBe('active');
    expect(toolFallbackStatusTone('')).toBe('muted');
    expect(toolFallbackStatusTone('mystery')).toBe('muted');
  });
});

describe('buildToolFallbackCard', () => {
  it('renders title, status chip and highlighted sections', () => {
    const card = buildToolFallbackCard(document, {
      title: 'read_files',
      status: 'done',
      sections: [{ label: 'Result', text: '{"lines": 12}' }],
    });
    expect(card.tagName).toBe('DETAILS');
    expect(card.querySelector('.conversation-tool-card__title')?.textContent)
      .toBe('read_files');
    const chip = card.querySelector('.conversation-tool-card__chip');
    expect(chip?.textContent).toBe('done');
    expect(chip?.className).toContain('conversation-tool-card__chip--ok');
    const code = card.querySelector('.conversation-tool-card__pre code');
    expect(code?.innerHTML).toContain('tok-key');
    expect(code?.innerHTML).toContain('tok-number');
  });

  it('escapes hostile content end to end', () => {
    const card = buildToolFallbackCard(document, {
      title: 'x',
      status: 'error',
      sections: [{ label: 'Result', text: '{"x": "<img src=x>"}' }],
    });
    expect(card.innerHTML).not.toContain('<img');
    expect(card.querySelector('code')?.textContent).toContain('<img src=x>');
  });

  it('renders child tool chips and backend badge for program cards', () => {
    const card = buildToolFallbackCard(document, {
      title: 'Program orchestration',
      status: 'completed',
      badges: ['native_openai'],
      childTools: ['read_files', 'web_search'],
      sections: [
        { label: 'Program', text: 'return 1;', language: 'toolscript' },
      ],
    });
    expect(card.querySelector('.conversation-tool-card__badge')?.textContent)
      .toBe('native_openai');
    const children = card.querySelectorAll('.conversation-tool-card__child');
    expect(children.length).toBe(2);
    expect(card.querySelector('.conversation-tool-card__children-label')
      ?.textContent).toBe('2 calls');
    expect(card.querySelector('code')?.innerHTML)
      .toContain('<span class="tok-keyword">return</span>');
  });

  it('skips empty sections and unknown statuses stay muted', () => {
    const card = buildToolFallbackCard(document, {
      title: 'tool',
      status: 'waiting',
      sections: [{ label: 'Input', text: '' },
        { label: 'Result', text: 'plain output' }],
    });
    const sections = card.querySelectorAll(
      '.conversation-tool-card__section');
    expect(sections.length).toBe(1);
    expect(card.querySelector('.conversation-tool-card__chip')?.className)
      .toContain('--muted');
    // Plain text renders verbatim without token spans.
    expect(card.querySelector('code')?.innerHTML).toBe('plain output');
  });
});
