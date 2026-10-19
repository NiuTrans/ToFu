// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest';
import { renderPublishedFile, renderScriptWarning } from './artifact-delivery';

describe('published file panel presenters', () => {
  it('uses inert metadata and a native factory download action, never model markup', () => {
    const host = document.createElement('div'); const download = vi.fn();
    renderPublishedFile(host, { filename: '<img src=x onerror=alert(1)>.pdf', mime: 'application/pdf', sizeBytes: 42, sha256: 'a'.repeat(64) }, { download });
    expect(host.querySelector('img,iframe,script,[onerror]')).toBeNull();
    expect(host.textContent).toContain('<img src=x onerror=alert(1)>.pdf');
    const control = host.querySelector('button')!;
    expect(control.type).toBe('button'); expect(control.classList.contains('ui-btn')).toBe(true);
    expect(download).not.toHaveBeenCalled(); control.click(); expect(download).toHaveBeenCalledOnce();
    expect(host.querySelectorAll('dd')).toHaveLength(4);
  });
  it('requires an explicit native action and explains code and network risks', () => {
    const host = document.createElement('div'); const allow = vi.fn();
    renderScriptWarning(host, allow);
    expect(host.textContent).toMatch(/untrusted code/); expect(host.textContent).toMatch(/network requests/);
    const button = host.querySelector('button')!;
    expect(allow).not.toHaveBeenCalled(); button.click(); expect(allow).toHaveBeenCalledOnce();
    expect(host.querySelector('[role=note]')).not.toBeNull();
  });
});
