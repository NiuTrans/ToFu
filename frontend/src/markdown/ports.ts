/** Render environment and optional-library ports; no effects or imports.
 * Entry points: MarkdownRenderEnvironment, MarkdownRendererPorts, MarkdownOptions.
 * Dependencies: DOM types only. Rendering never discovers application globals.
 */
export interface MarkdownOptions {
  readonly streaming?: boolean;
  readonly escapeRawHtml?: boolean;
}

export interface MarkdownParser { parse(source: string): string }
export interface MarkdownSanitizer { sanitize(html: string): string }
export interface MarkdownHighlighter {
  getLanguage(language: string): unknown;
  highlight(text: string, options: { language: string }): { value: string };
  highlightAuto(text: string): { value: string };
}
export interface MarkdownMathRenderer {
  renderToString(tex: string, options: { displayMode: boolean; throwOnError: boolean }): string;
}

export interface MarkdownRenderEnvironment {
  readonly document: Document | null;
  readonly parser: MarkdownParser | null;
  readonly sanitizer: MarkdownSanitizer | null;
  readonly highlighter: MarkdownHighlighter | null;
  readonly mathRenderer: MarkdownMathRenderer | null;
  readonly basePath: string;
  readonly projectActive: boolean;
}

export interface MarkdownRendererPorts {
  readonly getEnvironment: () => MarkdownRenderEnvironment;
  /** The composition boundary supplies its resource policy; no hidden ceiling. */
  readonly maximumCacheEntries: number;
  readonly ensureMath: () => void | Promise<unknown>;
  readonly ensureHighlightLanguage?: (language: string) => Promise<boolean>;
  readonly canEnsureHighlightLanguage?: (language: string) => boolean;
  readonly reportFailure?: (error: unknown) => void;
}

/** Every value that changes emitted HTML participates in cache admission. */
export function sameRenderEnvironment(a: MarkdownRenderEnvironment, b: MarkdownRenderEnvironment): boolean {
  return a.document === b.document && a.parser === b.parser && a.sanitizer === b.sanitizer
    && a.highlighter === b.highlighter && a.mathRenderer === b.mathRenderer
    && a.basePath === b.basePath && a.projectActive === b.projectActive;
}
