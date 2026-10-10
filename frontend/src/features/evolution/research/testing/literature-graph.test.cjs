const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ts = require('typescript');
const { JSDOM } = require('jsdom');
const root = path.resolve(__dirname, '..');
function load(file, ports = {}, cache = new Map()) {
  const full = path.resolve(root, file);
  if (cache.has(full)) return cache.get(full);
  const exports = {};
  cache.set(full, exports);
  const js = ts.transpileModule(fs.readFileSync(full, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  new Function('require', 'exports', js)((name) => {
    if (name in ports) return ports[name];
    return load(path.relative(root, path.resolve(path.dirname(full), name + '.ts')), ports, cache);
  }, exports);
  return exports;
}

test('graph projection keeps 200 nodes and only validated cites edges', () => {
  const { graphNodes, GRAPH_NODE_LIMIT } = load('components/literature-graph.ts');
  assert.equal(GRAPH_NODE_LIMIT, 200);
  const papers = Array.from({ length: 260 }, (_, i) => ({
    paperId: 'p' + i, title: 'Paper ' + i, year: 1990 + (i % 35),
    citationCount: i === 0 ? 0 : i * 10,
  }));
  const edges = [
    { from: 'p0', to: 'p1', relation: 'cites' },
    { from: 'p1', to: 'p0', relation: 'cites' },      // duplicate pair
    { from: 'p0', to: 'p0', relation: 'cites' },      // self loop
    { from: 'p0', to: 'outside', relation: 'cites' }, // external endpoint
    { from: 'p2', to: 'p3', relation: 'similar' },    // never inferred
    { from: 'p2', to: 'p3', relation: 'cites' },
  ];
  const nodes = graphNodes(papers, edges);
  assert.equal(nodes.length, 200);
  const kept = nodes._edges;
  assert.equal(kept.length, 2);
  assert.deepEqual(kept.map(e => e.from + '>' + e.to).sort(), ['p0>p1', 'p2>p3']);
  const zero = nodes.find(n => n.id === 'p0');
  assert.equal(zero.citationCount, 0, 'zero citations stay zero, not null');
  assert.ok(zero.radius >= 5);
  const hot = nodes.find(n => n.id === 'p199');
  assert.ok(hot.radius > zero.radius, 'radius grows with citations');
  assert.ok(nodes.every(n => Number.isFinite(n.x) && Number.isFinite(n.y)));
});

test('edge budget follows the 800-edge graph ceiling', () => {
  const { graphNodes } = load('components/literature-graph.ts');
  const papers = Array.from({ length: 200 }, (_, i) => ({ paperId: 'p' + i }));
  const edges = [];
  for (let i = 0; i < 200; i += 1) {
    for (let j = 0; j < 5; j += 1) {
      edges.push({ from: 'p' + i, to: 'p' + ((i + j + 1) % 200), relation: 'cites' });
    }
  }
  const nodes = graphNodes(papers, edges);
  assert.ok(nodes._edges.length <= 800);
  assert.ok(nodes._edges.length > 320, 'graph carries more than the old lineage cap');
});

test('layout separates overlapping nodes and keeps positions finite', () => {
  const { graphNodes, layoutTick } = load('components/literature-graph.ts');
  const papers = Array.from({ length: 40 }, (_, i) => ({ paperId: 'p' + i, citationCount: i }));
  const edges = [{ from: 'p0', to: 'p1', relation: 'cites' }];
  const nodes = graphNodes(papers, edges);
  nodes.forEach(n => { n.x = 0; n.y = 0; });
  layoutTick(nodes, edges, 30);
  let overlaps = 0;
  for (let i = 0; i < nodes.length; i += 1) {
    for (let j = i + 1; j < nodes.length; j += 1) {
      const d = Math.hypot(nodes[i].x - nodes[j].x, nodes[i].y - nodes[j].y);
      if (d < (nodes[i].radius + nodes[j].radius) * 0.5) overlaps += 1;
    }
  }
  assert.ok(overlaps <= 4, 'layout relaxes the initial pile, got ' + overlaps + ' hard overlaps');
  assert.ok(nodes.every(n => Number.isFinite(n.x) && Number.isFinite(n.y)));
  layoutTick(nodes, edges, 0); // zero iterations is a no-op, never NaN
  assert.ok(nodes.every(n => Number.isFinite(n.x)));
});

test('graph view is the default; toggle switches to the sortable table', () => {
  const dom = new JSDOM('<main id="host"></main>');
  global.document = dom.window.document;
  global.window = dom.window;
  const library = load('components/live-library.ts', {
    '../../../../feature-registry': { featureRegistry: {} },
  });
  const { patchResearchDom } = load('components/stable-dom.ts');
  const stream = {
    papers: [{ paperId: 'a', title: 'Alpha method', year: 2024, arxivId: '2401.00001' }],
    citationEdges: [],
  };
  const host = document.querySelector('#host');
  const paint = () => {
    patchResearchDom(host, library.paperCards(stream));
    library.bindPaperCards(host, stream, paint, (k) => k);
  };
  paint();
  assert.ok(host.querySelector('#researchLiteratureGraph'), 'graph host renders by default');
  assert.equal(host.querySelector('.rs-paper-table'), null);
  // jsdom has no 2D context: the component must degrade to an explicit notice.
  assert.ok(host.textContent.includes('graphUnavailable'));
  const toggle = host.querySelector('[data-literature-view="list"]');
  assert.ok(toggle, 'list toggle missing');
  toggle.onclick();
  assert.equal(stream.literatureView, 'list');
  assert.ok(host.querySelector('.rs-paper-table tbody tr'), 'table renders after toggle');
  assert.equal(host.querySelector('#researchLiteratureGraph'), null);
  library.destroyLiteratureGraph();
  dom.window.close();
});

test('graph host subtree survives telemetry repaints', () => {
  const dom = new JSDOM('<main id="host"></main>');
  global.document = dom.window.document;
  global.window = dom.window;
  const { patchResearchDom } = load('components/stable-dom.ts');
  const host = document.querySelector('#host');
  patchResearchDom(host, '<section><div id="researchLiteratureGraph"><canvas class="rs-graph-canvas"></canvas></div><p>1</p></section>');
  const canvas = host.querySelector('canvas');
  patchResearchDom(host, '<section><div id="researchLiteratureGraph"></div><p>2</p></section>');
  assert.equal(host.querySelector('canvas'), canvas, 'graph descendants are graph-owned');
  assert.equal(host.querySelector('p').textContent, '2');
  dom.window.close();
});

test('draw applies the camera so the origin-centered world lands in the viewport', () => {
  const dom = new JSDOM('<main id="host"></main>');
  global.document = dom.window.document;
  global.window = dom.window;
  global.getComputedStyle = dom.window.getComputedStyle.bind(dom.window);
  global.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
  const rect = { width: 800, height: 600, left: 0, top: 0, right: 800, bottom: 600, x: 0, y: 0 };
  dom.window.HTMLCanvasElement.prototype.getBoundingClientRect = () => rect;
  const calls = [];
  const fakeCtx = new Proxy({}, {
    get(target, prop) {
      if (prop in target) return target[prop];
      return (...args) => { calls.push([prop, ...args]); };
    },
    set(target, prop, value) { target[prop] = value; return true; },
  });
  dom.window.HTMLCanvasElement.prototype.getContext = () => fakeCtx;
  const host = document.querySelector('#host');
  host.getBoundingClientRect = () => rect;
  const { LiteratureGraph } = load('components/literature-graph.ts');
  const graph = new LiteratureGraph(host);
  const papers = Array.from({ length: 60 }, (_, i) => ({
    paperId: 'p' + i, title: 'Paper ' + i, year: 2000 + (i % 25), citationCount: i * 3,
  }));
  graph.update({ papers, citationEdges: [{ from: 'p0', to: 'p1', relation: 'cites' }] }, key => key, {});
  const camera = graph.camera;
  assert.ok(camera && camera.k > 0, 'fit() produced a camera');
  const translate = calls.findIndex(call => call[0] === 'translate');
  const scale = calls.findIndex(call => call[0] === 'scale');
  const arc = calls.findIndex(call => call[0] === 'arc');
  assert.ok(translate > -1 && scale > -1, 'draw() applies the camera transform');
  assert.ok(arc > -1 && translate < arc && scale < arc, 'transform precedes node painting');
  assert.deepEqual(calls[translate].slice(1), [camera.x, camera.y]);
  assert.deepEqual(calls[scale].slice(1), [camera.k, camera.k]);
  let minX = Infinity; let maxX = -Infinity; let minY = Infinity; let maxY = -Infinity;
  for (const node of graph.nodes) {
    minX = Math.min(minX, node.x - node.radius);
    maxX = Math.max(maxX, node.x + node.radius);
    minY = Math.min(minY, node.y - node.radius);
    maxY = Math.max(maxY, node.y + node.radius);
  }
  const centerX = minX + Math.max(60, maxX - minX) / 2;
  const centerY = minY + Math.max(60, maxY - minY) / 2;
  assert.ok(Math.abs(centerX * camera.k + camera.x - 400) < 1e-9,
    'world bbox center maps to viewport center x, got ' + (centerX * camera.k + camera.x));
  assert.ok(Math.abs(centerY * camera.k + camera.y - 300) < 1e-9,
    'world bbox center maps to viewport center y, got ' + (centerY * camera.k + camera.y));
  const saves = calls.filter(call => call[0] === 'save').length;
  const restores = calls.filter(call => call[0] === 'restore').length;
  assert.equal(saves, restores, 'canvas state stays balanced');
  graph.destroy();
  dom.window.close();
});
