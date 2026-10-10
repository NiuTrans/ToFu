/** Markdown composition: exact cache identity, streaming policy and teardown.
 * Entry point: createMarkdownRenderer. Dependencies: ports, source preparation,
 * HTML/math projection, code projection and render cache. No ambient globals.
 */
import { escapeHtml } from '../html-safety';
import { createMarkdownRenderCache } from './render-cache';
import { createMarkdownCodeProjection } from './code-blocks';
import { prepareMarkdownSource } from './prepare-source';
import { projectMarkdownHtml, renderMathFragments, completePendingMath } from './html-policy';
import { sameRenderEnvironment, type MarkdownRendererPorts, type MarkdownRenderEnvironment, type MarkdownOptions } from './ports';
import { createDeferredEnrichment } from './deferred-enrichment';
export type { MarkdownRendererPorts, MarkdownRenderEnvironment, MarkdownOptions } from './ports';

export function createMarkdownRenderer(ports: MarkdownRendererPorts) {
  const cache = createMarkdownRenderCache(ports.maximumCacheEntries);
  const reportFailure = ports.reportFailure ?? ((error: unknown) => console.warn('Markdown rendering failed; showing escaped source', error));
  const enrichment = createDeferredEnrichment(reportFailure);
  const code = createMarkdownCodeProjection(ports, () => cache.clear(), enrichment.run);
  let previousEnvironment: MarkdownRenderEnvironment | null = null;
  let disposed = false;
  let mathLoad: Promise<void> | null = null;

  function ensureMath(environment: MarkdownRenderEnvironment): void {
    if (mathLoad) return;
    mathLoad = Promise.resolve().then(() => disposed ? undefined : ports.ensureMath()).then(() => {
      if (disposed) return;
      cache.clear();
      const current = ports.getEnvironment();
      if (current.document && current.document === environment.document) {
        enrichment.run(current.document, () => {
          const latest = ports.getEnvironment();
          return latest.document !== current.document || completePendingMath(latest, code.owner);
        });
      }
    }).catch(reportFailure).finally(() => { mathLoad = null; });
  }

  return {
    render(text: string, options: MarkdownOptions = {}): string {
      if (disposed) throw new Error('Markdown renderer has been disposed');
      if (!text) return '';
      try {
        const environment = ports.getEnvironment();
        if (previousEnvironment && previousEnvironment.document !== environment.document) enrichment.clear();
        if (previousEnvironment && !sameRenderEnvironment(previousEnvironment, environment)) cache.clear();
        previousEnvironment = environment;
        if (!environment.parser) return '<pre style="white-space:pre-wrap">' + escapeHtml(text) + '</pre>';
        const streaming = Boolean(options.streaming);
        const escaped = Boolean(options.escapeRawHtml);
        if (!streaming) {
          const cached = cache.get(text, escaped);
          if (cached !== undefined) return cached;
        }
        const prepared = prepareMarkdownSource(text, escaped);
        let html = projectMarkdownHtml(environment.parser.parse(prepared.source), environment);
        const projectedCode = code.render(html, environment, streaming);
        html = projectedCode.html.replace(/<table>/g,
          '<div class="md-table-wrapper"><div class="table-header"><span>table</span><button class="copy-btn" data-tofu-action="copyTableMarkdown(this)">Copy</button></div><table>')
          .replace(/<\/table>/g, '</table></div>');
        if (prepared.math.length && !environment.mathRenderer) ensureMath(environment);
        html = renderMathFragments(html, prepared.math, environment, streaming, code.owner);
        if (!streaming && projectedCode.cacheable) cache.set(text, escaped, html);
        return html;
      } catch (error) { reportFailure(error); return '<pre>' + escapeHtml(text) + '</pre>'; }
    },
    clearCache(): void { cache.clear(); },
    cacheStats: () => cache.stats(),
    dispose(): void { disposed = true; cache.clear(); previousEnvironment = null; code.dispose(); enrichment.clear(); },
  };
}
