/** Markdown composition: exact cache identity, streaming policy and teardown.
 * Entry point: createMarkdownRenderer. Dependencies: ports, source preparation,
 * HTML/math projection, code projection and render cache. No ambient globals.
 */
import { escapeHtml } from '../html-safety';
import { createMarkdownRenderCache } from './render-cache';
import { createMarkdownCodeProjection } from './code-blocks';
import { prepareMarkdownSource } from './prepare-source';
import { projectMarkdownHtml, renderMathFragments, completePendingMath } from './html-policy';
import {
  findStreamingPrefixEnd, streamingPrefixApplies, type StreamingPrefixSnapshot,
} from './streaming-prefix';
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

  /* Reusable streaming prefix from the previous streaming frame. Only a
   * blank-line-aligned, fence-free, math-free stable prefix is ever recorded,
   * and only a terminal render clears it — a streaming frame never feeds the
   * terminal cache, so an approximation can never be frozen as final. */
  let streamingPrefix: StreamingPrefixSnapshot | null = null;

  function project(source: string, environment: MarkdownRenderEnvironment,
    streaming: boolean): { html: string; cacheable: boolean } {
    let html = projectMarkdownHtml(environment.parser!.parse(source), environment);
    const projectedCode = code.render(html, environment, streaming);
    html = projectedCode.html.replace(/<table>/g,
      '<div class="md-table-wrapper"><div class="table-header"><span>table</span><button class="copy-btn" data-tofu-action="copyTableMarkdown(this)">Copy</button></div><table>')
      .replace(/<\/table>/g, '</table></div>');
    return { html, cacheable: projectedCode.cacheable };
  }

  return {
    render(text: string, options: MarkdownOptions = {}): string {
      if (disposed) throw new Error('Markdown renderer has been disposed');
      if (!text) return '';
      try {
        const environment = ports.getEnvironment();
        if (previousEnvironment && previousEnvironment.document !== environment.document) enrichment.clear();
        if (previousEnvironment && !sameRenderEnvironment(previousEnvironment, environment)) {
          cache.clear();
          streamingPrefix = null;
        }
        previousEnvironment = environment;
        if (!environment.parser || !environment.sanitizer) return '<pre style="white-space:pre-wrap">' + escapeHtml(text) + '</pre>';
        const streaming = Boolean(options.streaming);
        const escaped = Boolean(options.escapeRawHtml);
        if (!streaming) {
          const cached = cache.get(text, escaped);
          if (cached !== undefined) return cached;
        }
        let prepared;
        let html: string;
        let cacheable: boolean;
        /* Streaming prefix reuse: render only the grown tail, prepending the
         * prior frame's verified stable-prefix HTML. Math-bearing prefixes
         * opt out (globally numbered markers), so no tail renumbering is ever
         * needed. */
        const reuse = streaming
          && streamingPrefix && streamingPrefixApplies(streamingPrefix, text, escaped)
          ? streamingPrefix : null;
        if (reuse) {
          prepared = prepareMarkdownSource(text.slice(reuse.sourcePrefix.length), escaped);
          const tail = project(prepared.source, environment, streaming);
          html = reuse.htmlPrefix + tail.html;
          cacheable = tail.cacheable;
        } else {
          prepared = prepareMarkdownSource(text, escaped);
          const whole = project(prepared.source, environment, streaming);
          html = whole.html;
          cacheable = whole.cacheable;
        }
        if (prepared.math.length && !environment.mathRenderer) ensureMath(environment);
        html = renderMathFragments(html, prepared.math, environment, streaming, code.owner);
        if (streaming) {
          /* Record the stable prefix's own rendering so the next frame only
           * projects the grown tail. The prefix is prepared/parsed/sanitized
           * in isolation here (O(stable prefix)), once per completed block
           * rather than once per frame; steady-state frames then reuse the
           * stored htmlPrefix and do O(grown tail) work. Math markers are
           * globally numbered across the whole source, so a stable prefix that
           * itself carries math is never reused (the tail's fragment indices
           * would shift). */
          const prefixEnd = findStreamingPrefixEnd(prepared.source);
          if (prefixEnd > 0 && !prepared.math.length) {
            const prefixSource = prepared.source.slice(0, prefixEnd);
            const prefixRender = project(prefixSource, environment, streaming);
            streamingPrefix = { escaped, sourcePrefix: prefixSource, htmlPrefix: prefixRender.html };
          } else {
            streamingPrefix = null;
          }
        } else if (cacheable) {
          streamingPrefix = null;
          cache.set(text, escaped, html);
        } else {
          streamingPrefix = null;
        }
        return html;
      } catch (error) { reportFailure(error); return '<pre>' + escapeHtml(text) + '</pre>'; }
    },
    clearCache(): void { cache.clear(); streamingPrefix = null; },
    cacheStats: () => cache.stats(),
    dispose(): void { disposed = true; cache.clear(); streamingPrefix = null; previousEnvironment = null; code.dispose(); enrichment.clear(); },
  };
}
