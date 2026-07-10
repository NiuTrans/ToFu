/*
 * tofu-pet.js — the little KITTEN that LIVES in the project bar.
 *
 * The project bar (tofu theme) is a tiny living DIORAMA: a procedural
 * Impressionist scene fills the bar (tofu-scene.js) and a pixel-art CAT roams
 * left-right along its baseline — walking, pausing, sitting, grooming,
 * scratching, napping, and CHASING a little scene critter (butterfly / fish /
 * bird) that drifts through the background. The classic desktop-pet wander
 * model (oneko / Shimeji), but coupled to the scene so the two interact. The
 * cat passes BEHIND the opaque control pills via z-index, so it can NEVER
 * block a hit target. Each pose is a public-domain 32px sprite frame in
 * static/icons/pet/oneko/oneko-<x>.png.
 *
 * CHARACTER: one cat, one look. (Earlier iterations had a swappable "pack"
 * registry with a tofu-block alternate + a switch button; per the owner we
 * keep ONLY the kitten and dropped the switcher — the wander/pose engine is
 * unchanged, it just always resolves oneko frames.)
 *
 * MOTION is a real frame-by-frame WALK CYCLE: keyposes advanced by a frame
 * ticker on the rAF loop while walking (NOT one PNG sliding). Layers (so
 * transforms never fight): .tofu-pet owns POSITION (translateX, set by the
 * wander loop) · .tofu-pet-facing owns DIRECTION (scaleX ±1) · .tofu-pet-img
 * owns the swapped frame + idle breathe.
 *
 * PET ⋈ SCENE INTERACTION (owner ask — "interaction between the pet and the
 * background"): tofu-scene.js exposes the drifting critter's position
 * (TofuScene.critterX) and a spook() hook. The wander FSM has a 'chase' state:
 * when the critter drifts near, the cat perks (alert) then pursues it; on
 * catch it pounces (surprised) and the critter darts away (scene.spook()). The
 * scene in turn glows softly under the cat (it reads TofuPet.getState().x). Both
 * couplings are OPTIONAL + guarded, so each module still works entirely alone.
 *
 * Public surface `window.TofuPet`:
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
 * the cat does NOT roam or chase (stands still, single frame); it pauses when
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
  var SCENE_LABELS = { meadow: 'Meadow', pool: 'Pool', sky: 'Sky', off: 'Off' };
  var DEFAULT_DECOR = 'meadow';
  var _decor = DEFAULT_DECOR;

  // ── The kitten sprite frames live under static/icons/pet/oneko/ as
  // oneko-<frame>.png (32px public-domain Neko art; see the LICENSE there).
  // URL built with BASE_PATH (from core.js) so it stays correct behind a
  // base-path / VS Code proxy. ──
  var PET_DIR = '/static/icons/pet/oneko';
  var PET_PREFIX = 'oneko-';
  var _base = (typeof BASE_PATH === 'string') ? BASE_PATH : '';
  function _frameUrl(frame) {
    return _base + PET_DIR + '/' + PET_PREFIX + frame + '.png';
  }

  // Resting expressions (the mood/time FSM resolves to one of these). Every
  // name here must have an oneko-<name>.png on disk.
  var EXPRESSIONS = ['idle', 'happy', 'sleepy', 'sleeping', 'thinking',
    'surprised', 'sad', 'celebrating', 'alert'];
  // Poses that reuse another frame's art (a cat has poses, not emotions).
  // 'curious' → the alert perk; nothing else needs remapping now.
  var FRAME_ALIAS = { curious: 'alert' };

  // The WALK CYCLE: four keyposes played in order while state==='walk'.
  var WALK_FRAMES = ['walk1', 'walk2', 'walk3', 'walk4'];
  var WALK_FRAME_MS = 150;   // per keypose → full ~600ms stride cycle
  var _walkIdx = 0, _walkAccum = 0;

  // GROOM cycle (sit + lick paw) and WALL-SCRATCH cycle (stretch up) — the new
  // "forms". Played frame-by-frame in their own states, same ticker as walk.
  var GROOM_FRAMES = ['groom1', 'groom2', 'groom3'];
  var SCRATCH_FRAMES = ['scratch1', 'scratch2'];
  var POSE_FRAME_MS = 220;
  var _poseIdx = 0, _poseAccum = 0, _poseFrames = null;

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
      var name = SCENE_LABELS[_decor] || _decor;
      var lbl = b.querySelector('.scene-switch-label');
      if (lbl) lbl.textContent = name;
      b.setAttribute('data-scene', _decor);
      b.setAttribute('title', 'Scene: ' + name + ' \u00b7 click to change');
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

  var _greetWord = {
    deepNight: 'fast asleep', earlyMorning: 'waking up', morning: 'bright and early',
    afternoon: 'hanging out', evening: 'winding down', night: 'getting sleepy'
  };
  function _title() {
    var tb = _timeBucket(new Date().getHours());
    var feel = S.mood >= 75 ? 'feeling great' : S.mood >= 45 ? 'doing fine' : 'a bit blue';
    return 'Kitty — ' + (_greetWord[tb.bucket] || 'here') + ' \u00b7 ' + feel;
  }

  // ── DOM ──
  var _el = null;         // .tofu-pet (position layer)
  var _facing = null;     // .tofu-pet-facing (direction layer)
  var _img = null;        // <img> (frame layer)
  var _bar = null;        // .project-bar
  var _revertTimer = null;

  function _preload() {
    var all = EXPRESSIONS.concat(WALK_FRAMES, GROOM_FRAMES, SCRATCH_FRAMES);
    for (var i = 0; i < all.length; i++) {
      var im = new Image();
      im.src = _frameUrl(all[i]);
    }
  }

  function _setWalkFrame(i) {
    _walkIdx = ((i % WALK_FRAMES.length) + WALK_FRAMES.length) % WALK_FRAMES.length;
    if (_img) _img.src = _frameUrl(WALK_FRAMES[_walkIdx]);
  }
  // Advance a generic pose cycle (groom / scratch), used by the sit/scratch
  // states. Mirrors _setWalkFrame but over the active pose frame list.
  function _setPoseFrame(i) {
    if (!_poseFrames || !_poseFrames.length) return;
    _poseIdx = ((i % _poseFrames.length) + _poseFrames.length) % _poseFrames.length;
    if (_img) _img.src = _frameUrl(_poseFrames[_poseIdx]);
  }

  function _setFrame(expr) {
    var frame = FRAME_ALIAS[expr] || expr;
    if (EXPRESSIONS.indexOf(frame) === -1) frame = 'idle';
    if (_img) _img.src = _frameUrl(frame);
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
    S.energy = Math.min(100, S.energy + 2);
    _save();
    react('happy', 1500);
    _showBubble();   // a tap also reports your day (see _showBubble)
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
    var _tt = (typeof t === 'function') ? t : null;
    var _k = function (key, params, fb) {
      if (_tt) { var s = _tt(key, params); if (s && s !== key) return s; }
      return fb;
    };
    if (!_day) return _k('pet.dayGreeting', null, 'Hi there!');
    var done = (_day.streams.done || 0) + (_day.todos.done || 0);
    var total = (_day.streams.total || 0) + (_day.todos.total || 0);
    var blocked = _day.streams.blocked || 0;
    if (total === 0) return _k('pet.dayIdle', null, 'Nothing logged yet today');
    var base = _k('pet.dayReport', { done: done, total: total }, done + '/' + total + ' done today');
    if (blocked > 0) base += ' ' + _k('pet.dayBlocked', { n: blocked }, '\u00b7 ' + blocked + ' blocked');
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
    // Clamp the bubble's left within the bar so it never overflows the right
    // edge (the callout is centred on the pet via translateX(-50%)).
    var petW = (_el && _el.offsetWidth) || 30;
    var barW = (_bar.getBoundingClientRect && _bar.getBoundingClientRect().width) || 0;
    var left = W.x + petW / 2;
    if (barW > 0) left = Math.max(40, Math.min(left, barW - 40));
    b.style.left = Math.max(2, left).toFixed(1) + 'px';
    _bar.appendChild(b);
    _bubble = b;
    _bubbleTimer = setTimeout(_hideBubble, 4200);
  }

  // ══════════════════════════════════════════════════════════════════════
  //  WANDER ENGINE — a 1-D finite state machine along the bar baseline.
  //  States: walk · idle · turn · sit · groom · scratch · sleep · chase ·
  //  react. The pet eases between them on a rAF loop; position is clamped to a
  //  SAFE TRACK between the left chip and the right controls so it never sits
  //  under a hit target.
  // ══════════════════════════════════════════════════════════════════════
  var W = {
    x: 30,            // px from bar's left content edge
    dir: 1,           // +1 right, -1 left
    state: 'idle',
    until: 0,         // ms timestamp when the current state ends
    min: 8, max: 200, // safe-track bounds (recomputed from layout)
    speed: 34,        // px/s while walking
    chaseSpeed: 82,   // px/s while chasing the critter (a quick dash)
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
  }

  function _face(dir) {
    W.dir = dir;
    if (_facing) _facing.style.transform = 'scaleX(' + dir + ')';
  }
  function _place() {
    if (_el) _el.style.transform = 'translateX(' + W.x.toFixed(1) + 'px)';
    // Subtle ground parallax: the scene drifts a fraction of the pet's travel
    // (opposite direction) for depth. The ground ::after reads --bar-scene-x.
    // Static under reduced-motion (the pet doesn't move → this never updates).
    if (_bar) _bar.style.setProperty('--bar-scene-x', (-(W.x) * 0.12).toFixed(1) + 'px');
  }

  // Is there a scene critter close enough to chase? Reads TofuScene.critterX()
  // (CSS px along the bar), guarded so the pet works with no scene present.
  function _critterX() {
    try {
      if (window.TofuScene && typeof window.TofuScene.critterX === 'function') {
        return window.TofuScene.critterX();
      }
    } catch (e) { /* scene absent — no chase, harmless */ }
    return null;
  }

  // Pick the next state with research-grounded weights; sitting/idling is
  // weighted heavily so the pet isn't hyperactive. A nearby critter (off the
  // cooldown, healthy energy) can trigger a chase instead.
  function _pickNext() {
    // Deep-night → strongly prefer sleeping wherever it stands.
    var tb = _timeBucket(new Date().getHours());
    if (tb.expr === 'sleeping' || S.energy < 12) { _enter('sleep'); return; }
    // Chase a critter that has drifted into the safe track (with a cooldown).
    if (_now() >= W.nextChaseOk && S.energy > 30) {
      var cx = _critterX();
      if (cx != null && cx > W.min - 20 && cx < W.max + 20 && Math.abs(cx - W.x) > 6) {
        _enter('chase'); return;
      }
    }
    var r = Math.random();
    if (W.state === 'walk' || W.state === 'chase') {
      if (r < 0.4) _enter('idle'); else if (r < 0.58) _enter('sit'); else if (r < 0.7) _enter('groom'); else _enter('walk');
    } else if (W.state === 'idle') {
      if (r < 0.5) _enter('walk'); else if (r < 0.68) _enter('sit'); else if (r < 0.8) _enter('groom'); else if (r < 0.9) _enter('idle'); else _enter('sleep');
    } else if (W.state === 'sit' || W.state === 'groom') {
      if (r < 0.4) _enter('idle'); else if (r < 0.62) _enter('walk'); else if (r < 0.75) _enter('scratch'); else _enter('sleep');
    } else if (W.state === 'scratch') {
      _enter(r < 0.6 ? 'idle' : 'walk');
    } else { // sleep / react / turn
      _enter(r < 0.7 ? 'idle' : 'walk');
    }
  }

  function _enter(state) {
    W.state = state;
    if (_el) _el.setAttribute('data-state', state);
    if (state === 'walk') {
      W.until = _now() + _rand(2200, 5000);
      S.expr = 'walk';
      _walkAccum = 0;
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
    } else if (state === 'sleep') {
      W.until = _now() + _rand(6000, 14000); _setExpr('sleeping');
    } else if (state === 'turn') {
      W.until = _now() + 260;
    }
  }
  // Force a resting expression even though _applyResolved guards on walk state.
  function _applyResolvedForced() {
    if (TRANSIENT[S.expr] && W.state === 'react') return;
    _setExpr(_resolve());
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
    _walkAccum += dt * 1000;
    if (_walkAccum >= WALK_FRAME_MS) {
      var adv = Math.floor(_walkAccum / WALK_FRAME_MS);
      _walkAccum -= adv * WALK_FRAME_MS;
      _setWalkFrame(_walkIdx + adv);
    }
    _place();
    if (Math.abs(target - W.x) <= 5) {   // caught it → pounce + spook
      try { if (window.TofuScene && window.TofuScene.spook) window.TofuScene.spook(); } catch (e) { /* harmless */ }
      W.nextChaseOk = _now() + _rand(6000, 12000);
      S.mood = Math.min(100, S.mood + 2);
      react('surprised', 900);
    }
  }

  function _step(ts) {
    W.raf = requestAnimationFrame(_step);
    if (W.paused || W.state === 'drag') { W.last = ts; return; }   // finger owns position while dragging
    var dt = W.last ? (ts - W.last) / 1000 : 0;
    W.last = ts;
    if (dt > 0.1) dt = 0.1;   // clamp after a background stall

    if (W.state === 'walk') {
      W.x += W.dir * W.speed * dt;
      // Advance the walk-cycle frame in step with travel (the actual animation).
      _walkAccum += dt * 1000;
      if (_walkAccum >= WALK_FRAME_MS) {
        var adv = Math.floor(_walkAccum / WALK_FRAME_MS);
        _walkAccum -= adv * WALK_FRAME_MS;
        _setWalkFrame(_walkIdx + adv);
      }
      if (W.x <= W.min) { W.x = W.min; _enter('turn'); _face(1); }
      else if (W.x >= W.max) { W.x = W.max; _enter('turn'); _face(-1); }
      _place();
    } else if (W.state === 'chase') {
      _chaseStep(dt);
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
    }
    if (W.state !== 'turn' && W.state !== 'chase' && _now() >= W.until && W.state !== 'react') {
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
  //  DRAG — pick the kitten up and move it along the bar. A press starts a
  //  candidate drag; once the pointer moves past DRAG_SLOP the cat is "held"
  //  (the wander FSM yields: _step early-returns while state==='drag', so the
  //  autonomous walk/chase never fights the finger). On release: a real drag
  //  drops the cat where you let go (then it resumes wandering from there); a
  //  press that never crossed the slop is treated as a plain CLICK → interact()
  //  (preserves the day-report bubble). Uses Pointer Events (mouse + touch) with
  //  capture so a fast drag that outruns the sprite still tracks. Guarded so a
  //  browser without PointerEvent falls back to the plain click handler. ──
  var D = { active: false, moved: false, id: null, startX: 0, offX: 0, prevState: 'idle' };
  var DRAG_SLOP = 4;   // px of pointer travel before a press becomes a drag

  function _barLeft() {
    return (_bar && _bar.getBoundingClientRect) ? _bar.getBoundingClientRect().left : 0;
  }
  function _onPointerDown(e) {
    if (e.button != null && e.button !== 0) return;   // left/primary only
    D.active = true; D.moved = false; D.id = e.pointerId;
    D.startX = e.clientX;
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
    }
    _measure();
    var x = (e.clientX - _barLeft()) - D.offX;
    W.x = Math.max(W.min, Math.min(W.max, x));
    // face the direction of motion for a bit of life
    if (e.movementX) _face(e.movementX >= 0 ? 1 : -1);
    _place();
    if (e.cancelable) e.preventDefault();
  }
  function _onPointerUp(e) {
    if (!D.active) return;
    D.active = false;
    try { if (_el.releasePointerCapture && D.id != null) _el.releasePointerCapture(D.id); } catch (_e) { /* harmless */ }
    if (!D.moved) { interact(); return; }             // never crossed slop → a click
    // dropped after a real drag → settle here, resume autonomous life
    S.lastInteraction = Date.now();
    S.mood = Math.min(100, S.mood + 3); _save();
    if (W.reduced) { _enter('idle'); _setExpr(_resolve()); _place(); }
    else { W.last = 0; _enter('idle'); }              // idle a beat, then _pickNext resumes
  }
  function _wireDrag() {
    if (!_el) return;
    if (typeof window.PointerEvent !== 'undefined') {
      _el.addEventListener('pointerdown', _onPointerDown);
      _el.addEventListener('pointermove', _onPointerMove);
      _el.addEventListener('pointerup', _onPointerUp);
      _el.addEventListener('pointercancel', function () { D.active = false; D.moved = false; });
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
    _el.setAttribute('data-pet', 'oneko');
    _facing = document.createElement('span');
    _facing.className = 'tofu-pet-facing';
    _img = document.createElement('img');
    _img.className = 'tofu-pet-img';
    _img.alt = '';
    _img.setAttribute('aria-hidden', 'true');
    _img.setAttribute('draggable', 'false');
    _facing.appendChild(_img);
    _el.appendChild(_facing);
    _wireDrag();   // press = click (interact); press+move = drag the kitten
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
  }

  window.TofuPet = {
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
    getFrame: function () { return _img ? _img.src : ''; },
    WALK_FRAMES: WALK_FRAMES,
    GROOM_FRAMES: GROOM_FRAMES,
    SCRATCH_FRAMES: SCRATCH_FRAMES,
    setDecor: setDecor,
    cycleDecor: cycleDecor,
    getDecor: function () { return _decor; },
    EXPRESSIONS: EXPRESSIONS,
    FRAME_ALIAS: FRAME_ALIAS,
    DECOR_STYLES: DECOR_STYLES,
    SCENE_LABELS: SCENE_LABELS
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', _boot);
  } else {
    _boot();
  }
})();
