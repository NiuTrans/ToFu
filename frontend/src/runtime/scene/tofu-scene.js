/* ===== migrated source: tofu-scene.js ===== */
var runtimeScope = (typeof window !== 'undefined' && window.TofuModules &&
  window.TofuModules.sceneRuntime)
  || (typeof window !== 'undefined' ? window : globalThis);
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
 * Public surface (runtimeScope.TofuScene): mount() · repaint() · setScene(s) ·
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
      grad: [[0, '#BFD9C9'], [0.42, '#DFEBCB'], [0.72, '#CFE0AE'], [1, '#A8C77E']],
      spark: '#F2F6C8',
      glow: 'rgba(255,244,196,',   // warm sun
      flow: 'sway',                 // grass sways
      layers: [
        // far hazy field — pale, low contrast, flatter strokes
        { density: 3.6, yTop: 0.06, yBot: 0.64, ang: -1.15, jit: 0.5, lo: 1.6, hi: 3.4,
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
      grad: [[0, '#BBDCD6'], [0.4, '#D6E8E3'], [0.72, '#AFD2CC'], [1, '#5E948E']],
      spark: '#EAF7F4',
      glow: 'rgba(220,246,255,',
      flow: 'drift',                // water glints drift
      layers: [
        // far shimmering surface — pale horizontal strokes
        { density: 3.8, yTop: 0.08, yBot: 0.6, ang: 0.0, jit: 0.24, lo: 2.2, hi: 5.0,
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
      grad: [[0, '#C3CFE8'], [0.4, '#E6E0EE'], [0.72, '#F6E7DA'], [1, '#F2CFB4']],
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

  // ── BATCHED DAB RENDERER ────────────────────────────────────────────────
  // One oriented brush-dab is a rotated filled ellipse. Layering many of them
  // with jittered colour + alpha is what reads as painterly broken colour — but
  // the NAIVE way to draw one costs NINE canvas calls (save / globalAlpha /
  // fillStyle / translate / rotate / beginPath / ellipse / fill / restore) AND
  // its own rasterizer flush. At ~2000 dabs on a wide bar that was ~18k calls +
  // 2000 separate fills every frame — the single dominant cost of the scene.
  //
  // Two changes delete almost all of it, with no change to what is painted:
  //   1. `ellipse()` takes a ROTATION argument natively, so the
  //      save/translate/rotate/restore quartet was pure waste — the dab is
  //      placed and rotated by its own arguments.
  //   2. Dabs are QUEUED into (colour, alpha-bucket) buckets and flushed as ONE
  //      path + ONE fill per bucket, so a few thousand fills collapse into a
  //      few dozen. Each ellipse is preceded by a moveTo to its own start point,
  //      because arc/ellipse otherwise draws a connecting LINE from the current
  //      point into the new subpath (a spec detail that would web the whole
  //      field together with hairlines).
  //
  // ORDERING CONTRACT (why this is safe): `_bqKeys` preserves FIRST-TOUCH
  // order, so buckets flush in the order their colour first appeared, and
  // callers flush at every LAYER seam (see flushDabs call sites). Depth-plane
  // order — the thing atmospheric perspective depends on — is therefore exactly
  // preserved; only the order of same-colour, same-alpha dabs WITHIN one layer
  // can change, which is invisible in a field of overlapping translucent dabs
  // of the same tone.
  var TAU = 6.283185307179586;
  var ALPHA_STEPS = 12;         // alpha quantisation for bucketing (~0.083 apart)
  var _bqCtx = null;            // context the queue belongs to
  var _bq = null;               // key → flat [x,y,rx,ry,ang, ...]
  var _bqKeys = null;           // keys in first-touch order

  function dab(ctx, x, y, len, wid, ang, color, alpha) {
    if (ctx !== _bqCtx) { flushDabs(); _bqCtx = ctx; }
    if (!(alpha > 0)) return;                       // fully transparent → free
    var step = alpha >= 1 ? ALPHA_STEPS : ((alpha * ALPHA_STEPS + 0.5) | 0);
    if (step <= 0) return;
    if (!_bq) { _bq = {}; _bqKeys = []; }
    var k = color + '|' + step;
    var arr = _bq[k];
    if (!arr) { arr = _bq[k] = []; _bqKeys.push(k); }
    arr.push(x, y, len, wid, ang);
  }

  // Emit everything queued so far. MUST be called before any state change the
  // queue does not own (composite op, a gradient fillRect, a blit, restore()),
  // and at every layer seam so depth order is preserved.
  function flushDabs() {
    var ctx = _bqCtx;
    if (!ctx || !_bqKeys || !_bqKeys.length) { _bq = null; _bqKeys = null; return; }
    for (var i = 0; i < _bqKeys.length; i++) {
      var k = _bqKeys[i], arr = _bq[k];
      if (!arr || !arr.length) continue;
      var cut = k.lastIndexOf('|');
      ctx.fillStyle = k.slice(0, cut);
      ctx.globalAlpha = (+k.slice(cut + 1)) / ALPHA_STEPS;
      ctx.beginPath();
      for (var j = 0; j < arr.length; j += 5) {
        var x = arr[j], y = arr[j + 1];
        var rx = arr[j + 2] > 0.01 ? arr[j + 2] : 0.01;
        var ry = arr[j + 3] > 0.01 ? arr[j + 3] : 0.01;
        var ang = arr[j + 4];
        // start the subpath at the ellipse's own 0-angle point, else the path
        // draws a line here from wherever the previous ellipse ended.
        ctx.moveTo(x + rx * Math.cos(ang), y + rx * Math.sin(ang));
        ctx.ellipse(x, y, rx, ry, ang, 0, TAU);
      }
      ctx.fill();
    }
    // The queue owns no save/restore, so it must hand the context back at the
    // neutral alpha it borrowed. Without this the NEXT unrelated draw on this
    // context — the base wash on a re-bake, or the per-frame `drawImage` blit of
    // the baked buffer — would inherit the last bucket's alpha and paint faded.
    ctx.globalAlpha = 1;
    _bq = null;
    _bqKeys = null;
  }

  // ── SCENE RENDER RESOLUTION ─────────────────────────────────────────────
  // ⚠️ DO NOT "FIX" THIS TO 2 OR 3 BECAUSE THE CANVAS "LOOKS SOFT". It is
  // deliberate, it is the single cheapest win in the whole renderer, and the
  // softness is the art style rather than a defect.
  //
  // Every pixel cost in this file scales with dpr². Rendering at the device's
  // full ratio (2 on a Retina panel, 3 on some phones) therefore costs 4–9×,
  // and buys NOTHING here: this scene is Monet broken colour — thousands of
  // soft, overlapping, translucent ellipses with no hard edges and no text.
  // There is no high-frequency detail for the extra samples to resolve.
  //
  // Nothing legible lives on these canvases (verified: zero fillText/strokeText
  // in this module). The pet is a DOM <img> with its own transforms and the
  // bar's folder/stat labels are DOM spans — all of them keep full device
  // resolution, because this cap is scoped to the scene canvases ONLY and never
  // touches a global.
  //
  // 1.5 keeps a little supersampling for the dab edges while cutting the bill
  // ~44% against dpr=2. Pinned by
  // tests/test_frontend_tofu_scene_perf.py::test_scene_render_dpr_is_capped.
  var SCENE_DPR_CAP = 1.5;

  // ── TIME OF DAY ─────────────────────────────────────────────────────────
  // The pet already lives on a clock: tofu-pet.js::_timeBucket() sends it to
  // sleep at 3am and makes it sleepy in the evening. The SCENE did not, so the
  // cat could doze off at midnight while standing in a bright noon meadow —
  // the pet and its world disagreed about what time it was.
  //
  // These bucket boundaries are a DELIBERATE MIRROR of tofu-pet.js::_timeBucket
  // (0/5/8/12/17/21). Keep them identical: the whole point is that the light on
  // the cat and the light in the field come from one sun. A guard test asserts
  // both modules still agree.
  //
  // The tint is applied as a WASH over the scene's own palette rather than as
  // six hand-authored palettes per scene: a wash preserves each scene's
  // identity (a meadow at dusk is still recognisably that meadow) and it costs
  // nothing at runtime, because it happens once at BAKE time. `sat` pulls
  // toward grey for the low-light buckets, since colour vision genuinely
  // desaturates at night — that reads as dusk far more than darkening alone.
  var TIME_TINTS = {
    deepNight:    { wash: '#26345B', amt: 0.50, sat: 0.62, glow: 'rgba(150,175,235,', spark: '#C3D4F5' },
    earlyMorning: { wash: '#6E5A7A', amt: 0.34, sat: 0.78, glow: 'rgba(255,206,190,', spark: '#F3D9DF' },
    morning:      { wash: '#FFF6DE', amt: 0.12, sat: 1.0,  glow: null, spark: null },
    afternoon:    { wash: null,      amt: 0,    sat: 1.0,  glow: null, spark: null },
    evening:      { wash: '#F0A25E', amt: 0.30, sat: 0.95, glow: 'rgba(255,196,130,', spark: '#FFE0B0' },
    night:        { wash: '#34456F', amt: 0.42, sat: 0.68, glow: 'rgba(170,192,240,', spark: '#D2DFF8' }
  };

  /** Time bucket for an hour. MUST match tofu-pet.js::_timeBucket boundaries.
   *  `hour` is injectable so tests never have to mock the system clock. */
  function _sceneBucket(hour) {
    var h = (hour == null) ? new Date().getHours() : hour;
    if (h >= 0 && h < 5) return 'deepNight';
    if (h < 8) return 'earlyMorning';
    if (h < 12) return 'morning';
    if (h < 17) return 'afternoon';
    if (h < 21) return 'evening';
    return 'night';
  }

  /** Desaturate toward this colour's own luma. */
  function _desat(hex, keep) {
    if (keep >= 1) return hex;
    var p = parseInt(hex.slice(1), 16);
    var r = (p >> 16) & 255, g = (p >> 8) & 255, b = p & 255;
    var y = 0.2126 * r + 0.7152 * g + 0.0722 * b;
    var rr = Math.round(lerp(y, r, keep)), gg = Math.round(lerp(y, g, keep)), bb = Math.round(lerp(y, b, keep));
    return '#' + (((1 << 24) + (rr << 16) + (gg << 8) + bb).toString(16).slice(1));
  }

  /** Return `pal` washed toward the given time-of-day tint. Pure: never mutates
   *  the PALETTES entry (which is module-level and shared across re-bakes). */
  function _tintPalette(pal, bucket) {
    var t = TIME_TINTS[bucket];
    if (!t || !t.wash || t.amt <= 0) return pal;
    function c(hex) { return _mixHex(_desat(hex, t.sat), t.wash, t.amt); }
    var out = {
      seed: pal.seed, flow: pal.flow,
      grad: pal.grad.map(function (s) { return [s[0], c(s[1])]; }),
      spark: t.spark || c(pal.spark),
      glow: t.glow || pal.glow,
      layers: pal.layers.map(function (L) {
        var o = {}; for (var k in L) if (Object.prototype.hasOwnProperty.call(L, k)) o[k] = L[k];
        o.colors = L.colors.map(c);
        return o;
      })
    };
    return out;
  }

  // ── ACTIVITY WEATHER ────────────────────────────────────────────────────
  // The pet already reacts to what the app is doing (TofuPet.setActivity via the
  // 'tofu:activity' event); the scene ignored it. Letting the weather carry that
  // signal turns ambient decoration into PERIPHERAL STATUS — something you can
  // read out of the corner of your eye without looking at it.
  //
  // ⚠️ THE ONE INVARIANT: every effect DECAYS TO NEUTRAL ON ITS OWN.
  // No code path may leave the bar permanently stormy. This is not a style
  // preference — a weather state pinned to an error becomes ambient anxiety, and
  // errors already surface in the chat where they belong. So the entire model is
  // ONE SCALAR PER EFFECT that only ever ramps toward a target and decays to
  // zero; there is no "error mode" the bar can get stuck in, because there is no
  // mode at all. `success` and `error` are one-shot IMPULSES (set to 1, decay
  // out); only `loading` holds, and it holds a GENTLE value and is released the
  // moment the app says anything else.
  //
  // The scene listens to the DOM event directly rather than having the pet
  // forward it: the two stay decoupled, and a scene re-bake cannot lose the
  // current weather (the scalars live outside the bake).
  var WEATHER_ENABLED = true;        // instantly killable; see setWeather()
  var _wx = {
    overcast: 0,   // 0..1 — cloud shadow dims the sun while work is in flight
    burst: 0,      // 0..1 — one-shot light-break on success
    rain: 0        // 0..1 — one-shot rain pass on error (NEVER a held state)
  };
  var _wxTarget = 0;                 // overcast target (the only holding value)
  var OVERCAST_MAX = 0.55;           // how far the sun can be dimmed (subtle)
  var OVERCAST_RAMP = 0.6;           // per second, toward target
  var IMPULSE_DECAY = 0.55;          // per second — a burst/rain pass self-clears
  // WATCHDOG. `loading` is the only holding state, and it is held by an event
  // we do not control: if the terminal signal never arrives — a crashed task, a
  // dropped stream, a tab closed mid-request — the hold would never release and
  // the bar would sit under cloud forever. That is precisely the "permanently
  // stormy" failure the whole model exists to prevent, so the hold ALSO expires
  // on its own. Long enough not to fight a genuinely slow request; short enough
  // that a stranded bar heals itself well inside a working session.
  var OVERCAST_MAX_HOLD_MS = 120000;   // 2 minutes
  var _overcastSince = -1;             // ms timestamp the hold began (-1 = not held)

  /** Drive the weather scalars. `dt` seconds. Pure decay: with no further
   *  events every scalar returns to 0, which is the neutral scene. */
  function _stepWeather(dt, ms) {
    if (!WEATHER_ENABLED) { _wx.overcast = _wx.burst = _wx.rain = 0; return; }
    // Watchdog: an abandoned `loading` (terminal event never arrived) must not
    // hold the cloud forever — release it after OVERCAST_MAX_HOLD_MS.
    if (_overcastSince >= 0 && (ms - _overcastSince) > OVERCAST_MAX_HOLD_MS) {
      _wxTarget = 0;
      _overcastSince = -1;
    }
    var d = _wxTarget - _wx.overcast;
    var step = OVERCAST_RAMP * dt;
    _wx.overcast += (Math.abs(d) <= step) ? d : (d > 0 ? step : -step);
    // Impulses ONLY decay — nothing can hold them up.
    _wx.burst = Math.max(0, _wx.burst - IMPULSE_DECAY * dt);
    _wx.rain = Math.max(0, _wx.rain - IMPULSE_DECAY * dt);
  }

  /** Map an app activity to weather. Every branch either sets a decaying
   *  impulse or RELEASES the hold — none of them can pin the bar. */
  function _onActivity(kind, ms) {
    if (!WEATHER_ENABLED) return;
    if (kind === 'loading') {
      _wxTarget = OVERCAST_MAX;
      // Stamp the hold so the watchdog can expire it if no terminal signal
      // comes. -1 means "not held": a genuine timestamp of 0 is possible (the
      // very first frame), so 0 cannot be used as the sentinel.
      if (_overcastSince < 0) _overcastSince = (typeof ms === 'number') ? ms : _lastMs;
      return;
    }
    // Anything that is not 'loading' releases the overcast hold, so a missed or
    // unknown terminal event can never strand the bar under cloud.
    _wxTarget = 0;
    _overcastSince = -1;
    if (kind === 'success') _wx.burst = 1;
    else if (kind === 'error') _wx.rain = 1;
  }

  // ── PER-FRAME LIVE BUDGET ───────────────────────────────────────────────

  // Every element of a LIVE population (near band, flow overlay, foreground
  // occluders) is re-resolved and re-queued EVERY frame, so its count is the
  // per-frame cost. Seeding them by AREA — as the baked planes rightly are —
  // made that cost grow without limit with the bar's width: a 1400px bar paid
  // ~4x a 360px one for a painting that is only ever 48px tall.
  //
  // So the live populations are CAPPED, and when a cap bites, each surviving
  // element is scaled up by sqrt(wanted/capped) — the same total painted AREA
  // spread over fewer, slightly broader strokes. That is why a wide bar still
  // looks like a full painting instead of a thinning one: Impressionist dabs
  // carry the field by coverage, not by count. Detail that WOULD have gone into
  // more live strokes is instead spent in the BAKED buffer (see BAKE_BOOST),
  // which costs nothing per frame.
  //
  // The caps are set ABOVE a normal bar's natural count, so at the widths the
  // project bar actually takes (~360–900px) nothing is thinned at all; they
  // only engage on very wide windows.
  var LIVE_CAP_NEAR = 300;            // live near-band elements (grass/water/cloud)
  var LIVE_CAP_FLOW = 200;            // breathing flow-overlay dabs
  var LIVE_CAP_FG = 190;              // foreground occluders (each = up to 4 dabs)
  var LIVE_CAP_UNDERSTORY = 90;       // base mounds on the fg plane
  var LIVE_CAP_SPARK = 40;            // twinkling specular dabs
  // Baked planes are painted ONCE per resize/scene-change, so extra density
  // there is free at runtime — this is where the "intricate" budget goes.
  var BAKE_BOOST = 1.85;

  /** Clamp a wanted live count to `cap`, returning the count plus the linear
   *  size scale that preserves total painted area. */
  function _budget(wanted, cap) {
    if (wanted <= cap) return { n: wanted, scale: 1 };
    return { n: cap, scale: Math.sqrt(wanted / cap) };
  }

  // ── FULL-BLEED PAINTING (owner, 2026-07-26) ─────────────────────────────
  // The painting fills the WHOLE bar, edge to edge. An earlier iteration tore
  // the canvas to a deckle (handmade-paper) outline and let the bar's cream
  // body show through as a torn margin — the owner rejected the result ("the
  // irregular outer border with a white background behind it is not
  // appealing"), so the torn edge + white mount are GONE. Cropping to the
  // rounded shell is done ONCE, in CSS, by the clip-path on the canvas
  // elements (styles.css) — zero per-frame canvas cost, no margin, no white
  // halo. Do not reintroduce a canvas-side cut/clip: the even-odd
  // destination-out cut and the per-frame deckle clip were both pure overhead
  // here, and the full-bleed look is the requirement.

  // ── SUN-GLOW TILE ────────────────────────────────────────────────────────
  // The drifting sun was a `createRadialGradient` rebuilt EVERY frame and then
  // `fillRect`-ed across the WHOLE canvas under 'lighter'. Two separate costs
  // hid in that one line:
  //   * the fill touched w×h device pixels with an additive blend, ~20% of the
  //     frame's entire pixel budget and growing linearly with the bar's width;
  //   * yet the gradient is fully TRANSPARENT beyond its radius (h*1.6), so the
  //     overwhelming majority of those pixels were blending a no-op.
  // And it was all to move the sun 0.6px — the sweep travels ~18px/second.
  //
  // So the glow is baked ONCE into a small square tile (2R×2R) at bake time and
  // blitted at the sun's position each frame. A blit of a ~154px tile replaces a
  // full-width gradient fill: the per-frame glow cost becomes O(h²), constant in
  // the bar's width.
  var _glowTile = null, _glowR = 0;

  function _bakeGlowTile(pal, h) {
    _glowTile = null;
    if (!(h > 0) || !pal || !pal.glow) return;
    var R = h * 1.6;
    var side = Math.max(2, Math.ceil(2 * R * _dpr));
    var t, tc;
    try {
      t = document.createElement('canvas');
      t.width = side; t.height = side;
      tc = t.getContext('2d');
    } catch (e) { return; }                    // no second context — glow degrades off
    if (!tc) return;
    tc.setTransform(_dpr, 0, 0, _dpr, 0, 0);
    var g = tc.createRadialGradient(R, R, 0, R, R, R);
    g.addColorStop(0, pal.glow + SUN_GLOW_PEAK + ')');
    g.addColorStop(0.4, pal.glow + SUN_GLOW_MID + ')');
    g.addColorStop(1, pal.glow + '0)');
    tc.fillStyle = g;
    tc.fillRect(0, 0, 2 * R, 2 * R);
    _glowTile = t;
    _glowR = R;
  }


  // ── module state ──
  var _bar = null;
  // ── module state ──
  var _bar = null;
  var _canvas = null, _ctx = null;
  var _buf = null, _bctx = null;      // offscreen static-scene buffer
  var _dpr = 1;
  var _w = 0, _h = 0;                 // CSS px
  var _scene = 'meadow';
  var _raf = 0, _t0 = 0, _lastMs = 0;
  var _reduced = false, _paused = false;
  // Frame pacing (see _loop): the scene is slow ambient weather, so it is
  // painted on a fixed cadence rather than every vsync. rAF still drives the
  // clock — we just skip the paint on the in-between ticks.
  var SCENE_FPS = 30;
  var SCENE_FRAME_MS = 1000 / SCENE_FPS - 1;   // -1 so a 30Hz-aligned tick never slips a frame
  var _lastPaint = -1e9;
  // True while the bar is scrolled/collapsed out of view (IntersectionObserver).
  // An off-screen painting is invisible work, so the loop skips it entirely.
  var _offscreen = false;
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
  // Top of the band the fg plane actually paints (CSS px). The plane is rooted
  // at the bottom, so everything above this is permanently transparent — the
  // per-frame clear is confined to [_fgTop, h] instead of the whole canvas.
  // Recomputed from the seeded geometry at bake time, so it can never drift out
  // of step with what is drawn.
  var _fgTop = 0;
  // Time-of-day state. `_hourOverride` is the test seam (null = read the real
  // clock). `_bakedBucket` records which bucket the CURRENT buffer was baked
  // in, so the boundary watcher can tell when the world needs to move on.
  // `_livePal` is the tinted palette the baked buffer was painted with — the
  // per-frame layers MUST read it rather than the raw PALETTES entry, or the
  // live grass would stay noon-green on top of a dusk-washed field.
  var _hourOverride = null;
  var _bakedBucket = null;
  var _livePal = null;
  // Cross-fade state for a time-of-day change. A bucket boundary crossed while
  // someone is looking at the bar must NOT snap the palette — a hard colour
  // jump mid-session reads as a rendering bug, not as dusk falling. So the
  // PREVIOUS baked buffer is kept and blended out over the new one.
  // `_xfadeBuf` holds the outgoing painting; `_xfadeT` runs 1 → 0.
  var _xfadeBuf = null;
  var _xfadeT = 0;
  var XFADE_MS = 2600;              // slow enough to read as light changing
  var BUCKET_POLL_MS = 60000;       // check the clock once a minute; not per frame
  var _lastBucketCheck = -1e9;
  // Set after a re-bake (size/scene change): the torn outline itself has moved,
  // so stale pixels can survive OUTSIDE the new clip. One full clear retires
  // them; steady-state frames need none.
  var _needFullClear = true;

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
    // Wash the scene's palette toward the current time of day, so the light on
    // the field agrees with the light on the cat. Free at runtime: this is the
    // bake, not the frame.
    _bakedBucket = _sceneBucket(_hourOverride);
    pal = _tintPalette(pal, _bakedBucket);
    _livePal = pal;
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
      // A BAKED plane is painted once → spend the intricacy budget here. A LIVE
      // plane is re-queued every frame → cap it and widen the survivors so the
      // painted area (hence the look) holds. See LIVE_CAP_* / BAKE_BOOST.
      var want = Math.max(4, Math.round(L.density * area / 1000 * (L.live ? 1 : BAKE_BOOST)));
      var bud = L.live ? _budget(want, LIVE_CAP_NEAR) : { n: want, scale: 1 };
      var n = bud.n, gsc = bud.scale;
      for (var i = 0; i < n; i++) {
        var x = R() * w;
        var y = lerp(L.yTop, L.yBot, R()) * h;
        var len = lerp(L.lo, L.hi, R()) * gsc;
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
          // IMPASTO. Real broken-colour painting is THICK — a loaded brush
          // leaves a ridge along one side of the stroke, and that ridge catches
          // the light while the furrow beside it holds shadow. Flat ellipses
          // alone read as printed, not painted. So a minority of baked strokes
          // get a paired highlight + shadow sliver offset PERPENDICULAR to the
          // stroke (±90°), which is what makes the field read as physical paint
          // under raking light. Baked-only and deliberately so: it triples the
          // detail of the static planes at exactly ZERO per-frame cost, which
          // is the whole trade this optimization bought.
          if (R() < 0.26) {
            var pnx = -Math.sin(ang), pny = Math.cos(ang);
            var off = wid * 0.85;
            dab(b, x + pnx * off, y + pny * off, len * 0.82, wid * 0.4, ang,
                _mixHex(color, pal.spark, 0.45), alpha * 0.55);
            dab(b, x - pnx * off, y - pny * off, len * 0.7, wid * 0.34, ang,
                _mixHex(color, '#4A4230', 0.3), alpha * 0.3);
          }
        }
      }
      flushDabs();   // layer seam — keep depth-plane order exact
    }
    // pre-seed the living shimmer dabs (their positions are stable; only their
    // alpha/offset oscillate per frame in the overlay).
    _sparks = [];
    var sbud = _budget(Math.max(3, Math.round(w / 46)), LIVE_CAP_SPARK);
    var sn = sbud.n;
    var sang = (pal.layers[0] && pal.layers[0].ang) || 0;
    for (var si = 0; si < sn; si++) {
      _sparks.push({
        x: R() * w,
        y: lerp(0.5, 0.92, R()) * h,
        len: lerp(1.6, 3.4, R()) * sbud.scale,
        ph: R() * 6.283185,          // phase offset
        sp: lerp(0.6, 1.6, R()),     // twinkle speed
        ang: sang + (R() - 0.5) * 0.5
      });
    }
    // pre-seed the FLOW dabs — the layer that makes the painting BREATHE. They
    // ride on top of the baked scene each frame, swaying (grass), drifting
    // (water glints), or gliding (clouds) per the palette's `flow` mode.
    _flow = [];
    var fbud = _budget(Math.max(12, Math.round(w / 5)), LIVE_CAP_FLOW);
    var fn = fbud.n;
    var nearColors = (pal.layers[pal.layers.length - 1] || {}).colors || ['#FFFFFF'];
    for (var fi = 0; fi < fn; fi++) {
      var isCloud = pal.flow === 'clouds';
      _flow.push({
        x: R() * w,
        y: (pal.flow === 'sway' ? lerp(0.5, 0.99, R()) : lerp(0.3, 0.9, R())) * h,
        len: lerp(isCloud ? 5 : 2.2, isCloud ? 11 : 5.5, R()) * fbud.scale,
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
    // SKY (clouds): the near plane is a SUNLIT CLOUD BANK the cat wades through
    // — broad, rounded, BRIGHT puffs whose tops arc over the rim, not a row of
    // thin stalks. Two earlier passes tried to make it out of the scene's own
    // warm sand tones and it was invisible for a measurable reason: mixing the
    // near colour toward #F4C594 landed on ~#F2CAA1, and the base of the sky
    // gradient IS #F2CFB4 — the plane was painted the same colour as the wall
    // behind it, so there was nothing to see (and every attempt to fix it by
    // DARKENING produced the "dirty border" the owner rejected). A near plane
    // needs a VALUE gap, and on a luminous dawn sky the only direction with
    // headroom is BRIGHTER: cloud tops catch the sun, so they read as a plane
    // in front while making the band lighter, never dirtier.
    var fgAiry = (fgKind === 'clouds');
    var fgBud = _budget(fgAiry ? Math.max(12, Math.round(w / 13)) : Math.max(18, Math.round(w / 5.5)),
                        LIVE_CAP_FG);
    var fgN = fgBud.n;
    var fgBase = h + 1;                                  // rooted at the very bottom
    for (var gi2 = 0; gi2 < fgN; gi2++) {
      var gx = R() * w;
      // tall enough to reach up into the pet's paw/ankle band (pet box ~30px in
      // a ~48px bar sits with its feet ~1px off the bottom): 9–16px blades.
      // Cloud puffs are instead BROAD and rounded (the fg 'clouds' branch draws
      // rx=len/2, ry=wid), so they overlap into one continuous bank.
      var glen = fgAiry ? lerp(22, 42, R()) : lerp(9, 16, R());
      var gwid = (fgAiry ? lerp(4.5, 8.5, R()) : lerp(1.4, 2.6, R())) * fgBud.scale;
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
      // Sky near CLOUD BANK reads by LUMINANCE, upward: mix toward a near-white
      // sunlit cloud top (#FFFDF6), well above the #F2CFB4 base of the sky
      // gradient, so the bottom band measurably BRIGHTENS. That is both the
      // depth cue and the guarantee it can never become the rejected dark
      // border — a plane made of light cannot pool into dirt.
      var fgShade = fgDark ? '#182A0C' : (fgKind === 'drift' ? '#3E7E80' : '#FFFDF6');
      var fgMix = fgDark ? 0.55 : (fgKind === 'drift' ? 0.26 : 0.72);
      // Cloud puffs sit LOW so they bank around the cat's legs, their crowns
      // arcing over the rim; their broad radii overlap into one continuous
      // sunlit bank rather than scattered mid-air specks.
      var ghy = fgAiry ? (h - lerp(-1, 7, R())) : fgBase;
      _fgBlades.push({ kind: fgKind, x: gx, hy: ghy,
                       base: { x: gx - glen * Math.cos(gang), y: fgBase - glen * Math.sin(gang) },
                       rootY: fgBase, len: glen, wid: gwid, ang: gang,
                       color: _mixHex(fgColors[(R() * fgColors.length) | 0], fgShade, fgMix),
                       shade: fgShade,
                       alpha: fgDark ? lerp(0.92, 1.0, R())
                                     : (fgAiry ? lerp(0.52, 0.74, R()) : lerp(0.42, 0.6, R())),
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
    // Like the other LIVE populations (§ LIVE_CAP_*), the mounds are re-queued
    // every frame, so on a very wide bar we widen each mound instead of adding
    // more: the broken, gapped SILHOUETTE that keeps this from reading as a
    // ruled border comes from the jittered width/height/sink, which survives
    // the widening — only the stride grows.
    var uStride = 1;
    if (!uSkip) {
      var uWant = Math.round((w + 12) / 12.5);        // ~mounds at the natural stride
      if (uWant > LIVE_CAP_UNDERSTORY) uStride = uWant / LIVE_CAP_UNDERSTORY;
    }
    while (!uSkip && ux < w + 6) {
      var uw = lerp(10, 22, R()) * uStride;             // wide, overlapping
      var uh = lerp(2.5, uDark ? 5.0 : 2.6, R());        // low (much lower on airy scenes)
      var uc = _mixHex(fgColors[(R() * fgColors.length) | 0], uShadeSeed, uMix);
      _understory.push({ x: ux + uw * 0.5, rx: uw * 0.6, ry: uh,
                         sink: lerp(3.0, uDark ? 6.0 : 8.0, R()),  // airy: sunk deeper so only a hint pokes up
                         color: uc, alpha: uDark ? lerp(0.55, 0.75, R()) : lerp(0.14, 0.26, R()),
                         ph: R() * 6.283185 });
      ux += uw * lerp(0.55, 0.9, R());                   // overlap + occasional gap
    }
    _spawnCritter(R);
    // Compute the top of the band the fg plane paints, from the SEEDED geometry
    // (blade root minus its length/sway headroom, and the highest airy puff), so
    // the per-frame clear can be confined to it without ever clipping a stroke.
    // Generous headroom: blades sway, and the disturbance shove lengthens them.
    var fgTop = h;
    for (var ti = 0; ti < _fgBlades.length; ti++) {
      var tb = _fgBlades[ti];
      var anchor = (tb.hy != null ? tb.hy : tb.rootY);
      fgTop = Math.min(fgTop, anchor - tb.len * 1.6 - 6);
    }
    _fgTop = Math.max(0, Math.floor(fgTop));
    // The sun glow is baked to a tile once here, not rebuilt per frame.
    _bakeGlowTile(pal, h);
    // Size/scene changed → one full clear on the next frame retires any
    // stale pixels from the previous geometry.
    _needFullClear = true;
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
    var pal = _livePal || PALETTES[_scene] || PALETTES.meadow;
    var c = _ctx, w = _w, h = _h;
    var dt = Math.max(0, Math.min(0.08, (ms - _lastMs) / 1000));
    _lastMs = ms;
    c.setTransform(_dpr, 0, 0, _dpr, 0, 0);
    // NO full-canvas clear. The baked buffer is OPAQUE over the whole
    // canvas (full-bleed), so blitting it already overwrites the whole of last
    // frame's overlay. Clearing w×h was ~20% of the frame's pixel budget spent
    // erasing pixels that were about to be overwritten anyway. A full clear is
    // still done ONCE after any re-bake (size/scene change), where stale pixels
    // from the previous geometry really can survive.
    if (_needFullClear) { c.clearRect(0, 0, w, h); _needFullClear = false; }
    c.save();
    if (_buf) c.drawImage(_buf, 0, 0, w, h);
    // TIME OF DAY: has the clock moved into a new bucket since this buffer was
    // baked? Checked once a minute (not per frame — the boundary moves at most
    // six times a day). Re-baking mid-session would SNAP the palette, so the
    // outgoing painting is kept and faded out over the incoming one.
    if (ms - _lastBucketCheck > BUCKET_POLL_MS) {
      _lastBucketCheck = ms;
      if (_bakedBucket && _sceneBucket(_hourOverride) !== _bakedBucket) _beginTimeShift();
    }
    if (_xfadeT > 0 && _xfadeBuf) {
      _xfadeT -= dt * (1000 / XFADE_MS);
      if (_xfadeT <= 0) { _xfadeT = 0; _xfadeBuf = null; }
      else {
        c.save();
        c.globalAlpha = _xfadeT;
        c.drawImage(_xfadeBuf, 0, 0, w, h);
        c.restore();
      }
    }
    // Track the pet's foot motion (guarded) so the flow-deform + wake marks can
    // react to WHERE and HOW FAST the cat is moving. Runs before the layers so
    // _paintFlow can press the grass under the current foot position.
    _trackPet(dt, w);
    // advance the ground-disturbance field (the pet presses it, it springs back)
    _updateDisturb(dt, w);
    // advance the activity weather (pure decay — see _stepWeather)
    _stepWeather(dt, ms);
    // (The old bright additive "pet-attention" halo that pooled warm light
    // under the cat was REMOVED — owner: the pet reads as floating and the
    // moving light is fake. An additive glow ring under a sprite is exactly the
    // video-game "selection marker" tell; the cat is instead grounded by its
    // CSS cast shadow (.tofu-pet::after) + the NEW foreground occlusion plane
    // that draws the near scene IN FRONT of it. See _paintForeground below.)
    // drifting sun glow — a soft warm radial that sweeps horizontally (DIMMED:
    // owner found the moving light too glaring; peak/mid/sweep are tuned-down
    // named constants). Blitted from the baked tile (see _bakeGlowTile): the
    // gradient is no longer rebuilt per frame, and the additive blend now
    // touches only the tile's 2R×2R footprint instead of the whole bar.
    var sx = _sunX(ms, w);
    var sy = h * 0.14;
    // publish the live light direction for the pet (normalized, scene-warmth)
    _light.nx = w > 0 ? sx / w : 0.5;
    _light.ny = 0.14;
    _light.warm = SCENE_WARMTH[_scene] != null ? SCENE_WARMTH[_scene] : 0.7;
    c.save();
    c.globalCompositeOperation = 'lighter';
    // ACTIVITY WEATHER, carried by the LIGHT — the cheapest possible channel:
    // it modulates the alpha of a blit that already happens, so cloud shadow and
    // the success light-break cost literally zero extra pixels. Overcast dims
    // the sun while work is in flight; a success burst briefly over-brightens it.
    var wxGlow = 1 - _wx.overcast * 0.7 + _wx.burst * 0.6;
    if (wxGlow < 0) wxGlow = 0;
    if (wxGlow !== 1) c.globalAlpha = wxGlow;
    if (_glowTile) c.drawImage(_glowTile, sx - _glowR, sy - _glowR, 2 * _glowR, 2 * _glowR);
    c.globalAlpha = 1;
    // twinkling specular dabs (the shimmer): additive so they read as glints
    for (var i = 0; i < _sparks.length; i++) {
      var s = _sparks[i];
      var tw = 0.5 + 0.5 * Math.sin(ms * 0.001 * s.sp + s.ph);
      var a = 0.12 + 0.42 * tw * tw;
      var dx = Math.sin(ms * 0.0007 * s.sp + s.ph) * 0.8;   // micro sway
      dab(c, s.x + dx, s.y, s.len * (0.7 + 0.5 * tw), s.len * 0.5, s.ang, pal.spark, a);
    }
    flushDabs();          // MUST land while 'lighter' is still active
    c.restore();
    // RAIN IMPULSE — a single brief pass, never a held state. It exists only
    // while _wx.rain is decaying (see _stepWeather), so it self-clears within
    // ~2s and no code path can leave the bar stormy. Bounded by the same live
    // budget as everything else: a fixed small count, not area-seeded.
    if (_wx.rain > 0.01) {
      var rn = Math.round(LIVE_CAP_SPARK * 0.8 * _wx.rain);
      var rcol = _mixHex(pal.spark, '#8FA6C8', 0.75);
      for (var ri = 0; ri < rn; ri++) {
        // deterministic streak positions from the index, drifting downward with
        // the impulse so the pass reads as falling rather than flickering
        var rx0 = ((ri * 137.5) % 100) / 100 * w;
        var ry0 = (((ri * 61.8) % 100) / 100 + (1 - _wx.rain) * 0.8) * h;
        if (ry0 > h) continue;
        dab(c, rx0, ry0, 3.2, 0.5, 1.28, rcol, 0.34 * _wx.rain);
      }
      flushDabs();
    }
    // the LIVE near layer — swaying/flattening grass · rippling/splashing water ·
    // drifting/shoved clouds — the near band of EVERY scene, rendered live (not
    // baked) so it moves and reacts to the pet. Drawn before the thin flow
    // overlay so the fine breathing sits on top of the live bank.
    _paintLiveLayer(c, pal, ms, w, h);
    flushDabs();
    // the FLOW layer — swaying grass / drifting glints / gliding clouds. It now
    // also PRESSES/PARTS around the pet's foot (see _paintFlow's deform).
    _paintFlow(c, pal, ms, w, h);
    flushDabs();
    // the PET-WAKE marks — grass kicked up / a splash ripple / a cloud puff at
    // the foot, painted ON the canvas so pet & scene read as one layer.
    _paintWake(c, pal, ms, w, h);
    flushDabs();
    // the critter (drawn last, above the scene but below the pet at z1)
    _paintCritter(c, ms, dt, w, h);
    c.restore();
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
    // Clear only the BAND this plane actually paints. The near plane is rooted
    // at the bottom and reaches up into the pet's ankles — measured, that is
    // ~29% of a 48px bar — but the clear was full-height, making it (with the bg
    // clear) the single largest line in the frame's pixel budget. The band is
    // computed from the seeded geometry (see _fgTop), so it cannot drift out of
    // step with what is drawn.
    c.clearRect(0, _fgTop, w, h - _fgTop + 2);
    c.save();
    var pal = _livePal || PALETTES[_scene] || PALETTES.meadow;
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
    flushDabs();          // the mounds sit UNDER the blades — keep that order
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
    flushDabs();
    c.restore();
  }

  /** The clock crossed a time-of-day boundary: snapshot the painting we are
   *  leaving, re-bake in the new light, and fade the old one out over it.
   *  Degrades to a plain re-bake if a snapshot canvas is unavailable. */
  function _beginTimeShift() {
    var snap = null;
    try {
      if (_buf && _buf.width > 0) {
        snap = document.createElement('canvas');
        snap.width = _buf.width; snap.height = _buf.height;
        var sc = snap.getContext('2d');
        if (sc && sc.drawImage) sc.drawImage(_buf, 0, 0); else snap = null;
      }
    } catch (e) { snap = null; }     // harmless — we just re-bake without a fade
    // A time shift re-bakes COLOUR ONLY — the tint preserves `seed`, so every
    // dab lands byte-identically and no stale pixels survive to retire. The
    // full-canvas clear _paintBuffer unconditionally requests is therefore both
    // unnecessary and actively harmful here: _paintFrame consumes that flag at
    // the TOP of the frame while this runs BELOW it, so the clear would land a
    // frame LATE and wipe the first cross-fade composite before re-blitting.
    // Preserve whatever the flag was, so a genuine geometry change (resize,
    // scene switch) still gets its clear. (Pinned by
    // test_time_rebake_issues_no_full_canvas_clear.)
    var _keepClear = _needFullClear;
    _paintBuffer();                  // re-bakes with the NEW bucket's tint
    _needFullClear = _keepClear;
    if (snap) { _xfadeBuf = snap; _xfadeT = 1; }
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
      if (runtimeScope.TofuPet && typeof runtimeScope.TofuPet.getState === 'function') {
        var st = runtimeScope.TofuPet.getState();
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
      if (runtimeScope.TofuPet && typeof runtimeScope.TofuPet.getState === 'function') {
        var st = runtimeScope.TofuPet.getState();
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
      if (runtimeScope.TofuPet && typeof runtimeScope.TofuPet.getState === 'function') {
        var st = runtimeScope.TofuPet.getState();
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
    flushDabs();
    c.restore();
  }

  function _loop(ts) {
    _raf = 0;
    if (_paused || _reduced || !_isTofu() || _scene === 'off') return;   // loop parks
    if (_offscreen) return;                       // bar not on screen → nothing to paint
    if (!_t0) { _t0 = ts; _lastMs = 0; }
    // FRAME PACING. The scene is ambient weather — grass sway, drifting glints,
    // gliding cloud, a slow sun. None of it moves more than a fraction of a
    // pixel per 60Hz tick, so painting it every display refresh spent half the
    // work on frames the eye cannot separate. We paint on a ~SCENE_FPS cadence
    // and let rAF keep supplying the vsync clock (cheap: an early return), which
    // halves the scene's cost on a 60Hz panel and thirds it on a 120Hz one.
    // Nothing else has to change, because every animated quantity here is a
    // function of absolute `ms`, and `dt` is measured between PAINTED frames —
    // so the disturbance spring-back stays frame-rate independent.
    if (ts - _lastPaint >= SCENE_FRAME_MS) {
      _lastPaint = ts;
      _paintFrame(ts - _t0);
    }
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
    // Scrolled/collapsed out of view: stop the rAF chain outright. This is the
    // one park that costs literally nothing (no ticking callback at all),
    // unlike the paced loop which still wakes per vsync to decide.
    if (_offscreen) { if (_raf) { cancelAnimationFrame(_raf); _raf = 0; } return; }
    if (_reduced || _paused) {
      if (_raf) { cancelAnimationFrame(_raf); _raf = 0; }
      _lastMs = 0;
      _paintFrame(0);              // one static, fully-painted frame
      return;
    }
    // Re-arm the pace clock so a loop resumed after a park paints immediately
    // instead of waiting out a stale interval.
    if (!_raf) { _lastPaint = -1e9; _raf = requestAnimationFrame(_loop); }
  }
  // (Re)size the canvas + buffer to the bar's box at the current DPR, then
  // re-bake the static scene. Cheap-guards a zero-size (bar still display:none).
  function _resize() {
    if (!_canvas || !_bar) return;
    var r = _bar.getBoundingClientRect();
    var w = Math.round(r.width), h = Math.round(r.height);
    if (w <= 0 || h <= 0) return;                 // bar hidden — wait for a real box
    _dpr = Math.max(1, Math.min(SCENE_DPR_CAP, window.devicePixelRatio || 1));
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
    // The Android WebView never flips document.hidden when the app is
    // backgrounded; the shell dispatches tofu:native-visibility instead.
    document.addEventListener('tofu:native-visibility', function (e) {
      _paused = !!(e && e.detail && e.detail.hidden) || document.hidden;
      _ensureLoop();
    });
    // Follow the bar's box + the scene attribute (set by tofu-pet.js) + the
    // app theme, all without coupling to the pet: attribute/resize observers.
    if (window.ResizeObserver && _bar) {
      try { new ResizeObserver(function () { _resize(); }).observe(_bar); } catch (e) { /* harmless */ }
    }
    // Park the whole loop while the bar is scrolled/collapsed out of view — an
    // invisible painting is pure waste. Guarded + optional: without
    // IntersectionObserver the scene simply keeps its old always-on behaviour.
    // ACTIVITY WEATHER: listen to the app's activity signal DIRECTLY rather than
    // having the pet forward it. The two stay decoupled (neither needs to know
    // the other exists), and the weather scalars live outside the baked buffer,
    // so a re-bake — a resize, a scene switch, a time-of-day shift — cannot lose
    // the current weather. Guarded: no addEventListener → the scene simply never
    // reacts, which is the neutral behaviour it had before.
    try {
      document.addEventListener('tofu:activity', function (e) {
        _onActivity(e && e.detail, _lastMs);
      });
    } catch (err) { /* harmless — weather just stays neutral */ }
    if (window.IntersectionObserver && _bar) {
      try {
        new IntersectionObserver(function (entries) {
          for (var i = 0; i < entries.length; i++) {
            var vis = !!entries[i].isIntersecting;
            if (vis === !_offscreen) continue;
            _offscreen = !vis;
            _ensureLoop();
          }
        }, { threshold: 0 }).observe(_bar);
      } catch (e) { /* harmless — scene just never parks on scroll */ }
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

  runtimeScope.TofuScene = {
    mount: mount,
    repaint: repaint,
    setScene: setScene,
    getScene: getScene,
    SCENES: SCENES,
    critterX: critterX,
    critterInfo: critterInfo,
    lightInfo: lightInfo,
    spook: spook,
    // Time-of-day seam. `setHour(h)` pins the scene's clock (null = follow the
    // real one) so tests — and a future manual override — never have to mock
    // Date. Setting it re-bakes through the same cross-fade a natural boundary
    // crossing uses, so the two paths cannot drift apart.
    setHour: function (h) {
      _hourOverride = (typeof h === 'number') ? h : null;
      if (_bakedBucket && _sceneBucket(_hourOverride) !== _bakedBucket) _beginTimeShift();
      return _sceneBucket(_hourOverride);
    },
    getBucket: function () { return _sceneBucket(_hourOverride); },
    // Activity-weather seam. setWeather(false) is the instant kill switch: it
    // zeroes every scalar, so the bar returns to neutral on the next frame and
    // stays there. weatherInfo() exposes the scalars for tests and diagnostics.
    setWeather: function (on) {
      WEATHER_ENABLED = (on !== false);
      if (!WEATHER_ENABLED) { _wxTarget = 0; _overcastSince = -1; _wx.overcast = _wx.burst = _wx.rain = 0; }
      return WEATHER_ENABLED;
    },
    weatherInfo: function () {
      return { enabled: WEATHER_ENABLED, overcast: _wx.overcast,
               burst: _wx.burst, rain: _wx.rain, target: _wxTarget };
    },
    TIME_BUCKETS: ['deepNight', 'earlyMorning', 'morning', 'afternoon', 'evening', 'night']
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', _boot);
  } else {
    _boot();
  }
})();
