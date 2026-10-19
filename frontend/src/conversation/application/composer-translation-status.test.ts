// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest';
import { createComposerTranslationStatus } from './composer-translation-status';

afterEach(() => { vi.useRealTimers(); document.body.replaceChildren(); });
describe('composer translation progress', () => {
  it('ticks waiting time, preserves the draft and clears successful completion immediately', () => {
    vi.useFakeTimers();
    document.body.innerHTML = '<div class="input-box"><textarea>unsent draft</textarea></div>';
    let now = 0;
    const receipt = createComposerTranslationStatus({ document, now: () => now,
      activeConversationId: () => 'c1', label: key => key });
    const wait = receipt.begin('c1', 'send');
    expect(document.querySelector('[role="status"]')?.textContent).toContain('modelPending');
    now = 2300; vi.advanceTimersByTime(1000);
    expect(document.querySelector('[role="status"]')?.textContent).toContain('2.3s');
    receipt.finish(wait, 'completed', 'gpt-6-luna');
    expect(document.querySelector('[role="status"]')).toBeNull();
    expect((document.querySelector('textarea') as HTMLTextAreaElement).value).toBe('unsent draft');
    expect(vi.getTimerCount()).toBe(0);
    receipt.finish(wait, 'failed');
    receipt.refresh();
    vi.advanceTimersByTime(5000);
    expect(document.querySelector('[role="status"]')).toBeNull();
    receipt.destroy();
  });

  it('fences navigation and restores active pending receipts without idle timers', () => {
    vi.useFakeTimers();
    document.body.innerHTML = '<div class="input-box"></div>';
    let active = 'c1';
    const receipt = createComposerTranslationStatus({ document, now: () => 0,
      activeConversationId: () => active, label: key => key });
    const wait = receipt.begin('c1', 'turn');
    active = 'c2'; receipt.refresh();
    expect(document.querySelector('[role="status"]')).toBeNull();
    expect(vi.getTimerCount()).toBe(0);
    active = 'c1'; receipt.refresh();
    expect(document.querySelector('[role="status"]')).not.toBeNull();
    receipt.finish(wait, 'cancelled');
    expect(document.querySelector('[role="status"]')?.textContent).toContain('modelUnknown');
    expect(vi.getTimerCount()).toBe(0);
    receipt.destroy();
  });

  it('preserves useful failures and renders reported models as plain text', () => {
    vi.useFakeTimers();
    document.body.innerHTML = '<div class="input-box"></div>';
    const receipt = createComposerTranslationStatus({ document, now: () => 100,
      activeConversationId: () => 'c1', label: key => key });
    receipt.finish(receipt.begin('c1', 'turn'), 'failed', '<img src=x onerror=alert(1)>');
    expect(document.querySelector('[role="status"]')?.getAttribute('data-state')).toBe('failed');
    expect(document.querySelector('[role="status"]')?.textContent).toContain('<img');
    expect(document.querySelector('[role="status"] img')).toBeNull();
    expect(vi.getTimerCount()).toBe(0);
    receipt.destroy();
  });

  it.each(['older', 'newer'] as const)('keeps another concurrent wait when the %s request succeeds', order => {
    vi.useFakeTimers();
    document.body.innerHTML = '<div class="input-box"></div>';
    const receipt = createComposerTranslationStatus({ document, now: () => 0,
      activeConversationId: () => 'c1', label: key => key });
    const older = receipt.begin('c1', 'send');
    const newer = receipt.begin('c1', 'turn');
    receipt.finish(order === 'older' ? older : newer, 'completed', 'gpt-6-luna');
    expect(document.querySelector('[role="status"]')?.getAttribute('data-state')).toBe('pending');
    expect(document.querySelector('[role="status"]')?.textContent).toContain(
      order === 'older' ? 'translationWait.turn' : 'translationWait.send');
    expect(vi.getTimerCount()).toBe(1);
    receipt.finish(order === 'older' ? newer : older, 'completed');
    expect(document.querySelector('[role="status"]')).toBeNull();
    expect(vi.getTimerCount()).toBe(0);
    receipt.destroy();
  });

  it('does not resurrect completed work after navigation or destroyed work after late callbacks', () => {
    vi.useFakeTimers();
    document.body.innerHTML = '<div class="input-box"></div>';
    let active = 'c1';
    const receipt = createComposerTranslationStatus({ document, now: () => 0,
      activeConversationId: () => active, label: key => key });
    const wait = receipt.begin('c1', 'send');
    active = 'c2'; receipt.refresh();
    receipt.finish(wait, 'completed');
    active = 'c1'; receipt.refresh();
    expect(document.querySelector('[role="status"]')).toBeNull();
    const next = receipt.begin('c1', 'turn');
    receipt.destroy();
    receipt.finish(next, 'failed');
    receipt.begin('c1', 'send');
    receipt.refresh();
    expect(document.querySelector('[role="status"]')).toBeNull();
    expect(vi.getTimerCount()).toBe(0);
  });
});

it('keeps a newer failure visible when an older overlapping translation succeeds', () => {
  vi.useFakeTimers();
  document.body.innerHTML = '<div class="input-box"></div>';
  const receipt = createComposerTranslationStatus({ document, now: () => 0,
    activeConversationId: () => 'c1', label: key => key });
  const older = receipt.begin('c1', 'send');
  const newer = receipt.begin('c1', 'turn');
  receipt.finish(newer, 'failed');
  expect(document.querySelector('[role="status"]')?.getAttribute('data-state')).toBe('pending');
  receipt.finish(older, 'completed', 'gpt-6-luna');
  expect(document.querySelector('[role="status"]')?.getAttribute('data-state')).toBe('failed');
  expect(document.querySelector('[role="status"]')?.textContent).toContain('translationWait.failed');
  expect(vi.getTimerCount()).toBe(0);
  receipt.destroy();
});
