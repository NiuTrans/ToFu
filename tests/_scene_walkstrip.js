'use strict';
/*
 * Drives the REAL shipped static/js/tofu-scene.js with a MOVING pet (walking
 * left→right) and emits the full draw stream (buffer once + per-keyframe frame
 * overlay + fg occlusion plane) at a set of pet x-positions, so a cairo
 * rasterizer can render a multi-frame strip that shows the near blades PART as
 * the cat passes and SPRING BACK behind it.
 *
 * Unlike _scene_pixeldiff.js (fixed footx), this advances the pet a little each
 * animation frame so the disturbance field genuinely builds a travelling dent.
 *
 * Usage:  node tests/_scene_walkstrip.js <W> <H> <decor> <x0> <x1> <nKeys>
 * Emits:  {"w","h","buffer",[baked], "frames":[{x, frame:[...], fg:[...]}...]}
 */
const path = require('path');
const fs = require('fs');

const W = parseInt(process.argv[2] || '360', 10);
const H = parseInt(process.argv[3] || '48', 10);
const DECOR = process.argv[4] || 'meadow';
const X0 = parseFloat(process.argv[5] || '120');
const X1 = parseFloat(process.argv[6] || '210');
const NKEYS = parseInt(process.argv[7] || '4', 10);

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

// The pet's foot x is driven by this mutable var; getState() reads it live.
let petFoot = X0;
global.window.TofuPet = { getState() { return { x: petFoot - 16, state: 'walk' }; } };

global.document = {
  readyState: 'complete', hidden: false,
  documentElement: { getAttribute(k) { return k === 'data-theme' ? 'tofu' : null; } },
  addEventListener() {},
  getElementById(id) { return id === 'projectBar' ? bar : null; },
  createElement(t) {
    if (t === 'canvas') {
      canvasN++;
      const rec = canvasN === 1 ? visRec : (canvasN === 2 ? bufRec : fgRec);
      const e = mkEl();
      e.getContext = function () { return rec.ctx; };
      return e;
    }
    return mkEl();
  },
};

const src = fs.readFileSync(path.join(__dirname, '..', 'static', 'js', 'tofu-scene.js'), 'utf8');
eval(src);   // IIFE mounts + bakes buffer + registers _loop via rAF

// Walk the pet X0→X1 over many small animation steps so the disturbance field
// builds a TRAVELLING dent. Capture the frame+fg streams at NKEYS evenly-spaced
// positions along the path.
const buffer = bufRec.ops.slice();
const frames = [];
const totalSteps = 90;                       // ~ real playback frames
const captureAt = [];
for (let k = 0; k < NKEYS; k++) captureAt.push(Math.round((k + 1) / NKEYS * (totalSteps - 1)));

let t = 100;
if (rafCb) { rafCb(t); }                      // t0
for (let s = 0; s < totalSteps; s++) {
  t += 40;
  petFoot = X0 + (X1 - X0) * (s / (totalSteps - 1));   // advance the walker
  visRec.ops.length = 0;
  fgRec.ops.length = 0;
  if (!rafCb) break;
  const cb = rafCb; rafCb = null; cb(t);
  if (captureAt.includes(s)) {
    frames.push({ x: petFoot, frame: visRec.ops.slice(), fg: fgRec.ops.slice() });
  }
}

process.stdout.write(JSON.stringify({ w: W, h: H, buffer: buffer, frames: frames }));
