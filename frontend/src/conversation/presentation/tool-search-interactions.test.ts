// @vitest-environment jsdom
import { expect, it, vi } from 'vitest';
import { installActionRegistry } from '../../action-registry';
import { createToolSearchPresentation } from './tool-search-presentation';
import type { Translator } from '../../i18n';

it('search and engine disclosures update aria state while inspector links stay independent', () => {
  const translate = ((key: string) => key) as Translator;
  const presentation = createToolSearchPresentation({ translate, iconHtml: () => '' });
  document.body.innerHTML = presentation.renderSearchHtml({
    toolName: 'web_search', roundNum: 1,
    engineBreakdown: { google: [{ url: 'https://example.test', title: 'Result' }] },
  }, [{ title: 'Result', url: 'https://example.test', snippet: 'Synthetic snippet' }], {
    queryHtml: 'Synthetic query', iconHtml: '',
    rightControlsHtml: '<a class="ri-tool-anchor" href="#inspector">Inspect</a>',
  });
  const error = vi.spyOn(console, 'error').mockImplementation(() => {});
  const dispose = installActionRegistry(() => undefined);
  try {
    const header = document.querySelector<HTMLElement>('.ptool-results-header')!;
    header.click(); expect(header.getAttribute('aria-expanded')).toBe('true');
    expect(header.parentElement!.classList.contains('expanded')).toBe(true);
    header.click(); expect(header.getAttribute('aria-expanded')).toBe('false');
    const inspector = document.querySelector<HTMLAnchorElement>('.ri-tool-anchor')!;
    for (const open of [false, true]) {
      if (open) header.click();
      const click = new MouseEvent('click', { bubbles: true, cancelable: true });
      expect(inspector.dispatchEvent(click)).toBe(true);
      expect(click.defaultPrevented).toBe(false);
      expect(inspector.getAttribute('href')).toBe('#inspector');
      expect(header.getAttribute('aria-expanded')).toBe(String(open));
      expect(header.parentElement!.classList.contains('expanded')).toBe(open);
    }
    const engines = document.querySelector<HTMLElement>('.eb-toggle')!;
    engines.click(); expect(engines.getAttribute('aria-expanded')).toBe('true');
    engines.click(); expect(engines.getAttribute('aria-expanded')).toBe('false');
    expect(error).not.toHaveBeenCalled();
  } finally { dispose(); error.mockRestore(); }
});
