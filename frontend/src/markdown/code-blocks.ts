/** Single-pass code DOM projection with an empty scratch tree after each call.
 * Entry point: createMarkdownCodeProjection. Dependencies: ports.
 * Lazy grammar completion upgrades this renderer's mounted, terminal code only.
 */
import type { MarkdownRenderEnvironment, MarkdownRendererPorts } from './ports';
import { selectionIntersects } from './deferred-enrichment';

const applyExcludedLanguages = new Set(['bash', 'shell', 'sh', 'console', 'terminal', 'cmd', 'powershell', 'zsh']);
let rendererSequence = 0;

export function createMarkdownCodeProjection(ports: MarkdownRendererPorts, invalidateCache: () => void,
  defer: (document: Document, job: () => boolean) => void) {
  const owner = String(++rendererSequence);
  const pendingLanguages = new Map<string, Promise<void>>();
  let scratch: HTMLDivElement | null = null;
  let disposed = false;
  const report = (error: unknown) => ports.reportFailure?.(error);

  function requestLanguage(language: string, environment: MarkdownRenderEnvironment): void {
    if (!ports.ensureHighlightLanguage || pendingLanguages.has(language)) return;
    const request = Promise.resolve().then(() => disposed ? false : ports.ensureHighlightLanguage!(language)).then(ready => {
      if (!ready || disposed) return;
      const current = ports.getEnvironment();
      if (current.document !== environment.document
        || current.highlighter !== environment.highlighter) return;
      invalidateCache();
      const highlighter = environment.highlighter;
      if (!highlighter?.getLanguage(language)) return;
      if (!environment.document) return;
      defer(environment.document, () => {
        const current = ports.getEnvironment();
        if (current.document !== environment.document || current.highlighter !== highlighter) return true;
        let selected = false;
        for (const code of environment.document!.querySelectorAll<HTMLElement>(
          `code[data-markdown-renderer="${owner}"]`)) {
          if (!code.isConnected || code.dataset.markdownStreaming === 'true'
            || !code.classList.contains(`language-${language}`)) continue;
          if (selectionIntersects(code)) { selected = true; continue; }
          try { code.innerHTML = highlighter.highlight(code.textContent ?? '', { language }).value; }
          catch (error) { report(error); }
          delete code.dataset.markdownRenderer;
        }
        return !selected;
      });
    }).catch(report).finally(() => pendingLanguages.delete(language));
    pendingLanguages.set(language, request);
  }

  return {
    owner,
    render(html: string, environment: MarkdownRenderEnvironment, streaming: boolean) {
      html = html.replace(/<pre><code class="language-([\w+-]+)[^"]*">/g,
        '<pre><div class="code-header"><span>$1</span><button class="copy-btn" data-tofu-action="copyCode(this)">Copy</button></div><code class="language-$1">')
        .replace(/<pre><code class="hljs">/g,
          '<pre><div class="code-header"><span>code</span><button class="copy-btn" data-tofu-action="copyCode(this)">Copy</button></div><code>')
        .replace(/<pre><code>/g,
          '<pre><div class="code-header"><span>code</span><button class="copy-btn" data-tofu-action="copyCode(this)">Copy</button></div><code>');
      if (!html.includes('<pre') || !environment.document) return { html, cacheable: true };
      if (!scratch || scratch.ownerDocument !== environment.document) scratch = environment.document.createElement('div');
      const node = scratch;
      let cacheable = true;
      try {
        node.innerHTML = html;
        for (const pre of node.querySelectorAll('pre')) {
          const code = pre.querySelector('code');
          if (!code) continue;
          const highlighter = environment.highlighter;
          if (highlighter && !streaming) {
            const language = code.className.match(/language-([\w+-]+)/)?.[1];
            const text = code.textContent ?? '';
            try {
              if (language && highlighter.getLanguage(language)) {
                code.innerHTML = highlighter.highlight(text, { language }).value;
              } else {
                code.innerHTML = highlighter.highlightAuto(text).value;
                if (language && ports.ensureHighlightLanguage
                  && (ports.canEnsureHighlightLanguage?.(language) ?? true)) {
                  code.dataset.markdownRenderer = owner;
                  code.dataset.markdownStreaming = 'false';
                  cacheable = false;
                  requestLanguage(language, environment);
                }
              }
            } catch (error) { cacheable = false; report(error); }
            code.classList.add('hljs');
          }
          const header = pre.querySelector('.code-header');
          if (environment.projectActive && header) {
            const language = header.querySelector('span')?.textContent?.trim().toLowerCase() ?? '';
            if (!applyExcludedLanguages.has(language) && (code.textContent ?? '').trim().split('\n').length > 3) {
              const button = environment.document.createElement('button');
              button.className = 'apply-btn'; button.textContent = 'Apply';
              button.setAttribute('data-tofu-action', 'openApplyModal(this)'); header.append(button);
            }
          }
          const lineCount = (code.textContent ?? '').split('\n').length;
          if (lineCount > 15) {
            pre.classList.add('code-long'); pre.dataset.collapsed = 'true';
            if (header) {
              const title = header.querySelector('span');
              if (title) title.textContent += ` · ${lineCount} lines`;
              const button = environment.document.createElement('button');
              button.className = 'code-collapse-btn'; button.textContent = 'Expand';
              button.setAttribute('data-tofu-action', 'toggleCodeBlock(this)');
              header.insertBefore(button, header.querySelector('.copy-btn'));
            }
          }
        }
        return { html: node.innerHTML, cacheable };
      } finally { node.replaceChildren(); }
    },
    dispose(): void { disposed = true; pendingLanguages.clear(); scratch?.replaceChildren(); scratch = null; },
  };
}
