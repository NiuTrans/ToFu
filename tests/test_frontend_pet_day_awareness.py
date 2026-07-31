"""Pet day-awareness (Tier 1 + Tier 2a) — the project-bar pet is connected to
"your day" (the daily report) and reports it on tap.

The backend daily report is the SINGLE SOURCE OF TRUTH: myday.js derives the
digest {todos:{done,total}, streams:{done,blocked,total}, convCount} STRAIGHT
from the report the backend returns and fires `tofu:day`; the pet
(tofu-pet.js) only MAPS that digest → expression/mood and renders a click
bubble. It computes no day logic of its own.

These tests run the REAL shipped tofu-pet.js under node with a minimal DOM stub
and drive its public surface, asserting:
  (a) an all-done digest → 'celebrating' + a mood bump;
  (b) a digest with a blocked stream → the concerned 'sad' pose;
  (c) clicking the pet renders an SVG bubble carrying the right live counts;
  (d) no digest → the pet's generic time/mood behavior is unchanged.
A biting NEUTER of the resolve mapping proves the blocked-pose test bites.

It also guards the myday.js emit seam: the digest is derived from the report
shape, and both My Day open + the boot fetch route through the ONE cache choke.
"""
import json
import os
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PET_JS = REPO / "static" / "js" / "tofu-pet.js"
MYDAY_JS = REPO / "static" / "js" / "myday.js"


# ── node harness: boot the REAL pet with a fake DOM (reduced-motion ON so the
# pet stands and _resolve() drives the frame), fire a tofu:day digest through
# the public setDay(), optionally click, then report state + any bubble. ──
_HARNESS = r"""
'use strict';
const HOUR = __HOUR__;
const RealDate = Date;
class FakeDate extends RealDate {
  constructor(...a){ if(a.length){ super(...a); } else { super(); } }
  getHours(){ return HOUR; }
  static now(){ return __NOW__; }
}
global.Date = FakeDate;
global.window = {
  matchMedia(){ return { matches:true, addEventListener(){}, addListener(){} }; },
  addEventListener(){},
};
global.BASE_PATH = '';
global.requestAnimationFrame = function(){ return 0; };
global.cancelAnimationFrame = function(){};
global.ResizeObserver = function(){ return { observe(){}, disconnect(){} }; };
global.localStorage = { _d:{}, getItem(k){return this._d[k]||null;}, setItem(k,v){this._d[k]=v;} };
// The engine builds each frame with new Image() and decorates it (style,
// setAttribute, draggable, onload) — a bare src-accessor stub throws on
// node.style. _fakeEl below is a full enough element; frames never finish
// "loading" here (onload never fires), which these tests don't mind.
global.Image = function(){ return _fakeEl('img'); };
// t() is driven by the REAL i18n dictionary, read from the path in argv[1].
// It is passed as a FILE, not inlined: the dictionary is ~3000 keys and
// inlining it into `node -e` overflows the argv limit (measured: OSError
// "Argument list too long").
//
// It used to be `global.t = undefined`, simulating a state production cannot
// reach: index.html:80 installs a `window.t = key => key` boot stub before any
// script parses, and i18n.js is _BUNDLE_FILES[0] while tofu-pet.js is #135.
// That fiction forced the pet to carry a defensive `typeof t` alias, and the
// alias made its keys INVISIBLE to lib/i18n_boot_keys (charter #18) — the
// scanner only follows a literal string as t()'s FIRST argument, so every
// pet.* key silently missed the boot pack.
//
// Production signature is t(key, params) where params is a {placeholder} map,
// NOT a default string. A harness written `(k, d) => (d || k)` inverts that and
// manufactures prose the real UI never produces — the trap that shipped a
// literal `project.qrScan` to users while 11 render guards stayed green. Here a
// MISSING key returns the key, exactly as production does.
const _petDict = JSON.parse(require('fs').readFileSync(process.argv[1], 'utf8'));
global.t = function (key, params) {
  const e = _petDict[key];
  let text = (e && e.zh != null) ? e.zh : key;   // missing → the KEY
  if (params) {
    for (const k in params) {
      if (Object.prototype.hasOwnProperty.call(params, k)) {
        text = text.split('{' + k + '}').join(params[k]);
      }
    }
  }
  return text;
};

// A DOM that tracks children appended to the bar so we can inspect the bubble.
let _mounted = null;
const _barChildren = [];
function _fakeEl(tag){
  const el = {
    _attrs:{}, tagName:tag||'', className:'', alt:'', _src:'', innerHTML:'', textContent:'',
    offsetWidth:30, children:[], parentNode:null,
    style:{ _p:{}, setProperty(k,v){ this._p[k]=v; }, getPropertyValue(k){ return this._p[k]||''; } },
    set src(v){ this._src=v; }, get src(){ return this._src; },
    setAttribute(k,v){ this._attrs[k]=v; }, getAttribute(k){ return this._attrs[k]!==undefined?this._attrs[k]:null; },
    addEventListener(ev,fn){ this._ev=this._ev||{}; this._ev[ev]=fn; },
    appendChild(c){ c.parentNode=this; this.children.push(c); return c; },
    insertBefore(c){ c.parentNode=this; this.children.unshift(c); return c; },
    removeChild(c){ const i=this.children.indexOf(c); if(i>=0) this.children.splice(i,1); c.parentNode=null; return c; },
    querySelector(){ return null; }, querySelectorAll(){ return []; },
    getBoundingClientRect(){ return {left:0,right:400,top:0,bottom:48,width:400,height:48}; },
    firstChild:null,
  };
  return el;
}
const _bar = _fakeEl('span');
// Track children appended to the bar (the pet, fx layer, and the bubble).
const _origAppend = _bar.appendChild.bind(_bar);
_bar.appendChild = function(c){ _barChildren.push(c); return _origAppend(c); };
const _origInsert = _bar.insertBefore.bind(_bar);
_bar.insertBefore = function(c){ _barChildren.push(c); return _origInsert(c); };

let _docHandlers = {};
global.document = {
  readyState:'complete', hidden:false,
  addEventListener(ev,fn){ _docHandlers[ev]=fn; },
  dispatchEvent(e){ const h=_docHandlers[e.type]; if(h) h(e); return true; },
  getElementById(id){ if(id==='projectBar') return _bar; if(id==='tofuPet') return _mounted; return null; },
  createElement(t){ const e=_fakeEl(t); if(t!=='img' && !_mounted && t==='span') { /* first span is fx layer; pet is set explicitly below */ } return e; },
  querySelectorAll(){ return _mounted ? [_mounted] : []; },
};
// CustomEvent shim.
global.CustomEvent = function(type, opts){ return { type:type, detail:(opts||{}).detail }; };

__SRC__

const TP = window.TofuPet;
// mount() created the pet element; find it among bar children (className tofu-pet).
_mounted = _barChildren.find(c => c.className === 'tofu-pet') || null;

const digest = __DIGEST__;
if (digest) { document.dispatchEvent(new global.CustomEvent('tofu:day', { detail: digest })); }

const doClick = __CLICK__;
if (doClick) {
  // interact() pops the bubble; call it directly (the click handler calls interact()).
  TP.interact();
}

// Find a rendered bubble among bar children.
const bubble = _barChildren.find(c => c.className === 'tofu-pet-bubble') || null;
let bubbleText = '', bubbleReport = '', bubbleSvg = false;
if (bubble) {
  bubbleReport = bubble.getAttribute('data-report') || '';
  for (const ch of bubble.children) {
    if (ch.className === 'tofu-pet-bubble-text') bubbleText = ch.textContent || '';
    if (ch.className === 'tofu-pet-bubble-shape' && /<svg/.test(ch.innerHTML)) bubbleSvg = true;
  }
}

const st = TP.getState();
console.log(JSON.stringify({
  expr: st.expr,
  mood: st.mood,
  day: TP.getDay(),
  dayReport: TP.dayReport(),
  hasBubble: !!bubble,
  bubbleText: bubbleText,
  bubbleReport: bubbleReport,
  bubbleSvg: bubbleSvg,
}));
process.exit(0);
"""


def _i18n_dict():
    """Scrape ``static/js/i18n.js`` into ``{key: {zh, en}}``.

    Parsing the REAL file (instead of hand-listing keys) is what makes the
    harness honest: a key absent from production is absent here too, so ``t()``
    returns the bare key in BOTH places and a guard can actually see it.
    """
    src = (REPO / "static" / "js" / "i18n.js").read_text(encoding="utf-8")
    pat = re.compile(
        r"""^[ \t]*'([\w.\-]+)':\s*\{\s*zh:\s*(['"])(.*?)\2\s*,"""
        r"""\s*en:\s*(['"])(.*?)\4""",
        re.MULTILINE)
    return {m.group(1): {'zh': m.group(3), 'en': m.group(5)}
            for m in pat.finditer(src)}


def _run(hour=14, now=0, digest=None, click=False, mood=None, src=None):
    src = src if src is not None else PET_JS.read_text()
    script = (_HARNESS
              .replace("__SRC__", src)
              .replace("__HOUR__", str(hour))
              .replace("__NOW__", str(now))
              .replace("__DIGEST__", json.dumps(digest) if digest is not None else "null")
              .replace("__CLICK__", "true" if click else "false"))
    # The dictionary rides in a temp FILE (argv[1]), never inlined into `node
    # -e` — ~3000 keys overflows the argv limit (measured: "Argument list too
    # long"). node -e puts extra args at argv[1] (no script-path entry).
    import tempfile
    with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as fh:
        json.dump(_i18n_dict(), fh)
        dict_path = fh.name
    try:
        out = subprocess.run(["node", "-e", script, dict_path],
                             capture_output=True, text=True,
                             cwd=str(REPO), timeout=20)
    finally:
        os.unlink(dict_path)
    assert out.returncode == 0, f"node failed: {out.stderr}\n{out.stdout}"
    line = [ln for ln in out.stdout.strip().splitlines() if ln.strip().startswith("{")][-1]
    return json.loads(line)


# ── (a) all-done digest → celebrate + mood up ──────────────────────────────
def test_all_done_digest_celebrates_and_boosts_mood():
    d = {"streams": {"total": 3, "done": 3, "blocked": 0},
         "todos": {"total": 2, "done": 2}, "convCount": 5}
    r = _run(hour=14, digest=d)
    assert r["expr"] == "celebrating", f"all-done day should celebrate, got {r['expr']}"
    # Default starting mood is 72; an all-done transition bumps +6 → 78.
    assert r["mood"] >= 78, f"all-done day should bump mood, got {r['mood']}"
    assert r["day"] and r["day"]["streams"]["done"] == 3


# ── (b) blocked digest → concerned (sad) pose ──────────────────────────────
def test_blocked_digest_surfaces_concerned_pose():
    d = {"streams": {"total": 4, "done": 1, "blocked": 1},
         "todos": {"total": 0, "done": 0}, "convCount": 3}
    r = _run(hour=14, digest=d)
    assert r["expr"] == "sad", f"a blocked stream should surface the concerned pose, got {r['expr']}"


# ── (c) click renders an SVG bubble with the right counts ──────────────────
def test_click_renders_svg_bubble_with_counts():
    d = {"streams": {"total": 4, "done": 2, "blocked": 1},
         "todos": {"total": 2, "done": 1}, "convCount": 3}
    r = _run(hour=14, digest=d, click=True)
    assert r["hasBubble"], "clicking the pet must render a report bubble"
    assert r["bubbleSvg"], "bubble shape must be an inline SVG (no emoji/glyph, §3.4)"
    # done = streams.done(2)+todos.done(1)=3 ; total = 4+2 = 6 ; blocked = 1
    assert "3/6" in r["bubbleText"], f"bubble must carry live done/total counts, got {r['bubbleText']!r}"
    # Blocked count present. Assert the NUMBER, not an English word: this
    # harness drives t() from the REAL dictionary (zh), so the text is
    # `今日完成 3/6 · 1 项受阻`, not the English fallback the old `t=undefined`
    # fiction produced. Tying the guard to "block" would mean asserting on the
    # fallback language, not on whether the count reached the bubble.
    assert "1" in r["bubbleText"], \
        f"bubble must mention the blocked count, got {r['bubbleText']!r}"
    # No emoji / unicode-glyph icon smuggled into the text.
    assert not re.search(r"[\U0001F000-\U0001FAFF\u2600-\u27BF]", r["bubbleText"]), \
        f"bubble text must not contain emoji glyphs, got {r['bubbleText']!r}"


def test_click_without_digest_still_greets_no_counts():
    r = _run(hour=14, digest=None, click=True)
    assert r["hasBubble"], "clicking with no day digest should still greet"
    assert "/" not in r["bubbleText"], f"no-digest bubble must not fabricate counts, got {r['bubbleText']!r}"


# ── (d) no digest → generic time/mood behavior unchanged ───────────────────
def test_no_digest_keeps_generic_behavior():
    # Afternoon, healthy default mood, NO digest → the generic time-default 'idle'.
    r = _run(hour=14, digest=None)
    assert r["day"] is None, "no digest should leave day state unset"
    assert r["expr"] == "idle", f"afternoon with no digest must stay generic idle, got {r['expr']}"
    # Morning generic default is 'happy' — also unaffected by day logic.
    r2 = _run(hour=9, digest=None)
    assert r2["expr"] == "happy", f"morning generic default must be happy, got {r2['expr']}"


def test_malformed_digest_is_silent_noop():
    """A malformed digest (missing shape) must NOT set day state or change the
    generic pose — never break the pet."""
    r = _run(hour=14, digest={"garbage": True})
    assert r["day"] is None, "malformed digest must be ignored"
    assert r["expr"] == "idle", f"malformed digest must not alter generic pose, got {r['expr']}"


# ── NEUTER BITE on the resolve mapping ─────────────────────────────────────
def test_neuter_blocked_mapping_bites():
    """Remove the blocked→'sad' branch from _resolve()'s day signal → a blocked
    digest no longer surfaces the concerned pose (falls through to generic),
    proving test (b) actually bites."""
    src = PET_JS.read_text()
    neut = src.replace("if (_day.streams && _day.streams.blocked > 0) return 'sad';",
                       "/* neutered blocked mapping */", 1)
    assert neut != src, "neuter did not match the blocked-mapping branch"
    d = {"streams": {"total": 4, "done": 1, "blocked": 1},
         "todos": {"total": 0, "done": 0}, "convCount": 3}
    r = _run(hour=14, digest=d, src=neut)
    assert r["expr"] != "sad", f"neutered build still surfaced the concerned pose ({r['expr']}) — test does not bite"


# ── myday.js emit-seam guards (backend single source; one cache choke) ─────
def test_myday_derives_digest_from_report_shape():
    """The digest fields must be READ from the report the backend returns
    (streams[].status, today_todos[].done, stats.totalConversations) — not
    recomputed with day thresholds in JS."""
    src = MYDAY_JS.read_text()
    assert "_mydayBuildDigest" in src, "digest builder missing"
    m = re.search(r"function _mydayBuildDigest\(report\)\s*\{(.*?)\n\}", src, re.S)
    assert m, "could not isolate _mydayBuildDigest"
    body = m.group(1)
    assert "report.streams" in body and "report.today_todos" in body, \
        "digest must read the backend report arrays, not a client re-derivation"
    assert "totalConversations" in body, "digest must read stats.totalConversations"
    assert "s.status === 'done'" in body and "s.status === 'blocked'" in body, \
        "stream done/blocked must come from the backend status field"


def test_myday_emit_routes_through_single_cache_choke():
    """Both My Day open and the boot fetch must emit tofu:day via the ONE
    cache choke (_mydaySetCache) — no second source. And the boot fetch must go
    through Api.daily.status (no raw fetch) and be dedup-guarded."""
    src = MYDAY_JS.read_text()
    # _mydaySetCache emits for today's report.
    m = re.search(r"function _mydaySetCache\(dateStr, report\)\s*\{(.*?)\n\}", src, re.S)
    assert m and "_mydayEmitDay" in m.group(1), \
        "_mydaySetCache must emit the day digest (single choke)"
    # Boot fetch: uses the shared status API + IDB cache, guarded once per session.
    assert "_mydayBootDayDigest" in src, "boot digest fetch missing"
    boot = re.search(r"async function _mydayBootDayDigest\(\)\s*\{(.*?)\n\}", src, re.S)
    assert boot, "could not isolate _mydayBootDayDigest"
    bb = boot.group(1)
    assert "_myday._bootDigestDone" in bb, "boot fetch must be dedup-guarded (once per session)"
    assert "Api.daily.status" in bb, "boot fetch must use Api.daily.status (no raw fetch)"
    assert "_mydayIDB.get" in bb, "boot fetch must reuse the instant-paint IDB cache first"
    # No raw fetch( introduced anywhere in the added seam.
    assert "fetch('/api" not in src and 'fetch("/api' not in src, \
        "myday.js must not issue raw fetch — go through window.Api"


if __name__ == "__main__":
    for fn in [test_all_done_digest_celebrates_and_boosts_mood,
               test_blocked_digest_surfaces_concerned_pose,
               test_click_renders_svg_bubble_with_counts,
               test_click_without_digest_still_greets_no_counts,
               test_no_digest_keeps_generic_behavior,
               test_malformed_digest_is_silent_noop,
               test_neuter_blocked_mapping_bites,
               test_myday_derives_digest_from_report_shape,
               test_myday_emit_routes_through_single_cache_choke]:
        fn()
        print("PASS", fn.__name__)
    print("ALL GREEN")
