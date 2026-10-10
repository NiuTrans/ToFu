/** Sanitized HTML navigation/asset policy and math projection.
 * Entry points: projectMarkdownHtml, renderMathFragments.
 * Dependencies: html-safety, prepare-source, ports. No persistent state.
 */
import { escapeHtml } from '../html-safety';
import type { MarkdownMathFragment } from './prepare-source';
import type { MarkdownRenderEnvironment } from './ports';
import { selectionIntersects } from './deferred-enrichment';

export function projectMarkdownHtml(parsed: string, environment: MarkdownRenderEnvironment): string {
  if (parsed.includes('setting:') || parsed.includes('page:')) {
    parsed = parsed.replace(
      /<a href="(setting|page):([A-Za-z0-9_-]+)"[^>]*>([\s\S]*?)<\/a>/g,
      (_, scheme, key, label) => '<button type="button" class="md-ui-jump" data-tofu-action="'
        + (scheme === 'page' ? 'jumpToPage' : 'jumpToSetting') + "('" + key + "')\">" + label + '</button>',
    );
  }
  let html = environment.sanitizer ? environment.sanitizer.sanitize(parsed) : parsed;
  if (environment.basePath) {
    html = html.replace(/(<(?:img|a)\b[^>]*?\s(?:src|href)=["'])(\/(?:api|static|uploads)\/)/g,
      '$1' + environment.basePath + '$2');
  }
  return html.replace(/<a\b[^>]*>/g, tag => {
    if (/\starget=/.test(tag)) return tag;
    const match = tag.match(/\shref="([^"]*)"/);
    if (!match || match[1].startsWith('#') || match[1].startsWith('mailto:')) return tag;
    return tag.replace(/^<a\b/, '<a target="_blank" rel="noopener noreferrer"');
  });
}

export function renderMathFragments(html: string, math: readonly MarkdownMathFragment[],
  environment: MarkdownRenderEnvironment, streaming: boolean, owner: string): string {
  for (let index = 0; index < math.length; index++) {
    const { tex, display } = math[index];
    const marker = `\x02MATH${index}\x03`;
    let rendered: string;
    if (!streaming && environment.mathRenderer) {
      try { rendered = environment.mathRenderer.renderToString(tex, { displayMode: display, throwOnError: false }); }
      catch { rendered = `<code class="math-error">${escapeHtml(tex)}</code>`; }
      if (display) html = html.split(`<p>${marker}</p>`).join(rendered);
    } else {
      rendered = `<code class="math-pending" data-markdown-math="${owner}" data-math-display="${display}" data-math-streaming="${streaming}">${escapeHtml(tex)}</code>`;
    }
    html = html.split(marker).join(rendered);
  }
  return html;
}

/** Complete only this renderer's mounted terminal math; current text wins. */
export function completePendingMath(environment: MarkdownRenderEnvironment, owner: string): boolean {
  const document = environment.document, renderer = environment.mathRenderer;
  if (!document || !renderer) return true;
  let selected = false;
  for (const code of document.querySelectorAll<HTMLElement>(`code[data-markdown-math="${owner}"]`)) {
    if (!code.isConnected || code.dataset.mathStreaming === 'true') continue;
    if (selectionIntersects(code)) { selected = true; continue; }
    const display = code.dataset.mathDisplay === 'true', tex = code.textContent ?? '';
    const template = document.createElement('template');
    try { template.innerHTML = renderer.renderToString(tex, { displayMode: display, throwOnError: false }); }
    catch { template.innerHTML = `<code class="math-error">${escapeHtml(tex)}</code>`; }
    const parent = code.parentElement;
    const target = display && parent?.tagName === 'P' && parent.childNodes.length === 1 ? parent : code;
    target.replaceWith(template.content);
  }
  return !selected;
}
