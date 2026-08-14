"""tests/test_frontend_update_progress.py — regression for the self-update
stepper's LIVE per-step progress rendering.

WHY
---
"Update now" used to hang for a long time with hardly any visible stages: the
long-pole stages (network download, pip install) showed only a bare spinning
"active" dot for minutes, so the modal looked frozen. The backend now streams
structured progress frames — ``{stage,status,detail,pct,loaded,total,speed}``
for the download and per-line frames for pip — and update.js renders them as a
thin per-step bar (determinate when a percentage is known, animated
indeterminate otherwise).

This harness loads the REAL shipped update.js under bare node, stubs the DOM
enough to observe the stepper elements, and drives ``_applyStageFrame`` with
representative frames to assert:
  • a determinate frame (pct=42) creates a .upd-step-bar with a fill width;
  • an indeterminate frame (no pct) marks the bar .is-indeterminate;
  • a 'done' frame removes the bar and marks the step done;
  • a 'skip' frame relabels + removes the bar.

DOUBLE-NEUTER (on a mutated copy; shipped file untouched): stripping the
``_setStepBar`` body so it never touches the bar makes the determinate/
indeterminate assertions FAIL — proving the progress rendering is
load-bearing.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
JS_DIR = os.path.join(ROOT, 'static', 'js')


def _node_available() -> bool:
    return bool(shutil.which('node'))


_HARNESS = r"""
const fs = require('fs');
global.window = global;
const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

global.debugLog = () => {};
global.escapeHtml = (s) => String(s);
global.t = (k) => k;
global.addEventListener = () => {};
global._onReady = () => {};   // feature-bridge.js deferred-ready hook (Epic-E sub-9)
global.setTimeout = () => 0;
global.clearTimeout = () => {};
global.requestAnimationFrame = () => 0;
global.cancelAnimationFrame = () => {};
global.setInterval = () => 0;
global.clearInterval = () => {};

// ── Minimal DOM: a tiny element class that supports classList, querySelector,
//    appendChild, remove, innerHTML (parsed only enough for our needs). ──
function El(tag, cls) {
  const self = {
    tag: tag || 'div', _className: cls || '', _classes: new Set((cls||'').split(' ').filter(Boolean)),
    children: [], style: {}, textContent: '', _attrs: {},
    // className setter keeps _classes in sync (update.js sets wrap.className).
    set className(v){ self._className = v; self._classes = new Set(String(v||'').split(' ').filter(Boolean)); },
    get className(){ return self._className; },
    classList: {
      add(c){ self._classes.add(c); }, remove(){ for (const c of arguments) self._classes.delete(c); },
      toggle(c){ self._classes.has(c) ? self._classes.delete(c) : self._classes.add(c); },
      contains(c){ return self._classes.has(c); },
    },
    set innerHTML(v){ self._html = v;
      // Only the bar wrap sets innerHTML with a fill child in our code.
      if (/upd-step-bar-fill/.test(v)) { const f = El('div','upd-step-bar-fill'); self.children=[f]; }
    },
    get innerHTML(){ return self._html || ''; },
    appendChild(c){ self.children.push(c); return c; },
    remove(){ if (self._parent){ const i=self._parent.children.indexOf(self); if(i>=0) self._parent.children.splice(i,1);} },
    querySelector(sel){
      const want = sel.replace(/^\./,'');
      const walk = (node) => {
        for (const ch of node.children) {
          if (ch._classes.has(want)) return ch;
          const deep = walk(ch); if (deep) return deep;
        }
        return null;
      };
      return walk(self);
    },
  };
  const _ap = self.appendChild;
  self.appendChild = (c) => { c._parent = self; return _ap(c); };
  return self;
}
global.document = { createElement: (t) => El(t) };

const SRC = fs.readFileSync(process.argv[2], 'utf8');
function loadModule(src){ (0, eval)(src); }
loadModule(SRC);

if (typeof _applyStageFrame !== 'function' || typeof _setStepBar !== 'function') {
  console.log('FAIL fns_exposed'); console.log(out.join('\n')); process.exit(0);
}
check('fns_exposed', true);

// Wire up the module-level _updateStageEls with three fake step rows.
const fetchEl = El('li','upd-step'); fetchEl.appendChild(El('span','upd-step-dot'));
const lbl = El('span','upd-step-label'); fetchEl.appendChild(lbl);
fetchEl.appendChild(El('span','upd-step-detail'));
const depsEl = El('li','upd-step'); depsEl.appendChild(El('span','upd-step-dot'));
const depsLbl = El('span','upd-step-label'); depsEl.appendChild(depsLbl);
depsEl.appendChild(El('span','upd-step-detail'));
_updateStageEls = { fetch: fetchEl, pull: El('li','upd-step'), deps: depsEl };

// 1. determinate frame → bar exists, fill width set to 42%
_applyStageFrame({ stage:'fetch', status:'active', detail:'12 MB / 30 MB · 1 MB/s', pct:42, loaded:1, total:2, speed:1 });
let bar = fetchEl.querySelector('.upd-step-bar');
check('det_bar_created', !!bar);
check('det_not_indeterminate', bar && !bar._classes.has('is-indeterminate'));
let fill = bar && bar.querySelector('.upd-step-bar-fill');
check('det_fill_width', !!(fill && String(fill.style.width).indexOf('42') === 0));
check('det_detail_text', fetchEl.querySelector('.upd-step-detail').textContent.indexOf('/s') >= 0);

// 2. indeterminate frame (no pct) → bar marked is-indeterminate
_applyStageFrame({ stage:'deps', status:'active', detail:'Collecting httpx' });
let dbar = depsEl.querySelector('.upd-step-bar');
check('indet_bar_created', !!dbar);
check('indet_marked', dbar && dbar._classes.has('is-indeterminate'));

// 3. done frame → bar removed, step marked done
_applyStageFrame({ stage:'fetch', status:'done' });
check('done_bar_removed', !fetchEl.querySelector('.upd-step-bar'));
check('done_class', fetchEl._classes.has('is-done'));

// 4. skip frame → relabel + no bar
_applyStageFrame({ stage:'deps', status:'skip' });
check('skip_no_bar', !depsEl.querySelector('.upd-step-bar'));
check('skip_relabel', depsLbl.textContent === 'update.step.depsSkip');

// 6. REAL GIT FRAME SEQUENCE — replay exactly what _apply_via_git emits and
//    assert the bar is NEVER a static full determinate bar during the silent
//    checkout, and deps flips to indeterminate before pip lines. We rebuild a
//    fresh stepper and render the whole timeline, recording the bar state of
//    each stage after every frame.
{
  const F = El('li','upd-step'); F.appendChild(El('span','upd-step-dot')); F.appendChild(El('span','upd-step-label')); F.appendChild(El('span','upd-step-detail'));
  const P = El('li','upd-step'); P.appendChild(El('span','upd-step-dot')); P.appendChild(El('span','upd-step-label')); P.appendChild(El('span','upd-step-detail'));
  const D = El('li','upd-step'); D.appendChild(El('span','upd-step-dot')); D.appendChild(El('span','upd-step-label')); D.appendChild(El('span','upd-step-detail'));
  _updateStageEls = { fetch: F, pull: P, deps: D };
  const els = { fetch: F, pull: P, deps: D };

  // Exact frame order _apply_via_git produces (transfer ramps to 100 → flip to
  // pull indeterminate for the silent checkout → deps indeterminate → pip lines).
  const gitFrames = [
    { stage:'fetch', status:'active' },
    { stage:'fetch', status:'active', detail:'Receiving objects', pct:25, phase:'Receiving objects' },
    { stage:'fetch', status:'active', detail:'Receiving objects', pct:75, phase:'Receiving objects' },
    { stage:'fetch', status:'active', detail:'Receiving objects', pct:100, phase:'Receiving objects' },
    { stage:'fetch', status:'done' },
    { stage:'pull',  status:'active' },              // ← indeterminate sweep over checkout
    { stage:'pull',  status:'done' },
    { stage:'deps',  status:'active' },              // ← indeterminate before first pip line
    { stage:'deps',  status:'active', detail:'Collecting httpx' },
    { stage:'deps',  status:'active', detail:'Installing collected packages: httpx' },
    { stage:'deps',  status:'done' },
  ];

  // barState(el): 'full-det' (frozen full bar risk), 'det:<n>', 'sweep', 'none'.
  function barState(el){
    const b = el.querySelector('.upd-step-bar');
    if (!b) return 'none';
    if (b._classes.has('is-indeterminate')) return 'sweep';
    const f = b.querySelector('.upd-step-bar-fill');
    const w = f ? parseFloat(f.style.width) : 0;
    return (w >= 100) ? 'full-det' : ('det:' + Math.round(w||0));
  }

  const timeline = [];
  let everFrozenFull = false;      // a full-det bar that PERSISTS to next frame
  let prevFetchFull = false;
  gitFrames.forEach(function(fr, i){
    _applyStageFrame(fr);
    const snap = { i, frame: fr.stage + ':' + fr.status + (fr.pct!=null?(' '+fr.pct+'%'):''),
                   fetch: barState(F), pull: barState(P), deps: barState(D) };
    // If fetch was full-det last frame AND is STILL full-det now, it froze.
    if (prevFetchFull && snap.fetch === 'full-det') everFrozenFull = true;
    prevFetchFull = (snap.fetch === 'full-det');
    timeline.push(snap);
  });

  // Emit the frame-by-frame table for the human report.
  out.push('TIMELINE ' + JSON.stringify(timeline));

  // A: fetch never STAYS a full determinate bar into the next frame.
  check('git_no_frozen_full_bar', !everFrozenFull);
  // B: at the flip, pull is an indeterminate sweep (motion during checkout).
  const flip = timeline.find(function(s){ return s.frame === 'pull:active'; });
  check('git_pull_sweep_at_flip', !!flip && flip.pull === 'sweep');
  // C: at the flip, fetch bar is cleared (done), not lingering full.
  check('git_fetch_cleared_at_flip', !!flip && flip.fetch === 'none');
  // D: first deps-active is a sweep (no dead gap before first pip line).
  const depsFirst = timeline.find(function(s){ return s.frame === 'deps:active'; });
  check('git_deps_sweep_before_pip', !!depsFirst && depsFirst.deps === 'sweep');
  // E: no stage-to-stage handoff leaves the bar all-'none' for MORE THAN the
  //    single discrete transition frame. A momentary all-none on a
  //    '<stage>:done' frame is a normal handoff (the next stage's 'active'
  //    frame lands immediately after); a PERSISTENT all-none across two
  //    consecutive frames would be a real dead gap. Assert the latter never
  //    happens.
  let prevDead = false, persistentDead = false;
  timeline.forEach(function(s){
    const dead = (s.fetch==='none' && s.pull==='none' && s.deps==='none');
    if (dead && prevDead) persistentDead = true;
    prevDead = dead;
  });
  check('git_no_persistent_dead_gap', !persistentDead);
}

// 5. DOUBLE-NEUTER: strip _setStepBar body → determinate bar never created
{
  const NEEDLE = "function _setStepBar(el, pct, indeterminate) {";
  const neutered = SRC.replace(NEEDLE, "function _setStepBar(el, pct, indeterminate) { return;");
  check('neuter_applied', neutered !== SRC);
  loadModule(neutered);
  const f2 = El('li','upd-step'); f2.appendChild(El('span','upd-step-label'));
  _updateStageEls = { fetch: f2, pull: El('li','upd-step'), deps: El('li','upd-step') };
  _applyStageFrame({ stage:'fetch', status:'active', pct:55 });
  check('neuter_no_bar', !f2.querySelector('.upd-step-bar'));
}

console.log(out.join('\n'));
process.exit(0);
"""


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_update_stepper_progress_rendering():
    harness = os.path.join(HERE, '_update_progress_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, os.path.join(JS_DIR, 'update.js')],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'

    # Print the frame-by-frame bar-state timeline for the human report.
    import json
    for ln in output.splitlines():
        if ln.startswith('TIMELINE '):
            try:
                rows = json.loads(ln[len('TIMELINE '):])
                print('\n  git frame-by-frame bar state (fetch / pull / deps):')
                for r in rows:
                    print(f'    {r["frame"]:<28} fetch={r["fetch"]:<9} '
                          f'pull={r["pull"]:<6} deps={r["deps"]}')
            except (json.JSONDecodeError, KeyError, TypeError) as e:
                # best-effort reporting only — but the skip must be VISIBLE:
                # a silently-swallowed parse error hides a malformed harness
                # timeline line (the audit's bare-except:pass finding).
                print(f'  (timeline print skipped: {e})')

    fails = [l for l in output.splitlines() if l.startswith('FAIL')]
    assert not fails, 'update progress-render failures:\n' + output
    assert output.count('PASS') >= 18, f'expected >=18 PASS lines, got:\n{output}'
