import { describe, expect, it } from 'vitest';

import {
  detectCodeLanguage,
  escapeCodeText,
  highlightCodeToHtml,
  HIGHLIGHT_MAX_CHARS,
} from './code-highlight';

describe('escapeCodeText', () => {
  it('escapes markup-significant characters', () => {
    expect(escapeCodeText('<b>&"x"</b>')).toBe(
      '&lt;b&gt;&amp;&quot;x&quot;&lt;/b&gt;');
  });
});

describe('highlightCodeToHtml', () => {
  it('highlights json keys, strings, numbers and literals', () => {
    const html = highlightCodeToHtml(
      '{"a": 1, "b": true, "c": null, "d": "x"}', 'json');
    expect(html).toContain('<span class="tok-key">&quot;a&quot;</span>');
    expect(html).toContain('<span class="tok-number">1</span>');
    expect(html).toContain('<span class="tok-literal">true</span>');
    expect(html).toContain('<span class="tok-literal">null</span>');
    expect(html).toContain('<span class="tok-string">&quot;x&quot;</span>');
  });

  it('escapes malicious input inside json strings', () => {
    const html = highlightCodeToHtml('{"x": "<script>"}', 'json');
    expect(html).not.toContain('<script>');
    expect(html).toContain('&lt;script&gt;');
  });

  it('highlights javascript comments, strings and keywords', () => {
    const html = highlightCodeToHtml(
      'const r = await tools.call("read_files"); // done', 'javascript');
    expect(html).toContain('<span class="tok-keyword">const</span>');
    expect(html).toContain('<span class="tok-keyword">await</span>');
    expect(html).toContain(
      '<span class="tok-string">&quot;read_files&quot;</span>');
    expect(html).toContain('<span class="tok-comment">// done</span>');
  });

  it('highlights python comments, keywords and strings', () => {
    const html = highlightCodeToHtml(
      "def f():\n    return 'ok'  # tail", 'python');
    expect(html).toContain('<span class="tok-keyword">def</span>');
    expect(html).toContain('<span class="tok-keyword">return</span>');
    expect(html).toContain('<span class="tok-string">\'ok\'</span>');
    expect(html).toContain('<span class="tok-comment"># tail</span>');
  });

  it('does not mark identifiers containing digits as numbers', () => {
    const html = highlightCodeToHtml('abc123 = 7', 'javascript');
    expect(html).toContain('abc123');
    expect(html).not.toContain('tok-number">123');
    expect(html).toContain('<span class="tok-number">7</span>');
  });

  it('returns plain escaped text for unknown languages', () => {
    expect(highlightCodeToHtml('a < b', 'yaml')).toBe('a &lt; b');
  });

  it('returns plain escaped text beyond the highlight budget', () => {
    const big = `{"x": "${'y'.repeat(HIGHLIGHT_MAX_CHARS)}"}`;
    expect(highlightCodeToHtml(big, 'json')).toBe(escapeCodeText(big));
  });
});

describe('detectCodeLanguage', () => {
  it('detects exact json', () => {
    expect(detectCodeLanguage('  {"a": 1}')).toBe('json');
    expect(detectCodeLanguage('[1, 2]')).toBe('json');
  });

  it('rejects json lookalikes and falls back to hints', () => {
    expect(detectCodeLanguage('{not json}', 'javascript')).toBe('javascript');
    expect(detectCodeLanguage('plain text')).toBe('');
  });
});
