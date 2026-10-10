/** Prepare prose while fenced/inline code stays verbatim and math stays typed.
 * Entry points: prepareMarkdownSource. Dependencies: fences, table-repair. No ambient application state.
 */
import { extractFencedBlocks, upgradeFenceIfNeeded } from './fences';
import { repairTablePipes } from './table-repair';
export interface MarkdownMathFragment { readonly tex: string; readonly display: boolean }

const _CJK_PUNCT_CLASS =
  '[\u3000-\u303F\uFE30-\uFE4F\uFE50-\uFE6B\uFF01-\uFF0F\uFF1A-\uFF20\uFF3B-\uFF40\uFF5B-\uFF65]';
/* A homogeneous delimiter run (spec terminology). We keep runs homogeneous
 * (*+ | _+ | ~+) — do NOT mix, per CommonMark. */
const _EMPH_RUN_CLASS = '(?:\\*+|_+|~+)';
/* "Letter or number" using Unicode property classes — covers Latin, CJK,
 * Greek, digits, etc. but NOT punctuation/whitespace. */
const _LETTER_OR_NUM = '[\\p{L}\\p{N}]';
/* CJK_PUNCT before EMPH_RUN, with a letter immediately AFTER the run.
 * Matches `」**开` but NOT `)**：`. */
const _CJK_FRIENDLY_BEFORE_RE = new RegExp(
  '(' + _CJK_PUNCT_CLASS + ')(' + _EMPH_RUN_CLASS + ')(?=' + _LETTER_OR_NUM + ')', 'gu');
/* Letter before EMPH_RUN, then CJK_PUNCT.  Avoids JS lookbehind (Safari
 * <16.4) by capturing the letter and re-emitting it.  Matches `点**：` but
 * NOT `)**：`. */
const _CJK_FRIENDLY_AFTER_RE = new RegExp(
  '(' + _LETTER_OR_NUM + ')(' + _EMPH_RUN_CLASS + ')(' + _CJK_PUNCT_CLASS + ')', 'gu');

function preprocessCjkEmphasis(text: string): string {
  /* Fast reject: skip unless text contains both a CJK-range char AND a delimiter. */
  if (!/[\u3000-\uFFEF]/.test(text)) return text;
  if (!/[*_~]/.test(text)) return text;
  return text
    .replace(_CJK_FRIENDLY_BEFORE_RE, '$1\u200B$2')
    .replace(_CJK_FRIENDLY_AFTER_RE, '$1$2\u200B$3');
}


export function prepareMarkdownSource(text: string, escapeRawHtml: boolean) {
  const codeStore: string[] = [];
  let p = extractFencedBlocks(text, codeStore);
  const mathStore: MarkdownMathFragment[] = [];
  /* NOTE: Backtick spans are intentionally NOT inspected for "LaTeX-looking"
   * content.  In Markdown a backtick span is always code — full stop.  Code
   * is extracted to \x02CODE\x03 placeholders HERE, before any $…$ / \(…\) /
   * $$…$$ / \[…\] math detection runs below, so a `$` (or `\d`, `_foo`, `^{`)
   * inside backticks can never be mis-parsed as math.  An earlier override
   * reached into backticks to re-route "math-shaped" spans to KaTeX; it
   * corrupted ordinary code/regex (e.g. `r'\d+ : \d+'`, `_RG_MATCH_LINE`)
   * into garbled subscripts and was removed.  Models that want typeset math
   * must emit $…$ / \(…\), not backticks.
   * See tests/frontend/test_frontend_markdown_backtick_code.py. */
  p = p.replace(/(`[^`\n]+`)/g, (m) => {
    codeStore.push(m);
    return "\x02CODE" + (codeStore.length - 1) + "\x03";
  });
  p = p.replace(/\$\$([\s\S]*?)\$\$/g, (_, t) => {
    mathStore.push({ tex: t.trim(), display: true });
    return "\x02MATH" + (mathStore.length - 1) + "\x03";
  });
  p = p.replace(/\\\[([\s\S]*?)\\\]/g, (_, t) => {
    mathStore.push({ tex: t.trim(), display: true });
    return "\x02MATH" + (mathStore.length - 1) + "\x03";
  });
  // No lookbehind (Safari <16.4 compat) — safe because $$ blocks
  //   are already extracted above, so no $$ sequences remain.
  // FIX: [^$\\\n|] excludes newlines AND pipes — prevents $ in table cells
  //   (e.g. $2.28) from matching across rows/paragraphs/columns and
  //   destroying table structure.  For | in math, use \vert or \mid.
  p = p.replace(/\$(?!\$)((?:[^$\\\n|]|\\.)+?)\$(?!\$)/g, (_, t) => {
    mathStore.push({ tex: t.trim(), display: false });
    return "\x02MATH" + (mathStore.length - 1) + "\x03";
  });
  p = p.replace(/\\\((.*?)\\\)/g, (_, t) => {
    mathStore.push({ tex: t.trim(), display: false });
    return "\x02MATH" + (mathStore.length - 1) + "\x03";
  });
  /* Human-authored input is plain text, not HTML: escape every '<' so marked
   * can no longer treat '<tag>' sequences as raw inline HTML (they would be
   * swallowed — DOMPurify strips unknown tags — or worse, render as markup).
   * Runs while code spans/fences and math are still \x02 placeholders, so
   * code content keeps its literal '<' (marked escapes it once inside <code>).
   * Only '<' is escaped: a bare '>' is harmless text in HTML, and escaping it
   * would kill the '> quote' blockquote syntax. Pre-typed '&lt;' entities
   * stay untouched and still render as '<'. */
  if (escapeRawHtml) {
    p = p.replace(/</g, '&lt;');
  }
  /* FIX: CJK-friendly emphasis — insert U+200B between emphasis delimiters and
   * adjacent CJK punctuation so marked's stock flanking rules detect emphasis.
   * Runs while code/math are still placeholders → code blocks are untouched. */
  p = preprocessCjkEmphasis(p);
  for (let i = 0; i < codeStore.length; i++) {
    p = p
      .split("\x02CODE" + i + "\x03")
      .join(upgradeFenceIfNeeded(codeStore[i]));
  }

  /* FIX: Normalize missing spaces in Markdown structural markers.
   * CommonMark requires a space after #/##/### and after list markers (- * +).
   * LLMs (especially with CJK output) often omit the space:
   *   "###标题"  → should be "### 标题"
   *   "-项目一"  → should be "- 项目一"
   * Without the space, marked.js treats them as plain text.
   * Only applied to line-start patterns to avoid false positives in mid-line text. */
  p = p.replace(/^(\s{0,3}#{1,6})([^\s#])/gm, '$1 $2');
  p = p.replace(/^(\s*[-*+])([^\s\-*+\d])/gm, '$1 $2');
  p = p.replace(/^(\s*\d+\.)([^\s])/gm, '$1 $2');

  /* FIX: Repair Markdown table rows with extra unescaped pipes.
   * LLMs sometimes generate cell content like "82|181" where the pipe is
   * literal, not a column separator.  marked.js interprets it as an extra
   * column, causing misaligned ("串列") rendering.
   *
   * Strategy: detect the separator row (| --- | --- |) to learn the expected
   * column count, then for data rows with too many pipes, merge the excess
   * cells back together using the escaped pipe "&#124;".  */
  p = repairTablePipes(p);
  return { source: p, math: mathStore };
}
