// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest';
import { closeAttachmentPreview, showAttachmentPreview } from './attachment-preview-lifecycle';
afterEach(() => { vi.useRealTimers(); document.body.replaceChildren(); });
function fixture() {
  document.body.innerHTML = '<button id="trigger">Open</button><input id="userInput">'
    + '<div id="previewModal"><div id="previewBody"><button class="preview-close-btn">Close</button><img></div></div>';
  const trigger = document.getElementById('trigger')!;
  trigger.focus();
  return { trigger, modal: document.getElementById('previewModal')!, body: document.getElementById('previewBody')! };
}
describe('existing attachment preview lifecycle', () => {
  it('focuses the close control, restores the trigger and cancels stale cleanup after reopening', () => {
    vi.useFakeTimers();
    const { trigger, modal, body } = fixture();
    showAttachmentPreview(document, '附件预览');
    expect(document.activeElement).toBe(body.querySelector('button'));
    expect(modal.getAttribute('aria-label')).toBe('附件预览');
    closeAttachmentPreview(document);
    expect(document.activeElement).toBe(trigger);
    showAttachmentPreview(document);
    vi.advanceTimersByTime(350);
    expect(body.querySelector('img')).not.toBeNull();
    expect(modal.classList.contains('open')).toBe(true);
    closeAttachmentPreview(document);
    expect(document.activeElement).toBe(trigger);
    vi.advanceTimersByTime(350);
    expect(body.childNodes.length).toBe(0);
  });
  it('returns to the composer if streaming retired the trigger', () => {
    vi.useFakeTimers();
    const { trigger } = fixture();
    showAttachmentPreview(document);
    trigger.remove();
    closeAttachmentPreview(document);
    expect(document.activeElement?.id).toBe('userInput');
    vi.runAllTimers();
  });
});
