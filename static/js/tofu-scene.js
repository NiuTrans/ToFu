/*
 * tofu-scene.js — a PROCEDURAL, asset-free Impressionist background for the
 * project bar (tofu theme) that is ALIVE and INTERACTS with the pet.
 *
 * WHY this exists (and why it is a <canvas>, not more SVG): the bar's scene was
 * a flat vector SVG tile repeated across the width — the "simple illustration
 * that extends endlessly" look. This module paints the bar the way an
 * Impressionist canvas is built and the way a 2D game paints a living
 * backdrop: thousands of short, oriented BRUSH-DABS in broken/complementary
 * colour (Monet's grainstacks / water-lilies / dawn skies), stacked in depth
 * planes (hazy far → saturated near) for atmospheric perspective. No image
 * assets → single-file, proxy-relative, DPR-crisp at any zoom.
 *
 * THREE things make it feel alive & flowing (owner ask), layered CHEAPLY over
 * the ONE baked buffer so per-frame cost stays a blit + a few dozen dabs:
 *   1. FLOW — a thin set of pre-seeded overlay dabs that SWAY (meadow grass /
 *      sky clouds) or DRIFT (pool glints) every frame, so the painting breathes
 *      instead of sitting still.
 *   2. CRITTER — a small scene creature (meadow butterfly · pool fish · sky
 *      bird) drifts across the scene and can be SPOOKED. It exposes its x so
 *      the pet can chase it (TofuScene.critterX / spook).
 *   3. PET-ATTENTION — the ground GLOWS softly under the roaming pet (read from
 *      TofuPet.getState().x), so the background reacts to the pet. Both couplings
 *      are OPTIONAL and guarded — either module works alone.
 *
 * It renders the SAME three scenes the pet's switcher exposes (meadow · pool ·
 * sky · off), read from the bar's [data-decor] attribute. It sits at the scene
 * z-band (z0) — BELOW the roaming pet (z1) and the control pills (z2) — and
 * paints an OPAQUE base, so it fully occludes the SVG ::after ground beneath it
 * (the SVG stays as the no-JS / no-canvas fallback; every existing scene asset
 * + test invariant is untouched).
 *
 * Accessibility / energy (per the project's standing prefs): under
 * prefers-reduced-motion the scene is painted as ONE static frame with no rAF
 * loop (and the critter is frozen, so the pet — which also halts — never
 * chases); the loop pauses on `visibilitychange` (hidden tab) and whenever the
 * theme is not tofu or the scene is 'off'. Pointer-events:none + aria-hidden,
 * so it can never steal a click or a focus.
 *
 * Public surface (window.TofuScene): mount() · repaint() · setScene(s) ·
 * getScene() · SCENES · critterX() · critterInfo() · spook().
 */
(function () {
  'use strict';

  var CANVAS_CLASS = 'tofu-scene-canvas';
  var BAR_ID = 'projectBar';

  // ── Scene palettes. Each: a base vertical gradient (kept LIGHT in the upper
  // band where the frosted control pills float, so their #5E4E36 labels keep
  // contrast) + depth planes of dabs. A plane paints `density` dabs per 1000
  // px² of area, within a vertical band [yTop,yBot] (0..1 of height), oriented
  // at `ang` (radians, + jitter), sized [lo,hi] px, from a jittered colour set.
  // `spark` = the living specular shimmer colour. Palettes are Impressionist:
  // broken colour with complementary flecks (poppy pink in the meadow greens,
  // lavender lily reflections in the pool teals, peach rims in the dawn sky). ──
  var SCENES = ['meadow', 'pool', 'sky'];
  var PALETTES = {
    meadow: {
      seed: 1337,
      grad: [[0, '#EEF3E2'], [0.42, '#E4EDD0'], [0.72, '#CFE0AE'], [1, '#A8C77E']],
      spark: '#F2F6C8',
      glow: 'rgba(255,244,196,',   // warm sun
      flow: 'sway',                 // grass sways
      layers: [
        // far hazy field — pale, low contrast, flatter strokes
        { density: 3.4, yTop: 0.28, yBot: 0.64, ang: -1.15, jit: 0.5, lo: 1.6, hi: 3.4,
          colors: ['#C6D8A6', '#B9CE92', '#CDDCAE', '#D7C9D8', '#BFD59E'], alpha: [0.3, 0.55] },
        // poppy + buttercup flecks (the Impressionist complementary vibration)
        { density: 0.7, yTop: 0.4, yBot: 0.82, ang: -1.4, jit: 0.95, lo: 1.1, hi: 2.4,
          colors: ['#E0728A', '#E89AA6', '#E7B8C4', '#F0C24E', '#F4D06A'], alpha: [0.45, 0.78] },
        // mid grass bank
        { density: 5.2, yTop: 0.5, yBot: 0.9, ang: -1.45, jit: 0.42, lo: 1.8, hi: 4.0,
          colors: ['#9FC06C', '#7CA457', '#8DB55F', '#B7D488', '#6E9C48'], alpha: [0.42, 0.72] },
        // near dense grass — saturated, tall vertical blades, dark wet base
        { density: 8.5, yTop: 0.62, yBot: 1.04, ang: -1.55, jit: 0.3, lo: 2.4, hi: 6.0,
          colors: ['#6E9C48', '#5E8A3C', '#4C7233', '#3E5F28', '#9FCB70', '#57833A'], alpha: [0.55, 0.9] },
        // sunlit tips catching the light
        { density: 2.2, yTop: 0.58, yBot: 0.86, ang: -1.55, jit: 0.32, lo: 1.8, hi: 4.2,
          colors: ['#CFE6A6', '#D8EBA8', '#B9D888', '#E4F0B8'], alpha: [0.34, 0.6] }
      ]
    },
    pool: {
      seed: 4201,
      grad: [[0, '#EAF3F1'], [0.4, '#DDECE8'], [0.72, '#AFD2CC'], [1, '#5E948E']],
      spark: '#EAF7F4',
      glow: 'rgba(220,246,255,',
      flow: 'drift',                // water glints drift
      layers: [
        // far shimmering surface — pale horizontal strokes
        { density: 3.6, yTop: 0.28, yBot: 0.6, ang: 0.0, jit: 0.24, lo: 2.2, hi: 5.0,
          colors: ['#C6E0DB', '#B4D6D0', '#D2E7E3', '#CDBFD8', '#D8EBE6'], alpha: [0.28, 0.52] },
        // lily-pad + lavender reflection flecks
        { density: 1.1, yTop: 0.44, yBot: 0.9, ang: 0.05, jit: 0.5, lo: 1.6, hi: 3.6,
          colors: ['#8FB98A', '#7FAE7A', '#C9B6D6', '#E7C3CE', '#F0D7B0'], alpha: [0.4, 0.7] },
        // mid water
        { density: 5.6, yTop: 0.5, yBot: 0.92, ang: 0.0, jit: 0.2, lo: 2.6, hi: 5.8,
          colors: ['#7FB3AE', '#5E948E', '#6FA39D', '#93C3BD', '#6BA39C'], alpha: [0.42, 0.74] },
        // deep near water — dark, long horizontal strokes
        { density: 7.5, yTop: 0.66, yBot: 1.04, ang: 0.0, jit: 0.16, lo: 3.2, hi: 7.2,
          colors: ['#4E837C', '#3E706A', '#2F5A55', '#5E948E', '#356460'], alpha: [0.52, 0.86] },
        // horizontal specular glints riding the surface
        { density: 1.8, yTop: 0.52, yBot: 0.88, ang: 0.0, jit: 0.12, lo: 2.8, hi: 6.6,
          colors: ['#DFF3EF', '#EAF7F4', '#C7E7E1', '#F0FAF7'], alpha: [0.32, 0.6] }
      ]
    },
    sky: {
      seed: 90210,
      grad: [[0, '#F3F0F8'], [0.4, '#F1ECF3'], [0.72, '#F6E7DA'], [1, '#F2CFB4']],
      spark: '#FFF7E6',
      glow: 'rgba(255,232,196,',
      flow: 'clouds',               // clouds drift slowly
      layers: [
        // high haze — soft broad diagonal dabs
        { density: 2.6, yTop: 0.04, yBot: 0.52, ang: 0.22, jit: 0.42, lo: 3.2, hi: 7.2,
          colors: ['#EDE6F1', '#F0E3EC', '#F5EAE0', '#E7DAEC', '#F2EAF2'], alpha: [0.24, 0.44] },
        // lavender cloud shadow underbanks
        { density: 4.4, yTop: 0.4, yBot: 0.84, ang: 0.12, jit: 0.36, lo: 3.6, hi: 8.2,
          colors: ['#DED2E6', '#D9CFE0', '#E3D6C6', '#CFC2DC', '#D4C8DE'], alpha: [0.34, 0.62] },
        // sunlit cloud tops — warm cream
        { density: 5.4, yTop: 0.48, yBot: 0.98, ang: 0.08, jit: 0.32, lo: 3.8, hi: 9.0,
          colors: ['#FDF8F0', '#FBF0E2', '#F7E8D6', '#F3D9C0', '#FCF3E6'], alpha: [0.4, 0.72] },
        // warm peach rim near the base
        { density: 2.4, yTop: 0.64, yBot: 1.02, ang: 0.05, jit: 0.26, lo: 3.2, hi: 7.6,
          colors: ['#F3D9C0', '#EEC9A6', '#F6E1C6', '#F0CFAE'], alpha: [0.32, 0.58] }
      ]
    }
  };

  // ── Per-scene CRITTER: a little creature that drifts across the scene and
  // that the pet can chase. `y` is the baseline band (0..1 of height); `speed`
  // px/s; `colors` are dab colours. Kind picks the dab-only silhouette. ──
  var CRITTERS = {
    meadow: { kind: 'butterfly', y: 0.44, speed: 20, colors: ['#F0C24E', '#E0728A', '#FAF6E8'] },
    pool:   { kind: 'fish',      y: 0.74, speed: 15, colors: ['#5E948E', '#8FC3BD', '#EAF7F4'] },
    sky:    { kind: 'bird',      y: 0.26, speed: 30, colors: ['#B9A6C8', '#8E7BA0', '#F3E6D2'] }
  };

  // ── seeded PRNG (mulberry32): deterministic per (scene,size) so the painting
  // is stable across repaints (no flicker on resize / theme toggle). ──
  function rng(seed) {
    var s = seed >>> 0;
    return function () {
      s = (s + 0x6D2B79F5) | 0;
      var t = Math.imul(s ^ (s >>> 15), 1 | s);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }
  function lerp(a, b, t) { return a + (b - a) * t; }

  // One oriented brush-dab: a rotated, filled ellipse. Layering many of these
  // with jittered colour + alpha is what reads as painterly broken colour.
  function dab(ctx, x, y, len, wid, ang, color, alpha) {
    ctx.save();
    ctx.globalAlpha = alpha;
    ctx.fillStyle = color;
    ctx.translate(x, y);
    ctx.rotate(ang);
    ctx.beginPath();
    ctx.ellipse(0, 0, len, wid, 0, 0, 6.283185);
    ctx.fill();
    ctx.restore();
  }

  // ── module state ──
  var _bar = null;
  var _canvas = null, _ctx = null;
  var _buf = null, _bctx = null;      // offscreen static-scene buffer
  var _dpr = 1;
  var _w = 0, _h = 0;                 // CSS px
  var _scene = 'meadow';
  var _raf = 0, _t0 = 0, _lastMs = 0;
  var _reduced = false, _paused = false;
  var _sparks = [];                   // living shimmer dabs (positions seeded)
  var _flow = [];                     // living FLOW dabs (sway / drift overlay)
  var _critter = null;                // { x, y, dir, vx, base, phase, spookUntil }

  // ── Pet ⋈ scene WAKE: the pet's foot disturbs the PAINTED scene (owner ask —
  // "stepping on grass presses it down / stepping on the pool splashes"). Each
  // frame we read the pet's foot x from TofuPet (guarded), track its motion,
  // and (a) DEFORM nearby flow dabs — part + press grass / part water / stir
  // clouds — and (b) spawn transient scene-flavored wake marks — grass kicked
  // up / a splash ripple / a cloud puff — painted ON the canvas, so the pet and
  // the scene read as ONE layer instead of two stacked ones. Every bit no-ops
  // when TofuPet is absent, so the scene still works entirely alone. ──
  var _petPrevX = null, _petVX = 0;
  var _wake = [];                     // transient foot marks {x,y,born,seed}
  var _wakeAccum = 0;                 // foot travel since the last spawn
  var WAKE_STEP_PX = 20;              // px of foot travel between wake marks
  var WAKE_MAX = 8, WAKE_LIFE = 700;  // concurrent cap + lifetime(ms)
  var WAKE_RADIUS = 24;              // flow-deform reach around the foot (px)

  // Glow intensities — DIMMED per owner ("the moving light in the back is too
  // glaring"). Kept as named constants so the brightness stays tunable and the
  // dim is test-guarded. Both radials composite additively ('lighter').
  var SUN_GLOW_PEAK = 0.16;           // was 0.30 (drifting sun radial, centre)
  var SUN_GLOW_MID = 0.06;            // was 0.12 (mid stop)
  var SUN_SWEEP = 0.34;               // was 0.42 (horizontal travel amplitude)
  var PET_GLOW_PEAK = 0.14;           // was 0.26 (ground glow under the pet)
  var PET_GLOW_MID = 0.05;            // was 0.10 (mid stop)

  function _isTofu() {
    try {
      return document.documentElement.getAttribute('data-theme') === 'tofu';
    } catch (e) { return false; }
  }
  function _readScene() {
    var d = _bar && _bar.getAttribute('data-decor');
    if (d === 'off') return 'off';
    if (SCENES.indexOf(d) !== -1) return d;
    return 'meadow';
  }

  // Paint the static painterly scene into the offscreen buffer at the current
  // size. Called on mount, resize, and scene change — NOT per frame.
  function _paintBuffer() {
    if (!_bctx || _w <= 0 || _h <= 0) return;
    if (_scene === 'off') { _sparks = []; _flow = []; _critter = null; _wake = []; _petPrevX = null; _wakeAccum = 0; return; }
    _wake = []; _petPrevX = null; _wakeAccum = 0;
    var pal = PALETTES[_scene] || PALETTES.meadow;
    var b = _bctx, w = _w, h = _h;
    b.setTransform(_dpr, 0, 0, _dpr, 0, 0);
    b.clearRect(0, 0, w, h);
    // opaque base wash (occludes the SVG fallback beneath the canvas)
    var g = b.createLinearGradient(0, 0, 0, h);
    for (var gi = 0; gi < pal.grad.length; gi++) g.addColorStop(pal.grad[gi][0], pal.grad[gi][1]);
    b.fillStyle = g;
    b.fillRect(0, 0, w, h);
    // depth planes of brush-dabs
    var R = rng(pal.seed ^ (Math.round(w) * 131 + Math.round(h)));
    var area = w * h;
    for (var li = 0; li < pal.layers.length; li++) {
      var L = pal.layers[li];
      var n = Math.max(4, Math.round(L.density * area / 1000));
      for (var i = 0; i < n; i++) {
        var x = R() * w;
        var y = lerp(L.yTop, L.yBot, R()) * h;
        var len = lerp(L.lo, L.hi, R());
        var wid = len * lerp(0.32, 0.6, R());
        var ang = L.ang + (R() - 0.5) * 2 * L.jit;
        var color = L.colors[(R() * L.colors.length) | 0];
        var alpha = lerp(L.alpha[0], L.alpha[1], R());
        dab(b, x, y, len, wid, ang, color, alpha);
      }
    }
    // pre-seed the living shimmer dabs (their positions are stable; only their
    // alpha/offset oscillate per frame in the overlay).
    _sparks = [];
    var sn = Math.max(3, Math.round(w / 46));
    var sang = (pal.layers[0] && pal.layers[0].ang) || 0;
    for (var si = 0; si < sn; si++) {
      _sparks.push({
        x: R() * w,
        y: lerp(0.5, 0.92, R()) * h,
        len: lerp(1.6, 3.4, R()),
        ph: R() * 6.283185,          // phase offset
        sp: lerp(0.6, 1.6, R()),     // twinkle speed
        ang: sang + (R() - 0.5) * 0.5
      });
    }
    // pre-seed the FLOW dabs — the layer that makes the painting BREATHE. They
    // ride on top of the baked scene each frame, swaying (grass), drifting
    // (water glints), or gliding (clouds) per the palette's `flow` mode.
    _flow = [];
    var fn = Math.max(8, Math.round(w / 8));
    var nearColors = (pal.layers[pal.layers.length - 1] || {}).colors || ['#FFFFFF'];
    for (var fi = 0; fi < fn; fi++) {
      var isCloud = pal.flow === 'clouds';
      _flow.push({
        x: R() * w,
        y: (pal.flow === 'sway' ? lerp(0.5, 0.99, R()) : lerp(0.3, 0.9, R())) * h,
        len: lerp(isCloud ? 5 : 2.2, isCloud ? 11 : 5.5, R()),
        wid: lerp(0.34, 0.6, R()),
        ang: (pal.flow === 'sway' ? -1.5 : 0) + (R() - 0.5) * 0.5,
        color: nearColors[(R() * nearColors.length) | 0],
        alpha: lerp(0.28, 0.6, R()),
        ph: R() * 6.283185,
        sp: lerp(0.6, 1.5, R())
      });
    }
    _spawnCritter(R);
  }

  // (Re)seed the scene critter for the current scene, off-screen on a random
  // side so it drifts in. `R` is the seeded PRNG (falls back to Math.random).
  function _spawnCritter(R) {
    var rnd = R || Math.random;
    var C = CRITTERS[_scene];
    if (!C || _scene === 'off') { _critter = null; return; }
    var dir = rnd() < 0.5 ? 1 : -1;
    _critter = {
      kind: C.kind,
      x: dir > 0 ? -12 : _w + 12,
      base: C.y * _h,
      dir: dir,
      vx: dir * C.speed,
      speed: C.speed,
      colors: C.colors,
      phase: rnd() * 6.283185,
      spookUntil: 0
    };
  }

  // The living overlay drawn each frame on the visible canvas: blit the baked
  // scene, then a slow-drifting warm sun glow (additive) + twinkling specular
  // dabs + the FLOW layer + the CRITTER + the pet-attention glow. `ms` is
  // elapsed time; when static (reduced motion) it's a fixed 0.
  function _paintFrame(ms) {
    if (!_ctx || _w <= 0 || _h <= 0 || _scene === 'off') return;
    var pal = PALETTES[_scene] || PALETTES.meadow;
    var c = _ctx, w = _w, h = _h;
    var dt = Math.max(0, Math.min(0.08, (ms - _lastMs) / 1000));
    _lastMs = ms;
    c.setTransform(_dpr, 0, 0, _dpr, 0, 0);
    c.clearRect(0, 0, w, h);
    if (_buf) c.drawImage(_buf, 0, 0, w, h);
    // Track the pet's foot motion (guarded) so the flow-deform + wake marks can
    // react to WHERE and HOW FAST the cat is moving. Runs before the layers so
    // _paintFlow can press the grass under the current foot position.
    _trackPet(dt, w);
    // ground GLOW that follows the roaming pet — the background reacting to the
    // pet (read from TofuPet; optional + guarded, so the scene works alone).
    _paintPetAttention(c, pal, w, h);
    // drifting sun glow — a soft warm radial that sweeps horizontally (DIMMED:
    // owner found the moving light too glaring; peak/mid/sweep are tuned-down
    // named constants).
    var sx = (0.5 + SUN_SWEEP * Math.sin(ms * 0.00006)) * w;
    var sy = h * 0.14;
    var rg = c.createRadialGradient(sx, sy, 0, sx, sy, h * 1.6);
    rg.addColorStop(0, pal.glow + SUN_GLOW_PEAK + ')');
    rg.addColorStop(0.4, pal.glow + SUN_GLOW_MID + ')');
    rg.addColorStop(1, pal.glow + '0)');
    c.save();
    c.globalCompositeOperation = 'lighter';
    c.fillStyle = rg;
    c.fillRect(0, 0, w, h);
    // twinkling specular dabs (the shimmer): additive so they read as glints
    for (var i = 0; i < _sparks.length; i++) {
      var s = _sparks[i];
      var tw = 0.5 + 0.5 * Math.sin(ms * 0.001 * s.sp + s.ph);
      var a = 0.12 + 0.42 * tw * tw;
      var dx = Math.sin(ms * 0.0007 * s.sp + s.ph) * 0.8;   // micro sway
      dab(c, s.x + dx, s.y, s.len * (0.7 + 0.5 * tw), s.len * 0.5, s.ang, pal.spark, a);
    }
    c.restore();
    // the FLOW layer — swaying grass / drifting glints / gliding clouds. It now
    // also PRESSES/PARTS around the pet's foot (see _paintFlow's deform).
    _paintFlow(c, pal, ms, w, h);
    // the PET-WAKE marks — grass kicked up / a splash ripple / a cloud puff at
    // the foot, painted ON the canvas so pet & scene read as one layer.
    _paintWake(c, pal, ms, w, h);
    // the critter (drawn last, above the scene but below the pet at z1)
    _paintCritter(c, ms, dt, w, h);
  }

  // FLOW: the overlay that makes the scene breathe. `sway` rocks grass blades,
  // `drift` scrolls water glints, `clouds` glides cloud puffs — all cheap.
  function _paintFlow(c, pal, ms, w, h) {
    if (!_flow.length) return;
    var mode = pal.flow || 'sway';
    // Ground anchor (NOT the foot anchor): stays live during a DRAG so the
    // scene keeps compressing under the HELD pet — the owner ask that dragging
    // the cat must still interact with the background, not just float over it.
    var px = _petGroundX();
    // A slow global BREEZE that swells and eases, so the whole meadow/water/sky
    // moves as one instead of each dab twitching independently (the fix for
    // "the background doesn't move, only the light spots do").
    var gust = 1 + 0.6 * Math.sin(ms * 0.00022);
    for (var i = 0; i < _flow.length; i++) {
      var f = _flow[i];
      var x = f.x, ang = f.ang, y = f.y, sc = 1, af = 1;
      if (mode === 'sway') {
        ang = f.ang + Math.sin(ms * 0.0013 * f.sp + f.ph) * 0.42 * gust;   // blade rock
        x = f.x + Math.sin(ms * 0.0011 * f.sp + f.ph) * 2.6 * gust;
        y = f.y + Math.sin(ms * 0.0016 * f.sp + f.ph) * 0.8;               // vertical breathe
      } else if (mode === 'drift') {
        x = ((f.x + ms * 0.02 * f.sp) % (w + 20)) - 10;                    // glints slide
      } else { // clouds
        x = ((f.x + ms * 0.011 * f.sp) % (w + 24)) - 12;                   // slow glide
      }
      // FOOT DEFORM: blades/glints within WAKE_RADIUS of the foot get PARTED
      // (leaned away from the foot) and PRESSED (grass: shorter + toward the
      // ground; water: brighter/flatter splash; clouds: gently stirred). This
      // is what makes 'walking through' read on the scene layer itself.
      if (px != null) {
        var d = x - px;
        var ad = Math.abs(d);
        if (ad < WAKE_RADIUS) {
          var k = 1 - ad / WAKE_RADIUS;                 // 1 at foot → 0 at edge
          var lean = (d >= 0 ? 1 : -1) * k * 0.5;       // part away from the foot
          if (mode === 'sway') {
            ang += lean;                                // blades bend aside
            sc = 1 - k * 0.45;                          // pressed down (shorter)
            y += k * 2.2;                               // sink toward the base
          } else if (mode === 'drift') {
            sc = 1 + k * 0.5;                           // splash spreads
            af = 1 + k * 0.6;                           // glint brightens
          } else {
            x += lean * 3;                              // clouds nudged aside
          }
        }
      }
      var pulse = 0.75 + 0.25 * Math.sin(ms * 0.0013 * f.sp + f.ph);
      dab(c, x, y, f.len * sc, f.len * f.wid * sc, ang, f.color, Math.min(1, f.alpha * pulse * af));
    }
  }

  // A soft warm ground glow under the roaming pet — makes the scene react to
  // the pet. Reads TofuPet.getState().x (CSS px along the bar); fully guarded
  // so the scene never depends on the pet being present.
  function _paintPetAttention(c, pal, w, h) {
    var px = _petX();
    if (px == null) return;
    var gy = h * 0.82;
    var rg = c.createRadialGradient(px, gy, 0, px, gy, Math.max(20, h * 0.7));
    rg.addColorStop(0, pal.glow + PET_GLOW_PEAK + ')');
    rg.addColorStop(0.5, pal.glow + PET_GLOW_MID + ')');
    rg.addColorStop(1, pal.glow + '0)');
    c.save();
    c.globalCompositeOperation = 'lighter';
    c.fillStyle = rg;
    c.fillRect(0, 0, w, h);
    c.restore();
  }
  function _petX() {
    try {
      if (window.TofuPet && typeof window.TofuPet.getState === 'function') {
        var st = window.TofuPet.getState();
        // pet box is ~32px; its foot-centre is x + 16 along the bar
        if (st && typeof st.x === 'number') return st.x + 16;
      }
    } catch (e) { /* pet absent — no attention glow, harmless */ }
    return null;
  }

  // Foot-contact x used by the flow-deform + wake spawner. Same source as the
  // attention glow (_petX), but only reports a foot when the cat is actually on
  // the ground moving/standing — NOT while it's being dragged in the air (state
  // 'drag') — so a lifted cat doesn't press grass under thin air.
  function _petFootX() {
    try {
      if (window.TofuPet && typeof window.TofuPet.getState === 'function') {
        var st = window.TofuPet.getState();
        if (st && typeof st.x === 'number' && st.state !== 'drag') return st.x + 16;
      }
    } catch (e) { /* pet absent — no wake, harmless */ }
    return null;
  }
  // Ground-contact x used by the FLOW-DEFORM (grass parting) + the contact
  // shadow. UNLIKE _petFootX this stays live while the cat is DRAGGED, so the
  // scene keeps reacting under the held pet (owner ask — a dragged cat must
  // still interact with the background). Reports the drag state so callers can
  // soften the effect for a lifted (airborne) cat. Returns { x, drag } or null.
  function _petGround() {
    try {
      if (window.TofuPet && typeof window.TofuPet.getState === 'function') {
        var st = window.TofuPet.getState();
        if (st && typeof st.x === 'number') return { x: st.x + 16, drag: st.state === 'drag' };
      }
    } catch (e) { /* pet absent — harmless */ }
    return null;
  }
  function _petGroundX() {
    var g = _petGround();
    return g ? g.x : null;
  }

  // Follow the pet's foot each frame: record velocity + accumulate travel, and
  // when the foot has moved WAKE_STEP_PX (i.e. the cat is actually walking, not
  // just standing), drop a transient wake mark at the trailing foot. Fully
  // guarded: no pet → resets tracking and spawns nothing.
  function _trackPet(dt, w) {
    var px = _petFootX();
    if (px == null) { _petPrevX = null; _petVX = 0; _wakeAccum = 0; return; }
    if (_petPrevX == null) { _petPrevX = px; _petVX = 0; return; }
    var d = px - _petPrevX;
    _petPrevX = px;
    _petVX = dt > 0 ? d / dt : 0;
    _wakeAccum += Math.abs(d);
    if (_wakeAccum >= WAKE_STEP_PX && _wake.length < WAKE_MAX) {
      _wakeAccum = 0;
      // trailing foot: a touch behind the direction of travel
      var back = d >= 0 ? -4 : 4;
      _wake.push({ x: Math.max(0, Math.min(w, px + back)), born: _lastMs, seed: Math.random() });
    }
  }

  // Paint the transient wake marks the pet left in the scene — a canvas-level
  // footprint so pet & background feel like ONE layer. Flavor is keyed off the
  // SAME active scene (no parallel state): meadow kicks up a couple of grass
  // dabs, pool opens a splash ring, sky puffs a wisp. Each mark fades over
  // WAKE_LIFE and self-culls; drawn with the palette's own near/spark colours.
  function _paintWake(c, pal, ms, w, h) {
    if (!_wake.length) return;
    var mode = pal.flow || 'sway';
    var gy = h * 0.9;
    var kept = [];
    for (var i = 0; i < _wake.length; i++) {
      var mk = _wake[i];
      var age = ms - mk.born;
      if (age < 0 || age > WAKE_LIFE) continue;   // expired → drop
      kept.push(mk);
      var life = age / WAKE_LIFE;                 // 0..1
      var fade = 1 - life;
      var near = (pal.layers[pal.layers.length - 1] || {}).colors || ['#FFFFFF'];
      if (mode === 'sway') {
        // two blades flick up + apart, then settle — "grass parted underfoot"
        var lift = life * 5;
        dab(c, mk.x - 2, gy - lift, 2.4 * fade + 0.6, 1.0, -1.3 - life * 0.4, near[(mk.seed * near.length) | 0], 0.55 * fade);
        dab(c, mk.x + 2, gy - lift, 2.4 * fade + 0.6, 1.0, -1.8 + life * 0.4, near[((mk.seed * 7) | 0) % near.length], 0.5 * fade);
      } else if (mode === 'drift') {
        // an expanding splash: a bright centre plip + two spreading side dabs
        // that read as a ripple opening out (dab-only, no stroke — same
        // convention as the critter so it's mock-context safe).
        var rr = 1.5 + life * 7;
        dab(c, mk.x, gy, 1.4 * fade + 0.4, 0.7 * fade + 0.2, 0, pal.spark, 0.6 * fade);
        dab(c, mk.x - rr, gy, 1.6 * fade, 0.7 * fade, 0, pal.spark, 0.4 * fade);
        dab(c, mk.x + rr, gy, 1.6 * fade, 0.7 * fade, 0, pal.spark, 0.4 * fade);
      } else {
        // a soft rising cloud puff
        dab(c, mk.x, gy - 3 - life * 6, 3.2 * (0.6 + life), 2.0 * (0.6 + life), 0, pal.spark, 0.32 * fade);
      }
    }
    _wake = kept;
  }

  // Update + draw the critter. dab-only silhouette (no path/stroke), so it works
  // under the test's recording mock context. Wraps around the edges; a spook
  // makes it dart the other way for a beat.
  function _paintCritter(c, ms, dt, w, h) {
    if (!_critter) return;
    var cr = _critter;
    var spooked = ms < cr.spookUntil;
    var sp = cr.speed * (spooked ? 3.4 : 1);
    cr.vx = cr.dir * sp;
    cr.x += cr.vx * dt;
    // wrap around and re-randomise the baseline a touch when off-screen
    if (cr.x < -16) { cr.x = w + 14; cr.dir = -1; cr.spookUntil = 0; }
    else if (cr.x > w + 16) { cr.x = -14; cr.dir = 1; cr.spookUntil = 0; }
    var bob = Math.sin(ms * 0.004 + cr.phase) * (cr.kind === 'fish' ? 1.4 : 3.2);
    var y = cr.base + bob;
    var col = cr.colors;
    c.save();
    c.globalAlpha = 1;
    if (cr.kind === 'butterfly') {
      var flap = 0.5 + 0.5 * Math.sin(ms * 0.02 + cr.phase);   // wing beat
      dab(c, cr.x, y, 2.6, 1.0 + flap * 1.6, -0.5 * cr.dir, col[0], 0.85);
      dab(c, cr.x, y, 2.6, 1.0 + flap * 1.6, 0.5 * cr.dir, col[1], 0.8);
      dab(c, cr.x, y, 0.9, 2.2, 0, '#4A3A24', 0.9);            // body
    } else if (cr.kind === 'fish') {
      dab(c, cr.x, y, 3.6, 1.7, 0, col[1], 0.9);               // body
      dab(c, cr.x + 4.2 * -cr.dir, y, 1.8, 1.9, 0.4 * cr.dir, col[0], 0.85); // tail
      dab(c, cr.x + 1.4 * cr.dir, y - 0.4, 0.6, 0.6, 0, '#26433F', 0.9);     // eye
    } else { // bird — two flapping wing strokes + tiny body
      var wb = Math.sin(ms * 0.016 + cr.phase);
      dab(c, cr.x - 2.4, y - wb * 1.4, 3.0, 0.7, -0.5 + wb * 0.4, col[0], 0.8);
      dab(c, cr.x + 2.4, y - wb * 1.4, 3.0, 0.7, 0.5 - wb * 0.4, col[1], 0.8);
      dab(c, cr.x, y, 1.2, 0.9, 0, col[2], 0.85);
    }
    c.restore();
  }

  function _loop(ts) {
    _raf = 0;
    if (_paused || _reduced || !_isTofu() || _scene === 'off') return;   // loop parks
    if (!_t0) { _t0 = ts; _lastMs = 0; }
    _paintFrame(ts - _t0);
    _raf = requestAnimationFrame(_loop);
  }

  // Start (or keep) the animation loop iff it should be running; otherwise
  // paint a single static frame. One place decides run-vs-static.
  function _ensureLoop() {
    if (!_ctx) return;
    var active = _isTofu() && _scene !== 'off';
    if (!active) { if (_raf) { cancelAnimationFrame(_raf); _raf = 0; } return; }
    if (_reduced || _paused) {
      if (_raf) { cancelAnimationFrame(_raf); _raf = 0; }
      _lastMs = 0;
      _paintFrame(0);              // one static, fully-painted frame
      return;
    }
    if (!_raf) _raf = requestAnimationFrame(_loop);
  }

  // (Re)size the canvas + buffer to the bar's box at the current DPR, then
  // re-bake the static scene. Cheap-guards a zero-size (bar still display:none).
  function _resize() {
    if (!_canvas || !_bar) return;
    var r = _bar.getBoundingClientRect();
    var w = Math.round(r.width), h = Math.round(r.height);
    if (w <= 0 || h <= 0) return;                 // bar hidden — wait for a real box
    _dpr = Math.max(1, Math.min(3, window.devicePixelRatio || 1));
    _w = w; _h = h;
    _canvas.width = Math.round(w * _dpr);
    _canvas.height = Math.round(h * _dpr);
    _canvas.style.width = w + 'px';
    _canvas.style.height = h + 'px';
    if (_buf) { _buf.width = _canvas.width; _buf.height = _canvas.height; }
    _paintBuffer();
    _ensureLoop();
  }

  function repaint() { _paintBuffer(); _ensureLoop(); }

  function setScene(s) {
    if (s !== 'off' && SCENES.indexOf(s) === -1) return _scene;
    if (s === _scene) { _ensureLoop(); return _scene; }
    _scene = s;
    _paintBuffer();
    _ensureLoop();
    return _scene;
  }
  function getScene() { return _scene; }

  // ── Pet ⋈ scene interaction seam (read by tofu-pet.js's chase behaviour).
  // critterX(): where the critter is now (CSS px along the bar), or null when
  // there's nothing to chase (off / reduced / non-tofu / no critter). spook():
  // the pet pounced — the critter darts away for a beat. ──
  function critterX() {
    if (!_critter || _reduced || _paused || _scene === 'off' || !_isTofu()) return null;
    return _critter.x;
  }
  function critterInfo() {
    if (!_critter) return null;
    return { x: _critter.x, y: _critter.base, kind: _critter.kind, dir: _critter.dir };
  }
  function spook() {
    if (!_critter) return;
    // dart AWAY from the pet if we can tell where it is, else just bolt.
    var px = _petX();
    if (px != null) _critter.dir = (_critter.x >= px) ? 1 : -1;
    _critter.spookUntil = _lastMs + 900;
  }

  function mount() {
    var bar = document.getElementById(BAR_ID);
    if (!bar) return false;
    _bar = bar;
    _canvas = bar.querySelector('.' + CANVAS_CLASS);
    if (!_canvas) {
      _canvas = document.createElement('canvas');
      _canvas.className = CANVAS_CLASS;
      _canvas.setAttribute('aria-hidden', 'true');
      bar.insertBefore(_canvas, bar.firstChild);
    }
    try { _ctx = _canvas.getContext('2d'); } catch (e) { _ctx = null; }
    if (!_ctx) return false;                       // no 2d context — SVG fallback shows
    _buf = document.createElement('canvas');
    try { _bctx = _buf.getContext('2d'); } catch (e2) { _bctx = null; }
    // Stamp the runtime marker so CSS suppresses the SVG scene GROUND (::after)
    // in favour of this canvas. CRITICAL for correctness, not cosmetic: the SVG
    // ground and this canvas both sit at z0, but a generated ::after composites
    // AFTER the element's real children — so a full-height opaque ::after would
    // paint ON TOP of the canvas and the painting would be invisible. There is
    // no integer z between the canvas (0) and the ::before edge crest (1), so
    // the fix is to HIDE ::after under this marker (CSS), never a z nudge. Only
    // set once getContext('2d') has succeeded, so a no-canvas browser leaves
    // the marker off and keeps the SVG fallback visible.
    try { bar.setAttribute('data-scene-canvas', 'on'); } catch (e3) { /* attr set can't realistically fail; harmless */ }
    _scene = _readScene();
    _resize();
    return true;
  }

  function _watchReducedMotion() {
    if (!window.matchMedia) return;
    var mq = window.matchMedia('(prefers-reduced-motion: reduce)');
    _reduced = !!mq.matches;
    var onChange = function () { _reduced = !!mq.matches; _ensureLoop(); };
    if (mq.addEventListener) mq.addEventListener('change', onChange);
    else if (mq.addListener) mq.addListener(onChange);   // Safari <14
  }

  function _boot() {
    _watchReducedMotion();
    if (!mount()) {
      var tries = 0;
      var iv = setInterval(function () { if (mount() || ++tries > 20) clearInterval(iv); }, 250);
    }
    // Pause the loop when the tab is hidden (energy + attention).
    document.addEventListener('visibilitychange', function () {
      _paused = document.hidden; _ensureLoop();
    });
    // Follow the bar's box + the scene attribute (set by tofu-pet.js) + the
    // app theme, all without coupling to the pet: attribute/resize observers.
    if (window.ResizeObserver && _bar) {
      try { new ResizeObserver(function () { _resize(); }).observe(_bar); } catch (e) { /* harmless */ }
    }
    window.addEventListener('resize', _resize);
    if (window.MutationObserver) {
      try {
        if (_bar) new MutationObserver(function () {
          var s = _readScene();
          if (s !== _scene) setScene(s); else _ensureLoop();
        }).observe(_bar, { attributes: true, attributeFilter: ['data-decor'] });
        new MutationObserver(function () { repaint(); }).observe(
          document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
      } catch (e3) { /* harmless — scene just won't live-switch */ }
    }
    // Explicit event seam (mirrors tofu-pet.js) so app code can drive it too.
    document.addEventListener('tofu:decor', function (e) {
      if (e && typeof e.detail === 'string') setScene(e.detail === 'off' ? 'off' : e.detail);
    });
  }

  window.TofuScene = {
    mount: mount,
    repaint: repaint,
    setScene: setScene,
    getScene: getScene,
    SCENES: SCENES,
    critterX: critterX,
    critterInfo: critterInfo,
    spook: spook
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', _boot);
  } else {
    _boot();
  }
})();
