"""jsdom regression: the STREAMING describe-to-recommend RENDERER.

WHY
The recommend flow streams grounded arXiv cards one at a time (server-owned
task, polled like Q&A). The backend stream is proven real by
``tests/test_paper_recommend_stream.py`` (per-candidate emit + double-neuter).
But the half a USER sees is the reconciling renderer — ``_applyRecommendEvent``
mutates ``_recStream`` and ``_paintRecommendFromState`` / ``_paintRecommendNow``
reconciles ``#paperPdfViewer`` in place: skeleton slots (``data-status=
"searching"``) flip to grounded cards (``data-status="grounded"``) as each
lands, with a ``_recSig`` compare-before-swap so a re-paint never tears a card
down. A backend that streams into a renderer that mis-reconciles is not
"beautiful streaming rendering aligned with chatInner" — so this suite drives
the REAL shipped reconciler under jsdom over the real DOM.

Scripted sequence (mirrors a live poll):
  interpret_done(candidateCount=2) → 2 skeletons, both data-status="searching"
  candidate(index=0)               → slot 0 grounded (real title), slot 1 STILL searching
  candidate(index=1)+correction+done → both grounded, correction banner, hint shown

DOUBLE-NEUTER on the RECONCILER (both in COPIES; shipped file byte-identical):
  • NC-1 (child-node identity / compare-before-swap): re-apply the SAME state
    twice; the grounded card's DOM node must be the SAME object. Neuter: drop
    the ``if (node._recSig === sig) continue`` short-circuit → the node is
    recreated on re-paint → the identity check fails.
  • NC-2 (incremental reveal is real in the DOM): at the moment slot 0 is
    grounded, slot 1 must still be ``data-status="searching"`` (not both dumped
    at once). Neuter: make ``_paintRecommendNow`` only paint when
    ``status==='done'`` → the mid-stream partial-grounding assertion fails.

Skips cleanly when node + jsdom aren't installed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
_READER_SRC = os.path.join(
    ROOT, 'frontend', 'src', 'features', 'paper', 'recommend.ts')
ESBUILD = os.path.join(ROOT, 'scripts', 'vite_test_bundle.mjs')


def _node_deps_available():
    if not shutil.which('node'):
        return False
    return (os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))
            and os.path.isfile(ESBUILD))


_DOM = r'''<!DOCTYPE html><body>
<div id="paperPdfViewer"></div>
</body>'''

# Two grounded cards (as the `candidate` events carry them).
_CARD0 = {
    'arxiv_id': '2502.09992', 'title': 'Large Language Diffusion Models',
    'authors': ['Shen Nie', 'Fengqi Zhu'], 'summary': 'LLaDA.',
    'published': '2025-02-14', 'primary_category': 'cs.CL',
    'why': 'the flagship diffusion LM', 'venue': 'NeurIPS 2025 Oral',
}
_CARD1 = {
    'arxiv_id': '2504.12216',
    'title': 'd1: Scaling Reasoning in Diffusion LLMs via RL',
    'authors': ['Siyan Zhao'], 'summary': 'diffu-GRPO.',
    'published': '2025-04-16', 'primary_category': 'cs.CL',
    'why': 'RL for dLLMs', 'venue': 'NeurIPS 2025',
}
_CORRECTION = {'note': 'No dLLM won a Best Paper; the diffusion winner was about memorization.',
               'paper': {'arxiv_id': '2505.17638', 'title': "Why Diffusion Models Don't Memorize"}}


def _harness():
    return r'''
const fs = require('fs');
const path = require('path');
const READER = process.argv[1];
const ROOT = process.argv[2];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM(DOM_PLACEHOLDER, { url: 'http://localhost/' });
const win = dom.window;
global.window = win; global.document = win.document; global.console = console;
global.localStorage = win.localStorage;
// rAF/timers: paper-reader.js has a load-time responsive IIFE that touches
// matchMedia/addEventListener — stub them so eval doesn't throw.
global.requestAnimationFrame = win.requestAnimationFrame = (fn) => { fn(Date.now()); return 1; };
win.matchMedia = win.matchMedia || ((q) => ({ matches: false, media: q,
  addEventListener(){}, removeEventListener(){}, addListener(){}, removeListener(){} }));
global.setTimeout = (fn) => { if (typeof fn === 'function') fn(); return 0; };
global.clearTimeout = () => {};
global.setInterval = () => 0; global.clearInterval = () => {};
win.t = global.t = (k, f) => {
  // Faithful-enough English for the two streaming status keys (with {n}/{total}).
  const M = {
    'paper.recommendTitle': 'Recommended papers',
    'paper.recommendHint': 'Each is verified on arXiv — click to load',
    'paper.recommendInterpreting': 'Interpreting your description…',
    'paper.recommendGrounding': 'Verifying against arXiv ({n}/{total})…',
    'paper.recommendNoResults': 'Could not verify a matching paper on arXiv.',
    'paper.searchBack': 'Back', 'paper.correctionTitle': 'A quick correction',
    'paper.correctionActual': 'The paper that actually won',
  };
  return M[k] || f || k;
};
win.escapeHtml = global.escapeHtml = (s) => String(s == null ? '' : s)
  .replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
win.debugLog = global.debugLog = () => {};
win.Api = global.Api = { paper: {} };
win._openRecommendResult = global._openRecommendResult = () => {};
win._openRecommendCorrection = global._openRecommendCorrection = () => {};
win._showPaperLanding = global._showPaperLanding = () => {};
// Cross-file peer: _applyRecommendEvent persists each grounded card to the
// bookshelf via the native _persistRecommendedCard owner. This
// suite is about the RECONCILER, not the library write, so stub it — the same
// treatment the other paper/* peers above get. Without it the candidate branch
// throws ReferenceError and every assertion dies before it runs.
win._persistRecommendedCard = global._persistRecommendedCard = () => {};

eval(fs.readFileSync(READER, 'utf8'));

(function main() {
// The eval'd `function`/`var` declarations hoist into THIS function scope
// (bare names), not onto `win` — reference them directly.
const applyEv = win._applyRecommendEvent;
const paint = win._paintRecommendNow;
const newStream = win._newRecStream;
if (typeof applyEv !== 'function' || typeof paint !== 'function' || typeof newStream !== 'function') {
  console.log('__RESULT__' + JSON.stringify({ _missing: {
    applyEv: typeof applyEv, paint: typeof paint, newStream: typeof newStream } }));
  return;
}

const CARD0 = JSON.parse(CARD0_PH), CARD1 = JSON.parse(CARD1_PH), CORR = JSON.parse(CORR_PH);
const viewer = win.document.getElementById('paperPdfViewer');
const out = {};

// Fresh stream is the module SoT the reconciler paints from. Assign the
// module's own `var _recStream` (bare name, same eval scope) so the reconciler
// (which reads `_recStream`) sees it.
const s = newStream('diffusion LM award papers');
// The native factory is pure; the reconciler reads the feature
// registry, and the proxy's override table shadows window writes
// (the module seeded `_recStream ??= null` at import time). Register
// the stream through the registry, exactly where production puts it.
win.__recFeatureRegistry._recStream = s;

// ── Phase 1: interpret_done(candidateCount=2) → two searching skeletons ──
applyEv(s, { type: 'interpret_done', query: s.description, candidateCount: 2, correctionPending: true });
s.status = 'running';
paint();
let cards = viewer.querySelectorAll('.paper-result-list > *');
out.skeletonCount = cards.length;
out.bothSearching = cards.length === 2 &&
  cards[0].getAttribute('data-status') === 'searching' &&
  cards[1].getAttribute('data-status') === 'searching';
out.noGroundedYet = viewer.querySelectorAll('[data-status="grounded"]').length === 0;

// ── candidate(index=0): slot 0 grounded, slot 1 STILL a skeleton ──
applyEv(s, { type: 'candidate', index: 0, card: CARD0 });
paint();
cards = viewer.querySelectorAll('.paper-result-list > *');
const slot0 = cards[0], slot1 = cards[1];
out.slot0Grounded = !!slot0 && slot0.getAttribute('data-status') === 'grounded';
out.slot0HasRealTitle = !!slot0 && slot0.textContent.indexOf('Large Language Diffusion Models') !== -1;
out.slot0HasWhy = !!slot0 && slot0.textContent.indexOf('the flagship diffusion LM') !== -1;
// NC-2 target: the incremental reveal must be real — slot 1 still searching.
out.slot1StillSearchingMidStream = !!slot1 && slot1.getAttribute('data-status') === 'searching';
out.exactlyOneGroundedMidStream = viewer.querySelectorAll('[data-status="grounded"]').length === 1;

// NC-1 target (compare-before-swap): a re-paint of identical state must NOT
// rewrite the grounded card's innerHTML. The reconciler always REUSES the slot
// node object (it only createElement()s an empty slot), so parent identity is
// not what the `_recSig` short-circuit protects — it protects the node's
// CHILDREN from being torn down. Mark the current first child; a compare-
// before-swap re-paint keeps it (same object + mark survives), an
// unconditional innerHTML rewrite recreates the subtree → the mark is gone.
const slot0Node = viewer.querySelectorAll('.paper-result-list > *')[0];
const childBefore = slot0Node && slot0Node.firstChild;
if (childBefore) childBefore._recMark = 'keep';
paint();  // re-apply identical state
const slot0NodeAfter = viewer.querySelectorAll('.paper-result-list > *')[0];
out.reapplySameNode = !!(slot0NodeAfter && slot0NodeAfter.firstChild &&
  slot0NodeAfter.firstChild === childBefore && slot0NodeAfter.firstChild._recMark === 'keep');

// ── candidate(index=1) + correction + done: both grounded, banner + hint ──
applyEv(s, { type: 'candidate', index: 1, card: CARD1 });
applyEv(s, { type: 'correction', correction: CORR });
s.status = 'done';
paint();
cards = viewer.querySelectorAll('.paper-result-list > *');
out.bothGroundedAtDone = cards.length === 2 &&
  cards[0].getAttribute('data-status') === 'grounded' &&
  cards[1].getAttribute('data-status') === 'grounded';
out.slot1HasRealTitle = !!cards[1] && cards[1].textContent.indexOf('d1: Scaling Reasoning') !== -1;
out.correctionShown = !!viewer.querySelector('.paper-correction') &&
  viewer.textContent.indexOf('No dLLM won a Best Paper') !== -1;
const hintEl = viewer.querySelector('[data-rec-hint]');
out.hintShown = !!hintEl && hintEl.hidden === false;
// Status line settled (no spinner text) once done + all grounded.
const statusEl = viewer.querySelector('[data-rec-status]');
out.statusSettled = !!statusEl && statusEl.hidden === true;

console.log('__RESULT__' + JSON.stringify(out));
})();
'''.replace('DOM_PLACEHOLDER', json.dumps(_DOM)) \
   .replace('CARD0_PH', json.dumps(json.dumps(_CARD0))) \
   .replace('CARD1_PH', json.dumps(json.dumps(_CARD1))) \
   .replace('CORR_PH', json.dumps(json.dumps(_CORRECTION)))


def _run(reader=_READER_SRC):
    with tempfile.TemporaryDirectory(prefix='tofu-recommend-render-') as temp_dir:
        built = os.path.join(temp_dir, 'recommend.js')
        with open(reader, encoding='utf-8') as source_file:
            source = source_file.read()
        dependency = os.path.join(
            os.path.dirname(_READER_SRC), 'push-transport.ts').replace('\\', '/')
        source = source.replace(
            "from './push-transport';", f'from {json.dumps(dependency)};')
        registry = os.path.join(
            ROOT, 'frontend', 'src', 'feature-registry.ts').replace('\\', '/')
        source = source.replace(
            "from '../../feature-registry';", f'from {json.dumps(registry)};')
        # Bridge the module-private feature registry onto the jsdom window,
        # mirroring how main.ts injects the production service table. Without
        # the connection the harness cannot see any installed global owner.
        source += (
            '\nimport { connectFeatureRuntime as __connectRecTestRuntime,'
            ' featureRegistry as __recRegistry } from '
            + json.dumps(registry) + ';\n'
            '__connectRecTestRuntime('
            '(name) => (typeof window === "undefined"'
            ' ? undefined : (window as unknown as Record<string, unknown>)[name]),'
            '(name, value) => { if (typeof window !== "undefined")'
            ' (window as unknown as Record<string, unknown>)[name] = value; });\n'
            'if (typeof window !== "undefined")'
            ' (window as unknown as Record<string, unknown>).__recFeatureRegistry'
            ' = __recRegistry;\n')
        compiled = subprocess.run(
            [ESBUILD, '--bundle', '--format=iife', '--platform=browser',
             '--loader=ts', '--sourcefile=recommend.ts',
             f'--outfile={built}'], input=source, capture_output=True,
            text=True, timeout=60)
        if compiled.returncode != 0:
            raise AssertionError(f'vite test bundler failed: {compiled.stderr or compiled.stdout}')
        proc = subprocess.run(
            ['node', '-e', _harness(), built, ROOT],
            capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise AssertionError(f'harness failed: {proc.stderr or proc.stdout}')
    for line in proc.stdout.splitlines():
        if line.startswith('__RESULT__'):
            res = json.loads(line[len('__RESULT__'):])
            if res.get('_missing'):
                raise AssertionError(f'reconciler seam not exposed: {res["_missing"]}')
            return res
    raise AssertionError(f'no result line: {proc.stdout}\n{proc.stderr}')


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_reconciler_streams_skeleton_to_grounded_incrementally():
    out = _run()
    # Phase 1 → two searching skeletons, nothing grounded yet.
    assert out['skeletonCount'] == 2, f'interpret_done must reserve 2 skeleton slots: {out}'
    assert out['bothSearching'] is True, f'both slots must start data-status=searching: {out}'
    assert out['noGroundedYet'] is True, out
    # candidate(0) → slot 0 flips to grounded with real content; slot 1 UNCHANGED.
    assert out['slot0Grounded'] is True, f'slot 0 must flip to grounded: {out}'
    assert out['slot0HasRealTitle'] is True, f'grounded card must show the real arXiv title: {out}'
    assert out['slot0HasWhy'] is True, f'grounded card must show the "why this matches" line: {out}'
    assert out['slot1StillSearchingMidStream'] is True, \
        f'THE incremental-reveal invariant: slot 1 must STILL be a searching skeleton: {out}'
    assert out['exactlyOneGroundedMidStream'] is True, \
        f'exactly one card grounded mid-stream (not both dumped at once): {out}'
    # compare-before-swap: re-paint does NOT tear down the grounded card's
    # subtree (its child node survives with its sentinel mark).
    assert out['reapplySameNode'] is True, \
        f're-paint of identical state must NOT rewrite the grounded card subtree: {out}'
    # done → both grounded, correction banner + hint shown, status settled.
    assert out['bothGroundedAtDone'] is True, f'both slots grounded at done: {out}'
    assert out['slot1HasRealTitle'] is True, out
    assert out['correctionShown'] is True, f'correction banner must render: {out}'
    assert out['hintShown'] is True, f'the "verified on arXiv" hint must show once cards exist: {out}'
    assert out['statusSettled'] is True, f'status line must hide once done + all grounded: {out}'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC1_child_node_identity_compare_before_swap_is_load_bearing(tmp_path):
    """NC-1: drop the `_recSig` compare-before-swap short-circuit → a re-paint
    of identical state recreates the grounded node (identity check fails).
    Shipped file byte-identical after."""
    with open(_READER_SRC, encoding='utf-8') as f:
        original = f.read()
    anchor = '    if (node._recSig === signature) continue;'
    assert anchor in original, 'compare-before-swap anchor not found — update the neuter target'
    patched = original.replace(
        anchor, '    /* NC-1: compare-before-swap removed (always rewrite) */', 1)
    assert patched != original, 'NC-1 patch did not apply'
    src = os.path.join(tmp_path, 'paper-recommend-nc1.ts')
    with open(src, 'w', encoding='utf-8') as f:
        f.write(patched)
    out = _run(reader=src)
    assert out['reapplySameNode'] is False, \
        f'NC-1: without compare-before-swap a re-paint must recreate the node: {out}'
    with open(_READER_SRC, encoding='utf-8') as f:
        assert f.read() == original, 'shipped paper-reader.js must be byte-identical after NC-1'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC2_incremental_reveal_is_load_bearing(tmp_path):
    """NC-2: make _paintRecommendNow paint ONLY when status==='done' → the
    mid-stream partial-grounding assertion (slot 0 grounded while slot 1 still
    searching) fails, because the DOM only ever populates at done. Shipped file
    byte-identical after."""
    with open(_READER_SRC, encoding='utf-8') as f:
        original = f.read()
    anchor = ('export function paintRecommendNow(): void {\n'
              '  const stream = state()._recStream;\n'
              '  if (!stream) return;')
    assert anchor in original, 'paint-now anchor not found — update the neuter target'
    patched = original.replace(
        anchor,
        ('export function paintRecommendNow(): void {\n'
         '  const stream = state()._recStream;\n'
         '  if (!stream) return;\n'
         "  if (stream.status !== 'done') return;  // NC-2: only ever paint at done"),
        1)
    assert patched != original, 'NC-2 patch did not apply'
    src = os.path.join(tmp_path, 'paper-recommend-nc2.ts')
    with open(src, 'w', encoding='utf-8') as f:
        f.write(patched)
    out = _run(reader=src)
    # With paint gated on done, the mid-stream snapshot never shows a partial
    # reveal: slot 0 isn't grounded mid-stream (nothing is painted before done).
    assert out['slot1StillSearchingMidStream'] is False, \
        f'NC-2: gating paint on done must break the incremental-reveal invariant: {out}'
    # And it still ends up correct at done (the neuter only removes streaming,
    # not correctness) — proving the assertion discriminates streaming, not just
    # "renders eventually".
    assert out['bothGroundedAtDone'] is True, \
        f'NC-2 sanity: the neutered variant still renders both cards at done: {out}'
    with open(_READER_SRC, encoding='utf-8') as f:
        assert f.read() == original, 'shipped paper-reader.js must be byte-identical after NC-2'


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
