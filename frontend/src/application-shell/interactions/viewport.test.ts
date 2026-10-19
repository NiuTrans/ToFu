// @vitest-environment jsdom
import { expect, it, vi } from 'vitest';
import { installViewportHeightGuard } from './viewport';

it('coalesces mobile keyboard geometry, preserves reader intent and retires every delivery', () => {
  const visual = new EventTarget() as VisualViewport;
  let height = 800;
  Object.defineProperty(visual, 'height', { get: () => height });
  vi.stubGlobal('visualViewport', visual);
  const frames: FrameRequestCallback[] = [];
  vi.spyOn(window, 'requestAnimationFrame').mockImplementation(callback => { frames.push(callback); return frames.length; });
  vi.spyOn(window, 'cancelAnimationFrame').mockImplementation(() => {});
  const follow = vi.fn();
  const input = document.createElement('textarea'); input.id = 'userInput';
  input.scrollIntoView = vi.fn(); document.body.append(input); input.focus();
  const previous = document.body.style.height;
  const dispose = installViewportHeightGuard(window, document, {
    isMobileViewport: () => true, maintainConversationFollow: follow,
  });
  const flush = () => frames.splice(0).forEach(callback => callback(0));
  try {
    flush(); follow.mockClear();
    height = 400;
    for (let i = 0; i < 30; i += 1) visual.dispatchEvent(new Event('resize'));
    expect(frames).toHaveLength(1);
    flush();
    expect(document.body.style.height).toBe('400px');
    expect(input.scrollIntoView).toHaveBeenCalledTimes(1);
    expect(follow).toHaveBeenCalledTimes(1);
    height = 800; visual.dispatchEvent(new Event('resize')); flush();
    expect(document.body.style.height).toBe('800px');
    expect(follow).toHaveBeenCalledTimes(2);
    // Disposal must fence callbacks even if native cancellation fails.
    height = 300; visual.dispatchEvent(new Event('resize')); dispose(); flush();
    visual.dispatchEvent(new Event('resize')); flush();
    expect(document.body.style.height).toBe(previous);
    expect(follow).toHaveBeenCalledTimes(2);
  } finally { dispose(); input.remove(); vi.restoreAllMocks(); vi.unstubAllGlobals(); }
});
