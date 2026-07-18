'use strict';
/*
 * Records the COMPLETE ordered 2D-canvas draw stream produced by the REAL
 * shipped static/js/tofu-scene.js, for a given pet state, at the real
 * project-bar size. The stream (base gradient fill + every resolved dab:
 * x,y,rx,ry,ang,color,alpha + composite op) is emitted as JSON on stdout; a
 * Python cairo rasterizer replays it into a real image and pixel-diffs the foot
 * region. All geometry/colour/alpha come from the real module — only the
 * rasterizer is ours (cairo == node-canvas's engine), so this is a faithful
 * composite, not a re-implementation.
 *
 * Usage:  node tests/_scene_pixeldiff.js <W> <H> <FOOT_X|none> <ms> [decor]
 *   decor ∈ meadow|pool|sky (default meadow) — which scene to render.
 * Emits:  {"w":W,"h":H,"buffer":[ops...],"frame":[ops...]}
 *   buffer = the baked static scene (drawn once); frame = the per-frame overlay
 *   (glow + sparks + flow + wake + critter). Rasterizer paints buffer then frame.
 */
const path = require('path');
const fs = require('fs');

const W = parseInt(process.argv[2] || '360', 10);
const H = parseInt(process.argv[3] || '48', 10);
const FOOT = process.argv[4] || 'none';
const MS = parseFloat(process.argv[5] || '1600');
const DECOR = process.argv[6] || 'meadow';

function recorder() {
  const ops = [];
  const st = { fill: '#000000', alpha: 1, comp: 'source-over', grad: null };
  const stack = [];
  let cur = null;
  const ctx = {
    canvas: { width: W, height: H },
    setTransform() {}, clearRect() {},
    save() { stack.push({ fill: st.fill, alpha: st.alpha, comp: st.comp, grad: st.grad }); },
    restore() { const p = stack.pop(); if (p) { st.fill = p.fill; st.alpha = p.alpha; st.comp = p.comp; st.grad = p.grad; } cur = null; },
    translate(x, y) { cur = { x, y, ang: 0 }; },
    rotate(a) { if (cur) cur.ang = a; },
    beginPath() {},
    ellipse(cx, cy, rx, ry) { if (cur) { cur.rx = rx; cur.ry = ry; } },
    arc() {},
    fill() {
      if (cur && cur.rx != null) {
        ops.push({ t: 'dab', x: cur.x, y: cur.y, rx: cur.rx, ry: cur.ry,
                   ang: cur.ang, color: st.fill, alpha: st.alpha, comp: st.comp });
      }
    },
    fillRect(x, y, w, h) {
      ops.push({ t: 'rect', x, y, w, h, color: st.fill, grad: st.grad,
                 alpha: st.alpha, comp: st.comp });
    },
    createLinearGradient(x0, y0, x1, y1) {
      const g = { kind: 'linear', x0, y0, x1, y1, stops: [] };
      return { addColorStop(o, c) { g.stops.push([o, c]); }, __g: g };
    },
    createRadialGradient(x0, y0, r0, x1, y1, r1) {
      const g = { kind: 'radial', x0, y0, r0, x1, y1, r1, stops: [] };
      return { addColorStop(o, c) { g.stops.push([o, c]); }, __g: g };
    },
    drawImage() { ops.push({ t: 'blit' }); },
    set fillStyle(v) { if (v && v.__g) { st.grad = v.__g; st.fill = null; } else { st.fill = v; st.grad = null; } },
    get fillStyle() { return st.fill; },
    set strokeStyle(v) {}, get strokeStyle() { return '#000'; },
    set lineWidth(v) {}, get lineWidth() { return 1; },
    set globalAlpha(v) { st.alpha = v; }, get globalAlpha() { return st.alpha; },
    set globalCompositeOperation(v) { st.comp = v; }, get globalCompositeOperation() { return st.comp; },
  };
  return { ctx, ops };
}

let canvasN = 0;
const visRec = recorder();
const bufRec = recorder();
const fgRec = recorder();

let rafCb = null;
global.requestAnimationFrame = function (cb) { rafCb = cb; return 1; };
global.cancelAnimationFrame = function () {};
global.devicePixelRatio = 1;
global.window = {
  matchMedia() { return { matches: false, addEventListener() {}, addListener() {} }; },
  addEventListener() {},
  ResizeObserver: function () { return { observe() {}, disconnect() {} }; },
  MutationObserver: function () { return { observe() {}, disconnect() {} }; },
  devicePixelRatio: 1,
};
global.ResizeObserver = global.window.ResizeObserver;
global.MutationObserver = global.window.MutationObserver;

function mkEl() {
  return { _attrs: {}, className: '', style: {}, width: 0, height: 0,
    setAttribute(k, v) { this._attrs[k] = v; }, getAttribute(k) { return this._attrs[k]; },
    appendChild() {}, insertBefore() {}, querySelector() { return null; }, firstChild: null,
    getBoundingClientRect() { return { left: 0, right: W, top: 0, bottom: H, width: W, height: H }; } };
}
const bar = mkEl();
bar._attrs['data-decor'] = DECOR;
bar.getContext = undefined;
global.document = {
  readyState: 'complete', hidden: false,
  documentElement: { getAttribute(k) { return k === 'data-theme' ? 'tofu' : null; } },
  addEventListener() {},
  getElementById(id) { return id === 'projectBar' ? bar : null; },
  createElement(t) {
    if (t === 'canvas') {
      canvasN++;
      // #1 visible bg, #2 offscreen buffer, #3 foreground occlusion canvas
      const rec = canvasN === 1 ? visRec : (canvasN === 2 ? bufRec : fgRec);
      const e = mkEl();
      e.getContext = function () { return rec.ctx; };
      return e;
    }
    return mkEl();
  },
};

// Set the pet BEFORE loading so the very first frame already sees it.
if (FOOT !== 'none') {
  const fx = parseFloat(FOOT);
  global.window.TofuPet = { getState() { return { x: fx - 16, state: 'walk' }; } };
}

const src = fs.readFileSync(path.join(__dirname, '..', 'static', 'js', 'tofu-scene.js'), 'utf8');
eval(src);   // IIFE: mount()+_resize() bake into bufRec; _ensureLoop registers _loop via rAF

// The buffer bake is now in bufRec.ops. Pump the animation frame to render the
// visible overlay. _loop sets _t0 on first call (elapsed 0). To press a dent we
// need a few frames so the disturbance field builds; each _loop re-registers
// the next rAF, which our stub captures. Pump: t=100 (t0), then 40ms steps up
// to MS so the field accumulates like real playback.
visRec.ops.length = 0;
fgRec.ops.length = 0;
if (rafCb) {
  let t = 100;
  rafCb(t);                    // sets _t0=100, paints elapsed 0
  const step = 40;
  while (t < 100 + MS) {
    t += step;
    visRec.ops.length = 0;     // keep only the LAST frame's overlay
    fgRec.ops.length = 0;      // and the LAST frame's foreground occluders
    const cb = rafCb; rafCb = null; cb(t);
    if (!rafCb) break;         // loop parked (shouldn't, but guard)
  }
}

// buffer = baked bg (drawn once); frame = per-frame bg overlay; fg = the
// foreground occlusion plane (painted IN FRONT of the pet). The rasterizer
// composites buffer → frame → fg in that order.
process.stdout.write(JSON.stringify({ w: W, h: H, buffer: bufRec.ops, frame: visRec.ops, fg: fgRec.ops }));
