import DOMPurify from 'dompurify';
// highlight.js ships as core + per-language grammars. The eager set below is
// the high-frequency chat subset; every remaining `common` grammar registers
// on demand via ensureHljsLanguage() so the main chunk stops paying for
// grammars a session may never render.
import hljs from 'highlight.js/lib/core';
import hljsBash from 'highlight.js/lib/languages/bash';
import hljsCss from 'highlight.js/lib/languages/css';
import hljsDiff from 'highlight.js/lib/languages/diff';
import hljsIni from 'highlight.js/lib/languages/ini';
import hljsJavascript from 'highlight.js/lib/languages/javascript';
import hljsJson from 'highlight.js/lib/languages/json';
import hljsMarkdown from 'highlight.js/lib/languages/markdown';
import hljsPlaintext from 'highlight.js/lib/languages/plaintext';
import hljsPython from 'highlight.js/lib/languages/python';
import hljsShell from 'highlight.js/lib/languages/shell';
import hljsSql from 'highlight.js/lib/languages/sql';
import hljsTypescript from 'highlight.js/lib/languages/typescript';
import hljsXml from 'highlight.js/lib/languages/xml';
import hljsYaml from 'highlight.js/lib/languages/yaml';
import * as marked from 'marked';
// The code-highlight theme rides the main chunk alongside hljs itself; this
// replaces the legacy static/vendor/github-dark.min.css stylesheet link.
import 'highlight.js/styles/github-dark.css';

hljs.registerLanguage('python', hljsPython);
hljs.registerLanguage('javascript', hljsJavascript);
hljs.registerLanguage('typescript', hljsTypescript);
hljs.registerLanguage('bash', hljsBash);
hljs.registerLanguage('shell', hljsShell);
hljs.registerLanguage('json', hljsJson);
hljs.registerLanguage('yaml', hljsYaml);
hljs.registerLanguage('markdown', hljsMarkdown);
hljs.registerLanguage('xml', hljsXml);
hljs.registerLanguage('css', hljsCss);
hljs.registerLanguage('sql', hljsSql);
hljs.registerLanguage('diff', hljsDiff);
hljs.registerLanguage('ini', hljsIni);
hljs.registerLanguage('plaintext', hljsPlaintext);

const lazyHljsLanguageNames = new Set([
  'c', 'cpp', 'csharp', 'go', 'graphql', 'java', 'kotlin', 'less', 'lua',
  'makefile', 'objectivec', 'perl', 'php', 'php-template', 'python-repl', 'r',
  'ruby', 'rust', 'scala', 'scss', 'swift', 'vbnet', 'wasm',
]);

let lazyHljsChunkPromise: Promise<typeof import('./hljs-lazy-langs')> | undefined;

/** Register a lazy highlight.js grammar exactly once; false when unknown. */
export function ensureHljsLanguage(lang: string): Promise<boolean> {
  if (hljs.getLanguage(lang)) return Promise.resolve(true);
  if (!lazyHljsLanguageNames.has(lang)) return Promise.resolve(false);
  lazyHljsChunkPromise ??= import('./hljs-lazy-langs');
  return lazyHljsChunkPromise.then(({ lazyHljsGrammars }) => {
    const grammar = lazyHljsGrammars[lang as keyof typeof lazyHljsGrammars];
    if (!grammar || hljs.getLanguage(lang)) return Boolean(grammar);
    hljs.registerLanguage(lang, grammar);
    return true;
  }).catch(() => false);
}

export { DOMPurify, hljs, marked };

export type KatexModule = typeof import('katex');
export type Html2Canvas = typeof import('html2canvas')['default'];

let katexPromise: Promise<KatexModule> | undefined;
let html2canvasPromise: Promise<Html2Canvas> | undefined;

export function loadKatex(): Promise<KatexModule> {
  // KaTeX's stylesheet travels with its lazy chunk: Vite extracts the CSS and
  // injects it before the chunk evaluates, replacing the legacy eager
  // static/vendor/katex/katex.min.css link on the shell page.
  katexPromise ??= Promise.all([
    import('katex'),
    import('katex/dist/katex.min.css'),
  ]).then(([module]) => module);
  return katexPromise;
}

export function loadHtml2Canvas(): Promise<Html2Canvas> {
  html2canvasPromise ??= import('html2canvas').then((module) => module.default);
  return html2canvasPromise;
}
