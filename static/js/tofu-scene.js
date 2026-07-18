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
 *   3. DEPTH / OCCLUSION — a SECOND canvas at z2 (IN FRONT of the DOM pet at z1)
 *      paints the NEAR band of the scene — tall grass / reeds / low mist — so
 *      the cat is partly hidden by it and reads as standing AMONG the scene, not
 *      pasted on top of a backdrop (the 2.5D game-rendering depth). It reads the
 *      SAME disturbance field, so the FRONT of the scene parts around the cat as
 *      it walks through. Coupling is OPTIONAL + guarded — the scene works alone.
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
        // near dense grass — saturated, tall vertical blades, dark wet base.
        // `live:true` → this layer is NOT baked into the static buffer; it is
        // rendered per-frame as SWAYING, base-anchored BLADES that bend at their
        // root and FLATTEN when the pet presses them (the near grass is a live
        // interactive layer, not a photo — so it genuinely presses down instead
        // of a few overlay dabs popping out over a static field).
        { density: 8.5, yTop: 0.62, yBot: 1.04, ang: -1.55, jit: 0.3, lo: 2.4, hi: 6.0, live: true,
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
        // deep near water — dark, long horizontal strokes. `live:true` → this
        // near band is NOT baked; it is rendered per-frame as RIPPLING water
        // that undulates and, under the pet, SPLASHES outward + brightens (a
        // wet crown), not a grass-flatten. The near water is thus a live
        // interactive layer, not a photo.
        { density: 7.5, yTop: 0.66, yBot: 1.04, ang: 0.0, jit: 0.16, lo: 3.2, hi: 7.2, live: true,
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
        // sunlit cloud tops — warm cream. `live:true` → this near cloud band is
        // NOT baked; it is rendered per-frame as DRIFTING puffs that glide and,
        // under the pet, get SHOVED aside + compressed (not a grass-flatten).
        // The near clouds are thus a live interactive layer, not a photo.
        { density: 5.4, yTop: 0.48, yBot: 0.98, ang: 0.08, jit: 0.32, lo: 3.8, hi: 9.0, live: true,
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
  // Blend two #RRGGBB hex colours by t (0 → a, 1 → b). Used by the flow-deform
  // to tint a disturbed dab toward a darker "pressed" crease (grass/clouds) or
  // a brighter splash (water), so the dent reads as a VALUE contrast — not just
  // a position shift lost against the baked field.
  function _mixHex(a, b, t) {
    var pa = parseInt(a.slice(1), 16), pb = parseInt(b.slice(1), 16);
    var ar = (pa >> 16) & 255, ag = (pa >> 8) & 255, ab = pa & 255;
    var br = (pb >> 16) & 255, bg = (pb >> 8) & 255, bb = pb & 255;
    var r = Math.round(lerp(ar, br, t)), g = Math.round(lerp(ag, bg, t)), bl = Math.round(lerp(ab, bb, t));
    return '#' + (((1 << 24) + (r << 16) + (g << 8) + bl).toString(16).slice(1));
  }

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
  var _blades = [];                   // LIVE base-anchored near-grass blades (not baked)
  var _critter = null;                // { x, y, dir, vx, base, phase, spookUntil }
  // ── GROUND-DISTURBANCE FIELD (game-style interaction buffer). A 1-D array of
  // disturbance values (0..1) across the bar width: the pet PRESSES a bump into
  // it (instant), and it SPRINGS BACK slowly, so the disturbance LINGERS and
  // recovers after the pet passes instead of snapping to the pet's exact
  // position. _paintFlow samples it to bend + compress the near-ground layer,
  // so the scene reacts like a game terrain rather than a baked photo. ──
  var _disturb = [];                  // disturbance per bucket (0..1)
  var _disturbW = 1;                  // px per bucket
  var DISTURB_DECAY = 0.9;            // per-frame spring-back (grass recovers)

  // ── FOREGROUND OCCLUSION PLANE (the 2.5D depth fix — owner: "the pet floats
  // ON the background / refer to game rendering"). The bg scene canvas sits at
  // z0, BELOW the DOM pet at z1, so the whole scene could only ever paint
  // BEHIND the cat → it read as a sprite pasted on a backdrop. A SEPARATE
  // canvas mounted at z2 (IN FRONT of the pet, pointer-transparent) paints the
  // NEAR band of the scene — tall grass / reeds / low mist — so the cat is
  // partly OCCLUDED by it and reads as standing AMONG the scene. It samples the
  // SAME springy disturbance field, so the FRONT of the scene parts around the
  // cat's paws as it walks through and springs back after. Confined to the
  // bottom band (occludes paws/ankles, never the head). ──
  var FG_CLASS = 'tofu-scene-fg-canvas';
  var _fgCanvas = null, _fgctx = null;
  var _fgBlades = [];                 // near-foreground occluders (in front of the pet)
  var _understory = [];               // irregular dark mounds anchoring the fg base (not a solid strip)

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

  // ── SHARED LIGHT FIELD (owner: "the pet lacks a lighting system"). The scene
  // already drives a slow warm sun radial across the bar; the pet was lit only
  // by its baked PNG shading, so it read as pasted on. We cache the sun's live
  // position each painted frame as a NORMALIZED light descriptor and expose it
  // via TofuScene.lightInfo(), so tofu-pet.js can shade the sprite + point its
  // cast shadow with the SAME light source — one sun lighting the whole diorama.
  //   nx : sun x, 0..1 across the bar (0 = left edge, 1 = right edge)
  //   ny : sun y, 0..1 (it rides high in the sky band, ~0.14)
  //   warm: the scene's warmth 0..1 (meadow/sky warm, pool cooler)
  // Held even while the loop is parked (reduced-motion paints one frame), so a
  // static scene still yields a stable light direction.
  var _light = { nx: 0.5, ny: 0.14, warm: 0.7 };
  var SCENE_WARMTH = { meadow: 0.82, pool: 0.34, sky: 0.9 };
  // Sun x at time `ms` (CSS px). Single source of truth for the paint + the
  // exposed light descriptor, so they can never drift apart.
  function _sunX(ms, w) { return (0.5 + SUN_SWEEP * Math.sin(ms * 0.00006)) * w; }

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
    if (_scene === 'off') { _sparks = []; _flow = []; _blades = []; _fgBlades = []; _understory = []; _critter = null; _wake = []; _petPrevX = null; _wakeAccum = 0; _disturb = []; return; }
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
    _blades = [];
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
        if (L.live) {
          // LIVE near layer element: NOT baked — rendered per-frame by
          // _paintLiveLayer so it MOVES and reacts to the pet. `kind` (from the
          // scene's flow mode) picks the correct motion: 'sway' grass blades
          // FLATTEN (rotate about their root), 'drift' water RIPPLES + splashes,
          // 'clouds' puffs DRIFT + get shoved aside. Grass stores its ROOT so it
          // can pivot; water/clouds keep their home x/y and displace from it.
          _blades.push({ kind: pal.flow || 'sway', x: x, y: y,
                         base: { x: x - len * Math.cos(ang), y: y - len * Math.sin(ang) },
                         len: len, wid: wid, ang: ang, color: color,
                         alpha: alpha, ph: R() * 6.283185, sp: lerp(0.7, 1.5, R()) });
        } else {
          dab(b, x, y, len, wid, ang, color, alpha);
        }
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
    var fn = Math.max(12, Math.round(w / 5));
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
    // seed the ground-disturbance field at rest (~1 bucket every 6px).
    _disturb = [];
    var nb = Math.max(8, Math.round(w / 6));
    for (var qi = 0; qi < nb; qi++) _disturb.push(0);
    _disturbW = w / nb;
    // ── seed the FOREGROUND occluder band (painted in FRONT of the pet). A
    // dense row of near blades/reeds/mist tufts rooted at the very bottom,
    // TALLER than the pet's ankle line so they overlap its paws → real depth.
    // Same `kind` (flow mode) as the near live layer, and they read the SAME
    // disturbance field, so the FRONT of the scene parts as the cat walks
    // through. Colours are the scene's near/dominant colours, a touch DARKER +
    // more saturated (they're the closest plane), alpha near-opaque.
    _fgBlades = [];
    var fgKind = pal.flow || 'sway';
    var fgColors = (pal.layers[pal.layers.length - 1] || {}).colors || ['#FFFFFF'];
    // SKY (clouds): the near plane is bright WISPY HAZE, not a dense band. A
    // packed row of occluder puffs jammed at the clipped rim pools into the
    // owner's "large black border" no matter how warm the seed, because ~65
    // stacked translucent ellipses SUBTRACT brightness from the luminous sky.
    // So on clouds we seed FAR FEWER blades, scattered HIGHER (not clamped to
    // the rim). Meadow/pool keep the dense near band.
    var fgAiry = (fgKind === 'clouds');
    var fgN = fgAiry ? Math.max(10, Math.round(w / 16)) : Math.max(18, Math.round(w / 5.5));
    var fgBase = h + 1;                                  // rooted at the very bottom
    for (var gi2 = 0; gi2 < fgN; gi2++) {
      var gx = R() * w;
      // tall enough to reach up into the pet's paw/ankle band (pet box ~30px in
      // a ~48px bar sits with its feet ~1px off the bottom): 9–16px blades.
      var glen = lerp(9, 16, R());
      var gwid = lerp(1.4, 2.6, R());
      var gang = (fgKind === 'sway' ? -1.5 : (fgKind === 'clouds' ? 0.06 : 0.0)) + (R() - 0.5) * 0.5;
      // ATMOSPHERIC PERSPECTIVE — the near plane must read as CLOSER than the
      // hazy background: mix the source colour HARD toward a deep saturated
      // shade (~55%, was 22%) and paint it near-opaque. Without this value gap
      // the eye still reads one flat field with a cat pasted in — the near
      // stalks blended into the mid dabs. `_FG_SHADE` is a very dark saturated
      // green (sway) / teal (drift) / slate (clouds) picked per scene.
      // Meadow grass reads well as a DARK near plane; water/mist on the bright
      // pool+sky scenes must NOT pool into a dark band at the base — use a
      // gentler deepening of the scene's own near tone (teal/cool-grey, not
      // near-black) and a lower mix + a touch of transparency there.
      var fgDark = (fgKind === 'sway');
      // Airy scenes keep the near plane in the SCENE'S OWN WARM/COOL family — a
      // deeper tone of it, NOT a foreign cool grey/navy (which reads as a dirty
      // border under the warm sky). pool → deep teal, sky → deep warm sand.
      // Sky (clouds): the near plane is LIGHT wispy mist, not dark vegetation —
      // mix toward a bright warm-white so a near puff READS AS HAZE and can
      // never pool into a dark border on the luminous sky. Pool: a faint deeper
      // teal. Meadow: dark grass.
      // Sky near haze reads by CHROMA, not luminance: the base bg is already
      // near-white, so a brighter bloom is invisible and a darker one becomes
      // the border. A saturated warm-peach tint (more chroma than the pale bg)
      // gives a measurable near-plane presence while staying LIGHT.
      var fgShade = fgDark ? '#182A0C' : (fgKind === 'drift' ? '#3E7E80' : '#F4C594');
      var fgMix = fgDark ? 0.55 : (fgKind === 'drift' ? 0.26 : 0.5);
      // airy (clouds) puffs sit LOW in the base band as a bright warm HAZE
      // bloom — a near plane that reads by BRIGHTENING (lighter than the bg),
      // never a dark row. Kept in the bottom ~10px so it registers as a base
      // presence, not scattered mid-air specks. Meadow/pool stay rooted at rim.
      var ghy = fgAiry ? (h - lerp(1, 9, R())) : fgBase;
      _fgBlades.push({ kind: fgKind, x: gx, hy: ghy,
                       base: { x: gx - glen * Math.cos(gang), y: fgBase - glen * Math.sin(gang) },
                       rootY: fgBase, len: glen, wid: gwid, ang: gang,
                       color: _mixHex(fgColors[(R() * fgColors.length) | 0], fgShade, fgMix),
                       shade: fgShade,
                       alpha: fgDark ? lerp(0.92, 1.0, R())
                                     : (fgAiry ? lerp(0.30, 0.48, R()) : lerp(0.42, 0.6, R())),
                       ph: R() * 6.283185, sp: lerp(0.7, 1.5, R()) });
    }
    // Seed the IRREGULAR understory mounds (the base anchor, NOT a solid strip).
    // Overlapping ellipses with jittered width/height/shade, each rooted BELOW
    // the rim (`sink` px past h) so only a rounded top pokes above the clip line
    // — the top contour is broken/jagged and gaps let the scene show through, so
    // it reads as near ground, never a ruled border. On the airy 'clouds' scene
    // the mounds are LIGHTER + more translucent (a navy strip there looked worst).
    // The understory must read as NEAR GROUND, never a dark BORDER line at the
    // clipped rim. The depth cue is atmospheric: a near plane is a touch DEEPER
    // in tone than the hazy bg — but on a BRIGHT/airy scene (sky, pool) a dark
    // mass reads as dirty pebbles glued to the bottom edge (the owner's "large
    // black border"). So: sway (meadow, dark grass understory is natural) keeps
    // a moderate earthy shade; drift/clouds use only a GENTLE tonal deepening of
    // the scene's OWN near colour (no navy/slate), low alpha, so it's a hint of
    // ground/haze — never a ruled strip.
    _understory = [];
    var uDark = fgKind === 'sway';
    // SKY (clouds) has NO dark ground plane — a mound row at the clipped rim is
    // pure "black border" with zero depth payoff on a luminous sky. Skip the
    // understory entirely there; the faint occluding cloud blades alone carry
    // the (very light) near plane. Meadow/pool keep a mound band.
    var uSkip = (fgKind === 'clouds');
    // The understory is the NEAR-GROUND anchor. Only MEADOW (grass) genuinely
    // has a dark near ground; on BRIGHT/AIRY scenes (pool water, sky) a dark
    // mound row hugging the clipped rim reads as the owner's "large black
    // border". So airy scenes deepen only a WHISPER within their OWN family and
    // stay near-transparent — because the fg canvas composites over the BRIGHT
    // bloomed bg, any opaque warm mound SUBTRACTS brightness and darkens the
    // rim. pool → a faint deeper teal; sky → barely a warm haze (mix toward the
    // scene's own light near tone, tiny alpha).
    var uShadeSeed = uDark ? '#28401A' : (fgKind === 'drift' ? '#3E7E80' : '#E9D9C2');
    var uMix = uDark ? 0.55 : (fgKind === 'drift' ? 0.28 : 0.18);
    var ux = -6;
    while (!uSkip && ux < w + 6) {
      var uw = lerp(10, 22, R());                       // wide, overlapping
      var uh = lerp(2.5, uDark ? 5.0 : 2.6, R());        // low (much lower on airy scenes)
      var uc = _mixHex(fgColors[(R() * fgColors.length) | 0], uShadeSeed, uMix);
      _understory.push({ x: ux + uw * 0.5, rx: uw * 0.6, ry: uh,
                         sink: lerp(3.0, uDark ? 6.0 : 8.0, R()),  // airy: sunk deeper so only a hint pokes up
                         color: uc, alpha: uDark ? lerp(0.55, 0.75, R()) : lerp(0.14, 0.26, R()),
                         ph: R() * 6.283185 });
      ux += uw * lerp(0.55, 0.9, R());                   // overlap + occasional gap
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
  // dabs + the FLOW layer + the CRITTER + the foreground occluder plane. `ms` is
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
    // advance the ground-disturbance field (the pet presses it, it springs back)
    _updateDisturb(dt, w);
    // (The old bright additive "pet-attention" halo that pooled warm light
    // under the cat was REMOVED — owner: the pet reads as floating and the
    // moving light is fake. An additive glow ring under a sprite is exactly the
    // video-game "selection marker" tell; the cat is instead grounded by its
    // CSS cast shadow (.tofu-pet::after) + the NEW foreground occlusion plane
    // that draws the near scene IN FRONT of it. See _paintForeground below.)
    // drifting sun glow — a soft warm radial that sweeps horizontally (DIMMED:
    // owner found the moving light too glaring; peak/mid/sweep are tuned-down
    // named constants).
    var sx = _sunX(ms, w);
    var sy = h * 0.14;
    // publish the live light direction for the pet (normalized, scene-warmth)
    _light.nx = w > 0 ? sx / w : 0.5;
    _light.ny = 0.14;
    _light.warm = SCENE_WARMTH[_scene] != null ? SCENE_WARMTH[_scene] : 0.7;
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
    // the LIVE near layer — swaying/flattening grass · rippling/splashing water ·
    // drifting/shoved clouds — the near band of EVERY scene, rendered live (not
    // baked) so it moves and reacts to the pet. Drawn before the thin flow
    // overlay so the fine breathing sits on top of the live bank.
    _paintLiveLayer(c, pal, ms, w, h);
    // the FLOW layer — swaying grass / drifting glints / gliding clouds. It now
    // also PRESSES/PARTS around the pet's foot (see _paintFlow's deform).
    _paintFlow(c, pal, ms, w, h);
    // the PET-WAKE marks — grass kicked up / a splash ripple / a cloud puff at
    // the foot, painted ON the canvas so pet & scene read as one layer.
    _paintWake(c, pal, ms, w, h);
    // the critter (drawn last, above the scene but below the pet at z1)
    _paintCritter(c, ms, dt, w, h);
    // FINALLY, on the SEPARATE foreground canvas (z2, IN FRONT of the pet):
    // paint the near occluder band so the cat is partly hidden by it and reads
    // as standing AMONG the scene, not on top of it (the 2.5D depth fix).
    _paintForeground(ms, w, h);
  }

  // Paint the FOREGROUND occlusion plane onto its own canvas (z2, in front of
  // the DOM pet). This is the layer that gives the diorama DEPTH: the cat is
  // drawn between the bg scene (z0) and this near band (z2), so its paws are
  // occluded by tall grass / reeds / mist and it looks planted IN the scene. It
  // reads the SAME springy disturbance field, so the front of the scene parts
  // around the cat as it walks through and springs back after. Per-`kind`
  // motion mirrors _paintLiveLayer (sway/drift/clouds). Cleared + repainted each
  // frame; no-ops cleanly when the fg context is missing.
  function _paintForeground(ms, w, h) {
    if (!_fgctx || !_fgBlades.length) return;
    var c = _fgctx;
    c.setTransform(_dpr, 0, 0, _dpr, 0, 0);
    c.clearRect(0, 0, w, h);
    var pal = PALETTES[_scene] || PALETTES.meadow;
    var px = _petGroundX();
    var gust = 1 + 0.6 * Math.sin(ms * 0.00022 + 1.3);
    // A DARK UNDERSTORY at the base — the near plane's closest, darkest mass
    // (atmospheric-perspective anchor). It must NOT be a solid opaque strip:
    // clipped by the rounded shell that reads as an ugly hard BORDER line. So
    // it's an IRREGULAR row of overlapping mounds with a JAGGED top and GAPS
    // between them, deeper-rooted than tall, so the scene/pebbles show through
    // the notches and the top edge is broken, not a ruled line. Each mound's
    // colour + height + alpha is jittered from the seeded understory list.
    for (var ui = 0; ui < _understory.length; ui++) {
      var um = _understory[ui];
      var uxc = um.x + Math.sin(ms * 0.0004 + um.ph) * 1.0;
      // rooted BELOW the rim (y > h) so only the top of each mound pokes up —
      // no dab edge ever aligns with the clip line to look like a stroke.
      dab(c, uxc, h + um.sink, um.rx, um.ry, 0, um.color, um.alpha);
    }
    for (var i = 0; i < _fgBlades.length; i++) {
      var bl = _fgBlades[i];
      var kind = bl.kind || 'sway';
      if (kind === 'sway') {
        // A bold tapered STALK (4 stacked dabs from a wide dark root to a fine
        // tip), so the near blade reads as a DISTINCT foreground plane in front
        // of the cat — not just another faint field dab. Bends about its root.
        var bx = bl.base.x, by = bl.rootY;
        var ang = bl.ang + Math.sin(ms * 0.0013 * bl.sp + bl.ph) * 0.14 * gust;
        var len = bl.len, color = bl.color, alpha = bl.alpha;
        var q = _disturbAt(bx);
        if (q > 0.01) {
          var side = px == null ? (bx >= w / 2 ? 1 : -1) : (bx >= px ? 1 : -1);
          var flat = Math.min(1, q);
          ang += side * flat * 1.9;                 // lay the blade well over
          // SHOVE the whole stalk sideways AWAY from the foot too — this opens a
          // visible PARTING WEDGE around the cat (a lay-over alone barely moves
          // a short blade; the root shove is what reads as grass pushed aside in
          // motion). Springs back with the field once the cat passes.
          bx += side * flat * 7;
          len = bl.len * (1 - flat * 0.5);           // pressed shorter
          color = _mixHex(bl.color, '#243016', Math.min(0.6, flat * 0.75));
        }
        var seg = 4, dxu = Math.cos(ang), dyu = Math.sin(ang);
        var shade = bl.shade || '#182A0C';
        for (var k = 0; k < seg; k++) {
          var fr0 = k / seg, fr1 = (k + 1) / seg, frm = (fr0 + fr1) / 2;
          var sx2 = bx + dxu * len * frm;
          var sy2 = by + dyu * len * frm;
          // wider + a touch longer at the ROOT (near/thick), fine at the tip —
          // the base reads as the closest, chunkiest part of the plane.
          var segLen = len / seg * (0.78 - 0.28 * frm);
          var segWid = bl.wid * (1.45 - 1.05 * frm);
          // The near plane stays DARK top-to-bottom (atmospheric gap vs the
          // hazy bg); the ROOT is darkest (understory shade), the tip only
          // slightly relieved — NEVER lightened past the blade's own colour.
          var segCol = _mixHex(color, shade, (1 - frm) * 0.5);
          dab(c, sx2, sy2, segLen, segWid, ang, segCol, alpha);
        }
      } else if (kind === 'drift') {
        var wx = bl.x + Math.sin(ms * 0.0012 * bl.sp + bl.ph) * 2.4 * gust;
        var wy = bl.rootY - bl.len * 0.4 + Math.sin(ms * 0.0018 * bl.sp + bl.ph) * 1.0;
        var wlen = bl.len, wwid = bl.wid, wcolor = bl.color, walpha = bl.alpha;
        var qw = _disturbAt(bl.x);
        if (qw > 0.01) {
          var sidew = px == null ? (bl.x >= w / 2 ? 1 : -1) : (bl.x >= px ? 1 : -1);
          var sp2 = Math.min(1, qw);
          wx += sidew * sp2 * 10;
          wlen = bl.len * (1 + sp2 * 0.5);
          wcolor = _mixHex(bl.color, pal.spark, Math.min(0.8, sp2));
        }
        dab(c, wx, wy, wlen * 0.5, wwid, bl.ang, wcolor, walpha);
      } else {
        var cxp = bl.x + Math.sin(ms * 0.0006 * bl.sp + bl.ph) * 3.0 * gust;
        var cyp = (bl.hy != null ? bl.hy : bl.rootY - bl.len * 0.4) + Math.sin(ms * 0.001 * bl.sp + bl.ph) * 0.8;
        var clen = bl.len, ccolor = bl.color, calpha = bl.alpha;
        var qc = _disturbAt(bl.x);
        if (qc > 0.01) {
          var sidec = px == null ? (bl.x >= w / 2 ? 1 : -1) : (bl.x >= px ? 1 : -1);
          var shove = Math.min(1, qc);
          cxp += sidec * shove * 12;
          clen = bl.len * (1 - shove * 0.25);
          ccolor = _mixHex(bl.color, pal.spark, Math.min(0.5, shove * 0.6));
        }
        dab(c, cxp, cyp, clen * 0.5, bl.wid, bl.ang, ccolor, calpha);
      }
    }
  }

  // Advance the ground-disturbance field: PRESS a bump under the pet's ground
  // position (works while walking AND while dragged — the owner ask) and let
  // every bucket SPRING BACK toward rest. The press writes a small
  // neighbourhood (not one bucket) so the dent has width; the decay makes it
  // LINGER and recover after the pet leaves, which is what reads as a real
  // footprint in grass/water rather than a spotlight glued to the sprite.
  // Fully guarded: no pet → the field just relaxes to rest.
  function _updateDisturb(dt, w) {
    if (!_disturb.length) return;
    // frame-rate-independent spring-back toward 0
    var relax = Math.pow(DISTURB_DECAY, Math.max(0.2, dt * 60));
    for (var i = 0; i < _disturb.length; i++) _disturb[i] *= relax;
    var g = _petGround();
    if (!g) return;
    var reach = WAKE_RADIUS / _disturbW;               // buckets within the foot radius
    var c0 = g.x / _disturbW;
    var lo = Math.max(0, Math.floor(c0 - reach));
    var hi = Math.min(_disturb.length - 1, Math.ceil(c0 + reach));
    // a lifted (dragged) cat still presses, a touch softer than a planted foot
    var amp = g.drag ? 0.8 : 1;
    for (var b = lo; b <= hi; b++) {
      var dist = Math.abs(b - c0) / (reach || 1);
      var press = amp * (1 - dist * dist);             // rounded dome, 1 at centre
      if (press > _disturb[b]) _disturb[b] = press;    // press deepens, never lifts
    }
  }
  // Sample the disturbance field at CSS-px x (0..1). Linear interp between
  // buckets so the deform is smooth across the whole reactive layer.
  function _disturbAt(x) {
    if (!_disturb.length) return 0;
    var f = x / _disturbW - 0.5;
    var i = Math.floor(f);
    var t = f - i;
    var a = _disturb[Math.max(0, Math.min(_disturb.length - 1, i))] || 0;
    var b = _disturb[Math.max(0, Math.min(_disturb.length - 1, i + 1))] || 0;
    return a + (b - a) * t;
  }

  // LIVE near layer — the near/dominant band of EVERY scene is NOT baked; it is
  // rendered per-frame here so it MOVES and reacts to the pet (owner ask: all
  // background elements movable, in all three scenes). Motion is per-`kind`
  // (the scene's flow mode), because water and clouds must NOT "lay down like
  // grass":
  //   • 'sway'  (meadow grass) — base-anchored blades that sway and FLATTEN:
  //     press rotates each blade about its ROOT toward horizontal (away from the
  //     foot) + shortens it + darkens to a crease → the grass lies down.
  //   • 'drift' (pool water)   — ripple bands that undulate and, under the foot,
  //     SPLASH: displaced radially outward from the press + brightened toward
  //     the spark (a wet crown), widened, NOT laid flat.
  //   • 'clouds' (sky puffs)   — puffs that drift laterally and, under the foot,
  //     are SHOVED aside (horizontal displacement away from the press) +
  //     compressed, NOT rotated down.
  // All three read the SAME springy disturbance field (_disturbAt at the
  // element's anchor x), so drag/foot interaction + spring-back are identical.
  function _paintLiveLayer(c, pal, ms, w, h) {
    if (!_blades.length) return;
    var px = _petGroundX();
    var gust = 1 + 0.6 * Math.sin(ms * 0.00022);
    for (var i = 0; i < _blades.length; i++) {
      var bl = _blades[i];
      var kind = bl.kind || 'sway';
      if (kind === 'sway') {
        // ── GRASS: base-anchored blade that sways + flattens ──
        var bx = bl.base.x, by = bl.base.y;
        var ang = bl.ang + Math.sin(ms * 0.0013 * bl.sp + bl.ph) * 0.16 * gust;
        var len = bl.len;
        var q = _disturbAt(bx);
        var color = bl.color;
        if (q > 0.01) {
          var side = px == null ? (bx >= w / 2 ? 1 : -1) : (bx >= px ? 1 : -1);
          var flat = Math.min(1, q);
          ang += side * flat * 1.5;                     // lay the tip over
          len = bl.len * (1 - flat * 0.55);              // press shorter
          color = _mixHex(bl.color, '#2E3D20', Math.min(0.6, flat * 0.75));
        }
        var cx = bx + Math.cos(ang) * len * 0.5;
        var cy = by + Math.sin(ang) * len * 0.5;
        dab(c, cx, cy, len * 0.5, bl.wid, ang, color, bl.alpha);
      } else if (kind === 'drift') {
        // ── WATER: rippling band that splashes outward under the foot ──
        // undulate: home x slides gently, y bobs with a travelling wave.
        var wx = bl.x + Math.sin(ms * 0.0012 * bl.sp + bl.ph) * 2.2 * gust;
        var wy = bl.y + Math.sin(ms * 0.0018 * bl.sp + bl.ph) * 1.2;
        var wlen = bl.len, wwid = bl.wid, wcolor = bl.color, walpha = bl.alpha;
        var qw = _disturbAt(bl.x);
        if (qw > 0.01) {
          var sidew = px == null ? (bl.x >= w / 2 ? 1 : -1) : (bl.x >= px ? 1 : -1);
          var sp = Math.min(1, qw);
          wx += sidew * sp * 10;                         // shove the ripple outward
          wlen = bl.len * (1 + sp * 0.6);                // splash spreads wide
          wcolor = _mixHex(bl.color, pal.spark, Math.min(0.8, sp));   // wet crown brightens
          walpha = Math.min(1, bl.alpha * (1 + sp * 0.5));
        }
        dab(c, wx, wy, wlen * 0.5, wwid, bl.ang, wcolor, walpha);
      } else {
        // ── CLOUDS: drifting puff shoved aside + compressed under the foot ──
        var cxp = bl.x + Math.sin(ms * 0.0006 * bl.sp + bl.ph) * 3.0 * gust;
        var cyp = bl.y + Math.sin(ms * 0.001 * bl.sp + bl.ph) * 0.8;
        var clen = bl.len, ccolor = bl.color, calpha = bl.alpha;
        var qc = _disturbAt(bl.x);
        if (qc > 0.01) {
          var sidec = px == null ? (bl.x >= w / 2 ? 1 : -1) : (bl.x >= px ? 1 : -1);
          var shove = Math.min(1, qc);
          cxp += sidec * shove * 12;                     // puff pushed aside
          clen = bl.len * (1 - shove * 0.25);            // and compressed
          ccolor = _mixHex(bl.color, pal.spark, Math.min(0.5, shove * 0.6));
          calpha = Math.min(1, bl.alpha * (1 + shove * 0.4));
        }
        dab(c, cxp, cyp, clen * 0.5, bl.wid, bl.ang, ccolor, calpha);
      }
    }
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
      // GROUND-DISTURBANCE DEFORM: sample the springy disturbance field at this
      // dab's x. A game-style interaction buffer — the dent LINGERS and recovers
      // after the pet passes, and the response is LARGE + directional so it
      // reads unmistakably (the fix for "the pet just floats over an inert
      // scene"). Bend direction is away from the foot so grass parts + water
      // splashes outward.
      var q = _disturbAt(x);                            // 0..1 disturbance here
      var color = f.color;
      if (q > 0.01) {
        var side = px == null ? 1 : (x >= px ? 1 : -1);
        if (mode === 'sway') {
          // The near GRASS flatten is owned by the live _blades layer now; the
          // thin flow overlay just lies DOWN with it (shorter + darker crease),
          // deliberately NOT enlarging/brightening — the old pop-out made the
          // overlay "pop some grass leaves" instead of pressing the field down.
          ang += side * q * 1.5;                        // lie over toward the ground
          sc = 1 - q * 0.6;                             // pressed shorter
          y += q * 5;                                   // sink toward the base
          color = _mixHex(f.color, '#2E3D20', Math.min(0.6, q * 0.8));
        } else if (mode === 'drift') {
          // water: a real splash DOES spread + brighten (a wet crown), keep it.
          sc = 1 + q * 1.1;                             // splash spreads wide
          af = 1 + q * 1.4;                             // glint brightens
          y += q * 2;
          color = _mixHex(f.color, pal.spark, Math.min(0.85, q));
        } else {                                        // clouds
          x += side * q * 8;                            // puffs shoved aside
          sc = 1 + q * 0.4;
          af *= 1 + q * 0.8;
        }
      }
      var pulse = 0.75 + 0.25 * Math.sin(ms * 0.0013 * f.sp + f.ph);
      dab(c, x, y, f.len * sc, f.len * f.wid * sc, ang, color, Math.min(1, f.alpha * pulse * af));
    }
  }

  // A soft warm ground glow under the roaming pet — makes the scene react to
  // the pet. Reads TofuPet.getState().x (CSS px along the bar); fully guarded
  // so the scene never depends on the pet being present.
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
  function _clearForeground() {
    if (_fgctx && _w > 0 && _h > 0) {
      _fgctx.setTransform(_dpr, 0, 0, _dpr, 0, 0);
      _fgctx.clearRect(0, 0, _w, _h);
    }
  }
  function _ensureLoop() {
    if (!_ctx) return;
    var active = _isTofu() && _scene !== 'off';
    if (!active) { if (_raf) { cancelAnimationFrame(_raf); _raf = 0; } _clearForeground(); return; }
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
    if (_fgCanvas) {
      _fgCanvas.width = _canvas.width;
      _fgCanvas.height = _canvas.height;
      _fgCanvas.style.width = w + 'px';
      _fgCanvas.style.height = h + 'px';
    }
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
  // The live scene light (see _light). Returns a COPY so callers can't mutate
  // the field. Null when there's no lit scene (off / non-tofu) so the pet falls
  // back to flat baked shading rather than a stale direction.
  function lightInfo() {
    if (_scene === 'off' || !_isTofu()) return null;
    return { nx: _light.nx, ny: _light.ny, warm: _light.warm };
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
    // The FOREGROUND occlusion canvas (z2, IN FRONT of the pet). Appended LAST
    // so it's the bar's last child (paints above the pet at z1); pointer-
    // transparent so it never steals a click from the controls it sits at the
    // same z-band as. Created after the bg 2d context succeeds — a no-canvas
    // browser keeps only the SVG fallback and never reaches here.
    _fgCanvas = _bar.querySelector('.' + FG_CLASS);
    if (!_fgCanvas) {
      _fgCanvas = document.createElement('canvas');
      _fgCanvas.className = FG_CLASS;
      _fgCanvas.setAttribute('aria-hidden', 'true');
      _bar.appendChild(_fgCanvas);
    }
    try { _fgctx = _fgCanvas.getContext('2d'); } catch (e4) { _fgctx = null; }
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
    lightInfo: lightInfo,
    spook: spook
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', _boot);
  } else {
    _boot();
  }
})();
