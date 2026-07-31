"""Regression: the cookie-capture consent banner (cookie_capture_consent.js)
must render on a push 'request' frame, post the user's decision back through
Api.authSources.cookieConsentResolve, and toast on 'captured'.

WHY
---
The banner is the ONLY consent gate for the login-wall cookie-capture chain
(lib/browser/cookie_capture.py). If a push frame fails to render, or a click
fails to resolve, the backend's 180s consent wait times out and silently
records a denial — the user is never asked and the site stays walled, with
zero visible error. This drives the REAL shipped JS under node with a fake
DOM/Api/push, asserting OUTCOMES (banner exists with the domain; click → one
resolve POST with the right id/approved; captured → toast), not internals.

NEUTER: neuter the _handleFrame→_showBanner wiring in a COPY of the shipped
file → the render checks FAIL, proving the harness exercises the real path.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
JS_FILE = os.path.join(ROOT, 'static', 'js', 'cookie_capture_consent.js')


def _node_available() -> bool:
    return bool(shutil.which('node'))


_HARNESS = r"""
const fs = require('fs');
global.window = global;
global.console = console;
// i18n: return the key WITH the placeholder so the shipped .replace()
// still fires — assertions can then check both the key and the domain.
global.t = (k) => k + ' {domain}';
const _toasts = [];
global.showToast = (msg, kind) => { _toasts.push({ msg, kind }); };

// ── Fake Api ──
const _resolves = [];
const _pendings = [{ id: 'cc_p1', domain: 'pending.example.com', url: 'https://pending.example.com/a', created_at: 1 }];
global.Api = {
  authSources: {
    cookieConsentResolve: (id, approved) => { _resolves.push({ id, approved }); return Promise.resolve({}); },
    cookieConsentPending: () => Promise.resolve({ data: { pending: _pendings } }),
  },
};

// ── Fake push: capture the subscriber so frames can be driven by hand ──
let _frameHandler = null;
global.pushSubscribe = (channel, taskId, fn) => {
  if (channel === 'cookie_capture') _frameHandler = fn;
};

// ── Minimal DOM ──
function makeEl(tag) {
  return {
    tagName: tag, id: '', className: '', textContent: '', style: {},
    children: [], parentNode: null, onclick: null,
    setAttribute(k, v) { this['attr_' + k] = v; },
    appendChild(c) { c.parentNode = this; this.children.push(c); return c; },
    removeChild(c) { this.children = this.children.filter(x => x !== c); c.parentNode = null; },
  };
}
const _body = makeEl('body');
const _byId = {};
global.document = {
  readyState: 'complete',
  body: _body,
  getElementById: (id) => _byId[id] || null,
  createElement: (tag) => {
    const el = makeEl(tag);
    if (el.id) _byId[el.id] = el;
    return el;
  },
  addEventListener: () => {},
};
// Keep id→el mapping in sync when code assigns .id AFTER createElement.
const _origCreate = global.document.createElement;
global.document.createElement = (tag) => {
  const el = _origCreate(tag);
  Object.defineProperty(el, 'id', {
    get() { return this._id || ''; },
    set(v) { this._id = v; _byId[v] = this; },
  });
  return el;
};

eval(fs.readFileSync(process.argv[2], 'utf8'));   // REAL cookie_capture_consent.js

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

function findCards() {
  const stack = _byId['ccConsentStack'];
  if (!stack) return [];
  return stack.children.filter(c => c.className === 'cc-consent-card');
}
function btn(card, cls) {
  let found = null;
  (function walk(n) {
    if (n.className === cls) found = n;
    (n.children || []).forEach(walk);
  })(card);
  return found;
}

(async () => {
  // Init self-fired on load (readyState='complete'): refreshPending must have
  // rendered the seeded pending row.
  await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
  let cards = findCards();
  check('pending_banner_rendered_on_boot', cards.length === 1);
  check('pending_banner_domain_in_text',
        cards.length === 1 && cards[0].children[0].textContent.includes('pending.example.com'));

  // (A) A push 'request' frame renders a NEW banner carrying the domain.
  check('push_subscriber_registered', typeof _frameHandler === 'function');
  _frameHandler({ type: 'request', id: 'cc_live1', domain: 'your-llm-gateway.example.com',
                  url: 'https://api.openai.com/ml/modelPlaza' });
  cards = findCards();
  check('request_frame_renders_banner', cards.length === 2);
  const live = cards.find(c => c.children[0].textContent.includes('your-llm-gateway.example.com'));
  check('banner_shows_domain', !!live);
  check('banner_uses_i18n_title', live && live.children[0].textContent.includes('cc.banner.title'));

  // (B) Clicking Allow posts exactly one resolve(id, true) and drops the banner.
  if (live) {
    btn(live, 'cc-consent-allow').onclick();
    await Promise.resolve();
    check('allow_posts_resolve_true',
          _resolves.some(r => r.id === 'cc_live1' && r.approved === true));
    check('banner_removed_after_allow', findCards().length === 1);
  } else {
    check('allow_posts_resolve_true', false);
    check('banner_removed_after_allow', false);
  }

  // (C) Clicking Deny posts resolve(id, false).
  const pending = findCards()[0];
  btn(pending, 'cc-consent-deny').onclick();
  await Promise.resolve();
  check('deny_posts_resolve_false',
        _resolves.some(r => r.id === 'cc_p1' && r.approved === false));

  // (D) A 'captured' frame toasts the i18n key with the domain.
  _frameHandler({ type: 'captured', domain: 'your-llm-gateway.example.com', cookieCount: 7 });
  check('captured_frame_toasts',
        _toasts.some(tst => tst.msg.includes('cc.captured') && tst.msg.includes('your-llm-gateway.example.com')));

  console.log(out.join('\n'));
})();
"""


def _run_harness(js_path: str) -> subprocess.CompletedProcess:
    harness = os.path.join(HERE, '_cc_consent_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        return subprocess.run(['node', harness, js_path],
                              capture_output=True, text=True, timeout=60)
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_consent_banner_render_resolve_toast():
    proc = _run_harness(JS_FILE)
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'consent-banner behavior failures:\n' + output
    assert output.count('PASS') >= 10, f'expected >=10 PASS lines, got:\n{output}'


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_consent_banner_frame_wiring_double_neuter(tmp_path):
    """NEUTER: sever _handleFrame → _showBanner in a COPY. The render checks
    must FAIL — proving the harness drives the real frame-handling path."""
    with open(JS_FILE, encoding='utf-8') as f:
        src = f.read()
    needle = "    if (frame.type === 'request') {\n      _showBanner({ id: frame.id, domain: frame.domain, url: frame.url });"
    assert needle in src, 'frame-wiring fragment drifted — update the neuter target'
    copy = tmp_path / 'cc_consent_neutered.js'
    copy.write_text(src.replace(needle, "    if (false) {", 1), encoding='utf-8')

    proc = _run_harness(str(copy))
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    assert 'FAIL request_frame_renders_banner' in output, (
        'NEUTER did not bite: banner still rendered with frame wiring severed.\n' + output)

    with open(JS_FILE, encoding='utf-8') as f:
        assert f.read() == src, 'harness mutated the shipped file'
