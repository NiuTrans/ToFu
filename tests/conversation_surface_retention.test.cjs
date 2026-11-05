const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const path = require('node:path');
const { test } = require('node:test');

for (const scenario of ['plain text', 'focused tool search', 'open tool panel']) test(`keyed surface releases replaced and disposed graphs (${scenario})`, () => {
  // The owner and its public root remain reachable. A fresh GC process and
  // non-retaining render ports isolate Surface ownership from renderer caches.
  const script = String.raw`
    const path = require('node:path');
    const { JSDOM } = require('jsdom');
    const dom = new JSDOM('<main></main>', { url: 'http://localhost/' });
    for (const key of ['window', 'document', 'Element', 'HTMLElement', 'HTMLDetailsElement']) {
      global[key] = key === 'window' ? dom.window : dom.window[key];
    }
    const code = require('esbuild').buildSync({
      stdin: { contents: [
        "export { createConversationSurface } from './frontend/src/conversation/ui/conversation-surface';",
        "export { createTurnState } from './frontend/src/conversation/domain/turn-store';",
        "export { selectConversationViewModel } from './frontend/src/conversation/presentation/conversation-view-model';",
      ].join('\n'), resolveDir: process.argv[1] },
      bundle: true, platform: 'node', format: 'cjs', write: false,
      loader: { '.css': 'empty' }, logLevel: 'silent',
    }).outputFiles[0].text;
    const loaded = { exports: {} };
    new Function('module', 'exports', 'require', code)(loaded, loaded.exports, require);
    const { createConversationSurface, createTurnState, selectConversationViewModel } = loaded.exports;
    const withSearch = process.argv[2] === 'focused tool search';
    const withPanel = process.argv[2] === 'open tool panel';
    const counts = {};
    const options = {};
    for (const key of ['renderBlock', 'renderTurnAvatar', 'renderTurnHeader',
      'renderTurnActions', 'renderTurnFooter', 'renderTurnContextRail', 'renderLaneHeader']) {
      options[key] = node => { counts[key] = (counts[key] || 0) + 1; node.textContent = key; };
    }
    const surface = createConversationSurface(document.querySelector('main'), options);
    function render() {
      const state = createTurnState('conversation');
      state.turnsById.turn = { turnId: 'turn', presentationId: 'turn', runId: '',
        conversationId: 'conversation', laneId: 'main', ordinal: 1, actor: withSearch || withPanel ? 'assistant' : 'human',
        kind: withSearch || withPanel ? 'reply' : 'input', status: withSearch ? 'running' : 'completed', currentAttemptId: null, projectionRevision: 1,
        createdAt: 1, updatedAt: 1, projection: withPanel ? { segments: [
          {type: 'thinking', blockId: 'reasoning', text: 'evidence'},
          {type: 'tool_use', blockId: 'read', id: 'read', name: 'read_files', input: {path: 'a.ts'},
            result: {status: 'done', content: 'a'}},
          {type: 'text', blockId: 'final', text: 'Answer', terminal: true, deliverable: true},
        ] } : { content: 'same visible text' }, settlement: {} };
      state.laneOrder.main = ['turn'];
      // Detach from selector memoization: only the Surface owns these clones.
      const view = structuredClone(selectConversationViewModel(state));
      const payloadOwner = withPanel ? view.mainLane.turns[0].blocks[0].source
        : view.mainLane.turns[0].source.projection;
      payloadOwner.fixturePayload = new Uint8Array(1024 * 1024);
      if (withSearch) view.mainLane.turns[0].blocks = Array.from({ length: 12 }, (_, index) => ({
        kind: 'tool', blockId: 'tool-' + index, identitySource: 'contract',
        toolCallId: 'call-' + index, name: 'read', input: {}, result: { status: 'done' },
        source: { llmRound: 0 }, round: { llmRound: 0, toolName: 'read', status: 'done' },
      }));
      const refs = [view, view.mainLane, view.mainLane.turns[0], view.mainLane.turns[0].blocks[0]]
        .map(value => new WeakRef(value));
      surface.render(view);
      return refs;
    }
    async function collect() {
      for (let i = 0; i < 4; i++) {
        await new Promise(resolve => setImmediate(resolve)); global.gc();
      }
    }
    (async () => {
      const before = render();
      let panelButton;
      if (withPanel) {
        panelButton = surface.root.querySelector('[data-conversation-tool-panel]');
        panelButton.click();
        if (!surface.root.querySelector('[data-block-id="read"]')) throw Error('fixture did not open the tool panel');
      }
      let hit;
      if (withSearch) {
        const search = surface.root.querySelector('input[type=search]');
        search.value = 'read'; search.dispatchEvent(new dom.window.Event('input'));
        hit = surface.root.querySelector('.conversation-round-matches button');
        if (!hit) throw Error('fixture did not mount tool search results');
        hit.focus();
      }
      let node = surface.root.querySelector('[data-turn-id]');
      const firstCounts = { ...counts };
      const current = render();
      const nodeRetained = surface.root.querySelector('[data-turn-id]') === node;
      const searchRetained = !withSearch || (document.activeElement === hit
        && surface.root.querySelector('.conversation-round-matches button') === hit);
      const panelRetained = !withPanel || (surface.root.querySelector('[data-conversation-tool-panel]') === panelButton
        && panelButton.getAttribute('aria-expanded') === 'true');
      const noRepaint = JSON.stringify(firstCounts) === JSON.stringify(counts);
      node = null; // Only the owner and public root survive the GC phases.
      await collect();
      const beforeRetained = before.map(ref => Boolean(ref.deref()));
      const currentRetained = current.map(ref => Boolean(ref.deref()));
      surface.dispose();
      await collect();
      console.log(JSON.stringify({ nodeRetained, searchRetained, panelRetained, noRepaint, beforeRetained, currentRetained,
        disposedRetained: current.map(ref => Boolean(ref.deref())),
        disposedChildren: surface.root.childElementCount }));
      dom.window.close();
    })().catch(error => { console.error(error); process.exitCode = 1; });
  `;
  const result = execFileSync(process.execPath,
    ['--expose-gc', '-e', script, path.resolve(__dirname, '..'), scenario],
    // Includes module bundling and jsdom startup under the parallel Node gate;
    // reachability/render counts are the contract, not a 30-second latency SLA.
    { encoding: 'utf8', timeout: 90_000 });
  assert.deepEqual(JSON.parse(result.trim()), {
    nodeRetained: true, searchRetained: true, panelRetained: true, noRepaint: true,
    beforeRetained: [false, false, false, false],
    currentRetained: [true, true, true, true],
    disposedRetained: [false, false, false, false], disposedChildren: 0,
  });
});
