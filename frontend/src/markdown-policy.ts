/**
 * Responsibility: install the application's Markdown parser policy without
 * owning rendering, sanitization, highlighting, or DOM transforms.
 * Entry point: installMarkdownPolicy. Dependencies: an injected Marked port
 * and optional failure reporter.
 */

import type {
  MarkedExtension,
  MarkedOptions,
  TokenizerThis,
} from 'marked';

export interface MarkdownPolicyRuntime {
  setOptions(options: MarkedOptions): unknown;
  use(...extensions: MarkedExtension[]): unknown;
}

export type MarkdownPolicyFailureReporter = (error: unknown) => void;

function reportMarkdownPolicyFailure(error: unknown): void {
  console.warn('[Markdown] failed to install strict-del tokenizer:', error);
}

const strictGfmDeleteExtension: MarkedExtension = {
  tokenizer: {
    del(this: TokenizerThis, source: string) {
      const match = /^(~~+)(?=[^\s~])([\s\S]*?[^\s~])\1(?=[^~]|$)/
        .exec(source);
      // `false` asks Marked to call its previous tokenizer, which is exactly
      // the permissive single-tilde rule this policy replaces.
      if (!match) return undefined;
      return {
        type: 'del',
        raw: match[0],
        text: match[2],
        tokens: this.lexer.inlineTokens(match[2]),
      };
    },
  },
};

/** Configure one Marked runtime. False means the tokenizer extension failed. */
export function installMarkdownPolicy(
  runtime: MarkdownPolicyRuntime,
  onFailure: MarkdownPolicyFailureReporter = reportMarkdownPolicyFailure,
): boolean {
  runtime.setOptions({ breaks: true });
  try {
    runtime.use(strictGfmDeleteExtension);
    return true;
  } catch (error) {
    onFailure(error);
    return false;
  }
}
