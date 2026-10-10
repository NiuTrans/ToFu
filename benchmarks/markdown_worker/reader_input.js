/* Controlled real-keyboard/render contention; no application state authority. */
globalThis.createReaderInputFixture = function ({caseName}) {
  const parser = new marked.Marked();
  installMarkdownPolicy(parser);
  const renderer = createMarkdownRenderer({maximumCacheEntries: 30,
    getEnvironment: () => ({document, parser, sanitizer: DOMPurify,
      highlighter: readerHighlighter, mathRenderer: null, basePath: '', projectActive: false}),
    ensureMath() {}, reportFailure(error) { throw error; }});
  const input = document.createElement('input');
  input.id = 'reader-input-probe'; input.setAttribute('aria-label', 'Reader input probe');
  const node = document.createElement('section'); node.className = 'conversation-text-block';
  document.body.append(input, node);
  const prefix = Array.from({length: 160}, (_, index) =>
    `Paragraph ${index}: **reader evidence** 中文 😀 [source](/source).`).join('\n\n') + '\n\n';
  const tails = ['Tail **rich prose**.', '```js\nconst value = 1;\n',
    '```js\nconst value = 1;\n```\n\n[reference]',
    '| Name | Value |\n| --- | --- |\n| stable | 1 |\n\n[reference]: /source'];
  const ports = {renderSafeMarkdownHtml: (text, options) => renderer.render(text, options)};
  let index = 0, disposed = false, pending = 0;
  const raw = [], frames = [];
  let finalSource = '', finalTerminal = false;
  function render(source, terminal) {
    renderTextBlock(node, {kind: 'text', blockId: 'reader-input-probe-block', markdown: source, terminal}, ports);
    node.getBoundingClientRect();
  }
  function keydown(event) {
    if (event.key !== 'a' || disposed) return;
    if (pending) throw Error('input probe requires one outstanding key');
    const source = prefix + tails[index % tails.length] + '\n\nEvidence ' + index++;
    const terminal = caseName === 'completion';
    const started = performance.now();
    if (terminal) render(source, false);
    render(source, terminal);
    const renderMs = performance.now() - started;
    frames.push({source, terminal, html: node.querySelector('.md-content').innerHTML});
    finalSource = source; finalTerminal = terminal;
    pending = requestAnimationFrame(() => {
      pending = requestAnimationFrame(() => {
        pending = 0;
        if (!disposed) raw.push({keydown_to_second_raf_ms: performance.now() - event.timeStamp,
                                render_and_layout_ms: renderMs});
      });
    });
  }
  input.addEventListener('keydown', keydown);
  input.focus();
  return {
    count: () => raw.length,
    results: () => ({raw: raw.slice(20), warmupKeys: Math.min(raw.length, 20),
                    finalSource, inputValue: input.value, completedKeys: raw.length}),
    oracle() {
      renderer.clearCache();
      for (const frame of frames) {
        const expected = document.createElement('div');
        expected.innerHTML = renderer.render(frame.source, {streaming: !frame.terminal});
        if (expected.innerHTML !== frame.html) throw Error('input/render DOM differs from canonical renderer');
      }
      return frames.length;
    },
    close() {
      disposed = true; if (pending) cancelAnimationFrame(pending);
      input.removeEventListener('keydown', keydown);
      renderer.dispose(); input.remove(); node.remove(); raw.length = 0; frames.length = 0;
    },
  };
};
