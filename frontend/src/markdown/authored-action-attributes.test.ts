// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest';
import DOMPurify from 'dompurify';
import { marked } from 'marked';
import { installActionRegistry } from '../action-registry';
import { createMarkdownRenderer } from './index';

const parser = { parse: (source: string) => marked.parse(source, { async: false }) as string };

function renderAssistantMarkdown(source: string): HTMLElement {
  const renderer = createMarkdownRenderer({
    maximumCacheEntries: 8,
    getEnvironment: () => ({
      document, parser, sanitizer: DOMPurify, highlighter: null,
      mathRenderer: null, basePath: '', projectActive: false,
    }),
    ensureMath: () => undefined,
  });
  const host = document.createElement('div');
  host.className = 'md-content';
  host.innerHTML = renderer.render(source);
  renderer.dispose();
  document.body.append(host);
  return host;
}

const uninstallers: Array<() => void> = [];
afterEach(() => {
  uninstallers.splice(0).forEach(uninstall => uninstall());
  document.body.replaceChildren();
});

describe('model-authored delegated action attributes', () => {
  it('cannot run application actions or window methods from load/error/click events', () => {
    const resolve = vi.fn(() => vi.fn());
    const open = vi.spyOn(window, 'open').mockImplementation(() => null);
    uninstallers.push(() => open.mockRestore(), installActionRegistry(resolve));
    const host = renderAssistantMarkdown([
      '<img src="https://unreachable.invalid/pixel.png" alt="chart"'
        + ' data-tofu-action-error="forgedAction(\'current\')"'
        + ' data-tofu-action-load="forgedAction(\'current\')">',
      '',
      '<button data-tofu-action="forgedAction(\'current\')">Continue</button>',
      '',
      '<span data-tofu-action-click="this.ownerDocument.defaultView.open(\'https://evil.invalid\')">Details</span>',
    ].join('\n'));

    const image = host.querySelector('img');
    expect(image).not.toBeNull();
    image!.dispatchEvent(new Event('error'));
    image!.dispatchEvent(new Event('load'));
    host.querySelector('button')?.click();
    host.querySelector('span')?.click();

    expect(resolve).not.toHaveBeenCalled();
    expect(open).not.toHaveBeenCalled();
    expect(host.querySelector('[data-tofu-action],[data-tofu-action-error],[data-tofu-action-load],[data-tofu-action-click]'))
      .toBeNull();
  });

  it('keeps renderer-owned jump and copy controls', () => {
    const jump = vi.fn();
    const copy = vi.fn();
    uninstallers.push(installActionRegistry(name => (
      name === 'jumpToPage' ? jump : name === 'copyCode' ? copy : undefined)));
    const host = renderAssistantMarkdown('[Open memory](page:memory)\n\n```js\nconst a = 1;\n```');

    const jumpButton = host.querySelector<HTMLButtonElement>('button.md-ui-jump');
    expect(jumpButton?.textContent).toBe('Open memory');
    jumpButton!.click();
    expect(jump).toHaveBeenCalledWith('memory');
    host.querySelector<HTMLButtonElement>('.code-header .copy-btn')!.click();
    expect(copy).toHaveBeenCalledOnce();
  });
});
