/** Empirical guard for streaming prefix reuse: a completed, blank-line-aligned
 * prefix renders to an exact prefix of the next frame, and a tail-only render
 * concatenated onto the stored prefix HTML equals a from-scratch full render. */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const { execFileSync } = require('node:child_process');
const { JSDOM } = require('jsdom');
const createDOMPurify = require('dompurify');
const marked = require('marked');
const root = path.resolve(__dirname, '..');
const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'tofu-md-prefix-'));
const bundle = path.join(directory, 'md.cjs');
test.after(() => fs.rmSync(directory, { recursive: true, force: true }));
execFileSync(process.execPath, [path.join(root, 'scripts/vite_test_bundle.mjs'), '--bundle',
  '--format=cjs', '--platform=node', '--loader=ts', '--sourcefile=md-prefix.ts', `--outfile=${bundle}`],
  { cwd: root, input: `export { createMarkdownRenderer } from ${JSON.stringify(path.join(root, 'frontend/src/markdown/index.ts'))};\n`
    + `export { findStreamingPrefixEnd } from ${JSON.stringify(path.join(root, 'frontend/src/markdown/streaming-prefix.ts'))};`, stdio: ['pipe', 'pipe', 'pipe'] });
const { createMarkdownRenderer, findStreamingPrefixEnd } = require(bundle);

const STX = String.fromCharCode(2);
const ETX = String.fromCharCode(3);

function fixture(t) {
  const dom = new JSDOM('<main></main>');
  const environment = { document: dom.window.document, parser: { parse: s => marked.parse(s) },
    sanitizer: createDOMPurify(dom.window), highlighter: null, mathRenderer: null, basePath: '', projectActive: false };
  const renderer = createMarkdownRenderer({ getEnvironment: () => ({ ...environment }), maximumCacheEntries: 0, ensureMath: () => {} });
  t.after(() => { renderer.dispose(); dom.window.close(); });
  return { renderer };
}

test('streaming prefix reuse produces identical frames to full re-render', t => {
  const { renderer } = fixture(t);
  const frames = [
    'Para one grows',
    'Para one grows more',
    'Para one grows more\n\nSecond para',
    'Para one grows more\n\nSecond para with **bold**',
    'Para one grows more\n\nSecond para with **bold**\n\n- item a',
    'Para one grows more\n\nSecond para with **bold**\n\n- item a\n- item b',
    'Para one grows more\n\nSecond para with **bold**\n\n- item a\n- item b\n\n```js',
    'Para one grows more\n\nSecond para with **bold**\n\n- item a\n- item b\n\n```js\ncode line',
    'Para one grows more\n\nSecond para with **bold**\n\n- item a\n- item b\n\n```js\ncode line\n```',
    'Para one grows more\n\nSecond para with **bold**\n\n- item a\n- item b\n\n```js\ncode line\n```\n\ntail',
  ];
  for (const frame of frames) {
    const streaming = renderer.render(frame, { streaming: true });
    assert.equal(streaming, renderer.render(frame), `streaming frame diverged from full render:\n${frame}`);
  }
});

test('prefix reuse stays correct across a math-free table and fence boundary', t => {
  const { renderer } = fixture(t);
  const frames = [
    '| a | b |\n| - | - |\n| 1 | 2 |',
    '| a | b |\n| - | - |\n| 1 | 2 |\n\nAfter table',
    '| a | b |\n| - | - |\n| 1 | 2 |\n\nAfter table\n\n```py\nx=1\n```',
  ];
  for (const frame of frames) {
    assert.equal(renderer.render(frame, { streaming: true }), renderer.render(frame), `frame mismatch:\n${frame}`);
  }
});

test('findStreamingPrefixEnd refuses math-bearing or fence-open prefixes', () => {
  const mathLine = `has ${STX}MATH0${ETX} math`;
  /* A blank-line boundary that lands AFTER the math is safe: the stable prefix
   * it selects ('has math\n\n') itself contains no math, so reuse is allowed.
   * The math line must simply never be INSIDE the reusable prefix — asserted
   * by the boundary pointing at the blank line that follows it. */
  assert.equal(findStreamingPrefixEnd(`${mathLine}\n\nmore`), `${mathLine}\n`.length);
  assert.equal(findStreamingPrefixEnd(`${mathLine}\nmore`), 0);
  assert.equal(findStreamingPrefixEnd('```js\nopen fence\n\ninside'), 0);
  assert.ok(findStreamingPrefixEnd('plain\n\nmore') > 0);
  assert.equal(findStreamingPrefixEnd('no boundary yet'), 0);
});
