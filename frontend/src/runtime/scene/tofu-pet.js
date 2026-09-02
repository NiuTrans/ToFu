/* ===== migrated source: tofu-pet.js ===== */
var runtimeScope = (typeof window !== 'undefined' && window.TofuModules &&
  window.TofuModules.sceneRuntime)
  || (typeof window !== 'undefined' ? window : globalThis);
/* The i18n t() binding. This chunk is a native ES module in production, so it
 * shares NO lexical scope with the boot bundle (where t is an imported module
 * binding) — an unqualified free `t` here is a ReferenceError at evaluation,
 * which vite reports as `vite:preloadError` and the boot guard used to answer
 * with a page reload (2026-08-20 infinite-reload incident). Resolution order:
 * an already-defined global wins (classic-script test harnesses inject their
 * dictionary-backed t before evaluating this file as a plain script); else the
 * scoped scene capability published by TofuModules; else a
 * pass-through so a decorative feature can never crash the page. Call sites
 * stay literal `t('…')` — charter #18's scanner reads this file. */
var t = (typeof globalThis !== 'undefined' && typeof globalThis.t === 'function') ? globalThis.t
  : (typeof runtimeScope.t === 'function' ? runtimeScope.t
  : function (key) { return key; });
/*
 * tofu-pet.js — the little TOFU that LIVES in the project bar.
 *
 * The project bar (tofu theme) is a tiny living DIORAMA: a procedural
 * Impressionist scene fills the bar (tofu-scene.js) and the Tofu mascot itself
 * roams left-right along its baseline — walking, pausing, sitting, grooming,
 * stretching, napping, and CHASING a little scene critter (butterfly / fish /
 * bird) that drifts through the background. The classic desktop-pet wander
 * model (oneko / Shimeji), but coupled to the scene so the two interact. The
 * pet passes BEHIND the opaque control pills via z-index, so it can NEVER
 * block a hit target. Each pose is a transparent PNG frame in
 * static/icons/pet/tofu/tofu-<x>.png.
 *
 * CHARACTER: one tofu, one look. The pet IS the brand mascot — a front-facing
 * cream tofu block in the mascot's palette, same ω mouth, same blush — so
 * the creature in the bar and the logo above it are provably the same
 * character. (History: this bar first held a swappable "pack"
 * registry, then a single borrowed pixel-art cat. Both the pack switcher and,
 * later, a logo-skin picker were removed on owner instruction — mascot
 * switching has been vetoed twice. Do NOT re-add a character registry, a
 * picker, or a localStorage try-on: ship ONE character.)
 *
 * The 22 frames are AI-DESIGNED raster art (2026-07-30 revamp), not the old
 * procedural SVG: ai/hero_v1.png was drawn with the image tool and every other
 * pose is an img2img edit of it, then
 * static/icons/_gen/tofu-pet/process_ai_frames.py chroma-keys the green
 * backdrop, trims, recentres and downscales each one to tofu-<frame>.png
 * (it has a --check CI gate). One pipeline ⇒ the character cannot drift
 * between frames.
 *
 * MOTION is a real frame-by-frame WALK CYCLE: keyposes advanced by a frame
 * ticker on the rAF loop while walking (NOT one image sliding). Layers (so
 * transforms never fight): .tofu-pet owns POSITION (translateX, set by the
 * wander loop) · .tofu-pet-img owns the per-frame LIFE animation (breathe /
 * walk-bob) · and DIRECTION lives on the individual <img> frame elements
 * (scaleX(var(--pet-face-flip))) — a child of the animated layer, so the
 * mirror never fights the keyframes.
 *
 * WHY DIRECTION IS A WHOLE-SPRITE MIRROR AGAIN (the end of the one-sun split):
 * the previous isometric body encoded a sun in its three faces, so mirroring
 * was confined to a character-space subgroup (face+feet flipped, body never
 * did). That kept the light consistent but meant the BODY's 3/4 orientation
 * was frozen: half the time the cube's shaded depth face pointed against the
 * direction of travel — the "walks backward / moonwalk" complaint, structural
 * and unfixable while the body could not turn. The new character is
 * FRONT-FACING with soft symmetric lighting, so mirroring the WHOLE frame is
 * both safe (no baked sun to move) and complete (body, gaze and stride always
 * agree with travel). The scene-sun coupling survives where it is
 * art-independent: the cast shadow + the filter-based form light below.
 *
 * PET ⋈ SCENE INTERACTION (owner ask — "interaction between the pet and the
 * background"): tofu-scene.js exposes the drifting critter's position
 * (TofuScene.critterX) and a spook() hook. The wander FSM has a 'chase' state:
 * when the critter drifts near, the pet perks (alert) then pursues it; on
 * catch it pounces (surprised) and the critter darts away (scene.spook()). The
 * scene in turn glows softly under the pet (it reads TofuPet.getState().x). Both
 * couplings are OPTIONAL + guarded, so each module still works entirely alone.
 *
 * Public surface `runtimeScope.TofuPet`:
 *   TofuPet.react(expr, ms)      — play a transient expression, auto-return
 *   TofuPet.setActivity(kind)    — 'loading'|'success'|'error'|'none'
 *   TofuPet.interact()           — a "you touched the pet" bump (mood up, waves happy)
 *   TofuPet.getState()/setState(patch) — read/patch mood/energy/activity (+ x for the scene)
 *   TofuPet.refresh()            — recompute the resolved expression
 *   TofuPet.setDecor(style)/cycleDecor() — pick the SCENE ('meadow'|'pool'|'sky'|'off')
 *   TofuPet.setDay(digest)       — map the daily-report digest → mood/expression
 *   TofuPet.EXPRESSIONS / DECOR_STYLES
 * plus DOM event seams so app code can drive it without importing it:
 *   document.dispatchEvent(new CustomEvent('tofu:activity', {detail:'loading'}))
 *   document.dispatchEvent(new CustomEvent('tofu:react',    {detail:{expr:'happy', ms:1800}}))
 *   document.dispatchEvent(new CustomEvent('tofu:decor',    {detail:'pool'}))
 *
 * Accessibility (per WCAG 2.3.3 / 2.2.2 + oneko): under prefers-reduced-motion
 * the pet does NOT roam or chase (stands still, single frame); it pauses when
 * the tab is hidden; and it is aria-hidden + never focus/click-stealing.
 * mood/energy are best-effort persisted to localStorage. Nothing here touches
 * the chat/streaming pipeline.
 */
(function () {
  'use strict';

  var STORAGE_KEY = 'tofu_pet_state';
  var DECOR_KEY = 'tofu_bar_scene';
  var MOUNT_ID = 'tofuPet';

  // ── Scene styles (CSS maps each to a --bar-scene ground image via
  // [data-decor]). 'off' hides the scene. Extend by adding a style here AND a
  // [data-decor="<style>"] rule + a scene-<style>.svg asset. ──
  var DECOR_STYLES = ['meadow', 'pool', 'sky', 'off'];
  // Human labels for the visible scene-switch button in the project bar.
  // Resolved through t() at READ TIME, never cached: a cached label would
  // freeze whatever language was active when this module first ran.
  //
  // The call is t('pet.scene.' + style) — a DYNAMIC prefix, which is exactly the
  // shape lib/i18n_boot_keys.T_CALL_DYNAMIC_PREFIX_RE exists to catch
  // (charter #18). The scanner sees the `pet.scene.` prefix and expands it to
  // every matching key in the source dict, so the boot pack carries all four
  // without anyone hand-copying a list. Writing a literal map here instead
  // would put the English strings back in the JS and hide them from discovery.
  function _sceneLabel(style) {
    return t('pet.scene.' + style);
  }
  var DEFAULT_DECOR = 'meadow';
  var _decor = DEFAULT_DECOR;

  // ── The pet's art: BRAND-NATIVE tofu frames at static/icons/pet/tofu/ as
  // tofu-<frame>.png. The character IS the Tofu mascot (a cream tofu block in
  // the logo's palette) rather than a borrowed cat, so the thing living in the
  // bar and the logo above it are the same creature. The art is AI-designed —
  // static/icons/_gen/tofu-pet/process_ai_frames.py turns the raw poses into
  // every shipped frame from one pipeline, so the character cannot
  // desynchronise between frames. Transparent PNG + object-fit:contain: the
  // squash/stretch poses keep their height contrast inside the fixed box.
  // URL built with BASE_PATH (from core.js) so it stays correct behind a
  // base-path / VS Code proxy. ──
  var PET_DIR = '/static/icons/pet/tofu';
  var PET_PREFIX = 'tofu-';
  var PET_EXT = '.png';
  var _base = (typeof runtimeScope.BASE_PATH === 'string')
    ? runtimeScope.BASE_PATH : '';
  function _frameUrl(frame) {
    return _base + PET_DIR + '/' + PET_PREFIX + frame + PET_EXT;
  }

  // Resting expressions (the mood/time FSM resolves to one of these). Every
  // name here must have a tofu-<name>.svg on disk.
  var EXPRESSIONS = ['idle', 'happy', 'sleepy', 'sleeping', 'thinking',
    'surprised', 'sad', 'celebrating', 'alert'];
  // Poses that reuse another frame's art (the block has poses, not a separate
  // drawing per mood). 'curious' → the alert perk; nothing else needs remapping.
  var FRAME_ALIAS = { curious: 'alert' };

  // The WALK CYCLE: eight slots played in order while state==='walk'. Slots
  // 5..8 REPLAY the four authored drawings (the stride's second half-cycle is
  // the same art), so there are 8 slots but WALK_DISTINCT=4 real keyposes.
  //
  // FOUR authored poses, not one symmetric contact pose reused twice: the old
  // cycle reused ONE contact pose for both contact beats, so no foot ever led
  // and the gait read as shuffling in place / sliding BACKWARDS. A stride only
  // reads as walking if the leading foot alternates, which needs a near-leads
  // AND a far-leads pose.
  var WALK_FRAMES = ['walk1', 'walk2', 'walk3', 'walk4',
    'walk5', 'walk6', 'walk7', 'walk8'];
  var WALK_DISTINCT = 4;     // authored keyposes per gait cycle (5..8 replay 1..4)

  // ── STRIDE ⋈ SPEED: THE FRAME INTERVAL IS DERIVED, NEVER A LITERAL ──
  //
  // STRIDE_PX is a MEASUREMENT OF THE ART: how far one foot travels from
  // leading to trailing across a half gait cycle, in rendered px of the 30px
  // sprite (walk1's lead foot vs walk3's trail foot). The guard in
  // tests/test_frontend_pet_light_direction.py re-measures it from the shipped
  // PNGs, so this number cannot silently rot away from the drawings.
  //
  // WHY DERIVED: a fixed WALK_FRAME_MS made the gait's cadence independent of
  // how fast the body actually travelled, so the two silently disagreed — the
  // feet churned 1.62× faster than the pet moved (measured 2026-07-31), the
  // textbook foot-slip / "hummingbird legs on a sliding body" read. And ONE
  // constant cannot be right for THREE speeds anyway: walk 41, chase 82, flee
  // 120 px/s. So the interval is computed from the live speed:
  //
  //     per-keypose ms = STRIDE_PX / speed * 1000 / WALK_DISTINCT
  //
  // At the walk speed that lands on ~75ms (13.3fps), clearing the ~12fps floor
  // below which a keypose cycle reads as a flicker between drawings rather than
  // motion; a chase/flee correctly plays faster because the pet IS moving
  // faster. Being a computation rather than two hand-tuned numbers, a future
  // speed change cannot reintroduce slip.
  var STRIDE_PX = 12.28;
  var WALK_FPS_FLOOR = 12;   // below this a keypose cycle reads as flicker
  // Fastest cadence worth painting: ~2 display frames at 60Hz. A flee at
  // 120px/s would otherwise ask for ~26ms, finer than the rAF tick can honour,
  // which just drops keyposes unevenly.
  var MIN_FRAME_MS = 33;
  function _gaitMs(speed) {
    if (!(speed > 0)) return 1000 / WALK_FPS_FLOOR;
    return Math.max(MIN_FRAME_MS, STRIDE_PX / speed * 1000 / WALK_DISTINCT);
  }
  var _walkIdx = 0, _walkAccum = 0;

  // GROOM cycle (a settling wobble) and STRETCH cycle (a full-body stretch) —
  // the stationary "forms". Played frame-by-frame in their own states, same
  // ticker as walk. A cube has no paw to lick, so grooming is a tidy-up wobble
  // and scratching is the whole body stretching tall then settling wide.
  var GROOM_FRAMES = ['groom1', 'groom2', 'groom3'];
  var SCRATCH_FRAMES = ['scratch1', 'scratch2'];
  var POSE_FRAME_MS = 220;
  var _poseIdx = 0, _poseAccum = 0, _poseFrames = null;

  // GAZE: a "sit and look around" beat — the pet stays put (alert pose) and
  // periodically turns to face the other side, as if watching the scene. No
  // extra art (reuses the 'alert' frame + the facing flip). This is a distinct
  // stationary ACTIVITY so the pet does more than pace the bar.
  var GAZE_TURN_MS = 1300;
  var _gazeAccum = 0;
  var _turnTimer = null;   // clears the transient 'pivot' pop after a facing flip
  var _landTimer = null;   // clears the landing squash after a drop

  // Transient expressions auto-return to the resolved state after their beat.
  var TRANSIENT = { happy: 1, celebrating: 1, surprised: 1, curious: 1, alert: 1 };

  // ── State model ──
  var S = {
    mood: 72,          // 0..100 — up on interaction/success, decays toward 50
    energy: 82,        // 0..100 — decays over the day, restored at night
    lastInteraction: Date.now(),
    activity: 'none',  // none | loading | success | error
    expr: 'idle'
  };

  function _load() {
    try {
      var raw = localStorage.getItem(STORAGE_KEY);
      if (!raw) return;
      var v = JSON.parse(raw);
      if (typeof v.mood === 'number') S.mood = Math.max(0, Math.min(100, v.mood));
      if (typeof v.energy === 'number') S.energy = Math.max(0, Math.min(100, v.energy));
      if (typeof v.lastInteraction === 'number') S.lastInteraction = v.lastInteraction;
    } catch (e) { /* private-mode / corrupt — start fresh, best-effort */ }
  }
  function _save() {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify({
        mood: S.mood, energy: S.energy, lastInteraction: S.lastInteraction
      }));
    } catch (e) { /* storage disabled — pet just won't remember, harmless */ }
  }

  function _loadDecor() {
    try {
      var v = localStorage.getItem(DECOR_KEY);
      if (v && DECOR_STYLES.indexOf(v) !== -1) _decor = v;
    } catch (e) { /* storage disabled — use default, harmless */ }
  }

  function _applyDecor() {
    var bars = document.querySelectorAll('.project-bar');
    for (var i = 0; i < bars.length; i++) bars[i].setAttribute('data-decor', _decor);
    _syncSceneButton();
  }
  // Keep the visible scene-switch button (static markup in index.html) in step
  // with the current scene: update its label, tooltip, and active marker. This
  // is the ONE place the button reflects state — setDecor/cycleDecor both route
  // through _applyDecor, so there is no second state path.
  function _syncSceneButton() {
    var btns = document.querySelectorAll('.scene-switch-btn');
    for (var i = 0; i < btns.length; i++) {
      var b = btns[i];
      var name = _sceneLabel(_decor);
      var lbl = b.querySelector('.scene-switch-label');
      if (lbl) lbl.textContent = name;
      b.setAttribute('data-scene', _decor);
      b.setAttribute('title', t('pet.sceneTooltip', { scene: name }));
    }
  }
  function setDecor(style) {
    if (DECOR_STYLES.indexOf(style) === -1) return _decor;
    _decor = style;
    try { localStorage.setItem(DECOR_KEY, _decor); } catch (e) { /* harmless */ }
    _applyDecor();
    return _decor;
  }
  function cycleDecor() {
    var i = DECOR_STYLES.indexOf(_decor);
    return setDecor(DECOR_STYLES[(i + 1) % DECOR_STYLES.length]);
  }

  // ── Time buckets (research-grounded). Returns {bucket, expr}. ──
  function _timeBucket(h) {
    if (h >= 0 && h < 5) return { bucket: 'deepNight', expr: 'sleeping' };
    if (h < 8) return { bucket: 'earlyMorning', expr: 'sleepy' };
    if (h < 12) return { bucket: 'morning', expr: 'happy' };
    if (h < 17) return { bucket: 'afternoon', expr: 'idle' };
    if (h < 21) return { bucket: 'evening', expr: 'idle' };
    return { bucket: 'night', expr: 'sleepy' };
  }

  // ── Resolve the "resting" expression (used when NOT walking): activity >
  // time-sleep > mood/energy > recent-positive > time-default > idle. ──
  function _resolve() {
    if (S.activity === 'loading') return 'thinking';
    var tb = _timeBucket(new Date().getHours());
    if (tb.expr === 'sleeping') return 'sleeping';
    if (S.mood < 25) return 'sad';
    if (S.energy < 15 || tb.expr === 'sleepy') return 'sleepy';
    var recentPositive = (Date.now() - S.lastInteraction) < 30000 && S.mood > 74;
    if (recentPositive) return 'happy';
    // ── DAY SIGNAL (backend-derived digest) — sits ABOVE the time-default and
    // BELOW explicit activity/sleep/mood. The pet does NO day logic: it only
    // MAPS the shape myday.js already computed from the report — a blocked item
    // → concerned (sad); a fully-cleared day → happy. Absent digest falls
    // straight through to the time-default, so no-digest = unchanged behavior. ──
    if (_day) {
      if (_day.streams && _day.streams.blocked > 0) return 'sad';
      if (_dayAllDone(_day)) return 'happy';
    }
    return tb.expr;   // 'happy' (morning) or 'idle'
  }

  // The pet's hover tooltip. Both halves resolve through DYNAMIC t() prefixes
  // (pet.greet. / pet.feel.), so lib/i18n_boot_keys expands each namespace into
  // the boot pack automatically — add a time bucket or a mood tier and its key
  // ships without touching any list. Composed via a template key rather than
  // string concatenation, because word order differs by language.
  function _feelTier() {
    return S.mood >= 75 ? 'great' : S.mood >= 45 ? 'fine' : 'blue';
  }
  function _title() {
    var tb = _timeBucket(new Date().getHours());
    return t('pet.title', {
      greet: t('pet.greet.' + tb.bucket),
      feel: t('pet.feel.' + _feelTier())
    });
  }

  // ── DOM ──
  var _el = null;         // .tofu-pet (position layer)
  var _img = null;        // .tofu-pet-img (frame layer — holds the live <svg>)
  var _bar = null;        // .project-bar
  var _revertTimer = null;

  // ── FRAME ART: each frame is an <img> kept live in the DOM. The 22 frames
  // are created once and kept; frame changes then toggle which one is VISIBLE
  // — no node construction, nothing for the GC. Rebuilding src per frame
  // instead would re-decode a PNG 13 times a second, which is exactly the kind
  // of per-frame churn that shows up as stutter on a busy page. (Raster art is
  // what makes <img> safe here: the old SVG needed inlining so CSS vars could
  // reach into it; the front-facing raster frames take their lighting from the
  // filter chain on the parent layer, which works across an <img> boundary.)
  var _frameNode = Object.create(null);      // frame name → live <img> element
  var _frameFetching = Object.create(null);  // in-flight guard (create once)
  var _wantFrame = null;                     // last frame ASKED for
  var _curFrame = null;                      // frame currently visible

  function _paintFrame(name) {
    var node = _frameNode[name];
    if (!_img || !node) return;
    if (_curFrame && _frameNode[_curFrame]) {
      _frameNode[_curFrame].style.display = 'none';
    }
    node.style.display = '';
    _curFrame = name;
  }

  function _loadFrame(name) {
    if (_frameNode[name] || _frameFetching[name]) return;
    _frameFetching[name] = 1;
    var node = new Image();
    node.draggable = false;
    node.alt = '';
    node.style.display = 'none';
    node.setAttribute('data-frame', name);
    node.onload = function () {
      delete _frameFetching[name];
      _frameNode[name] = node;
      if (!_img) return;
      _img.appendChild(node);
      // Paint only if this is still the frame we want — a walk cycle may have
      // advanced several times while this decode was in flight.
      if (_wantFrame === name) _paintFrame(name);
    };
    node.onerror = function () {
      delete _frameFetching[name];
      // Same-origin static asset served by the same app that served this JS, so
      // this is not an expected state; surface it instead of failing silently.
      console.warn('[TofuPet] frame art unavailable:', name);
    };
    node.src = _frameUrl(name);
  }

  // The ONE seam every pose path goes through, so "which art is on screen" has a
  // single writer (the old code wrote _img.src from three places).
  function _setFrameArt(name) {
    _wantFrame = name;
    if (_frameNode[name]) _paintFrame(name);
    else _loadFrame(name);
  }

  function _preload() {
    var all = EXPRESSIONS.concat(WALK_FRAMES, GROOM_FRAMES, SCRATCH_FRAMES);
    for (var i = 0; i < all.length; i++) _loadFrame(all[i]);
  }

  function _setWalkFrame(i) {
    _walkIdx = ((i % WALK_FRAMES.length) + WALK_FRAMES.length) % WALK_FRAMES.length;
    _setFrameArt(WALK_FRAMES[_walkIdx]);
  }
  // THE ONE gait advancer, shared by walk / chase / flee. It takes the LIVE
  // speed of the leg that is running, so the cadence is always tied to actual
  // travel: the same drawings play faster during a chase and faster still
  // during a flee, because the body really is covering ground faster. Three
  // copies of this loop against one hard-coded interval is how the feet came to
  // churn 1.62x faster than the pet moved (see _gaitMs).
  function _advanceGait(dt, speed) {
    var per = _gaitMs(speed);
    _walkAccum += dt * 1000;
    if (_walkAccum < per) return;
    var adv = Math.floor(_walkAccum / per);
    _walkAccum -= adv * per;
    _setWalkFrame(_walkIdx + adv);
  }
  // Advance a generic pose cycle (groom / scratch), used by the sit/scratch
  // states. Mirrors _setWalkFrame but over the active pose frame list.
  function _setPoseFrame(i) {
    if (!_poseFrames || !_poseFrames.length) return;
    _poseIdx = ((i % _poseFrames.length) + _poseFrames.length) % _poseFrames.length;
    _setFrameArt(_poseFrames[_poseIdx]);
  }

  function _setFrame(expr) {
    var frame = FRAME_ALIAS[expr] || expr;
    if (EXPRESSIONS.indexOf(frame) === -1) frame = 'idle';
    _setFrameArt(frame);
  }

  // Show a resting expression (idle/sleepy/…): used by the wander FSM's
  // non-walking states + by reactions. Distinct from a walking pose.
  function _setExpr(expr) {
    S.expr = expr;
    _setFrame(expr);
    if (_el) {
      _el.setAttribute('data-expr', expr);
      _el.setAttribute('title', _title());
    }
  }

  function _applyResolved() {
    if (W.state === 'walk' || W.state === 'turn' || W.state === 'chase') return;  // motion owns the frame
    _setExpr(_resolve());
  }

  function react(expr, ms) {
    if (!_el) return;
    _setExpr(expr);
    W.state = 'react'; W.until = _now() + (typeof ms === 'number' ? ms : 1400);
    if (_revertTimer) { clearTimeout(_revertTimer); _revertTimer = null; }
    if (TRANSIENT[expr]) {
      _revertTimer = setTimeout(function () { _revertTimer = null; }, typeof ms === 'number' ? ms : 1400);
    }
  }

  function setActivity(kind) {
    S.activity = (kind === 'loading') ? 'loading' : 'none';
    if (kind === 'success') { S.mood = Math.min(100, S.mood + 8); _save(); react('celebrating', 2200); return; }
    if (kind === 'error') { react('surprised', 1600); return; }
    if (kind === 'loading') { react('thinking', 3000); return; }
    _applyResolved();
  }

  function interact() {
    S.lastInteraction = Date.now();
    S.mood = Math.min(100, S.mood + 6);
    // A poke WAKES the cat up: bump energy hard so the post-startle _pickNext
    // can't immediately fall back into sleep (the "clicked it, it just went
    // back to sleep" bug), and clear any sleepy/day resolution bias.
    S.energy = Math.min(100, S.energy + 22);
    _save();
    _startle();      // startled scramble away — the tap is felt, not a nap
    _showBubble();   // a tap also reports your day (see _showBubble)
  }

  // A poke/tap STARTLES the cat: it flashes 'surprised' and SCURRIES a short
  // dash away from where it was touched, instead of just flipping to 'happy'
  // in place (owner ask: clicking should startle it and make it move — and it
  // shouldn't nap right after). Drives a brief high-speed 'flee' leg the FSM
  // owns, then resumes normal wandering. Guarded for reduced-motion (no dash).
  function _startle() {
    if (!_el) return;
    if (_revertTimer) { clearTimeout(_revertTimer); _revertTimer = null; }
    _setExpr('surprised');
    W.nextChaseOk = _now() + 1500;         // don't immediately chase either
    if (W.reduced) { react('surprised', 1200); return; }
    _measure();
    // flee AWAY from the near wall it's closest to (more room to run)
    var dir = (W.x - W.min) < (W.max - W.x) ? 1 : -1;
    _face(dir);
    W.fleeDir = dir;
    _enter('flee');
  }

  // ══════════════════════════════════════════════════════════════════════
  //  DAY AWARENESS — the pet is connected to "your day" (the daily report).
  //  It does NOT compute any day logic: myday.js derives the digest
  //  {todos:{done,total}, streams:{done,blocked,total}, convCount} STRAIGHT
  //  from the report the backend returns (single source of truth) and fires
  //  `tofu:day`. Here we only MAP that shape → mood/expression (a trivial
  //  mapping) and surface it in the click bubble. A malformed/absent digest is
  //  a silent no-op, so a session that never opens My Day is unaffected.
  // ══════════════════════════════════════════════════════════════════════
  var _day = null;
  function _validDay(d) {
    return !!(d && typeof d === 'object' && d.todos && d.streams &&
      typeof d.streams.total === 'number' && typeof d.todos.total === 'number');
  }
  function _dayAllDone(d) {
    if (!d || !d.streams || !d.todos) return false;
    var total = (d.streams.total || 0) + (d.todos.total || 0);
    var done = (d.streams.done || 0) + (d.todos.done || 0);
    return total > 0 && done >= total && !(d.streams.blocked > 0);
  }
  function setDay(d) {
    if (!_validDay(d)) return;                 // silent no-op — never break the pet
    var wasAllDone = _dayAllDone(_day);
    _day = d;
    // Trivial mood mapping: a fresh transition INTO an all-cleared day earns a
    // one-shot celebration + small mood bump; otherwise just re-resolve so a
    // blocked item surfaces the concerned pose (via _resolve's day branch).
    if (_dayAllDone(d) && !wasAllDone) {
      S.mood = Math.min(100, S.mood + 6); _save();
      react('celebrating', 2200);
      return;
    }
    _applyResolved();
  }

  // ── CLICK-TO-REPORT speech bubble. Tapping the pet pops a small SVG speech
  // bubble summarising the backend day digest. The bubble SHAPE is inline SVG
  // (no emoji/glyph, per CLAUDE.md §3.4); the text is the live counts. It floats
  // above the controls but is pointer-events:none, so it never blocks a hit
  // target, and auto-dismisses. It is position:absolute (its own overlay z-band,
  // see the CSS allowlist note) so it never reserves an in-flow flex slot. ──
  var _bubble = null, _bubbleTimer = null;
  function _dayReportText() {
    // Literal t('...') calls, NOT a local alias. A wrapper like
    // `var _k = function (key, ...) { ... t(key) ... }` reads fine but is
    // INVISIBLE to lib/i18n_boot_keys.T_CALL_KEY_RE, which only matches a
    // literal string as t()'s first argument — so every pet.* key silently
    // missed the boot pack (measured: 0 discovered while 4 were in the dict).
    // t() already falls back to the key and interpolates params, so the
    // wrapper bought nothing and cost discoverability.
    if (!_day) return t('pet.dayGreeting');
    var done = (_day.streams.done || 0) + (_day.todos.done || 0);
    var total = (_day.streams.total || 0) + (_day.todos.total || 0);
    var blocked = _day.streams.blocked || 0;
    if (total === 0) return t('pet.dayIdle');
    var base = t('pet.dayReport', { done: done, total: total });
    if (blocked > 0) base += ' ' + t('pet.dayBlocked', { n: blocked });
    return base;
  }
  var _BUBBLE_SVG = '<svg viewBox="0 0 120 46" preserveAspectRatio="none" aria-hidden="true" xmlns="http://www.w3.org/2000/svg"><path d="M8 2h104a6 6 0 0 1 6 6v22a6 6 0 0 1-6 6H70l-9 11-3-11H8a6 6 0 0 1-6-6V8a6 6 0 0 1 6-6z"/></svg>';
  function _hideBubble() {
    if (_bubbleTimer) { clearTimeout(_bubbleTimer); _bubbleTimer = null; }
    if (_bubble && _bubble.parentNode) _bubble.parentNode.removeChild(_bubble);
    _bubble = null;
  }
  function _showBubble() {
    if (!_bar) return;
    _hideBubble();
    var txt = _dayReportText();
    var b = document.createElement('span');
    b.className = 'tofu-pet-bubble';
    b.setAttribute('aria-hidden', 'true');
    b.setAttribute('data-report', txt);        // observable content anchor
    var bg = document.createElement('span');
    bg.className = 'tofu-pet-bubble-shape';
    bg.innerHTML = _BUBBLE_SVG;
    var tx = document.createElement('span');
    tx.className = 'tofu-pet-bubble-text';
    tx.textContent = txt;
    b.appendChild(bg);
    b.appendChild(tx);
    _bar.appendChild(b);
    _bubble = b;
    _positionBubble();
    _bubbleTimer = setTimeout(_hideBubble, 4200);
  }
  // The bubble is ANCHORED to the pet, not to the spot where the tap landed:
  // a tap also STARTLES the pet into a flee dash, so a bubble positioned only
  // once at show time was left hanging over empty floor while the pet scurried
  // away (the "bubble doesn't follow" report). _place() calls this every frame
  // while the bubble is alive, so it rides above the pet wherever it goes.
  // Left is clamped inside the bar so the callout never overflows an edge (it
  // is centred on the pet via translateX(-50%)).
  function _positionBubble() {
    if (!_bubble || !_bar) return;
    var petW = (_el && _el.offsetWidth) || 30;
    var barW = (_bar.getBoundingClientRect && _bar.getBoundingClientRect().width) || 0;
    var left = W.x + petW / 2;
    if (barW > 0) left = Math.max(40, Math.min(left, barW - 40));
    _bubble.style.left = Math.max(2, left).toFixed(1) + 'px';
  }

  // ══════════════════════════════════════════════════════════════════════
  //  WANDER ENGINE — a 1-D finite state machine along the bar baseline.
  //  States: walk · idle · turn · sit · gaze · groom · scratch · sleep ·
  //  chase · react. Walking is a MINORITY outcome (see _pickNext) so the pet
  //  mostly does stationary activities rather than pacing. The pet eases
  //  between them on a rAF loop; position is clamped to a
  //  SAFE TRACK between the left chip and the right controls so it never sits
  //  under a hit target.
  // ══════════════════════════════════════════════════════════════════════
  var W = {
    x: 30,            // px from bar's left content edge
    // HEIGHT above the baseline, in px (positive = lifted off the ground).
    // The pet is normally planted (y === 0); only a DRAG lifts it. This lives
    // on the POSITION layer next to x because .tofu-pet owns exactly ONE
    // transform — see _place(), which writes translateX and translateY in a
    // SINGLE declaration. Writing Y from anywhere else (or as a second
    // transform on the same element) would silently replace the X the wander
    // loop just wrote: `transform` is one property, not two channels.
    y: 0,
    vy: 0,            // px/s vertical velocity, only while state==='fall'
    dir: 1,           // +1 right, -1 left
    state: 'idle',
    until: 0,         // ms timestamp when the current state ends
    min: 8, max: 200, // safe-track bounds (recomputed from layout)
    speed: 41,        // px/s while walking — _gaitMs(41) lands at ~75ms/13.3fps
    chaseSpeed: 82,   // px/s while chasing the critter (a quick dash)
    fleeSpeed: 120,   // px/s while fleeing a poke (a quick startled scramble)
    fleeDir: 1,
    last: 0,
    raf: 0,
    reduced: false,
    paused: false,
    nextChaseOk: 0    // cooldown so the cat doesn't chase non-stop
  };
  function _now() { return Date.now(); }
  function _rand(a, b) { return a + Math.random() * (b - a); }

  // NOTE: the pet's footstep disturbance is drawn ON the scene canvas itself
  // (tofu-scene.js reads TofuPet.getState().x each frame and presses/parts the
  // grass, splashes the pool, stirs the clouds under the foot). An earlier
  // iteration ALSO spawned floating DOM particles here in a separate overlay;
  // that was a SECOND footstep system on a different layer than the thing it
  // disturbed (the "two stacked layers" smell), so it was retired in favour of
  // the single canvas surface.

  // Recompute the safe track: from just past the left chip to just before the
  // right-hand controls, so the roaming pet never overlaps an interactive pill.
  function _measure() {
    if (!_bar || !_el) return;
    var br = _bar.getBoundingClientRect();
    var petW = _el.offsetWidth || 30;
    var padL = 6, padR = 8;
    // Left bound: right edge of the folder chip if present, else a small inset.
    var chip = _bar.querySelector('.project-bar-folders, .project-bar-icon');
    var leftPx = padL;
    if (chip) { var cr = chip.getBoundingClientRect(); leftPx = Math.max(padL, cr.right - br.left + 6); }
    // Right bound: left edge of the earliest right-hand control.
    var ctrl = _bar.querySelector('.project-autoapply-toggle, .project-bar-btn, .project-bar-close');
    var rightPx = br.width - 80;
    if (ctrl) { var rr = ctrl.getBoundingClientRect(); rightPx = rr.left - br.left - padR; }
    W.min = Math.max(0, leftPx);
    W.max = Math.max(W.min + 1, rightPx - petW);
    if (W.x < W.min) W.x = W.min;
    if (W.x > W.max) W.x = W.max;
    // CEILING: how far the pet may be lifted before its top leaves the bar.
    // The sprite already sits `bottom` px off the floor and is petH tall, so the
    // headroom left inside the band is (barHeight - bottom - petH). Derived here
    // rather than hard-coded so re-padding the bar or resizing the sprite cannot
    // silently re-open an overhang. Floored at LIFT_MIN_CEIL_PX: on a bar too
    // short to give any headroom, a lift that rounds to zero would take the
    // owner's reported defect straight back, so a small overhang is the lesser
    // evil there — but on the real bar the derived value is what applies.
    var petH = (_el.offsetHeight || 30);
    var bottomPx = 1;                       // .tofu-pet { bottom:1px }
    LIFT_MAX_PX = Math.max(LIFT_MIN_CEIL_PX,
                           Math.round(br.height - bottomPx - petH));
  }

  // pivot defaults to TRUE: a LOCOMOTION turn (wall bounce / chase / flee) is a
  // whole-body about-face and gets the squash-hop. The stationary gaze
  // head-turn passes pivot=false — you don't hop when you merely glance the
  // other way, so it uses the instant mirror alone (no hop = no in-place jitter).
  function _face(dir, pivot) {
    if (pivot === undefined) pivot = true;
    var changed = (dir !== W.dir);
    W.dir = dir;
    // ── THE MIRROR IS THE WHOLE FRAME, ON THE <img> ELEMENTS ──
    // --pet-face-flip is consumed by the CSS on .tofu-pet-img > img — the
    // CHILDREN of the animated layer, so the mirror never fights the breathe/
    // walk keyframes on the parent. Whole-sprite mirroring is SAFE here
    // because the character is front-facing with soft symmetric shading (there
    // is no baked sun to drag around — see the header note), and it is
    // REQUIRED for the same reason: the body must turn with the stride or the
    // pet moonwalks. Still a bare property write with no transition: tweening
    // the flip would pass the face through scaleX(0) and smear it, the
    // paper-flip the old wrapper was careful to avoid.
    if (_el) _el.style.setProperty('--pet-face-flip', String(dir));
    // A real turn is a PIVOT, not a page-flip: overlay a brief squash-hop on the
    // sprite (CSS keyframe gated by data-turning) so the cat reads as planting
    // its feet and spinning to face the other way. Skipped under reduced-motion
    // and for non-locomotion (gaze) flips.
    if (changed && pivot && _el && !W.reduced) {
      _el.setAttribute('data-turning', '1');
      if (_turnTimer) clearTimeout(_turnTimer);
      _turnTimer = setTimeout(function () {
        _turnTimer = null;
        if (_el) _el.removeAttribute('data-turning');
      }, 260);
    }
  }
  function _place() {
    // ONE transform declaration carrying BOTH axes. translateY is negative
    // because W.y is "height above the ground" and screen Y grows downward.
    if (_el) {
      _el.style.transform = 'translateX(' + W.x.toFixed(1) + 'px) translateY(' +
        (-W.y).toFixed(1) + 'px)';
      // The CAST SHADOW is what sells the lift: a raised object's shadow
      // shrinks, softens and separates. Driven by HEIGHT rather than by a
      // static [data-state="drag"] rule so it tracks continuously while the
      // pointer moves AND during the release fall (which is NOT the drag
      // state) — the old two-state version popped back to full size mid-air.
      var lift = Math.max(0, Math.min(1, W.y / LIFT_MAX_PX));
      _el.style.setProperty('--pet-lift', lift.toFixed(3));
    }
    _positionBubble();   // the bubble rides above the pet, wherever it moved to
    // Subtle ground parallax: the scene drifts a fraction of the pet's travel
    // (opposite direction) for depth. The ground ::after reads --bar-scene-x.
    // Static under reduced-motion (the pet doesn't move → this never updates).
    //
    // ONLY written while the SVG ground is the thing on screen. When
    // tofu-scene.js has a live canvas it stamps data-scene-canvas="on" and the
    // CSS sets that ::after to display:none — so this write was invalidating the
    // whole bar's custom-property inheritance every single frame to move the
    // background-position of a box that is not rendered. The canvas paints its
    // own parallax; the property is still maintained for the no-JS/no-canvas
    // fallback, which is the only reader that exists.
    if (_bar && _bar.getAttribute('data-scene-canvas') !== 'on') {
      _bar.style.setProperty('--bar-scene-x', (-(W.x) * 0.12).toFixed(1) + 'px');
    }
  }

  // ── LIGHTING: read the scene's live sun (TofuScene.lightInfo) and point
  // the cast shadow + the filter-based form light with the SAME source, so
  // the pet is lit by the one sun that drifts over the diorama instead of
  // reading as pasted on. Both channels are art-independent (the ::after
  // shadow ellipse + a CSS drop-shadow/brightness filter on the frame layer),
  // so they work on the raster frames exactly as they did on the SVG — the
  // per-face recolouring the SVG gradients allowed is gone with them, which
  // is precisely why the front-facing art keeps its own soft symmetric
  // shading. Fully guarded: no scene / no light → neutral defaults. Cheap: a
  // few CSS var writes per frame, no layout.
  var _lastLightK = '';
  function _applyLight() {
    if (!_el) return;
    var info = null;
    try {
      if (runtimeScope.TofuScene && typeof runtimeScope.TofuScene.lightInfo === 'function') {
        info = runtimeScope.TofuScene.lightInfo();
      }
    } catch (e) { /* scene absent — flat shading, harmless */ }
    var dx = 0, scale = 1, shade = 0, warm = 0;
    if (info && typeof info.nx === 'number') {
      // Pet foot-centre as a 0..1 position along the bar, so "which side is the
      // sun on" is measured relative to the CAT, not the bar centre.
      var span = Math.max(1, (W.max - W.min));
      var petN = Math.max(0, Math.min(1, (W.x - W.min) / span));
      // signed sun offset: >0 sun to the pet's RIGHT, <0 to its LEFT.
      var rel = Math.max(-1, Math.min(1, info.nx - petN));
      // cast shadow points AWAY from the sun (opposite sign), grows as the sun
      // moves to a side (a low/side sun casts a longer shadow).
      dx = -rel * 7;                                  // px, capped by rel range
      scale = 1 + Math.abs(rel) * 0.5;                // up to 1.5x length
      // form shade sits on the anti-sun side of the body (same sign as shadow).
      shade = -rel * 0.7;                             // px drop-shadow x-offset
      warm = Math.max(0, Math.min(1, info.warm || 0)) * (1 - Math.abs(rel) * 0.4);
    }
    // only touch the DOM when a rounded value actually changed (avoid per-frame
    // style churn when the cat + sun are momentarily static).
    var k = dx.toFixed(1) + '|' + scale.toFixed(2) + '|' + shade.toFixed(2) + '|' +
      warm.toFixed(2);
    if (k === _lastLightK) return;
    _lastLightK = k;
    var st = _el.style;
    st.setProperty('--pet-shadow-dx', dx.toFixed(1) + 'px');
    st.setProperty('--pet-shadow-scale', scale.toFixed(2));
    st.setProperty('--pet-shade-dx', shade.toFixed(2) + 'px');
    st.setProperty('--pet-light-warm', warm.toFixed(2));
  }

  // Is there a scene critter close enough to chase? Reads TofuScene.critterX()
  // (CSS px along the bar), guarded so the pet works with no scene present.
  function _critterX() {
    try {
      if (runtimeScope.TofuScene && typeof runtimeScope.TofuScene.critterX === 'function') {
        return runtimeScope.TofuScene.critterX();
      }
    } catch (e) { /* scene absent — no chase, harmless */ }
    return null;
  }

  // Pick the next state with research-grounded weights; sitting/idling is
  // weighted heavily so the pet isn't hyperactive. A nearby critter (off the
  // cooldown, healthy energy) can trigger a chase instead.
  function _pickNext() {
    // Deep-night → strongly prefer sleeping wherever it stands. (Daytime sleep
    // is now RARE — see below — because the owner found it naps far too often.)
    var tb = _timeBucket(new Date().getHours());
    if (tb.expr === 'sleeping' || S.energy < 8) { _enter('sleep'); return; }
    // Chase a critter that has drifted into the safe track (with a cooldown).
    if (_now() >= W.nextChaseOk && S.energy > 30) {
      var cx = _critterX();
      if (cx != null && cx > W.min - 20 && cx < W.max + 20 && Math.abs(cx - W.x) > 6) {
        _enter('chase'); return;
      }
    }
    var r = Math.random();
    // A just-fled cat keeps its blood up: resume walking/idling, never nap.
    if (W.state === 'flee') { _enter(r < 0.6 ? 'idle' : 'walk'); return; }
    // ── VARIETY REBALANCE (owner: "too monotonous, don't just keep running").
    // Walking is now a MINORITY outcome from every state (~15–25%); the pet
    // mostly does STATIONARY activities — sit, gaze (look around), groom,
    // scratch — so it reads as living in the bar, not pacing it. The only
    // nap path is the energy<40 guard in the sit/groom branch (deep-night +
    // energy<8 are handled above), which keeps the "healthy daytime cat never
    // sleeps" invariant (test_daytime_sleep_is_rare_not_frequent). ──
    if (W.state === 'walk' || W.state === 'chase') {
      // just travelled → strongly prefer to stop and do something; walk 15%.
      if (r < 0.25) _enter('idle'); else if (r < 0.47) _enter('sit'); else if (r < 0.67) _enter('gaze'); else if (r < 0.85) _enter('groom'); else _enter('walk');
    } else if (W.state === 'idle') {
      // no daytime sleep from idle; walk is a minority (~22%).
      if (r < 0.24) _enter('sit'); else if (r < 0.46) _enter('gaze'); else if (r < 0.64) _enter('groom'); else if (r < 0.86) _enter('walk'); else _enter('scratch');
    } else if (W.state === 'sit' || W.state === 'groom') {
      // 8% sleep, and only when energy is genuinely low; walk ~20%.
      if (r < 0.22) _enter('idle'); else if (r < 0.44) _enter('gaze'); else if (r < 0.62) _enter('scratch'); else if (r < 0.82) _enter('walk');
      else if (r < 0.92 && S.energy < 40) _enter('sleep'); else _enter('idle');
    } else if (W.state === 'gaze') {
      // after looking around → sit/groom/idle, occasionally wander off (~22%).
      if (r < 0.30) _enter('sit'); else if (r < 0.55) _enter('groom'); else if (r < 0.78) _enter('idle'); else _enter('walk');
    } else if (W.state === 'scratch') {
      if (r < 0.4) _enter('idle'); else if (r < 0.7) _enter('gaze'); else _enter('walk');
    } else { // sleep / react / turn / flee-fallthrough
      if (r < 0.45) _enter('idle'); else if (r < 0.75) _enter('gaze'); else _enter('walk');
    }
  }

  function _enter(state) {
    W.state = state;
    if (_el) _el.setAttribute('data-state', state);
    if (state === 'walk') {
      W.until = _now() + _rand(1400, 3000);   // shorter trips — walking is one behavior among many, not the default
      S.expr = 'walk';
      _walkAccum = 0;
      // Face the way we are ABOUT to travel. Without this the pet inherits
      // whatever direction the previous state left behind — and 'gaze' flips
      // facing every 1.3s on purpose — so entering a walk straight out of a
      // glance could set off with the body facing backwards and the stride
      // pushing the wrong way. Cheap, and it closes the last path by which the
      // pet could appear to walk backwards.
      _face(W.dir, false);
      if (_el) { _el.setAttribute('data-expr', 'walk'); _el.setAttribute('title', _title()); }
      _setWalkFrame(0);
    } else if (state === 'chase') {
      // A dash after the scene critter: perk first (alert), then pursue. Ends
      // on a timer or on catch (handled in _step).
      W.until = _now() + _rand(1600, 3200);
      _walkAccum = 0;
      S.expr = 'chase';
      if (_el) { _el.setAttribute('data-expr', 'walk'); _el.setAttribute('title', _title()); }
      _setWalkFrame(0);
    } else if (state === 'idle') {
      W.until = _now() + _rand(1400, 3200); _applyResolvedForced();
    } else if (state === 'sit') {
      W.until = _now() + _rand(2600, 5200); _applyResolvedForced();
    } else if (state === 'groom') {
      W.until = _now() + _rand(2400, 4600);
      _poseFrames = GROOM_FRAMES; _poseAccum = 0;
      if (_el) _el.setAttribute('data-expr', 'groom');
      _setPoseFrame(0);
    } else if (state === 'scratch') {
      W.until = _now() + _rand(1800, 3200);
      _poseFrames = SCRATCH_FRAMES; _poseAccum = 0;
      if (_el) _el.setAttribute('data-expr', 'scratch');
      _setPoseFrame(0);
    } else if (state === 'gaze') {
      // sit still and watch the scene, turning the head every GAZE_TURN_MS
      W.until = _now() + _rand(2600, 5200);
      _gazeAccum = 0;
      if (_el) _el.setAttribute('data-expr', 'alert');
      _setExpr('alert');
    } else if (state === 'sleep') {
      W.until = _now() + _rand(6000, 14000); _setExpr('sleeping');
    } else if (state === 'flee') {
      // startled scramble: a short, fast, walk-animated dash away from the poke
      W.until = _now() + _rand(650, 1100);
      _walkAccum = 0;
      if (_el) { _el.setAttribute('data-expr', 'surprised'); _el.setAttribute('title', _title()); }
      _setWalkFrame(0);
    } else if (state === 'turn') {
      W.until = _now() + 260;
    }
  }
  // Force a resting expression even though _applyResolved guards on walk state.
  function _applyResolvedForced() {
    if (TRANSIENT[S.expr] && W.state === 'react') return;
    _setExpr(_resolve());
  }

  // ── DROP: the third beat of "lift → carry → land" ──────────────────────
  // Releasing a held pet must not teleport it to the floor: an object that
  // vanishes from height to ground never reads as having been HELD — the same
  // single-frame-jump problem as picking it up with no rise. So the drop is
  // integrated under gravity in _step and ends with a landing squash, which is
  // what makes the weight legible.
  var FALL_GRAVITY = 520;   // px/s² — a short, snappy drop at this scale
  function _enterFall() {
    if (W.y <= 0.5) { W.y = 0; W.last = 0; _place(); _enter('idle'); return; }
    W.vy = 0;
    W.last = 0;              // don't integrate the gap since the last rAF tick
    _enter('fall');
  }
  function _landed() {
    W.y = 0; W.vy = 0;
    _place();
    // The squash rides the FRAME layer (data-landing) so it composites with the
    // position layer's transform instead of overwriting it.
    if (_el && !W.reduced) {
      _el.setAttribute('data-landing', '1');
      if (_landTimer) clearTimeout(_landTimer);
      _landTimer = setTimeout(function () {
        _landTimer = null;
        if (_el) _el.removeAttribute('data-landing');
      }, 240);
    }
    W.last = 0;
    _enter('idle');
  }

  // Chase step: dash toward the critter's live x; on catch, pounce + spook it.
  function _chaseStep(dt) {
    var cx = _critterX();
    if (cx == null) {                 // critter gone (off/reduced) → give up
      W.nextChaseOk = _now() + 4000;
      _enter('idle'); return;
    }
    var target = Math.max(W.min, Math.min(W.max, cx));
    var d = target - W.x;
    _face(d >= 0 ? 1 : -1);
    W.x += (d >= 0 ? 1 : -1) * Math.min(Math.abs(d), W.chaseSpeed * dt);
    _advanceGait(dt, W.chaseSpeed);
    _place();
    if (Math.abs(target - W.x) <= 5) {   // caught it → pounce + spook
      try { if (runtimeScope.TofuScene && runtimeScope.TofuScene.spook) runtimeScope.TofuScene.spook(); } catch (e) { /* harmless */ }
      W.nextChaseOk = _now() + _rand(6000, 12000);
      S.mood = Math.min(100, S.mood + 2);
      react('surprised', 900);
    }
  }

  function _step(ts) {
    W.raf = requestAnimationFrame(_step);
    _applyLight();   // shade the sprite + point its shadow from the live scene sun (also while dragged)
    if (W.paused || W.state === 'drag') { W.last = ts; return; }   // finger owns position while dragging
    var dt = W.last ? (ts - W.last) / 1000 : 0;
    W.last = ts;
    if (dt > 0.1) dt = 0.1;   // clamp after a background stall

    if (W.state === 'walk') {
      W.x += W.dir * W.speed * dt;
      _advanceGait(dt, W.speed);
      if (W.x <= W.min) { W.x = W.min; _enter('turn'); _face(1); }
      else if (W.x >= W.max) { W.x = W.max; _enter('turn'); _face(-1); }
      _place();
    } else if (W.state === 'chase') {
      _chaseStep(dt);
    } else if (W.state === 'fall') {
      // integrate the drop; land exactly on the ground plane
      W.vy += FALL_GRAVITY * dt;
      W.y -= W.vy * dt;
      if (W.y <= 0) { _landed(); }
      else { _place(); }
    } else if (W.state === 'flee') {
      W.x += W.fleeDir * W.fleeSpeed * dt;
      _advanceGait(dt, W.fleeSpeed);
      if (W.x <= W.min) { W.x = W.min; W.fleeDir = 1; _face(1); }
      else if (W.x >= W.max) { W.x = W.max; W.fleeDir = -1; _face(-1); }
      _place();
    } else if (W.state === 'turn') {
      if (_now() >= W.until) { _enter('walk'); }
    } else if ((W.state === 'groom' || W.state === 'scratch') && _poseFrames) {
      // advance the pose cycle frames while sitting/scratching
      _poseAccum += dt * 1000;
      if (_poseAccum >= POSE_FRAME_MS) {
        var padv = Math.floor(_poseAccum / POSE_FRAME_MS);
        _poseAccum -= padv * POSE_FRAME_MS;
        _setPoseFrame(_poseIdx + padv);
      }
    } else if (W.state === 'gaze') {
      // sit and look around: flip facing every GAZE_TURN_MS (a stationary
      // "watching the scene" beat, no travel).
      _gazeAccum += dt * 1000;
      if (_gazeAccum >= GAZE_TURN_MS) {
        _gazeAccum -= GAZE_TURN_MS;
        _face(-W.dir, false);   // glance only — instant mirror, no plant-and-hop
      }
    }
    // 'fall' is excluded like 'turn'/'chase': it ends on a GEOMETRIC condition
    // (touching the ground), not on a timer. _enter('fall') sets no `until`, so
    // without this exclusion the stale `until` left by the state before the drag
    // is already in the past and _pickNext() fires on the very first airborne
    // frame — stranding the pet mid-air with y > 0 and no leg to bring it down.
    if (W.state !== 'turn' && W.state !== 'chase' && W.state !== 'fall' &&
        _now() >= W.until && W.state !== 'react') {
      _pickNext();
    } else if (W.state === 'react' && _now() >= W.until) {
      _pickNext();
    } else if (W.state === 'chase' && _now() >= W.until) {
      W.nextChaseOk = _now() + _rand(5000, 9000);   // chase timed out
      _pickNext();
    }
  }

  function _startWander() {
    if (W.reduced) { _enter('idle'); _setExpr(_resolve()); _place(); return; }
    _measure();
    W.x = _rand(W.min, W.max); _place(); _face(Math.random() < 0.5 ? 1 : -1);
    _enter('walk');
    if (!W.raf) W.raf = requestAnimationFrame(_step);
  }

  // Slow ambient drift (mood/energy) — same cadence as before.
  function _tick() {
    var h = new Date().getHours();
    if (h >= 0 && h < 6) S.energy = Math.min(100, S.energy + 3);
    else S.energy = Math.max(0, S.energy - 1);
    if (S.mood > 50) S.mood -= 0.5; else if (S.mood < 50) S.mood += 0.5;
    _save();
  }

  var _hoverThrottle = 0;
  function _onHover() {
    var now = Date.now();
    if (now - _hoverThrottle < 4000) return;
    _hoverThrottle = now;
    if (!TRANSIENT[S.expr] && S.expr !== 'sleeping' && S.expr !== 'sleepy') react('curious', 1100);
  }

  // ══════════════════════════════════════════════════════════════════════
  //  DRAG — pick the tofu up and move it along the bar. A press starts a
  //  candidate drag; once the pointer moves past DRAG_SLOP the pet is "held"
  //  (the wander FSM yields: _step early-returns while state==='drag', so the
  //  autonomous walk/chase never fights the finger). On release: a real drag
  //  drops it where you let go (then it resumes wandering from there); a
  //  press that never crossed the slop is treated as a plain CLICK → interact()
  //  (preserves the day-report bubble). Uses Pointer Events (mouse + touch) with
  //  capture so a fast drag that outruns the sprite still tracks. Guarded so a
  //  browser without PointerEvent falls back to the plain click handler. ──
  var D = { active: false, moved: false, id: null, startX: 0, offX: 0,
            startY: 0, baseY: 0, prevState: 'idle' };
  var DRAG_SLOP = 4;   // px of pointer travel before a press becomes a drag
  // ── HOW HIGH THE PET CAN BE HELD ────────────────────────────────
  // DERIVED from the bar's measured height, never a hand-written constant.
  //
  // The pet must stay inside the bar's visual band while held. It is a
  // GRABBABLE element lifted to z-index 5, so an overhang paints over whatever
  // sits above the bar and can cover a hit target — unlike the speech bubble,
  // which also pokes above the rim but is `pointer-events:none` and therefore
  // cannot steal a click. `overflow:visible` on the bar means nothing clips it,
  // so the clamp is the ONLY thing keeping the pet in its own furniture.
  //
  // A literal ceiling was measured wrong exactly once: 34px against a ~46px bar
  // with the sprite already 31px off the floor put its top ~19px ABOVE the rim.
  // Deriving it from the live box means re-padding the bar, or resizing the
  // sprite, cannot silently re-open that overhang.
  var LIFT_MAX_PX = 15;      // recomputed in _measure(); this is only a floor-safe default
  var LIFT_MIN_CEIL_PX = 10; // never clamp so hard that the lift stops reading as a lift
  // The "snatched up" impulse applied the moment a press becomes a drag.
  // WITHOUT it, height could only come from the pointer's own rise — so a
  // purely SIDEWAYS drag would still skate the pet along the floor, which is
  // exactly the reported defect. A grab is a lift even when the hand moves
  // horizontally, so this is asserted on state entry, not derived from motion.
  // Kept BELOW the derived ceiling so a grab alone can never clip.
  var LIFT_GRAB_PX = 9;

  function _barLeft() {
    return (_bar && _bar.getBoundingClientRect) ? _bar.getBoundingClientRect().left : 0;
  }
  function _onPointerDown(e) {
    if (e.button != null && e.button !== 0) return;   // left/primary only
    D.active = true; D.moved = false; D.id = e.pointerId;
    D.startX = e.clientX;
    D.startY = e.clientY;
    // grab offset so the cat doesn't jump its left edge to the cursor
    D.offX = e.clientX - (_barLeft() + W.x);
    D.prevState = W.state;
    try { if (_el.setPointerCapture && e.pointerId != null) _el.setPointerCapture(e.pointerId); } catch (_e) { /* harmless */ }
  }
  function _onPointerMove(e) {
    if (!D.active) return;
    if (!D.moved && Math.abs(e.clientX - D.startX) < DRAG_SLOP) return;   // still a click candidate
    if (!D.moved) {
      // cross the slop → become a real drag: lift the cat, freeze wandering
      D.moved = true;
      _hideBubble();
      W.state = 'drag';
      if (_el) _el.setAttribute('data-state', 'drag');
      _setExpr('surprised');   // startled at being picked up
      // SNATCH: assert an immediate lift so a purely horizontal drag still
      // reads as carrying rather than sliding. Reduced-motion keeps it planted.
      D.baseY = W.reduced ? 0 : LIFT_GRAB_PX;
    }
    _measure();
    var x = (e.clientX - _barLeft()) - D.offX;
    W.x = Math.max(W.min, Math.min(W.max, x));
    // VERTICAL: follow the pointer's own rise/fall on top of the grab lift.
    // Measured from where the press STARTED (not from the sprite's box), so the
    // pet stays under the finger instead of jumping to it.
    if (!W.reduced) {
      var rise = D.startY - e.clientY;          // up is positive
      W.y = Math.max(0, Math.min(LIFT_MAX_PX, D.baseY + rise));
    }
    // face the direction of motion for a bit of life
    if (e.movementX) _face(e.movementX >= 0 ? 1 : -1);
    _place();
    if (e.cancelable) e.preventDefault();
  }
  // ── THE ONE TERMINATION PATH ──────────────────────────────────
  // Every way a drag can END routes through here. There are THREE of them and
  // only one used to do the right thing:
  //   · pointerup            — the normal release
  //   · pointercancel        — NOT an error path: the browser fires it on
  //     touch-scroll, on window blur / alt-tab mid-drag, and whenever the OS
  //     takes over the gesture. On a touch device it is a ROUTINE way for a
  //     drag to end. It used to reset only D.active/D.moved, leaving the pet at
  //     its held height in state 'drag' — and because _step early-returns on
  //     'drag', nothing could ever bring it down again: frozen in mid-air, with
  //     the whole wander loop dead, until a page reload.
  //   · lostpointercapture   — was not wired at all. setPointerCapture is inside
  //     a try/catch, so when capture is refused or lost, a pointerup landing
  //     outside the element never reaches us and the drag hangs the same way.
  //
  // Measured before this fix — settled state after each terminator:
  //   pointerup          ty=0    lift=0      idle    OK
  //   pointercancel      ty=-12  lift=0.353  drag    STUCK
  //   lostpointercapture ty=-12  lift=0.353  drag    STUCK (no handler)
  //
  // Hence ONE function rather than a `W.y = 0` bolted onto the cancel handler:
  // with three entry points and one implementation, a fourth cannot drift.
  // `settled` distinguishes a deliberate release (mood credit, day interaction)
  // from an interruption — the pet still has to LAND either way, because
  // landing is about not stranding it, not about rewarding the user.
  function _endDrag(settled) {
    // NOT load-bearing, and measured as such: with this line removed, both
    // "pointerup then pointercancel" and "press then cancel twice" behave
    // identically (mood unchanged, pet lands, state idle), because settled=false
    // already withholds the tap credit and _enterFall() is a no-op once y is 0.
    // Kept as a cheap explicit statement that re-entry is expected — browsers do
    // emit both terminators in some gesture hand-offs — rather than leaving the
    // next reader to re-derive that it happens to be harmless.
    if (!D.active && !D.moved) return;
    var wasDrag = D.moved;
    D.active = false;
    D.moved = false;
    try {
      if (_el && _el.releasePointerCapture && D.id != null) _el.releasePointerCapture(D.id);
    } catch (_e) { /* capture already gone — harmless */ }
    D.id = null;
    if (!wasDrag) {
      // never crossed the slop → a plain click, but ONLY when the user actually
      // let go. An interrupted press is not a tap and must not pop the bubble.
      if (settled) interact();
      return;
    }
    if (settled) {
      S.lastInteraction = Date.now();
      S.mood = Math.min(100, S.mood + 3); _save();
    }
    if (W.reduced) { W.y = 0; _place(); _enter('idle'); _setExpr(_resolve()); }
    else { _enterFall(); }   // fall back to the ground, land, THEN resume
  }

  function _onPointerUp(e) {
    _endDrag(true);
  }
  function _wireDrag() {
    if (!_el) return;
    if (typeof window.PointerEvent !== 'undefined') {
      _el.addEventListener('pointerdown', _onPointerDown);
      _el.addEventListener('pointermove', _onPointerMove);
      _el.addEventListener('pointerup', _onPointerUp);
      // Both interruption paths land the pet too — see _endDrag. Passing
      // settled=false marks them as "the gesture was taken away" rather than
      // "the user let go", so they land without crediting a tap.
      _el.addEventListener('pointercancel', function () { _endDrag(false); });
      _el.addEventListener('lostpointercapture', function () { _endDrag(false); });
    } else {
      // no Pointer Events (very old browser) → plain click keeps the bubble.
      _el.addEventListener('click', function (e) { e.stopPropagation(); interact(); });
    }
  }

  function mount() {
    if (document.getElementById(MOUNT_ID)) return true;
    var bar = document.getElementById('projectBar');
    if (!bar) return false;
    _bar = bar;
    _el = document.createElement('span');
    _el.className = 'tofu-pet';
    _el.id = MOUNT_ID;
    _el.setAttribute('role', 'img');
    _el.setAttribute('aria-hidden', 'true');
    _el.setAttribute('data-pet', 'tofu');
    // The frame layer is a plain box that HOLDS the live <svg> (see the frame-art
    // note above). There is no separate direction layer any more: mirroring the
    // whole sprite is exactly the bug that dragged the block's lighting around
    // with it, so the mirror now lives inside the sprite on the character-space
    // group and this element owns only the per-frame life animation.
    _img = document.createElement('span');
    _img.className = 'tofu-pet-img';
    _img.setAttribute('aria-hidden', 'true');
    _el.appendChild(_img);
    _wireDrag();   // press = click (interact); press+move = drag the pet
    _el.addEventListener('contextmenu', function (e) { e.preventDefault(); e.stopPropagation(); cycleDecor(); react('happy', 1000); });
    _el.addEventListener('mouseenter', _onHover);
    bar.insertBefore(_el, bar.firstChild);
    _applyDecor();
    _startWander();
    return true;
  }

  function _watchReducedMotion() {
    if (!window.matchMedia) return;
    var mq = window.matchMedia('(prefers-reduced-motion: reduce)');
    W.reduced = mq.matches;
    var onChange = function () {
      W.reduced = mq.matches;
      if (W.reduced) { _enter('idle'); _setExpr(_resolve()); }
      else if (W.state === 'idle') { _enter('walk'); }
    };
    if (mq.addEventListener) mq.addEventListener('change', onChange);
    else if (mq.addListener) mq.addListener(onChange);   // Safari <14
  }

  function _boot() {
    _load();
    _loadDecor();
    _preload();
    _watchReducedMotion();
    if (!mount()) {
      var tries = 0;
      var iv = setInterval(function () { if (mount() || ++tries > 20) clearInterval(iv); }, 250);
    }
    setInterval(_tick, 60000);

    // Pause the loop when the tab is hidden (CPU + attention). oneko pattern.
    document.addEventListener('visibilitychange', function () { W.paused = document.hidden; });
    // Keep the safe track correct as the bar/controls resize.
    if (window.ResizeObserver && _bar) {
      try { new ResizeObserver(function () { _measure(); }).observe(_bar); } catch (e) { /* harmless */ }
    }
    window.addEventListener('resize', _measure);

    document.addEventListener('tofu:activity', function (e) { setActivity(e && e.detail); });
    document.addEventListener('tofu:react', function (e) {
      var d = (e && e.detail) || {};
      if (d.expr) react(d.expr, d.ms);
    });
    document.addEventListener('tofu:decor', function (e) {
      if (e && typeof e.detail === 'string') setDecor(e.detail);
    });
    document.addEventListener('tofu:day', function (e) { setDay(e && e.detail); });
    // The locale chunk is a dynamic import that lands AFTER DOMContentLoaded,
    // so every t() read at mount can only render raw keys; i18n ready() and
    // every later language switch both dispatch this (on window) — re-sync ALL
    // mount-time t() surfaces: the scene-switch button AND the pet's own hover
    // tooltip (stamped at mount, otherwise raw pet.title until some later state
    // change happens to re-stamp it).
    window.addEventListener('tofu:language-change', function () {
      _syncSceneButton();
      if (_el) _el.setAttribute('title', _title());
    });
  }

  runtimeScope.TofuPet = {
    react: react,
    setActivity: setActivity,
    interact: interact,
    setDay: setDay,
    getDay: function () { return _day; },
    dayReport: _dayReportText,
    refresh: _applyResolved,
    getState: function () { return { mood: S.mood, energy: S.energy, activity: S.activity, expr: S.expr, state: W.state, x: W.x, decor: _decor }; },
    setState: function (patch) {
      if (!patch) return;
      if (typeof patch.mood === 'number') S.mood = Math.max(0, Math.min(100, patch.mood));
      if (typeof patch.energy === 'number') S.energy = Math.max(0, Math.min(100, patch.energy));
      if (typeof patch.activity === 'string') S.activity = patch.activity;
      _save(); _applyResolved();
    },
    mount: mount,
    frameUrl: _frameUrl,
    getFrame: function () { return _curFrame ? _frameUrl(_curFrame) : ''; },
    WALK_FRAMES: WALK_FRAMES,
    GROOM_FRAMES: GROOM_FRAMES,
    SCRATCH_FRAMES: SCRATCH_FRAMES,
    setDecor: setDecor,
    cycleDecor: cycleDecor,
    getDecor: function () { return _decor; },
    EXPRESSIONS: EXPRESSIONS,
    FRAME_ALIAS: FRAME_ALIAS,
    DECOR_STYLES: DECOR_STYLES,
    sceneLabel: _sceneLabel
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', _boot);
  } else {
    _boot();
  }
})();
