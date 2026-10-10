/**
 * Dependency-free syntax highlighting for tool result/code viewers.
 *
 * The generic tool-result viewer and the typed-surface fallback cards show
 * verbatim model/tool output. Raw `<pre>` dumps read as noise; a small
 * tokenizer that wraps tokens in `<span class="tok-*">` (colors owned by
 * `09-tool-terminal-details.css`) makes the dominant shapes — JSON result
 * envelopes and ToolScript programs — scannable without shipping a
 * highlighter dependency. Every token's text is HTML-escaped at emission,
 * so highlighted output is always safe to assign via `innerHTML`.
 *
 * Supported languages: 'json', 'javascript' (ToolScript), 'python', 'bash'.
 * Anything else returns plain escaped text — verbatim display stays
 * authoritative.
 */

const ESCAPE_MAP: Readonly<Record<string, string>> = {
  '&': '&amp;',
  '<': '&lt;',
  '>': '&gt;',
  '"': '&quot;',
};

export function escapeCodeText(text: string): string {
  return text.replace(/[&<>"]/g, (ch) => ESCAPE_MAP[ch] ?? ch);
}

type TokenKind = 'key' | 'string' | 'number' | 'literal' | 'comment'
  | 'keyword';

interface Token {
  kind: TokenKind | '';
  text: string;
}

const JSON_TOKEN_RE = /("(?:\\.|[^"\\])*")(\s*:)?|(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)|\b(true|false|null)\b|(\s+)|(.)/gys;

function tokenizeJson(text: string): Token[] {
  const tokens: Token[] = [];
  let match: RegExpExecArray | null;
  JSON_TOKEN_RE.lastIndex = 0;
  while ((match = JSON_TOKEN_RE.exec(text)) !== null) {
    const [raw, str, colon, num, literal, space] = match;
    if (str !== undefined) {
      tokens.push({ kind: colon ? 'key' : 'string', text: str });
      if (colon) tokens.push({ kind: '', text: colon });
    } else if (num !== undefined) {
      tokens.push({ kind: 'number', text: num });
    } else if (literal !== undefined) {
      tokens.push({ kind: 'literal', text: literal });
    } else if (space !== undefined) {
      tokens.push({ kind: '', text: space });
    } else {
      tokens.push({ kind: '', text: raw });
    }
    if (match.index === JSON_TOKEN_RE.lastIndex) {
      JSON_TOKEN_RE.lastIndex += 1;
    }
  }
  return tokens;
}

const JS_KEYWORDS = new Set([
  'await', 'async', 'function', 'return', 'const', 'let', 'var', 'if',
  'else', 'for', 'while', 'of', 'in', 'new', 'try', 'catch', 'finally',
  'throw', 'break', 'continue', 'yield', 'this', 'null', 'undefined',
  'true', 'false', 'typeof', 'instanceof',
]);

const PYTHON_KEYWORDS = new Set([
  'def', 'return', 'if', 'elif', 'else', 'for', 'while', 'in', 'import',
  'from', 'as', 'with', 'try', 'except', 'finally', 'raise', 'class',
  'pass', 'break', 'continue', 'lambda', 'None', 'True', 'False', 'and',
  'or', 'not', 'is', 'yield', 'await', 'async', 'print',
]);

interface WordLanguage {
  keywords: ReadonlySet<string>;
  lineComment: string;
  blockComment?: readonly [string, string];
  stringQuotes: readonly string[];
  tripleQuotes?: readonly string[];
}

function tokenizeWords(text: string, language: WordLanguage): Token[] {
  const tokens: Token[] = [];
  let index = 0;
  const length = text.length;
  const push = (kind: TokenKind | '', value: string) => {
    if (value) tokens.push({ kind, text: value });
  };
  while (index < length) {
    const rest = text.slice(index);
    if (language.lineComment && rest.startsWith(language.lineComment)) {
      const end = text.indexOf('\n', index);
      const stop = end < 0 ? length : end;
      push('comment', text.slice(index, stop));
      index = stop;
      continue;
    }
    if (language.blockComment && rest.startsWith(language.blockComment[0])) {
      const close = text.indexOf(language.blockComment[1],
        index + language.blockComment[0].length);
      const stop = close < 0 ? length : close + language.blockComment[1].length;
      push('comment', text.slice(index, stop));
      index = stop;
      continue;
    }
    const triple = (language.tripleQuotes ?? []).find((quote) => (
      rest.startsWith(quote)
    ));
    if (triple) {
      const close = text.indexOf(triple, index + triple.length);
      const stop = close < 0 ? length : close + triple.length;
      push('string', text.slice(index, stop));
      index = stop;
      continue;
    }
    const quote = language.stringQuotes.find((mark) => rest.startsWith(mark));
    if (quote) {
      let cursor = index + quote.length;
      while (cursor < length) {
        if (text[cursor] === '\\') {
          cursor += 2;
          continue;
        }
        if (text.startsWith(quote, cursor)) {
          cursor += quote.length;
          break;
        }
        if (quote !== '`' && text[cursor] === '\n') break;
        cursor += 1;
      }
      push('string', text.slice(index, Math.min(cursor, length)));
      index = Math.min(cursor, length);
      continue;
    }
    const char = text[index];
    if (/[0-9]/.test(char) && !/[A-Za-z_$]/.test(text[index - 1] ?? '')) {
      const numMatch = /^-?\d(?:[\d_]*\d)?(?:\.\d+)?(?:[eE][+-]?\d+)?\b/
        .exec(rest);
      if (numMatch) {
        push('number', numMatch[0]);
        index += numMatch[0].length;
        continue;
      }
    }
    if (/[A-Za-z_$]/.test(char)) {
      const wordMatch = /^[A-Za-z_$][\w$]*/.exec(rest);
      if (wordMatch) {
        const word = wordMatch[0];
        push(language.keywords.has(word) ? 'keyword' : '', word);
        index += word.length;
        continue;
      }
    }
    push('', char);
    index += 1;
  }
  return tokens;
}

const JAVASCRIPT_LANGUAGE: WordLanguage = {
  keywords: JS_KEYWORDS,
  lineComment: '//',
  blockComment: ['/*', '*/'],
  stringQuotes: ["'", '"', '`'],
};

const PYTHON_LANGUAGE: WordLanguage = {
  keywords: PYTHON_KEYWORDS,
  lineComment: '#',
  stringQuotes: ["'", '"'],
  tripleQuotes: ['"""', "'''"],
};

const BASH_LANGUAGE: WordLanguage = {
  keywords: new Set(),
  lineComment: '#',
  stringQuotes: ["'", '"'],
};

/** Upper bound on highlighted output; larger bodies render plain escaped. */
export const HIGHLIGHT_MAX_CHARS = 200_000;

/**
 * Return *text* as safe HTML with token spans, or plain escaped text when
 * the language is unknown or the input exceeds the highlight budget.
 */
export function highlightCodeToHtml(text: string, language: string): string {
  if (!text) return '';
  if (text.length > HIGHLIGHT_MAX_CHARS) return escapeCodeText(text);
  let tokens: Token[];
  switch (language) {
    case 'json':
      tokens = tokenizeJson(text);
      break;
    case 'javascript':
    case 'toolscript':
      tokens = tokenizeWords(text, JAVASCRIPT_LANGUAGE);
      break;
    case 'python':
      tokens = tokenizeWords(text, PYTHON_LANGUAGE);
      break;
    case 'bash':
    case 'shell':
      tokens = tokenizeWords(text, BASH_LANGUAGE);
      break;
    default:
      return escapeCodeText(text);
  }
  return tokens.map((token) => (token.kind
    ? `<span class="tok-${token.kind}">${escapeCodeText(token.text)}</span>`
    : escapeCodeText(token.text))).join('');
}

/**
 * Best-effort language guess for a tool-result body: exact JSON parse for
 * structured envelopes, otherwise the caller's hint (usually a tool name).
 */
export function detectCodeLanguage(text: string, hint = ''): string {
  const trimmed = text.trimStart();
  if (trimmed.startsWith('{') || trimmed.startsWith('[')) {
    if (trimmed.length <= HIGHLIGHT_MAX_CHARS) {
      try {
        JSON.parse(trimmed);
        return 'json';
      } catch {
        // Prefix resembled JSON; fall through to the hint.
      }
    }
  }
  const normalized = hint.trim().toLowerCase();
  if (normalized === 'json' || normalized === 'javascript'
    || normalized === 'toolscript' || normalized === 'python'
    || normalized === 'bash' || normalized === 'shell') {
    return normalized;
  }
  return '';
}
