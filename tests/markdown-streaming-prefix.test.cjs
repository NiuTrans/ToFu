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

test('late reference definitions preserve full-render parity across consecutive streaming frames', t => {
  const { renderer } = fixture(t);
  const { renderer: oracle } = fixture(t);
  const initial = '[reference]\n\nTail.';
  const defined = initial + '\n\n[reference]: /source "Source"';
  const frames = [initial, defined, defined, defined + '\n\nMore evidence.',
    initial + '\n\n[reference]: /changed "Changed"', initial];
  for (const frame of frames) {
    assert.equal(renderer.render(frame, { streaming: true }), oracle.render(frame),
      `consecutive reference frame diverged:\n${frame}`);
  }
});

test('reference definitions before their uses and reference images remain exact', t => {
  const { renderer } = fixture(t);
  const { renderer: oracle } = fixture(t);
  const definition = '[reference]: /source\n\n';
  const initial = definition + 'Stable evidence.\n\nTail.';
  const frames = [initial, initial + '\n\n[reference]', initial + '\n\n[reference]',
    initial + '\n\n![evidence][reference]'];
  for (const frame of frames) {
    assert.equal(renderer.render(frame, { streaming: true }), oracle.render(frame),
      `reference document diverged:\n${frame}`);
  }
});

test('a plain leading prefix stays reusable while reference-bearing suffixes parse together', t => {
  const { renderer } = fixture(t);
  const { renderer: oracle } = fixture(t);
  const prefix = 'Stable reader paragraph.\n\n';
  const initial = prefix + '[证据]\n\nTail.';
  const defined = initial + '\n\n[证据]: /source';
  for (const frame of [initial, defined, defined, defined + '\n\nMore.']) {
    assert.equal(renderer.render(frame, { streaming: true }), oracle.render(frame));
  }
  assert.equal(findStreamingPrefixEnd(defined), 'Stable reader paragraph.\n'.length);
  assert.equal(findStreamingPrefixEnd('[reference]\n\nTail.\n\n[reference]: /source'), 0);
  assert.ok(findStreamingPrefixEnd('plain\n\n```js\nconst a = [1];\n```\n\ntail') > 'plain\n'.length);
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

function countingFixture(t) {
  const dom = new JSDOM('<main></main>');
  const counter = { characters: 0 };
  const environment = { document: dom.window.document,
    parser: { parse: s => { counter.characters += s.length; return marked.parse(s); } },
    sanitizer: createDOMPurify(dom.window), highlighter: null, mathRenderer: null, basePath: '', projectActive: false };
  const renderer = createMarkdownRenderer({ getEnvironment: () => ({ ...environment }), maximumCacheEntries: 0, ensureMath: () => {} });
  t.after(() => { renderer.dispose(); dom.window.close(); });
  return { renderer, counter };
}

function chunkedFrames(documentText, chunk) {
  const frames = [];
  for (let end = chunk; end < documentText.length + chunk; end += chunk) {
    frames.push(documentText.slice(0, Math.min(end, documentText.length)));
  }
  return frames;
}

// A reuse-free oracle: a fresh renderer per frame has no prior streaming
// prefix. Math placeholders carry the renderer owner id; normalize only that.
function freshStreamingRender(t, frame) {
  const { renderer } = fixture(t);
  return renderer.render(frame, { streaming: true });
}
const withoutMathOwner = html => html.replace(/data-markdown-math="\d+"/g, 'data-markdown-math="owner"');

test('every chunked streaming frame matches an independent from-scratch streaming render', t => {
  const { renderer } = fixture(t);
  const documentText = [
    'Intro paragraph with **bold** and `code`.',
    '###标题 without a space',
    '-项目一\n-项目二',
    '```py\nx = [1]\n\nprint(x)\n```',
    '| a | b |\n| - | - |\n| 1 | 2 |',
    'Inline math $a+b$ inside prose.',
    'After math, plain prose again.',
    '「重点」**强调**内容。',
    '$$\nE = mc^2\n$$',
    'Plain paragraph after display math.',
    'A [reference] use.',
    'More plain text.',
    '[reference]: /source',
    'Closing paragraph.',
  ].join('\n\n');
  for (const chunk of [1, 3, 7, 19]) {
    for (const frame of chunkedFrames(documentText, chunk)) {
      assert.equal(withoutMathOwner(renderer.render(frame, { streaming: true })),
        withoutMathOwner(freshStreamingRender(t, frame)), `chunk ${chunk} diverged at:\n${frame}`);
    }
    renderer.clearCache();
  }
});

test('streaming prefix reuse keeps parsed work proportional to the grown tail', t => {
  const { renderer, counter } = countingFixture(t);
  let documentText = '';
  for (let index = 0; index < 60; index++) {
    documentText += `Paragraph ${index} explains **step ${index}** with \`inline code\` and enough prose to resemble a model answer.\n\n`;
    if (index % 10 === 5) documentText += '```js\nconst x = ' + index + ';\n```\n\n';
  }
  const frames = chunkedFrames(documentText, 16);
  for (const frame of frames) renderer.render(frame, { streaming: true });
  const fullRenderCharacters = frames.reduce((sum, frame) => sum + frame.length, 0);
  // Each frame parses its open tail plus each completed block once more when it
  // joins the prefix. Re-parsing the accumulated prefix every frame is
  // quadratic (about frames/2 times the document length).
  assert.ok(counter.characters < documentText.length * 12,
    `parsed ${counter.characters} characters; full re-render parses ${fullRenderCharacters}`);
});
