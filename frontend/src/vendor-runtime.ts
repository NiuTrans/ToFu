import DOMPurify from 'dompurify';
import hljs from 'highlight.js/lib/common';
import * as marked from 'marked';

export { DOMPurify, hljs, marked };

export type KatexModule = typeof import('katex');
export type Html2Canvas = typeof import('html2canvas')['default'];

let katexPromise: Promise<KatexModule> | undefined;
let html2canvasPromise: Promise<Html2Canvas> | undefined;

export function loadKatex(): Promise<KatexModule> {
  katexPromise ??= import('katex');
  return katexPromise;
}

export function loadHtml2Canvas(): Promise<Html2Canvas> {
  html2canvasPromise ??= import('html2canvas').then((module) => module.default);
  return html2canvasPromise;
}
