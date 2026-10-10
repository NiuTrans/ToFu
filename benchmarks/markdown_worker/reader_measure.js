/* Browser-only reader reconciliation measurement; no application authority. */
globalThis.createReaderMeasureFixture = function ({caseName}) {
  const parser = new marked.Marked();
  installMarkdownPolicy(parser);
  const renderer = createMarkdownRenderer({maximumCacheEntries: 30,
    getEnvironment: () => ({document, parser, sanitizer: DOMPurify,
      highlighter: readerHighlighter, mathRenderer: null, basePath: '', projectActive: false}),
    ensureMath() {}, reportFailure(error) { throw error; }});
  const node = document.createElement('section');
  node.className = 'conversation-text-block';
  document.body.append(node);
  const prefix = Array.from({length: 160}, (_, i) =>
    `Paragraph ${i}: **reader evidence** 中文 😀 with a [source](/source).`).join('\n\n') + '\n\n';
  const tails = ['Tail **rich prose**.', '```js\nconst value = 1;\n',
    '```js\nconst value = 1;\n```\n\n[reference]',
    '| Name | Value |\n| --- | --- |\n| stable | 1 |\n\n[reference]: /source'];
  const ports = {renderSafeMarkdownHtml: (text, options) => renderer.render(text, options)};
  const frames = [];
  let finalSource = '', finalTerminal = false;
  function source(index, version) {
    const tail = caseName === 'completion'
      ? '```js\nconst value = ' + (index + version) + ';\n```'
      : tails[index % tails.length] + '\n\nEvidence ' + (index + version);
    return prefix + tail;
  }
  function render(markdown, terminal) {
    renderTextBlock(node, {kind: 'text', blockId: 'reader-measure', markdown, terminal}, ports);
    node.getBoundingClientRect();
  }
  function prepare(markdown) {
    if (caseName === 'completion') render(markdown, false);
  }
  return {
    warm(version) {
      renderer.clearCache();
      for (let index = 0; index < 20; index++) {
        const markdown = source(index, version);
        prepare(markdown); render(markdown, caseName === 'completion');
      }
    },
    latency(count, capture) {
      const raw = [];
      for (let index = 20; index < 20 + count; index++) {
        const markdown = source(index, 0);
        prepare(markdown);
        const start = performance.now();
        render(markdown, caseName === 'completion');
        raw.push(performance.now() - start);
        if (capture) frames.push({source: markdown, html: node.querySelector('.md-content').innerHTML});
        finalSource = markdown; finalTerminal = caseName === 'completion';
      }
      return {raw, finalSource};
    },
    cpu(count) {
      for (let index = 20; index < 20 + count; index++) {
        const markdown = source(index, 10000);
        prepare(markdown); render(markdown, caseName === 'completion');
        finalSource = markdown; finalTerminal = caseName === 'completion';
      }
    },
    oracle() {
      const last = {source: finalSource, html: node.querySelector('.md-content').innerHTML};
      renderer.clearCache();
      for (const frame of [...frames, last]) {
        const expected = document.createElement('div');
        expected.innerHTML = renderer.render(frame.source, {streaming: !finalTerminal});
        if (expected.innerHTML !== frame.html) throw Error('reader fixture differs from canonical DOM');
      }
      return frames.length;
    },
    close() { renderer.dispose(); node.remove(); frames.length = 0; },
  };
};
